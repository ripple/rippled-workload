"""Antithesis assertion helpers for the workload."""

from antithesis.assertions import assert_raw
from antithesis.lifecycle import send_event
from xrpl.models import Transaction, TransactionFlag

_LOC_FILE = "workload/assertions.py"
_LOC_CLASS = ""
_LOC_COL = 0

# Inner batch txns (XLS-56d) consolidate effects into the outer Batch's meta, so
# their own AffectedNodes may be empty — skip to avoid false invariant fires.
_TF_INNER_BATCH_TXN = int(TransactionFlag.TF_INNER_BATCH_TXN)

# Sponsor (XLS-68) SponsorFlags bits; xrpl-py ships them as raw ints (no enum),
# same as params.py/ws_listener.py mirror these bitmasks locally.
_SPF_SPONSOR_FEE = 0x00000001
_SPF_SPONSOR_RESERVE = 0x00000002

# Real tx types rippled allow-lists for reserve sponsorship (isReserveSponsorAllowed,
# SponsorHelpers.cpp). A reserve sponsor on any OTHER type is preflight temINVALID_FLAG
# -- the /sponsor/malformation disallowed_type vector. Mirror, not import (rippled C++);
# drift only under-fires a `sometimes`, never breaks correctness.
_RESERVE_SPONSOR_ALLOWED_TYPES = {
    "DelegateSet",
    "DepositPreauth",
    "Payment",
    "SignerListSet",
    "CheckCancel",
    "CheckCash",
    "CheckCreate",
    "EscrowCancel",
    "EscrowCreate",
    "EscrowFinish",
    "PaymentChannelClaim",
    "PaymentChannelCreate",
    "PaymentChannelFund",
    "Clawback",
    "MPTokenAuthorize",
    "MPTokenIssuanceCreate",
    "MPTokenIssuanceDestroy",
    "MPTokenIssuanceSet",
    "TrustSet",
    "CredentialAccept",
    "CredentialCreate",
    "CredentialDelete",
    "AccountSet",
    "SetRegularKey",
    "SponsorshipTransfer",
}

# Engine results signalling a rippled internal error. Checked on BOTH submit
# and validated WS: tef* never validates (submit-side catches it); but
# tecINVARIANT_FAILED claims a fee and DOES validate, so the validated side
# must catch that one.
_RIPPLED_INTERNAL_ERRORS = (
    "tefEXCEPTION",
    "tefINTERNAL",
    "tefINVARIANT_FAILED",
    "tefFAILURE",
    "tecINVARIANT_FAILED",
)

# Types whose faulty handler never reliably produces a tec. The failure bucket is
# fed only by ledger-entering results, so tem*/tef* (preflight) and tesSUCCESS
# no-ops don't satisfy it — they feed seen + the submit-time internal-error check.
# Generative fuzz mostly yields tem*/tef*, so it doesn't lift these out of the set.
# - SignerListSet: fake AccountIDs are accepted as phantom accounts; quorum/weights
#   stay in bounds, so every curated vector reaches tesSUCCESS.
# - MPTokenIssuanceCreate: a fresh issuance from a funded account succeeds; curated
#   vectors + fuzz are tem-only (the reserve/dir tecs need an exhausted account).
# - AccountSet: every malformation is preflight tem (bad TransferRate/TickSize,
#   set==clear flag); the lone tec (tecNO_PERMISSION) comes from OTHER txns.
# - SetRegularKey: self-key is temBAD_REGKEY; a fake (unfunded) key just succeeds.
# - TrustSet: self-trust is temDST_IS_SRC; no tec is reachable with funded accounts.
# - TicketCreate: bad count is temINVALID_COUNT; reserve tecs need an exhausted account.
# - NFTokenCancelOffer: canceling an unknown offer id is a tesSUCCESS no-op.
# - SponsorMalformation: every vector is a preflight tem* that never enters a ledger,
#   so the validated failure bucket can't fill (submit-time seen + dims cover it).
_NO_FAILURE_TYPES = {
    "SignerListSet",
    "MPTokenIssuanceCreate",
    "AccountSet",
    "SetRegularKey",
    "TrustSet",
    "TicketCreate",
    "NFTokenCancelOffer",
    "SponsorMalformation",
}

# Types that effectively never succeed in this test environment:
# - AccountDelete: every account owns directory objects → tecHAS_OBLIGATIONS.
# - AMMDelete: rippled auto-deletes empty AMMs on the last LP's TF_WITHDRAW_ALL,
#   so it finds the AMM gone (tecAMM_NOT_FOUND) or non-empty (tecAMM_NOT_EMPTY).
# - PaymentDomainXC: cross-currency domain payment needs resting domain
#   liquidity the workload doesn't guarantee; usually tecPATH_DRY. Drop if hit.
# - SponsorMalformation: all vectors are preflight tem*, so no tesSUCCESS ever lands.
_NO_SUCCESS_TYPES = {"AccountDelete", "AMMDelete", "PaymentDomainXC", "SponsorMalformation"}

