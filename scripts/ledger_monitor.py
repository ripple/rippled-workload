#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["websockets>=15.0"]
# ///
"""Ledger cadence monitor — shows per-ledger transaction counts in real time.

Standalone diagnostic tool, no dependency on the workload codebase.

Usage as script:
    uv run scripts/ledger_monitor.py                      # default ws://localhost:6006
    uv run scripts/ledger_monitor.py ws://10.0.0.5:6006   # custom endpoint

Usage as module:
    from scripts.ledger_monitor import listen_ledgers, LedgerClose

    async for lc in listen_ledgers("ws://localhost:6006"):
        print(f"Ledger {lc.index}: {lc.txn_count} txns")

NOTE: The WS connection and ledger-close event parsing in this script
overlaps with workload/src/workload/ws.py (ws_listener + classify_message)
and ws_processor.py (_handle_ledger_closed). When those modules are
refactored into a cleaner WS abstraction (see lifecycle-audit-report.md),
this script should import from there instead of reimplementing the
subscribe/parse logic.
"""

import asyncio
import json
import sys
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field


@dataclass(slots=True)
class LedgerClose:
    """Data from a single ledger close event."""

    index: int
    txn_count: int
    ledger_hash: str
    close_time: int  # rippled epoch
    wall_time: float = field(default_factory=time.monotonic)


async def listen_ledgers(ws_url: str, *, reconnect: bool = True) -> AsyncIterator[LedgerClose]:
    """Yield LedgerClose events from a rippled WebSocket stream.

    Args:
        ws_url: WebSocket endpoint (e.g. "ws://localhost:6006")
        reconnect: If True, reconnect on disconnect. If False, return on first disconnect.

    """
    import websockets

    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "command": "subscribe",
                            "streams": ["ledger"],
                        }
                    )
                )
                resp = json.loads(await ws.recv())
                if "error" in resp:
                    raise ConnectionError(f"Subscription error: {resp}")

                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") != "ledgerClosed":
                        continue
                    yield LedgerClose(
                        index=msg.get("ledger_index", 0),
                        txn_count=msg.get("txn_count", 0),
                        ledger_hash=msg.get("ledger_hash", ""),
                        close_time=msg.get("ledger_time", 0),
                    )

        except (ConnectionError, OSError) as e:
            if not reconnect:
                return
            print(f"\033[31mDisconnected: {e} — reconnecting in 2s...\033[0m", file=sys.stderr)
            await asyncio.sleep(2)


@dataclass
class CadenceStats:
    """Rolling statistics for ledger cadence monitoring."""

    window: deque[int] = field(default_factory=lambda: deque(maxlen=50))
    total_txns: int = 0
    total_ledgers: int = 0
    empty_streak: int = 0
    max_streak: int = 0
    last_close: float = field(default_factory=time.monotonic)

    def record(self, lc: LedgerClose) -> float:
        """Record a ledger close, return elapsed time since last close."""
        now = lc.wall_time
        elapsed = now - self.last_close
        self.last_close = now

        self.window.append(lc.txn_count)
        self.total_txns += lc.txn_count
        self.total_ledgers += 1

        if lc.txn_count == 0:
            self.empty_streak += 1
            self.max_streak = max(self.max_streak, self.empty_streak)
        else:
            self.empty_streak = 0

        return elapsed

    @property
    def avg_recent(self) -> float:
        return sum(self.window) / len(self.window) if self.window else 0

    @property
    def avg_overall(self) -> float:
        return self.total_txns / self.total_ledgers if self.total_ledgers else 0


def format_ledger_line(lc: LedgerClose, elapsed: float) -> str:
    """Format a single ledger close as a terminal line with visual bar."""
    bar_len = min(lc.txn_count, 60)
    bar = "\u2588" * bar_len
    if lc.txn_count > 0:
        return f" {lc.index}  {lc.txn_count:3d} txns  \033[32m{bar}\033[0m{'':>{max(1, 55 - bar_len)}}  {elapsed:.1f}s"
    return f" {lc.index}    0 txns{'':>56}  {elapsed:.1f}s"


def format_stats_line(stats: CadenceStats) -> str:
    """Format rolling stats as a dimmed summary line."""
    return (
        f"\033[90m{'─' * 72}\033[0m\n"
        f"\033[90m avg(last {len(stats.window)}): {stats.avg_recent:.1f} txns/ledger"
        f" | overall: {stats.avg_overall:.1f}"
        f" | empty streak: {stats.empty_streak}"
        f" | max streak: {stats.max_streak}\033[0m\n"
        f"\033[90m{'─' * 72}\033[0m"
    )


async def run_monitor(ws_url: str) -> None:
    """Run the interactive terminal monitor."""
    print(f"Ledger Monitor — {ws_url}")
    print("─" * 72)

    stats = CadenceStats()

    try:
        async for lc in listen_ledgers(ws_url):
            elapsed = stats.record(lc)
            print(format_ledger_line(lc, elapsed))

            if stats.total_ledgers % 10 == 0:
                print(format_stats_line(stats))
    except KeyboardInterrupt:
        pass

    print(f"\n{'─' * 72}")
    if stats.total_ledgers:
        print(f"Total: {stats.total_txns} txns across {stats.total_ledgers} ledgers ({stats.avg_overall:.1f} avg)")
        print(f"Max empty streak: {stats.max_streak}")


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:6006"
    try:
        asyncio.run(run_monitor(url))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
