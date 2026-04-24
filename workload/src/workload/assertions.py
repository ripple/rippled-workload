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

# XRPL fields → normalized event keys. Only object identifiers needed for tracking.
_OBJECT_ID_FIELDS: dict[str, str] = {
    "Destination": "destination",
    "VaultID": "vault_id",
    "NFTokenID": "nftoken_id",
    "LoanBrokerID": "loan_broker_id",
    "LoanID": "loan_id",
    "MPTokenIssuanceID": "mpt_issuance_id",
    "Subject": "subject",
    "Issuer": "issuer",
    "CredentialType": "credential_type",
}


def _seen_id(name: str) -> str:
    return f"workload::seen : {name}"


def _success_id(name: str) -> str:
    return f"workload::success : {name}"


def _failure_id(name: str) -> str:
    return f"workload::failure : {name}"


def _extract_object_ids(raw: dict) -> dict[str, str]:
    """Extract known object ID fields from a transaction dict."""
    ids: dict[str, str] = {}
    for xrpl_key, event_key in _OBJECT_ID_FIELDS.items():
        if xrpl_key in raw:
            ids[event_key] = str(raw[xrpl_key])
    return ids


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

    # Types whose faulty mutations always succeed at the rippled level
    # (no tec* results possible), so the failure assertion can't be met.
    _NO_FAILURE_TYPES = {"SetRegularKey", "SignerListSet"}

    for name in TX_TYPES:
        _emit_catalog_entry(_seen_id(name), "reachability", "Reachable", must_hit=True)
        _emit_catalog_entry(_success_id(name), "sometimes", "Sometimes", must_hit=True)
        _emit_catalog_entry(
            _failure_id(name), "sometimes", "Sometimes",
            must_hit=(name not in _NO_FAILURE_TYPES),
        )
    _emit_catalog_entry("workload::always : valid_engine_result", "always", "Always", must_hit=True)
    _emit_catalog_entry(
        "workload::always : no_internal_rippled_error", "always", "Always", must_hit=True
    )
    _emit_catalog_entry(
        "workload::always : no_temDISABLED", "always", "Always", must_hit=True
    )
    for setup_key in [
        "gateways",
        "trust_lines",
        "iou_distribution",
        "mpt_issuances",
        "mpt_authorizations",
        "mpt_distribution",
        "vaults",
        "vault_deposits",
        "nfts",
        "nft_offers",
        "credentials",
        "tickets",
        "domains",
        "loan_brokers",
        "cover_deposits",
        "loans",
    ]:
        _emit_catalog_entry(
            f"workload::setup_{setup_key}", "reachability", "Reachable", must_hit=True
        )


def tx_submitted(name: str, txn: object = None) -> None:
    """Report that a transaction was created and submitted to the network."""
    details: dict[str, str] = {"tx_type": name}
    if txn is not None:
        try:
            raw = txn.to_xrpl()
            details["account"] = raw.get("Account", "")
            details["sequence"] = str(raw.get("Sequence", ""))
            details.update(_extract_object_ids(raw))
        except Exception:
            pass
    send_event(f"workload::submitted : {name}", details)
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
    """Report a validated transaction result to Antithesis."""
    tx_json = result.get("tx_json", {})
    meta = result.get("meta", {})
    engine_result = result.get("engine_result") or meta.get("TransactionResult") or "unknown"
    details: dict[str, str] = {
        "tx_type": name,
        "engine_result": engine_result,
        "account": tx_json.get("Account", ""),
        "sequence": str(tx_json.get("Sequence", "")),
        "hash": result.get("hash", ""),
    }
    details.update(_extract_object_ids(tx_json))
    for node in meta.get("AffectedNodes", []):
        created = node.get("CreatedNode", {})
        if created.get("LedgerIndex"):
            details["created_id"] = created["LedgerIndex"]
            details["created_type"] = created.get("LedgerEntryType", "")
            break
    for node in meta.get("AffectedNodes", []):
        deleted = node.get("DeletedNode", {})
        if deleted.get("LedgerIndex"):
            details["deleted_id"] = deleted["LedgerIndex"]
            details["deleted_type"] = deleted.get("LedgerEntryType", "")
            break
    send_event(f"workload::result : {name}", details)

    _RIPPLED_INTERNAL_ERRORS = (
        "tefEXCEPTION",
        "tefINTERNAL",
        "tefINVARIANT_FAILED",
        "tefFAILURE",
    )
    assert_raw(
        condition=engine_result not in _RIPPLED_INTERNAL_ERRORS,
        message="workload::always : no_internal_rippled_error",
        details={
            "engine_result": engine_result,
            "tx_type": name,
            "hash": details.get("hash", ""),
        },
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
    assert_raw(
        condition=engine_result != "temDISABLED",
        message="workload::always : no_temDISABLED",
        details={
            "engine_result": engine_result,
            "tx_type": name,
            "hash": details.get("hash", ""),
        },
        loc_filename=_LOC_FILE,
        loc_function="tx_result",
        loc_class=_LOC_CLASS,
        loc_begin_line=0,
        loc_begin_column=_LOC_COL,
        hit=True,
        must_hit=True,
        assert_type="always",
        display_type="Always",
        assert_id="workload::always : no_temDISABLED",
    )
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
