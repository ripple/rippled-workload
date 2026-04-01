"""Antithesis assertion helpers for the workload.

Follows the fuzzer's naming convention:
  "workload::seen : TxType"    — transaction was created and submitted (reachable)
  "workload::success : TxType" — transaction got tesSUCCESS (sometimes)

Catalog entries are pre-registered at startup via assert_raw(hit=False) because
the static scanner cannot extract assertion names from f-strings. Runtime
assertions use the simple SDK functions (reachable, sometimes, always) which
match catalog entries by message string.
"""

from antithesis.assertions import assert_raw, always, sometimes, reachable
from antithesis.lifecycle import send_event

_RIPPLED_INTERNAL_ERRORS = ("tefEXCEPTION", "tefINTERNAL", "tefINVARIANT_FAILED", "tefFAILURE")


def _seen_id(name: str) -> str:
    return f"workload::seen : {name}"


def _success_id(name: str) -> str:
    return f"workload::success : {name}"


def _failure_id(name: str) -> str:
    return f"workload::failure : {name}"


def _catalog(message: str, assert_type: str, display_type: str, must_hit: bool) -> None:
    """Register a catalog entry (hit=False) so Antithesis knows this assertion exists."""
    assert_raw(
        condition=True, message=message, details={},
        loc_filename="workload/assertions.py", loc_function="register_assertions",
        loc_class="", loc_begin_line=0, loc_begin_column=0,
        hit=False, must_hit=must_hit,
        assert_type=assert_type, display_type=display_type, assert_id=message,
    )


def register_assertions() -> None:
    """Pre-register all assertion catalog entries at startup."""
    from workload.transactions import TX_TYPES
    for name in TX_TYPES:
        _catalog(_seen_id(name), "reachability", "Reachable", must_hit=True)
        _catalog(_success_id(name), "sometimes", "Sometimes", must_hit=True)
        _catalog(_failure_id(name), "sometimes", "Sometimes", must_hit=True)
    _catalog("workload::always : valid_engine_result", "always", "Always", must_hit=True)
    _catalog("workload::always : no_internal_rippled_error", "always", "Always", must_hit=True)
    for key in [
        "gateways", "trust_lines", "iou_distribution",
        "mpt_issuances", "mpt_authorizations", "mpt_distribution",
        "vaults", "vault_deposits", "nfts", "nft_offers",
        "credentials", "tickets", "domains",
        "loan_brokers", "cover_deposits", "loans",
    ]:
        _catalog(f"workload::setup_{key}", "reachability", "Reachable", must_hit=True)


def tx_submitted(name: str, txn=None) -> None:
    """Report that a transaction was submitted to the network."""
    details = {}
    if txn is not None:
        try:
            details = txn.to_xrpl()
        except Exception:
            details = {"raw": str(txn)}
    send_event(f"workload::seen : {name}", details)
    reachable(_seen_id(name), {})


def tx_result(name: str, result: dict) -> None:
    """Report a validated transaction result."""
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
    always(engine_result not in _RIPPLED_INTERNAL_ERRORS,
           "workload::always : no_internal_rippled_error",
           {"engine_result": engine_result, "tx_type": name, "hash": details.get("hash", "")})
    always(engine_result not in (None, "", "unknown"),
           "workload::always : valid_engine_result",
           {"engine_result": engine_result, "tx_type": name})
    sometimes(engine_result == "tesSUCCESS",
              _success_id(name), {"engine_result": engine_result})
    sometimes(engine_result != "tesSUCCESS",
              _failure_id(name), {"engine_result": engine_result})