# tx types that must touch a specific ledger entry on tesSUCCESS.
# Value: allowed node ops followed by the expected LedgerEntryType.
_META_EXPECTATIONS: dict[str, tuple[str, ...]] = {
    "DIDSet": ("Created", "Modified", "DID"),
    "DIDDelete": ("Deleted", "DID"),
    # Convert Creates the holder MPToken on first use, else Modifies; others Modify.
    "ConfidentialMPTConvert": ("Created", "Modified", "MPToken"),
    "ConfidentialMPTMergeInbox": ("Modified", "MPToken"),
    "ConfidentialMPTSend": ("Modified", "MPToken"),
    "ConfidentialMPTConvertBack": ("Modified", "MPToken"),
    "ConfidentialMPTClawback": ("Modified", "MPToken"),
    # Verified against rippled transactors: each touches its entry on every
    # tesSUCCESS. Create-or-update txns list both ops (new → Created, existing →
    # Modified); NFTokenMint places into a new page (Created) or existing (Modified).
    "EscrowCreate": ("Created", "Escrow"),
    "EscrowFinish": ("Deleted", "Escrow"),
    "EscrowCancel": ("Deleted", "Escrow"),
    "CheckCreate": ("Created", "Check"),
    "CheckCash": ("Deleted", "Check"),
    "CheckCancel": ("Deleted", "Check"),
    "PaymentChannelCreate": ("Created", "PayChannel"),
    "TicketCreate": ("Created", "Ticket"),
    "CredentialCreate": ("Created", "Credential"),
    "DepositPreauth": ("Created", "DepositPreauth"),
    "CredentialAccept": ("Modified", "Credential"),
    "CredentialDelete": ("Deleted", "Credential"),
    "MPTokenIssuanceCreate": ("Created", "MPTokenIssuance"),
    "MPTokenIssuanceDestroy": ("Deleted", "MPTokenIssuance"),
    "NFTokenMint": ("Created", "Modified", "NFTokenPage"),
    "PermissionedDomainSet": ("Created", "Modified", "PermissionedDomain"),
    "PermissionedDomainDelete": ("Deleted", "PermissionedDomain"),
    "AMMCreate": ("Created", "AMM"),
    "VaultCreate": ("Created", "Vault"),
    "VaultDelete": ("Deleted", "Vault"),
    "LoanBrokerSet": ("Created", "Modified", "LoanBroker"),
    "LoanBrokerDelete": ("Deleted", "LoanBroker"),
    "LoanSet": ("Created", "Loan"),
    "LoanDelete": ("Deleted", "Loan"),
    # Create/refill Modifies or Creates; TF_DELETE_OBJECT Deletes.
    "SponsorshipSet": ("Created", "Modified", "Deleted", "Sponsorship"),
}

# XRPL fields → normalized event keys; only object identifiers, for tracking.
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
    ids: dict[str, str] = {}
    for xrpl_key, event_key in _OBJECT_ID_FIELDS.items():
        if xrpl_key in raw:
            ids[event_key] = str(raw[xrpl_key])
    return ids


def _emit_catalog_entry(message: str, assert_type: str, display_type: str, must_hit: bool) -> None:
    """hit=False registers existence with Antithesis without claiming a hit."""
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


def assert_network_functional(ok: bool, engine_result: str) -> None:
    """Post-fault liveness: at least one probe payment must validate tesSUCCESS."""
    assert_raw(
        condition=ok,
        message="workload::sometimes : network_functional_after_faults",
        details={"engine_result": engine_result},
        loc_filename=_LOC_FILE,
        loc_function="assert_network_functional",
        loc_class=_LOC_CLASS,
        loc_begin_line=0,
        loc_begin_column=_LOC_COL,
        hit=True,
        must_hit=True,
        assert_type="sometimes",
        display_type="Sometimes",
        assert_id="workload::sometimes : network_functional_after_faults",
    )


