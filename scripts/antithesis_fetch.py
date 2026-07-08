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
- Properties embed exactly ONE representative counterexample; no param exposes the rest. Moments
  are re-derived per properties fetch and go stale -- a saved moment gives ``/logs`` HTTP 400, so
  ``crashes`` always works from a fresh properties fetch.
- Log-stream line shapes (every line: ``moment{input_hash,vtime}``, ``source{name,...}``):
  signal deaths ``source.name=processes_terminated_with_signal`` with ``output_text`` JSON
  ``{executable(=thread), signal, pid}``; container exits ``source.name=containers_meta`` with
  ``event=died``, ``name``, ``container_exit_code``; faults ``source.name=fault_injector`` with
  ``fault{name,type,affected_nodes,details}``; container stdout has ``source.container`` +
  ``output_text``. Exit codes 0/137/143 are fault-injector stops/kills, not crashes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
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


# ── crash dossier ─────────────────────────────────────────────────────────────

# fault-injector stop/kill and clean exit -- expected under fault injection
_BENIGN_EXITS = {0, 137, 143}
_SIGNAMES = {4: "SIGILL", 6: "SIGABRT", 7: "SIGBUS", 8: "SIGFPE", 11: "SIGSEGV"}
_KEY_LINE_RE = re.compile(
    r"terminate called|what\(\):|[Aa]ssertion|UNREACHABLE|Segmentation"
    r"|:FTL |Server stopping: Signal|stack smashing|double free|corrupted"
)
_HEX_RE = re.compile(r"[0-9A-Fa-f]{16,}")


def _crash_counterexamples(props: list[dict]) -> list[tuple[str, dict]]:
    """(property name, counterexample) for every failing crash bucket.

    Thread buckets (``j:NetHeart``, ``io svc #0``) carry a signal counterexample;
    ``container: X, exit code: N`` buckets carry a container-died counterexample.
    """
    out: list[tuple[str, dict]] = []
    for p in failing(props):
        for ex in p.get("counterexamples") or []:
            if not isinstance(ex, dict) or not (ex.get("moment") or {}).get("vtime"):
                continue
            src = (ex.get("source") or {}).get("name", "")
            if src == "processes_terminated_with_signal" or (
                src == "containers_meta"
                and ex.get("event") == "died"
                and ex.get("container_exit_code") not in _BENIGN_EXITS
            ):
                out.append((p["name"], ex))
    return out


def _fetch_stream(api: Api, rid: str, moment: dict, cache_dir: Path) -> Path | None:
    ih, vt = moment["input_hash"], moment["vtime"]
    path = cache_dir / f"logs_{ih}_{vt}.ndjson"
    if path.is_file() and path.stat().st_size:
        return path
    code, body = api.get(f"/api/v0/runs/{rid}/logs?input_hash={ih}&vtime={vt}")
    if code != 200:
        print(f"warn: /logs ih={ih} vt={vt} -> HTTP {code}, skipping", file=sys.stderr)
        return None
    path.write_bytes(body)
    return path


def _scan_stream(path: Path) -> dict[str, Any]:
    signals: list[tuple[float, str, int | None]] = []  # (vtime, thread, signal)
    deaths: list[tuple[float, str, int | None]] = []  # (vtime, container, exit)
    faults: list[tuple[float, dict]] = []
    stdout: dict[str, list[tuple[float, str]]] = {}  # container -> [(vtime, line)]
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            src = d.get("source") or {}
            try:
                vt = float((d.get("moment") or {}).get("vtime", ""))
            except ValueError:
                continue
            name = src.get("name", "")
            if name == "processes_terminated_with_signal":
                try:
                    info = json.loads(d.get("output_text") or "{}")
                except ValueError:
                    info = {}
                signals.append((vt, info.get("executable", "?"), info.get("signal")))
            elif name == "containers_meta" and d.get("event") == "died":
                deaths.append((vt, d.get("name", "?"), d.get("container_exit_code")))
            elif name == "fault_injector" and "fault" in d:
                faults.append((vt, d["fault"]))
            elif src.get("container") and "output_text" in d:
                stdout.setdefault(name, []).append((vt, d["output_text"]))
    return {"signals": signals, "deaths": deaths, "faults": faults, "stdout": stdout}


def _fault_brief(f: dict) -> str:
    det = f.get("details") or {}
    bits = [str(f.get("name", "?"))]
    if det.get("disruption_type"):
        bits.append(str(det["disruption_type"]))
    if f.get("affected_nodes"):
        bits.append(",".join(map(str, f["affected_nodes"])))
    if det.get("partitions"):
        bits.append(" | ".join("[" + ",".join(p) + "]" for p in det["partitions"]))
    return " ".join(bits)


