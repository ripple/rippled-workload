#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx>=0.28"]
# ///
"""XRPL Workload health assessment — quick diagnostic snapshot.

Standalone diagnostic tool, no dependency on the workload codebase.

Usage:
    uv run scripts/assess_health.py                        # default http://localhost:8000
    uv run scripts/assess_health.py http://10.0.0.5:8000   # custom endpoint
"""

import sys
from collections import Counter

import httpx

# ── ANSI helpers ─────────────────────────────────────────────────────────────

BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"

NO_COLOR = not sys.stdout.isatty()
if NO_COLOR:
    BOLD = DIM = RED = GREEN = YELLOW = CYAN = RESET = ""


def bar(count: int, max_count: int, width: int = 30) -> str:
    if max_count == 0:
        return ""
    filled = round(count / max_count * width)
    return f"{CYAN}{'█' * filled}{RESET}"


def fmt_num(n: float) -> str:
    return f"{n:,.0f}" if isinstance(n, float) else f"{n:,}"


def color_pct(pct: float) -> str:
    if pct < 5:
        return f"{GREEN}{pct:.1f}%{RESET}"
    if pct < 20:
        return f"{YELLOW}{pct:.1f}%{RESET}"
    return f"{RED}{pct:.1f}%{RESET}"


# ── Fetch helpers ────────────────────────────────────────────────────────────


def get(client: httpx.Client, path: str) -> dict | None:
    """GET an endpoint, return JSON dict or None on failure."""
    try:
        r = client.get(path)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


# ── Main ─────────────────────────────────────────────────────────────────────