def register_assertions() -> None:
    """Call once at startup, before any transactions are submitted."""
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
    # must_hit=False: only fires against an XLS-0096 xrpld, so daily non-confidential
    # runs must not starve on it.
    _emit_catalog_entry(
        "workload::always : conf_mpt_version_monotonic", "always", "Always", must_hit=False
    )
    _emit_catalog_entry(
        "workload::sometimes : network_functional_after_faults",
        "sometimes",
        "Sometimes",
        must_hit=True,
    )
    _emit_catalog_entry("workload::always : no_sponsored_queue", "always", "Always", must_hit=True)
    # Ticket modifier (workload.modifiers): a validated tx carried a TicketSequence.
    _emit_catalog_entry(
        "workload::sometimes : ticket_used", "sometimes", "Sometimes", must_hit=True
    )
    for key in (
        "sponsor_fee_prefunded_used",
        "sponsor_fee_cosigned_used",
        "sponsor_reserve_budget_exhausted",
        "sponsor_reserve_succeeded",
        "sponsor_reserve_failed",
        "sponsor_reserve_exhausted",
        "sponsor_disallowed_type_rejected",
        "sponsor_no_permission_seen",
        "sponsor_has_obligations_seen",
        "sponsorship_audit_object_consistent",
        "sponsorship_audit_account_consistent",
    ):
        _emit_catalog_entry(f"workload::sometimes : {key}", "sometimes", "Sometimes", must_hit=True)
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
    """For direct xrpl-py submits (bypassing submit_tx): tef* internal errors
    never enter a closed ledger, so only the submit side can catch them."""
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


def _fire_sometimes(key: str, condition: bool, details: dict[str, str]) -> None:
    """Shared plumbing for the sponsor-state reachability signals below --
    ``sometimes`` only needs one True hit, so a False call is a harmless no-op."""
    msg = f"workload::sometimes : {key}"
    assert_raw(
        condition=condition,
        message=msg,
        details=details,
        loc_filename=_LOC_FILE,
        loc_function="_fire_sometimes",
        loc_class=_LOC_CLASS,
        loc_begin_line=0,
        loc_begin_column=_LOC_COL,
        hit=True,
        must_hit=True,
        assert_type="sometimes",
        display_type="Sometimes",
        assert_id=msg,
    )


def _assert_sponsor_submit_signals(name: str, raw: dict, result: dict) -> None:
    """Sponsor-state reachability + the TxQ invariant below, all read straight off
    the submit response (no extra RPCs, no waiting on validation). ``ter*`` results
    never enter a ledger, so terNO_SPONSORSHIP would never reach tx_result's
    validated stream -- this is the only place that can catch it."""
    engine_result = result.get("engine_result", "") or ""
    if not engine_result:
        return
    tx_hash = result.get("tx_json", {}).get("hash", "") or result.get("hash", "")
    details = {"engine_result": engine_result, "tx_type": name, "hash": tx_hash}

    if raw.get("Sponsor"):
        # rippled's TxQ only rejects FEE-sponsored txns (TxQ.cpp: sfSponsor &&
        # isFeeSponsored); reserve-only sponsorship queues legitimately.
        fee_sponsored = bool(int(raw.get("SponsorFlags", 0) or 0) & _SPF_SPONSOR_FEE)
        assert_raw(
            condition=not (fee_sponsored and engine_result == "terQUEUED"),
            message="workload::always : no_sponsored_queue",
            details=details,
            loc_filename=_LOC_FILE,
            loc_function="_assert_sponsor_submit_signals",
            loc_class=_LOC_CLASS,
            loc_begin_line=0,
            loc_begin_column=_LOC_COL,
            hit=True,
            must_hit=True,
            assert_type="always",
            display_type="Always",
            assert_id="workload::always : no_sponsored_queue",
        )
        _fire_sometimes(
            "sponsor_reserve_budget_exhausted",
            engine_result in ("terNO_SPONSORSHIP", "tecINSUFFICIENT_RESERVE"),
            details,
        )
        # Reserve sponsor on a type outside rippled's allow-list -> preflight
        # temINVALID_FLAG (the /sponsor/malformation disallowed_type vector).
        reserve_sponsored = bool(int(raw.get("SponsorFlags", 0) or 0) & _SPF_SPONSOR_RESERVE)
        if reserve_sponsored and raw.get("TransactionType") not in _RESERVE_SPONSOR_ALLOWED_TYPES:
            _fire_sometimes(
                "sponsor_disallowed_type_rejected", engine_result == "temINVALID_FLAG", details
            )

    _fire_sometimes(
        "sponsor_no_permission_seen", engine_result == "tecNO_SPONSOR_PERMISSION", details
    )
    if name == "AccountDelete":
        _fire_sometimes(
            "sponsor_has_obligations_seen", engine_result == "tecHAS_OBLIGATIONS", details
        )


def _assert_sponsor_fee_usage(name: str, tx_json: dict, engine_result: str, tx_hash: str) -> None:
    """Distinguishes the two ways a fee-sponsored tx reaches tesSUCCESS: a prefunded
    Sponsorship budget (no counter-signature) vs. a co-signed one. Needs the
    validated tx, unlike the submit-time checks above -- SponsorSignature is present
    on the wire either way, but "did it actually land" is only certain post-validation."""
    if engine_result != "tesSUCCESS":
        return
    sponsor_flags = int(tx_json.get("SponsorFlags", 0) or 0)
    if not tx_json.get("Sponsor") or not (sponsor_flags & _SPF_SPONSOR_FEE):
        return
    cosigned = bool(tx_json.get("SponsorSignature"))
    details = {"tx_type": name, "hash": tx_hash}
    _fire_sometimes("sponsor_fee_cosigned_used", cosigned, details)
    _fire_sometimes("sponsor_fee_prefunded_used", not cosigned, details)


