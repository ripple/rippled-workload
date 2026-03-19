"""Assertions framework with optional Antithesis SDK delegation.

When running inside Antithesis, delegates to the SDK for coverage-guided
assertion tracking.  In standalone mode, logs violations/coverage locally
and maintains per-message counters exposed via ``get_stats()``.
"""

import logging
from typing import Any

log = logging.getLogger("workload.assertions")

# ---------------------------------------------------------------------------
# SDK detection
# ---------------------------------------------------------------------------
try:
    from antithesis.assertions import (
        always as _sdk_always,
        always_or_unreachable as _sdk_always_or_unreachable,
        sometimes as _sdk_sometimes,
        unreachable as _sdk_unreachable,
    )
    from antithesis.lifecycle import setup_complete as _sdk_setup_complete, send_event as _sdk_send_event

    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False

# ---------------------------------------------------------------------------
# Standalone tracker
# ---------------------------------------------------------------------------
_stats: dict[str, dict[str, int]] = {}


def _track(message: str, condition: bool) -> None:
    entry = _stats.get(message)
    if entry is None:
        entry = {"true": 0, "false": 0, "total": 0}
        _stats[message] = entry
    entry["total"] += 1
    entry["true" if condition else "false"] += 1


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------
def always(condition: bool, message: str, details: dict[str, Any] | None = None) -> None:
    """Invariant assertion — must always hold."""
    _track(message, condition)
    if SDK_AVAILABLE:
        _sdk_always(condition, message, details or {})
    elif not condition:
        log.warning("ASSERTION VIOLATED: %s | %s", message, details or {})


def sometimes(condition: bool, message: str, details: dict[str, Any] | None = None) -> None:
    """Coverage assertion — should be true at least once."""
    _track(message, condition)
    if SDK_AVAILABLE:
        _sdk_sometimes(condition, message, details or {})
    elif condition:
        log.debug("COVERAGE: %s | %s", message, details or {})


def always_or_unreachable(condition: bool, message: str, details: dict[str, Any] | None = None) -> None:
    """Invariant that passes if never reached. Use for conditional code paths."""
    _track(message, condition)
    if SDK_AVAILABLE:
        _sdk_always_or_unreachable(condition, message, details or {})
    elif not condition:
        log.warning("ASSERTION VIOLATED (or_unreachable): %s | %s", message, details or {})


def unreachable(message: str, details: dict[str, Any] | None = None) -> None:
    """This code path should never execute."""
    _track(message, False)
    if SDK_AVAILABLE:
        _sdk_unreachable(message, details or {})
    else:
        log.error("UNREACHABLE REACHED: %s | %s", message, details or {})


def reachable(message: str, details: dict[str, Any] | None = None) -> None:
    """This code path should execute at least once."""
    sometimes(True, message, details)


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------
def setup_complete(details: dict[str, Any] | None = None) -> None:
    """Signal that workload initialisation is finished."""
    if SDK_AVAILABLE:
        _sdk_setup_complete(details or {})
    else:
        log.info("SETUP_COMPLETE: %s", details or {})


def send_event(name: str, details: dict[str, Any] | None = None) -> None:
    """Emit a named event for the Antithesis dashboard."""
    if SDK_AVAILABLE:
        _sdk_send_event(name, details or {})
    else:
        log.debug("EVENT: %s | %s", name, details or {})


# ---------------------------------------------------------------------------
# Transaction helpers (standardised message format for dashboard)
# ---------------------------------------------------------------------------
def tx_submitted(tx_type: str, *, details: dict[str, Any] | None = None) -> None:
    send_event(f"workload::submitted:{tx_type}", details)
    sometimes(True, f"workload::submitted:{tx_type}")


def tx_validated(tx_type: str, result: str, *, details: dict[str, Any] | None = None) -> None:
    send_event(f"workload::validated:{tx_type}", {"result": result, **(details or {})})
    sometimes(result == "tesSUCCESS", f"workload::validated:{tx_type}", {"result": result})

    # tecINTERNAL is a bug
    if result == "tecINTERNAL": # TODO: enum
        unreachable(f"workload::tecINTERNAL:{tx_type}", {"result": result, **(details or {})})


def tx_rejected(tx_type: str, code: str, *, details: dict[str, Any] | None = None) -> None:
    send_event(f"workload::rejected:{tx_type}", {"code": code, **(details or {})})
    sometimes(True, f"workload::rejected:{tx_type}", {"code": code})

    # tefINTERNAL is a bug — should never happen
    if code == "tefINTERNAL": # TODO: enum
        unreachable(f"workload::tefINTERNAL:{tx_type}", {"code": code, **(details or {})})


# ---------------------------------------------------------------------------
# Stats accessor
# ---------------------------------------------------------------------------
def get_stats() -> dict[str, dict[str, int]]:
    """Return a snapshot of per-assertion counters."""
    return dict(_stats)
