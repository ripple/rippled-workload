"""Antithesis assertion helpers for the workload.

Follows the fuzzer's naming convention:
  "workload::seen : TxType"    — transaction was created and submitted
  "workload::success : TxType" — transaction got tesSUCCESS
"""

from antithesis.assertions import reachable, sometimes
from antithesis.lifecycle import send_event


def tx_submitted(name: str, txn=None) -> None:
    """Report that a transaction was created and submitted to the network."""
    details = {}
    if txn is not None:
        try:
            details = txn.to_xrpl()
        except Exception:
            details = {"raw": str(txn)}
    send_event(f"workload::seen : {name}", details)
    reachable(f"workload::seen : {name}", {})


def tx_result(name: str, result: dict) -> None:
    """Report a transaction result to Antithesis.

    Emits a sometimes assertion that the transaction succeeded at least once,
    plus a lifecycle event with full result details.
    """
    engine_result = result.get("engine_result", "unknown")
    details = {
        "engine_result": engine_result,
        "engine_result_message": result.get("engine_result_message", ""),
        "account": result.get("tx_json", {}).get("Account", ""),
        "tx_type": result.get("tx_json", {}).get("TransactionType", ""),
        "hash": result.get("hash", ""),
    }
    send_event(f"workload::result : {name}", details)
    sometimes(
        engine_result == "tesSUCCESS",
        f"workload::success : {name}",
        {"engine_result": engine_result},
    )
