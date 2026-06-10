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

# Engine results that indicate a rippled internal error (exception caught,
# invariant violated, etc.). Checked on BOTH the submit response and the
# validated WS stream:
#   - tef* never validates; the submit-time check catches those.
#   - tecINVARIANT_FAILED claims a fee and DOES validate (rippled returns it on
#     the first invariant failure, escalating to tefINVARIANT_FAILED only if the
#     fee-only retry also fails) — so the validated-side check catches it.
_RIPPLED_INTERNAL_ERRORS = (
    "tefEXCEPTION",
    "tefINTERNAL",
    "tefINVARIANT_FAILED",
    "tefFAILURE",
    "tecINVARIANT_FAILED",
)

# Types whose current faulty handler never produces a non-tesSUCCESS engine
# result, so the failure assertion can't be met.
# - SignerListSet: fake AccountIDs are accepted (rippled doesn't verify
#   listed accounts exist); current mutations stay within weight bounds.
# - MPTokenIssuanceCreate: no _faulty handler — always submits a valid create.
_NO_FAILURE_TYPES = {
    "SignerListSet",
    "MPTokenIssuanceCreate",
}

# Types that effectively never succeed in this test environment.
# - AccountDelete: every account owns directory objects (trust lines, NFTs,
#   etc.), so rippled rejects with tecHAS_OBLIGATIONS.
# - AMMDelete: rippled auto-deletes empty AMMs as a side effect of the last
#   LP's TF_WITHDRAW_ALL, so AMMDelete usually finds the AMM already gone
#   (tecAMM_NOT_FOUND) or with outstanding LP tokens (tecAMM_NOT_EMPTY).
# - PaymentDomainXC: cross-currency domain payment success needs resting domain
#   liquidity in the matching direction, which the workload does not guarantee;
#   it reliably exercises the both-in-domain preclaim + domain pathfinding but
#   usually ends tecPATH_DRY. Drop from this set if runs show success is hit.
_NO_SUCCESS_TYPES = {"AccountDelete", "AMMDelete", "PaymentDomainXC"}

# Metadata expectations for tx types that must create/modify/delete specific
# ledger entry types on tesSUCCESS.  Each value is a tuple of allowed node
# operations followed by the expected LedgerEntryType.  Checked inside
# tx_result() with a single shared assertion ID.
_META_EXPECTATIONS: dict[str, tuple[str, ...]] = {
    "DIDSet": ("Created", "Modified", "DID"),
    "DIDDelete": ("Deleted", "DID"),
}

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

    for name in TX_TYPES:
        _emit_catalog_entry(_seen_id(name), "reachability", "Reachable", must_hit=True)
        _emit_catalog_entry(
            _success_id(name),
            "sometimes",
            "Sometimes",
            must_hit=(name not in _NO_SUCCESS_TYPES),
        )
        _emit_catalog_entry(
            _failure_id(name),
            "sometimes",
            "Sometimes",
            must_hit=(name not in _NO_FAILURE_TYPES),
        )
    _emit_catalog_entry("workload::always : valid_engine_result", "always", "Always", must_hit=True)
    _emit_catalog_entry(
        "workload::always : no_internal_rippled_error", "always", "Always", must_hit=True
    )
    _emit_catalog_entry(
        "workload::always : no_internal_rippled_error_submit", "always", "Always", must_hit=True
    )
    _emit_catalog_entry("workload::always : no_temDISABLED", "always", "Always", must_hit=True)
    _emit_catalog_entry(
        "workload::always : meta_matches_tx_type", "always", "Always", must_hit=True
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
        "credential_accepts",
        "tickets",
        "domains",
        "loan_brokers",
        "cover_deposits",
        "loans",
    ]:
        _emit_catalog_entry(
            f"workload::setup_{setup_key}", "reachability", "Reachable", must_hit=True
        )


def assert_no_internal_error_submit(name: str, result: dict) -> None:
    """Fire the no_internal_rippled_error_submit always-assertion for a submit result.

    Use from any path that submits directly via xrpl-py (bypassing ``submit_tx``)
    — e.g. setup-phase LoanSet co-signing and the node probe. Submit-time is
    where tef* internal errors must be caught: they never enter a closed ledger
    and so never reach the validated WS stream that ``tx_result`` consumes.
    """
    engine_result = result.get("engine_result", "") or ""
    if not engine_result:
        return
    tx_hash = result.get("tx_json", {}).get("hash", "") or result.get("hash", "")
    assert_raw(
        condition=engine_result not in _RIPPLED_INTERNAL_ERRORS,
        message="workload::always : no_internal_rippled_error_submit",
        details={
            "engine_result": engine_result,
            "tx_type": name,
            "hash": tx_hash,
        },
        loc_filename=_LOC_FILE,
        loc_function="assert_no_internal_error_submit",
        loc_class=_LOC_CLASS,
        loc_begin_line=0,
        loc_begin_column=_LOC_COL,
        hit=True,
        must_hit=True,
        assert_type="always",
        display_type="Always",
        assert_id="workload::always : no_internal_rippled_error_submit",
    )


def tx_submitted(name: str, txn: object = None, result: dict | None = None) -> None:
    """Report that a transaction was submitted to the network.

    ``result`` is the immediate /submit response dict. Passing it here
    surfaces ``engine_result`` in the event details and triggers the
    submit-time tef* internal-error always-assertion.
    """
    details: dict[str, str] = {"tx_type": name}
    if txn is not None:
        try:
            # Accept either an xrpl-py model or an already-serialized XRPL dict
            # (the raw-submit path in submit.py passes the mutated dict directly).
            raw = txn.to_xrpl() if hasattr(txn, "to_xrpl") else txn
            details["account"] = raw.get("Account", "")
            details["sequence"] = str(raw.get("Sequence", ""))
            details.update(_extract_object_ids(raw))
        except Exception:
            pass
    if result is not None:
        engine_result = result.get("engine_result", "") or ""
        engine_result_message = result.get("engine_result_message", "") or ""
        tx_hash = result.get("tx_json", {}).get("hash", "") or result.get("hash", "")
        if engine_result:
            details["engine_result"] = engine_result
        if engine_result_message:
            details["engine_result_message"] = engine_result_message
        if tx_hash:
            details["hash"] = tx_hash
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
    if result is not None:
        assert_no_internal_error_submit(name, result)


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
        must_hit=name not in _NO_SUCCESS_TYPES,
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
        must_hit=name not in _NO_FAILURE_TYPES,
        assert_type="sometimes",
        display_type="Sometimes",
        assert_id=_failure_id(name),
    )
    # Meta-invariant: on tesSUCCESS, verify expected ledger entry operations.
    exp = _META_EXPECTATIONS.get(name)
    if exp and engine_result == "tesSUCCESS":
        *ops, entry_type = exp
        has_match = any(
            node.get(f"{op}Node", {}).get("LedgerEntryType") == entry_type
            for node in meta.get("AffectedNodes", [])
            for op in ops
        )
        assert_raw(
            condition=has_match,
            message="workload::always : meta_matches_tx_type",
            details={
                "tx_type": name,
                "expected": list(exp),
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
            assert_id="workload::always : meta_matches_tx_type",
        )
