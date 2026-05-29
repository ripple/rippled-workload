#!/usr/bin/env python3
"""Fetch Antithesis experiment results via the read REST API.

Replaces the manual "download events.log + read which assertion failed" loop. Talks to
``https://${ANTITHESIS_TENANT}.antithesis.com/api/v0`` with a Bearer token. HTTP goes through
``curl`` (the host's TLS rejects Python's urllib in some environments); only stdlib + curl are
required -- no jq / snouty / npm.

Auth: the token is taken from ``$ANTITHESIS_API_KEY`` if set, otherwise from a key file
(``$ANTITHESIS_KEY_FILE`` or ``~/antithesis.key``). The key file may be a bare token or contain a
``pass: <token>`` line (the ``user:`` line, if present, is just a label and is ignored).

Key API facts this relies on (verified against the live API):
- ``GET /runs`` -> ``{"data":[run...], "next_cursor": ...}``; paginate with ``after=<cursor>``,
  ``limit`` is 1..100. Each run carries a ``description`` embedding ``workload_ref=`` and
  ``run=<gh_run_id>-...`` so runs launched via the gh workflow can be matched back.
- ``GET /runs/{id}`` -> adds ``completed_at``, ``links.triage_report`` (signed URL), ``results``.
- ``GET /runs/{id}/properties`` -> ``{"data":[prop...]}`` (paginated). A prop has ``name``,
  ``status`` ("Passing"/"Failing"), ``counterexamples``/``examples`` each with a
  ``moment`` (``input_hash``, ``vtime``), ``source``, and ``antithesis_assert`` (incl. ``location``
  file:line + ``message``). There is NO top-level ``failure_moment`` on the run.
- ``GET /runs/{id}/logs?input_hash=H&vtime=V`` -> the whole event/log stream (NDJSON) up to that
  moment: SDK events (``workload::result : <Tx>``, ``val_health``, ``fault`` ...) and container
  stdout/stderr. ``begin_vtime``/``begin_input_hash`` restrict it to a range. This is the real
  "events.log"; ``/events`` is only a capped stdout text-search and is not used here.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

DEFAULT_KEY_FILE = "~/antithesis.key"


def _fail(msg: str) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(2)


def load_token() -> str:
    """Resolve the Bearer token from env or a key file."""
    env = os.environ.get("ANTITHESIS_API_KEY")
    if env and env.strip():
        return env.strip()
    path = Path(os.environ.get("ANTITHESIS_KEY_FILE", DEFAULT_KEY_FILE)).expanduser()
    if not path.is_file():
        _fail(
            "no token: set $ANTITHESIS_API_KEY or put it in "
            f"{path} (a bare token, or a 'pass: <token>' line)"
        )
    text = path.read_text()
    for line in text.splitlines():
        if line.strip().lower().startswith("pass:"):
            return line.split(":", 1)[1].strip()
    token = text.strip()
    if not token:
        _fail(f"key file {path} is empty")
    return token


def base_url() -> str:
    tenant = os.environ.get("ANTITHESIS_TENANT", "ripple")
    return f"https://{tenant}.antithesis.com"


class Api:
    """Thin curl-backed client. The token is fed via a curl config on stdin so it never lands in
    argv / process listings."""

    def __init__(self) -> None:
        self.base = base_url()
        self.token = load_token()

    def get(self, path: str) -> tuple[int, bytes]:
        cfg = f'url = "{self.base}{path}"\nheader = "Authorization: Bearer {self.token}"\n'
        proc = subprocess.run(
            ["curl", "-sS", "-K", "-", "-w", "\n%{http_code}"],
            input=cfg.encode(),
            capture_output=True,
        )
        if proc.returncode != 0:
            _fail(f"curl failed: {proc.stderr.decode(errors='replace').strip()}")
        out = proc.stdout
        nl = out.rfind(b"\n")
        body, code = out[:nl], out[nl + 1 :].decode().strip()
        return int(code or 0), body

    def get_json(self, path: str) -> Any:
        code, body = self.get(path)
        if code != 200:
            _fail(f"GET {path} -> HTTP {code}: {body.decode(errors='replace')[:200]}")
        return json.loads(body)

    def paginate(self, path: str, limit: int = 100, cap: int | None = None) -> list[dict]:
        """Follow ``next_cursor`` over a ``{data, next_cursor}`` collection."""
        sep = "&" if "?" in path else "?"
        items: list[dict] = []
        after = ""
        while True:
            page = f"{path}{sep}limit={limit}" + (f"&after={after}" if after else "")
            d = self.get_json(page)
            items.extend(d.get("data", []))
            after = d.get("next_cursor") or ""
            if not after or (cap is not None and len(items) >= cap):
                break
        return items


# ── run resolution ──────────────────────────────────────────────────────────


def _desc_fields(description: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for tok in (description or "").split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


def resolve(api: Api, selector: str) -> dict:
    """Map a selector to a run dict.

    Selectors: ``latest``, ``latest-failing`` (status ``incomplete``), ``gh=<gh_run_id>``,
    ``match=<substr of description>``, or a literal run_id.
    """
    if selector not in ("latest", "latest-failing") and "=" not in selector:
        # treat as a literal run_id
        run = api.get_json(f"/api/v0/runs/{selector}")
        return run

    runs = api.paginate("/api/v0/runs", limit=100, cap=400)
    if selector == "latest":
        return runs[0]
    if selector == "latest-failing":
        for r in runs:
            if r.get("status") == "incomplete":
                return r
        _fail("no run with status 'incomplete' found")
    key, val = selector.split("=", 1)
    for r in runs:
        desc = r.get("description", "")
        if key == "gh" and f"run={val}" in desc:
            return r
        if key == "match" and val in desc:
            return r
    _fail(f"no run matched {selector!r}")


# ── latest moment + full event stream ─────────────────────────────────────────


def latest_moment(props: list[dict]) -> tuple[str, str] | None:
    """Pick the (input_hash, vtime) with the greatest vtime across all property examples."""
    best: tuple[float, str, str] | None = None
    for p in props:
        for ex in (p.get("examples") or []) + (p.get("counterexamples") or []):
            m = ex.get("moment") if isinstance(ex, dict) else None
            if m and m.get("vtime"):
                v = float(m["vtime"])
                if best is None or v > best[0]:
                    best = (v, m["input_hash"], m["vtime"])
    return (best[1], best[2]) if best else None


def failing(props: list[dict]) -> list[dict]:
    return [p for p in props if str(p.get("status", "")).lower() != "passing"]


def _assert_location(prop: dict) -> str:
    for ex in prop.get("counterexamples") or []:
        if not isinstance(ex, dict):
            continue  # some counterexamples are bare values (e.g. a utilization figure)
        loc = (ex.get("antithesis_assert") or {}).get("location") or {}
        if loc.get("file"):
            return f"{loc['file']}:{loc.get('begin_line', '?')}"
    return "?"


# ── commands ──────────────────────────────────────────────────────────────────


def cmd_runs(api: Api, args: argparse.Namespace) -> None:
    path = "/api/v0/runs"
    if args.status:
        path += f"?status={args.status}"
    runs = api.paginate(path, limit=min(args.limit, 100), cap=args.limit)
    for r in runs[: args.limit]:
        if args.match and args.match not in r.get("description", ""):
            continue
        f = _desc_fields(r.get("description", ""))
        print(
            f"{r['run_id']}  {r['status']:11}  {r.get('created_at', ''):20}  "
            f"ref={f.get('workload_ref', '?')}  gh={f.get('run', '?')}"
        )


def cmd_resolve(api: Api, args: argparse.Namespace) -> None:
    print(resolve(api, args.selector)["run_id"])


def cmd_run(api: Api, args: argparse.Namespace) -> None:
    r = resolve(api, args.selector)
    d = api.get_json(f"/api/v0/runs/{r['run_id']}")
    f = _desc_fields(d.get("description", ""))
    print(f"run_id     {d['run_id']}")
    print(f"status     {d['status']}")
    print(f"created    {d.get('created_at')}   completed {d.get('completed_at')}")
    print(f"workload   ref={f.get('workload_ref')} commit={f.get('workload_commit')}")
    print(f"xrpld      ref={f.get('xrpld_ref')} commit={f.get('xrpld_commit')}  gh={f.get('run')}")
    triage = (d.get("links") or {}).get("triage_report")
    if triage:
        print(f"triage     {triage}")


def cmd_properties(api: Api, args: argparse.Namespace) -> None:
    r = resolve(api, args.selector)
    props = api.paginate(f"/api/v0/runs/{r['run_id']}/properties", limit=100)
    shown = failing(props) if args.failing else props
    print(f"{r['run_id']}: {len(props)} properties, {len(failing(props))} failing")
    for p in sorted(shown, key=lambda x: str(x.get("status"))):
        loc = f"  @ {_assert_location(p)}" if str(p.get("status")).lower() != "passing" else ""
        print(f"  [{p.get('status'):8}] cex={p.get('counterexample_count', 0):<4} {p['name']}{loc}")


def cmd_events(api: Api, args: argparse.Namespace) -> None:
    r = resolve(api, args.selector)
    props = api.paginate(f"/api/v0/runs/{r['run_id']}/properties", limit=100)
    mom = latest_moment(props)
    if not mom:
        _fail("no moment found in any property; cannot fetch event stream")
    ih, vt = mom
    code, body = api.get(f"/api/v0/runs/{r['run_id']}/logs?input_hash={ih}&vtime={vt}")
    if code != 200:
        _fail(f"/logs -> HTTP {code}")
    out = Path(args.out).expanduser() if args.out else None
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(body)
        n = body.count(b"\n") + 1
        print(f"wrote {n} lines to {out}")
    else:
        sys.stdout.buffer.write(body)


def cmd_logs(api: Api, args: argparse.Namespace) -> None:
    r = resolve(api, args.selector)
    path = f"/api/v0/runs/{r['run_id']}/logs?input_hash={args.input_hash}&vtime={args.vtime}"
    if args.begin_vtime and args.begin_input_hash:
        path += f"&begin_vtime={args.begin_vtime}&begin_input_hash={args.begin_input_hash}"
    code, body = api.get(path)
    if code != 200:
        _fail(f"/logs -> HTTP {code}")
    if args.out:
        Path(args.out).expanduser().write_bytes(body)
        nlines = body.count(b"\n") + 1
        print(f"wrote {nlines} lines to {args.out}")
    else:
        sys.stdout.buffer.write(body)


def cmd_fetch(api: Api, args: argparse.Namespace) -> None:
    """All-in-one: resolve -> save run.json, properties.json, failing summary, events.ndjson."""
    r = resolve(api, args.selector)
    rid = r["run_id"]
    detail = api.get_json(f"/api/v0/runs/{rid}")
    props = api.paginate(f"/api/v0/runs/{rid}/properties", limit=100)
    fails = failing(props)

    save = (
        Path(args.save_dir).expanduser() / rid
        if args.save_dir
        else (Path("~/Downloads/antithesis").expanduser() / rid)
    )
    save.mkdir(parents=True, exist_ok=True)
    (save / "run.json").write_text(json.dumps(detail, indent=2))
    (save / "properties.json").write_text(json.dumps(props, indent=2))

    f = _desc_fields(detail.get("description", ""))
    summary = [
        f"run_id   {rid}",
        f"status   {detail['status']}",
        f"workload ref={f.get('workload_ref')} commit={f.get('workload_commit')} gh={f.get('run')}",
        f"xrpld    ref={f.get('xrpld_ref')} commit={f.get('xrpld_commit')}",
        f"triage   {(detail.get('links') or {}).get('triage_report', '-')}",
        f"properties {len(props)} total, {len(fails)} failing:",
    ]
    for p in fails:
        summary.append(f"  [FAIL] {p['name']}  @ {_assert_location(p)}")
    summary_text = "\n".join(summary)
    (save / "failing.txt").write_text(summary_text + "\n")

    # full event stream via /logs at the latest moment
    events_note = ""
    mom = latest_moment(props)
    if mom:
        ih, vt = mom
        code, body = api.get(f"/api/v0/runs/{rid}/logs?input_hash={ih}&vtime={vt}")
        if code == 200:
            (save / "events.ndjson").write_bytes(body)
            nlines = body.count(b"\n") + 1
            events_note = f"events.ndjson ({nlines} lines, up to vtime {vt})"
        else:
            events_note = f"events fetch failed (HTTP {code})"
    else:
        events_note = "no moment available; events.ndjson skipped"

    print(summary_text)
    print(f"\nsaved to {save}/")
    print(f"  run.json  properties.json  failing.txt  {events_note}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch Antithesis run results via the REST API.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("runs", help="list recent runs")
    p.add_argument("--status")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--match", help="only show runs whose description contains this substring")
    p.set_defaults(fn=cmd_runs)

    p = sub.add_parser("resolve", help="print the run_id a selector resolves to")
    p.add_argument("selector")
    p.set_defaults(fn=cmd_resolve)

    p = sub.add_parser("run", help="show one run's details + triage link")
    p.add_argument("selector")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("properties", help="list property/assertion outcomes")
    p.add_argument("selector")
    p.add_argument("--failing", action="store_true", help="only non-passing")
    p.set_defaults(fn=cmd_properties)

    p = sub.add_parser("events", help="dump the full event stream (NDJSON) via /logs")
    p.add_argument("selector")
    p.add_argument("--out", help="write to this file instead of stdout")
    p.set_defaults(fn=cmd_events)

    p = sub.add_parser("logs", help="raw /logs at a specific moment or range")
    p.add_argument("selector")
    p.add_argument("--input-hash", required=True)
    p.add_argument("--vtime", required=True)
    p.add_argument("--begin-input-hash")
    p.add_argument("--begin-vtime")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_logs)

    p = sub.add_parser("fetch", help="resolve + save run.json/properties/failing/events")
    p.add_argument("selector")
    p.add_argument("--save-dir", help="parent dir (default ~/Downloads/antithesis)")
    p.set_defaults(fn=cmd_fetch)

    args = ap.parse_args()
    args.fn(Api(), args)


if __name__ == "__main__":
    main()