def _build_record(
    bucket: str, ex: dict, scan: dict[str, Any], tail_n: int, fault_window: float
) -> dict[str, Any]:
    anchor = float(ex["moment"]["vtime"])
    # the stream ends AT the moment, so the cex's own event may be absent from it -- seed
    # thread/signal (signal buckets) or container/exit (died buckets) from the cex itself
    thread = signal = exit_code = container = None
    if (ex.get("source") or {}).get("name") == "processes_terminated_with_signal":
        try:
            info = json.loads(ex.get("output_text") or "{}")
        except ValueError:
            info = {}
        thread, signal = info.get("executable"), info.get("signal")
    else:
        container, exit_code = ex.get("name"), ex.get("container_exit_code")
    sig = min(
        (s for s in scan["signals"] if anchor - 5 <= s[0] <= anchor + 1),
        key=lambda s: abs(s[0] - anchor),
        default=None,
    )
    if sig:
        thread, signal = thread or sig[1], signal if signal is not None else sig[2]
    death = min(
        (
            d
            for d in scan["deaths"]
            if anchor - 1 <= d[0] <= anchor + 5 and d[2] not in _BENIGN_EXITS
        ),
        key=lambda d: abs(d[0] - anchor),
        default=None,
    )
    if death:
        container = container or death[1]
        exit_code = exit_code if exit_code is not None else death[2]
    crash_vt = sig[0] if sig else anchor
    if container is None:  # signal events don't name the container; infer from key lines
        for cname, lines in scan["stdout"].items():
            near = (line for vt, line in lines if crash_vt - 10 <= vt <= crash_vt + 0.5)
            if any(_KEY_LINE_RE.search(t) for t in near):
                container = cname
                break
    tail = [(vt, t) for vt, t in scan["stdout"].get(container or "", []) if vt <= crash_vt + 0.5][
        -max(tail_n, 200) :
    ]
    key_lines = [(vt, t) for vt, t in tail if _KEY_LINE_RE.search(t)][-12:]
    shutdown = any("Server stopping" in t for vt, t in tail if vt >= crash_vt - 40)
    return {
        "buckets": [bucket],
        "container": container or "?",
        "thread": thread,
        "signal": signal,
        "exit_code": exit_code,
        "moment": ex["moment"],
        "crash_vtime": crash_vt,
        "context": "shutdown" if shutdown else "runtime",
        "faults": [
            (vt, _fault_brief(f))
            for vt, f in scan["faults"]
            if crash_vt - fault_window <= vt <= crash_vt
        ][-8:],
        "key_lines": key_lines,
        "tail": tail[-tail_n:],
    }


def _signature(rec: dict[str, Any]) -> tuple:
    # last key line = nearest the crash, most specific. Exit code stays out: the signal-bucket
    # stream ends before the container reap, so its record never sees the exit.
    head = rec["key_lines"][-1][1] if rec["key_lines"] else ""
    return (rec["container"], rec["signal"], _HEX_RE.sub("…", head.strip()))


def cmd_crashes(api: Api, args: argparse.Namespace) -> None:
    r = resolve(api, args.selector)
    rid = r["run_id"]
    f = _desc_fields(r.get("description", ""))
    props = api.paginate(f"/api/v0/runs/{rid}/properties", limit=100)
    cexs = _crash_counterexamples(props)
    counts = {p["name"]: p.get("counterexample_count", 0) for p in props}

    print(
        f"run {rid}  xrpld {f.get('xrpld_ref')}@{f.get('xrpld_commit')}  "
        f"workload {f.get('workload_ref')}@{f.get('workload_commit')}  gh={f.get('run')}"
    )
    if not cexs:
        print("no crash buckets (signal deaths / unexpected exits) among failing properties")
        return
    names = sorted({b for b, _ in cexs})
    print(f"crash buckets: {len(names)}")
    for n in names:
        print(f"  {n}  (cex={counts.get(n, '?')}, 1 inspectable -- API embeds one per property)")

    cache = Path(args.cache_dir).expanduser() / rid
    cache.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(4) as pool:
        paths = list(pool.map(lambda be: _fetch_stream(api, rid, be[1]["moment"], cache), cexs))

    scans: dict[Path, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    for (bucket, ex), path in zip(cexs, paths, strict=True):
        if path is None:
            continue
        if path not in scans:
            scans[path] = _scan_stream(path)
        records.append(_build_record(bucket, ex, scans[path], args.tail, args.fault_window))

    merged: dict[tuple, dict[str, Any]] = {}
    for rec in records:
        key = _signature(rec)
        if key in merged:
            kept = merged[key]
            kept["buckets"].extend(rec["buckets"])
            for field in ("exit_code", "thread"):
                if kept[field] is None:
                    kept[field] = rec[field]
        else:
            merged[key] = rec

    if args.json:
        print(json.dumps(list(merged.values()), indent=2))
        return
    for i, rec in enumerate(merged.values(), 1):
        signame = _SIGNAMES.get(rec["signal"] or 0, f"signal {rec['signal']}")
        exit_s = f", exit {rec['exit_code']}" if rec["exit_code"] is not None else ""
        thread_s = f", thread {rec['thread']}" if rec["thread"] else ""
        print(f"\n== crash {i}: {rec['container']} {signame}{exit_s}{thread_s}  [{rec['context']}]")
        print(f"   buckets: {'; '.join(rec['buckets'])}")
        m = rec["moment"]
        print(f"   moment:  input_hash={m['input_hash']} vtime={m['vtime']}")
        if rec["faults"]:
            print(f"   faults (last {args.fault_window:g} vtime):")
            for vt, brief in rec["faults"]:
                print(f"     vt {vt:9.2f}  {brief}")
        if rec["key_lines"]:
            print("   key lines:")
            for vt, t in rec["key_lines"]:
                print(f"     vt {vt:9.2f}  {t.strip()[:160]}")
        print(f"   {rec['container']} tail ({len(rec['tail'])} lines):")
        for vt, t in rec["tail"]:
            print(f"     vt {vt:9.2f}  {t.rstrip()[:160]}")
    print(f"\nstreams cached in {cache}/")


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

    p = sub.add_parser("crashes", help="crash dossier: buckets -> logs at moments -> signatures")
    p.add_argument("selector")
    p.add_argument("--tail", type=int, default=20, help="stdout lines per crash (default 20)")
    p.add_argument(
        "--fault-window", type=float, default=40.0, help="vtime window for faults (default 40)"
    )
    p.add_argument("--cache-dir", default="~/.cache/antithesis-fetch", help="log stream cache")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(fn=cmd_crashes)

    args = ap.parse_args()
    args.fn(Api(), args)


if __name__ == "__main__":
    main()
