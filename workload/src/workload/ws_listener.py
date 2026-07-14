"""WebSocket transaction observer: fires assertions and updates state from validated tx metadata."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from workload.app import Workload

from antithesis.lifecycle import send_event
from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.models import StreamParameter, Subscribe, TransactionFlag

from workload import logging
from workload.assertions import assert_ticket_used, tx_result
from workload.transactions import STATE_UPDATERS

log = logging.getLogger(__name__)

_TF_INNER_BATCH_TXN = int(TransactionFlag.TF_INNER_BATCH_TXN)
_TF_HYBRID = 0x00100000  # OfferCreateFlag.TF_HYBRID
_TF_SPONSORSHIP_DELETE_OBJECT = 0x00100000  # SponsorshipSetFlag.TF_DELETE_OBJECT
_TF_SPONSOR_CREATED_ACCOUNT = 0x00080000  # PaymentFlag.TF_SPONSOR_CREATED_ACCOUNT

# Object-creating types the sponsor Modifier attaches reserve sponsors to; a
# tesSUCCESS on one may carry a Sponsor on the created ledger entry, which
# _on_reserve_sponsored_create records into w.sponsored_objects (feeding
# SponsorshipTransfer's sponsored pool + the audit). The reserve outcome dims
# (sponsor_reserve_*) fire generically from assertions.tx_result, not here.
_SPF_SPONSOR_RESERVE = 0x00000002  # common SponsorFlags bit (any Transaction subtype)
_RESERVE_SPONSOR_TX_TYPES = {
    "CheckCreate",
    "EscrowCreate",
    "PaymentChannelCreate",
    "TrustSet",
    "CredentialCreate",
    "SignerListSet",
    "MPTokenAuthorize",
}


def _amount_is_mpt(amt: object) -> bool:
    return isinstance(amt, dict) and "mpt_issuance_id" in amt


def _delivered_amount(tx: dict) -> object:
    """api_version 2 renames Payment Amount→DeliverMax and drops Amount; fall back for v1."""
    return tx.get("DeliverMax", tx.get("Amount"))


def _on_reserve_sponsored_create(w: Workload, tx: dict, meta: dict) -> None:
    """Records w.sponsored_objects for any reserve-sponsored create, regardless of
    real TransactionType -- one generic parse of the CreatedNode's Sponsor field
    (HighSponsor/LowSponsor for RippleState) rather than a per-type copy. Only
    fires when the reserve actually landed on the object: spfSponsorFee-only
    sponsorship never touches the ledger entry, so the field's absence there
    (even with SponsorFlags present on the tx) correctly skips tracking."""
    owner = tx.get("Account", "")
    for node in meta.get("AffectedNodes", []):
        fields = node.get("CreatedNode", {}).get("NewFields", {})
        sponsor = fields.get("Sponsor") or fields.get("HighSponsor") or fields.get("LowSponsor")
        if not sponsor:
            continue
        idx = node["CreatedNode"].get("LedgerIndex")
        if isinstance(idx, str):
            w.sponsored_objects[idx] = (owner, sponsor)
        return


def _handle_validated_tx(workload: Workload, msg: dict) -> None:
    meta = msg.get("meta", {})
    tx = msg.get("tx_json", {})
    tx_type = tx.get("TransactionType", "")
    engine_result = msg.get("engine_result") or meta.get("TransactionResult", "")
    tx_hash = msg.get("hash", "")
    account = tx.get("Account", "")

    if account not in workload.accounts:
        return

    # Inner batch txns arrive as top-level entries alongside their outer Batch.
    # Tag them, then fall through so state updaters still see the side effects.
    if tx.get("Flags", 0) & _TF_INNER_BATCH_TXN:
        send_event(
            "workload::inner_batch_observed",
            {
                "tx_type": tx_type,
                "account": account,
                "sequence": str(tx.get("Sequence", "")),
                "hash": tx_hash,
                "engine_result": engine_result,
                "parent_batch_id": meta.get("ParentBatchID", ""),
            },
        )

    result = {
        "engine_result": engine_result,
        "engine_result_message": "",
        "tx_json": tx,
        "hash": tx_hash,
        "meta": meta,
    }
    tx_result(tx_type, result)

    # TicketSequence ⇒ a ticket was consumed (ticket modifier); tx still buckets under its real type
    if tx.get("TicketSequence") is not None:
        assert_ticket_used(tx_type, tx_hash)

    # DomainID ⇒ synthetic permissioned-DEX buckets so REGISTRY synthetic-name assertions resolve.
    if tx_type == "OfferCreate" and tx.get("DomainID"):
        is_hybrid = bool(tx.get("Flags", 0) & _TF_HYBRID)
        tx_result("OfferCreateHybrid" if is_hybrid else "OfferCreateDomain", result)
    elif tx_type == "Payment" and tx.get("DomainID"):
        # SendMax or non-string delivered amount ⇒ cross-currency (api_version 2 renames Amount).
        is_xc = tx.get("SendMax") is not None or not isinstance(_delivered_amount(tx), str)
        tx_result("PaymentDomainXC" if is_xc else "PaymentDomain", result)
    elif tx_type == "Payment" and tx.get("Flags", 0) & _TF_SPONSOR_CREATED_ACCOUNT:
        tx_result("PaymentSponsoredAccount", result)

    # MPT-on-DEX (XLS-82): pure MPT offer has no DomainID, so never double-fires domain buckets.
    if tx_type == "OfferCreate" and (
        _amount_is_mpt(tx.get("TakerGets")) or _amount_is_mpt(tx.get("TakerPays"))
    ):
        tx_result("OfferCreateMPT", result)

    # MPT-on-DEX (XLS-82): any MPT-bearing payment (Amount or SendMax) feeds PaymentMPT.
    if tx_type == "Payment" and (
        _amount_is_mpt(_delivered_amount(tx)) or _amount_is_mpt(tx.get("SendMax"))
    ):
        tx_result("PaymentMPT", result)

    # Sponsorship (XLS-68): synthetic buckets on top of the two real types.
    if tx_type == "SponsorshipSet" and tx.get("Flags", 0) & _TF_SPONSORSHIP_DELETE_OBJECT:
        tx_result("SponsorshipSetDelete", result)
    elif tx_type == "SponsorshipTransfer" and not tx.get("ObjectID"):
        tx_result("SponsorshipTransferAccount", result)

    # State-tree bloat driver: only PaymentFundNew sends XRP to an unfunded new
    # account, so a Payment that CREATED an AccountRoot uniquely feeds that bucket.
    if tx_type == "Payment" and any(
        node.get("CreatedNode", {}).get("LedgerEntryType") == "AccountRoot"
        for node in meta.get("AffectedNodes", [])
    ):
        tx_result("PaymentFundNew", result)

    if engine_result == "tesSUCCESS":
        if tx_type in _RESERVE_SPONSOR_TX_TYPES:
            _on_reserve_sponsored_create(workload, tx, meta)
        updater = STATE_UPDATERS.get(tx_type)
        if updater:
            try:
                updater(workload, tx, meta)
            except Exception as e:
                log.error("WS: state update failed for %s: %s", tx_type, e)


async def start_ws_listener(workload: Workload, ws_url: str) -> None:
    """Run forever with auto-reconnect; start as a background task."""
    while True:
        try:
            async with AsyncWebsocketClient(ws_url) as ws:
                await ws.send(Subscribe(streams=[StreamParameter.TRANSACTIONS]))
                async for msg in ws:
                    if msg.get("type") == "transaction" and msg.get("validated"):
                        _handle_validated_tx(workload, msg)
        except Exception as e:
            log.warning("WS listener disconnected: %s, reconnecting in 2s...", e)
            await asyncio.sleep(2)