def _assert_sponsor_reserve_usage(
    name: str, tx_json: dict, engine_result: str, tx_hash: str
) -> None:
    """Reserve-sponsor outcome dims off the validated tx (a tec claims a fee and
    validates, so it reaches here; tem/ter/tef never do). Mirrors the fee dims but
    routes both success and failure -- these cross-type buckets replace the deleted
    per-type Sponsored* success/failure buckets and their flakiness."""
    sponsor_flags = int(tx_json.get("SponsorFlags", 0) or 0)
    if not tx_json.get("Sponsor") or not (sponsor_flags & _SPF_SPONSOR_RESERVE):
        return
    details = {"tx_type": name, "hash": tx_hash, "engine_result": engine_result}
    _fire_sometimes("sponsor_reserve_succeeded", engine_result == "tesSUCCESS", details)
    _fire_sometimes("sponsor_reserve_failed", engine_result != "tesSUCCESS", details)
    _fire_sometimes(
        "sponsor_reserve_exhausted", engine_result == "tecINSUFFICIENT_RESERVE", details
    )


def assert_sponsorship_audit(kind: str, consistent: bool) -> None:
    """Soft cross-check (transactions/sponsorship.py's SponsorshipAudit) between
    tracked sponsor state and the validated ledger. Deliberately a ``sometimes``,
    not an ``always``: tracked state legitimately drifts (the caller prunes on a
    genuine miss), so only a systematic break -- this bucket never satisfying --
    is actually worth triaging."""
    _fire_sometimes(f"sponsorship_audit_{kind}_consistent", consistent, {"kind": kind})


def assert_ticket_used(tx_type: str, tx_hash: str) -> None:
    """A validated tx carrying TicketSequence consumed a ticket (workload.modifiers)."""
    _fire_sometimes("ticket_used", True, {"tx_type": tx_type, "hash": tx_hash})


def tx_submitted(
    name: str, txn: Transaction | dict | None = None, result: dict | None = None
) -> None:
    """Passing the /submit response triggers the submit-time tef* check."""
    details: dict[str, str] = {"tx_type": name}
    raw: dict = {}
    if txn is not None:
        try:
            # Accept an xrpl-py model or an already-serialized dict (raw-submit
            # path passes the mutated dict directly).
            raw = txn.to_xrpl() if isinstance(txn, Transaction) else txn
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
        _assert_sponsor_submit_signals(name, raw, result)


def tx_result(name: str, result: dict) -> None:
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
    # On tesSUCCESS, verify the expected ledger entry operations. Skip inner-batch
    # txns: effects consolidate into the outer Batch's meta, so their own may be empty.
    exp = _META_EXPECTATIONS.get(name)
    if (
        exp
        and engine_result == "tesSUCCESS"
        and not (int(tx_json.get("Flags", 0)) & _TF_INNER_BATCH_TXN)
    ):
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
    # Every successful Confidential MPT op must strictly bump the holder MPToken's
    # ConfidentialBalanceVersion (replay / stale-proof guard). Skip inner-batch txns
    # (consolidated meta); only check ModifiedNodes carrying both version fields.
    if (
        name.startswith("ConfidentialMPT")
        and engine_result == "tesSUCCESS"
        and not (int(tx_json.get("Flags", 0)) & _TF_INNER_BATCH_TXN)
    ):
        monotonic = True
        for node in meta.get("AffectedNodes", []):
            modified = node.get("ModifiedNode", {})
            if modified.get("LedgerEntryType") != "MPToken":
                continue
            prev = modified.get("PreviousFields", {})
            final = modified.get("FinalFields", {})
            if (
                "ConfidentialBalanceVersion" in prev
                and "ConfidentialBalanceVersion" in final
                and int(final["ConfidentialBalanceVersion"])
                <= int(prev["ConfidentialBalanceVersion"])
            ):
                monotonic = False
                break
        assert_raw(
            condition=monotonic,
            message="workload::always : conf_mpt_version_monotonic",
            details={"tx_type": name, "hash": details.get("hash", "")},
            loc_filename=_LOC_FILE,
            loc_function="tx_result",
            loc_class=_LOC_CLASS,
            loc_begin_line=0,
            loc_begin_column=_LOC_COL,
            hit=True,
            must_hit=False,
            assert_type="always",
            display_type="Always",
            assert_id="workload::always : conf_mpt_version_monotonic",
        )
    _assert_sponsor_fee_usage(name, tx_json, engine_result, details.get("hash", ""))
    _assert_sponsor_reserve_usage(name, tx_json, engine_result, details.get("hash", ""))
