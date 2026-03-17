"""Antithesis assertion helpers for the workload.

Follows the fuzzer's naming convention:
  "workload::seen : TxType"    — transaction was created and submitted
  "workload::success : TxType" — transaction got tesSUCCESS

Import at module level so the Antithesis instrumentor can catalog
assertion sites during its startup scan of /opt/antithesis/catalog/.
"""

from antithesis import assertions


def tx_submitted(name: str) -> None:
    """Report that a transaction was created and submitted to the network."""
    assertions.reachable(
        f"workload::seen : {name}",
        {},
    )


def tx_result(name: str, result: dict) -> None:
    """Report a transaction result to Antithesis.

    Emits a sometimes assertion that the transaction succeeded at least once.
    """
    engine_result = result.get("engine_result", "unknown")
    assertions.sometimes(
        engine_result == "tesSUCCESS",
        f"workload::success : {name}",
        {"engine_result": engine_result},
    )
