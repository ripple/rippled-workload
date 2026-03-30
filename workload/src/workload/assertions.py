"""Antithesis assertion helpers for the workload.

Follows the fuzzer's naming convention:
  "workload::seen : TxType"    — transaction was created and submitted (reachable)
  "workload::success : TxType" — transaction got tesSUCCESS (sometimes)

Uses assert_raw() to register catalog entries at startup and emit assertions
at runtime. The static scanner cannot extract assertion names from f-strings,
so we pre-register all known transaction types via register_assertions().
"""

from antithesis.assertions import assert_raw
from antithesis.lifecycle import send_event

_LOC_FILE = "workload/assertions.py"
_LOC_CLASS = ""
_LOC_COL = 0


def _seen_id(name: str) -> str:
    return f"workload::seen : {name}"


def _success_id(name: str) -> str:
    return f"workload::success : {name}"


def _failure_id(name: str) -> str:
    return f"workload::failure : {name}"


def _emit_catalog_entry(message: str, assert_type: str, display_type: str, must_hit: bool) -> None:
    """Register a single catalog entry (hit=False) so Antithesis knows this assertion exists."""
    assert_raw(
        condition=True,
        message=message,
        details={},
        loc_filename=_LOC_FILE,
        loc_function="register_assertions",
        loc_class=_LOC_CLASS,
        loc_begin_line=0,
        loc_begin_column=_LOC_COL,
        hit=False,
        must_hit=must_hit,
        assert_type=assert_type,
        display_type=display_type,
        assert_id=message,
    )


def register_assertions() -> None:
    """Register catalog entries for all known transaction types.

    Call once at app startup before any transactions are submitted.
    """
    from workload.transactions import TX_TYPES
    for name in TX_TYPES:
        _emit_catalog_entry(_seen_id(name), "reachability", "Reachable", must_hit=True)
        _emit_catalog_entry(_success_id(name), "sometimes", "Sometimes", must_hit=True)
        _emit_catalog_entry(_failure_id(name), "sometimes", "Sometimes", must_hit=True)
    _emit_catalog_entry("workload::always : valid_engine_result", "always", "Always", must_hit=True)
    _emit_catalog_entry("workload::always : no_internal_rippled_error", "always", "Always", must_hit=True)
    # Setup phase assertions
    for setup_key in [
        "gateways", "trust_lines", "iou_distribution",
        "mpt_issuances", "mpt_authorizations", "mpt_distribution",
        "vaults", "nfts", "credentials", "tickets", "domains",
    ]:
        _emit_catalog_entry(f"workload::setup_{setup_key}", "reachability", "Reachable", must_hit=True)


def tx_submitted(name: str, txn=None) -> None:
    """Report that a transaction was created and submitted to the network."""
    details = {}
    if txn is not None:
        try:
            details = txn.to_xrpl()
        except Exception:
            details = {"raw": str(txn)}
    send_event(f"workload::seen : {name}", details)
    assert_raw(
        condition=True,
        message=_seen_id(name),
        details={},
        loc_filename=_LOC_FILE,
        loc_function="tx_submitted",
        loc_class=_LOC_CLASS,
        loc_begin_line=0,
        loc_begin_column=_LOC_COL,
        hit=True,
        must_hit=True,
        assert_type="reachability",
        display_type="Reachable",
        assert_id=_seen_id(name),
    )


def tx_result(name: str, result: dict) -> None:
    """Report a transaction result to Antithesis.

    Emits a sometimes assertion that the transaction succeeded at least once,
    plus a lifecycle event with full result details.
    """
    engine_result = (
        result.get("engine_result")
        or result.get("meta", {}).get("TransactionResult")
        or "unknown"
    )
    details = {
        "engine_result": engine_result,
        "engine_result_message": result.get("engine_result_message", ""),
        "account": result.get("tx_json", {}).get("Account", ""),
        "tx_type": result.get("tx_json", {}).get("TransactionType", ""),
        "hash": result.get("hash", ""),
    }
    send_event(f"workload::result : {name}", details)
    # Internal rippled errors — these indicate bugs in transaction processing logic.
    # Must never occur; if they do, it's a finding worth investigating.
    _RIPPLED_INTERNAL_ERRORS = ("tefEXCEPTION", "tefINTERNAL", "tefINVARIANT_FAILED", "tefFAILURE")
    assert_raw(
        condition=engine_result not in _RIPPLED_INTERNAL_ERRORS,
        message="workload::always : no_internal_rippled_error",
        details={"engine_result": engine_result, "tx_type": name, "hash": details.get("hash", "")},
        loc_filename=_LOC_FILE,
        loc_function="tx_result",
        loc_class=_LOC_CLASS,
        loc_begin_line=0,
        loc_begin_column=_LOC_COL,
        hit=True,
        must_hit=True,
        assert_type="always",
        display_type="Always",
        assert_id="workload::always : no_internal_rippled_error",
    )
    # engine_result should always be a valid string
    assert_raw(
        condition=engine_result not in (None, "", "unknown"),
        message="workload::always : valid_engine_result",
        details={"engine_result": engine_result, "tx_type": name},
        loc_filename=_LOC_FILE,
        loc_function="tx_result",
        loc_class=_LOC_CLASS,
        loc_begin_line=0,
        loc_begin_column=_LOC_COL,
        hit=True,
        must_hit=True,
        assert_type="always",
        display_type="Always",
        assert_id="workload::always : valid_engine_result",
    )
    # tx succeeded at least once
    assert_raw(
        condition=engine_result == "tesSUCCESS",
        message=_success_id(name),
        details={"engine_result": engine_result},
        loc_filename=_LOC_FILE,
        loc_function="tx_result",
        loc_class=_LOC_CLASS,
        loc_begin_line=0,
        loc_begin_column=_LOC_COL,
        hit=True,
        must_hit=True,
        assert_type="sometimes",
        display_type="Sometimes",
        assert_id=_success_id(name),
    )
    # tx failed at least once (verifies error paths are exercised)
    assert_raw(
        condition=engine_result != "tesSUCCESS",
        message=_failure_id(name),
        details={"engine_result": engine_result},
        loc_filename=_LOC_FILE,
        loc_function="tx_result",
        loc_class=_LOC_CLASS,
        loc_begin_line=0,
        loc_begin_column=_LOC_COL,
        hit=True,
        must_hit=True,
        assert_type="sometimes",
        display_type="Sometimes",
        assert_id=_failure_id(name),
    )