def assess(base_url: str) -> int:
    line = "─" * 52
    print(f"\n{BOLD}XRPL Workload Health{RESET} — {base_url}")
    print(line)

    client = httpx.Client(base_url=base_url, timeout=5.0)

    # ── 1. Health check ──────────────────────────────────────────────────
    health = get(client, "/health")
    if health is None:
        print(f"  {RED}UNREACHABLE{RESET} — cannot connect to {base_url}/health")
        return 1

    # ── 2. Workload status ───────────────────────────────────────────────
    status = get(client, "/workload/status")
    if status:
        running = status.get("running", False)
        stats = status.get("stats", {})
        submitted = stats.get("submitted", 0)
        failed = stats.get("failed", 0)
        total = submitted + failed
        fail_rate = (failed / total * 100) if total else 0.0
        state_label = f"{GREEN}Running{RESET}" if running else f"{YELLOW}Stopped{RESET}"
        print(
            f"  STATUS:     {state_label}"
            f" (submitted: {fmt_num(submitted)} | failed: {fmt_num(failed)}"
            f" | {color_pct(fail_rate)} failure rate)",
        )
    else:
        print(f"  STATUS:     {DIM}unavailable{RESET}")

    # ── 3. Accounts ──────────────────────────────────────────────────────
    summary = get(client, "/state/summary")
    accounts = get(client, "/accounts")
    if summary:
        gw = summary.get("gateways", 0)
        users = summary.get("users", 0)
        total_accts = gw + users
        acct_str = f"{fmt_num(total_accts)} total ({gw} gateways, {fmt_num(users)} users)"
        if accounts:
            acct_str += f" | {fmt_num(accounts.get('count', 0))} in registry"
        print(f"  ACCOUNTS:   {acct_str}")

    # ── 4. Throughput ────────────────────────────────────────────────────
    fill = get(client, "/workload/fill-fraction")
    target = get(client, "/workload/target-txns")
    if fill or target:
        parts: list[str] = []
        if target:
            parts.append(f"target={target.get('target_txns_per_ledger', '?')}/ledger")
        if fill:
            frac = fill.get("fill_fraction", 0)
            parts.append(f"fill={frac:.0%}")
        print(f"  THROUGHPUT: {' | '.join(parts)}")

    # ── 5. Pending by state ──────────────────────────────────────────────
    if summary:
        by_state = summary.get("by_state", {})
        interesting = ["SUBMITTED", "CREATED", "RETRYABLE"]
        parts = [f"{s}: {fmt_num(by_state.get(s, 0))}" for s in interesting if by_state.get(s, 0)]
        if parts:
            print(f"  PENDING:    {' | '.join(parts)}")

    # ── 6. Validated ─────────────────────────────────────────────────────
    if summary:
        by_source = summary.get("validated_by_source", {})
        ws_count = by_source.get("WS", 0)
        poll_count = by_source.get("POLL", 0)
        total_val = ws_count + poll_count
        if total_val:
            print(
                f"  VALIDATED:  {fmt_num(total_val)} (WS: {fmt_num(ws_count)} | POLL: {fmt_num(poll_count)})",
            )

    # ── 7. Top failures ──────────────────────────────────────────────────
    failed_data = get(client, "/state/failed")
    if failed_data:
        txns = failed_data.get("failed", [])
        if txns:
            # Count by the most informative result code
            codes: Counter[str] = Counter()
            for tx in txns:
                code = tx.get("meta_txn_result") or tx.get("engine_result_final") or tx.get("state", "unknown")
                codes[code] += 1

            top = codes.most_common(8)
            max_count = top[0][1] if top else 1
            print(f"\n  {BOLD}TOP FAILURES{RESET} ({fmt_num(len(txns))} total):")
            for code, count in top:
                print(f"    {code:<24} {fmt_num(count):>6}  {bar(count, max_count)}")

    # ── 8. Submission results breakdown ──────────────────────────────────
    if summary:
        sub_results = summary.get("submission_results", {})
        if sub_results:
            interesting_subs = {k: v for k, v in sub_results.items() if k != "tesSUCCESS" and v > 0}
            if interesting_subs:
                top_subs = sorted(interesting_subs.items(), key=lambda x: -x[1])[:5]
                max_sub = top_subs[0][1] if top_subs else 1
                print(f"\n  {BOLD}SUBMISSION RESULTS{RESET} (non-success):")
                for code, count in top_subs:
                    print(f"    {code:<24} {fmt_num(count):>6}  {bar(count, max_sub)}")

    # ── 9. Disabled types ────────────────────────────────────────────────
    disabled = get(client, "/workload/disabled-types")
    if disabled:
        config_dis = set(disabled.get("config_disabled", []))
        runtime_dis = set(disabled.get("disabled_types", [])) - config_dis
        parts = []
        if config_dis:
            parts.append(", ".join(sorted(config_dis)) + f" {DIM}(config){RESET}")
        if runtime_dis:
            parts.append(", ".join(sorted(runtime_dis)) + f" {DIM}(runtime){RESET}")
        if parts:
            print(f"\n  DISABLED:   {'; '.join(parts)}")

    # ── 10. WS queue ─────────────────────────────────────────────────────
    ws = get(client, "/state/ws/stats")
    if ws:
        qs = ws.get("queue_size", 0)
        qm = ws.get("queue_maxsize", 0)
        color = RED if (qm and qs / qm > 0.8) else RESET
        print(f"  WS QUEUE:   {color}{qs}/{fmt_num(qm)}{RESET}")

    # ── 11. DEX pools ────────────────────────────────────────────────────
    dex = get(client, "/dex/metrics")
    if dex:
        pool_count = dex.get("pool_count", 0)
        deposits = dex.get("total_deposits", 0)
        withdrawals = dex.get("total_withdrawals", 0)
        parts = [f"{fmt_num(pool_count)} tracked"]
        if deposits:
            parts.append(f"{fmt_num(deposits)} deposits")
        if withdrawals:
            parts.append(f"{fmt_num(withdrawals)} withdrawals")
        print(f"  DEX POOLS:  {' | '.join(parts)}")

    print()
    return 0


def main() -> None:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    # Strip trailing slash
    base_url = base_url.rstrip("/")
    raise SystemExit(assess(base_url))


if __name__ == "__main__":
    main()
