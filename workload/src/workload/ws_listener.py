"""WebSocket transaction observer.

Subscribes to the xrpld transactions stream and processes all validated
transaction results. Fires Antithesis assertions and updates workload
state (vaults, NFTs, etc.) from the validated metadata.

This replaces the per-transaction submit_and_wait pattern — transaction
modules submit fire-and-forget, and this listener observes all outcomes.
"""

import asyncio

from workload import logging
from workload.assertions import tx_result
from workload.models import (
    Credential, LoanBroker, Loan, MPTokenIssuance, NFT, NFTOffer,
    PermissionedDomain, TrustLine, Vault,
)
from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.models import StreamParameter, Subscribe

log = logging.getLogger(__name__)


def _extract_created_id(meta: dict, ledger_entry_type: str) -> str | None:
    """Return the LedgerIndex of the first CreatedNode matching the type."""
    for node in meta.get("AffectedNodes", []):
        created = node.get("CreatedNode", {})
        if created.get("LedgerEntryType") == ledger_entry_type:
            return created.get("LedgerIndex")
    return None


def _extract_deleted_id(meta: dict, ledger_entry_type: str) -> str | None:
    """Return the LedgerIndex of the first DeletedNode matching the type."""
    for node in meta.get("AffectedNodes", []):
        deleted = node.get("DeletedNode", {})
        if deleted.get("LedgerEntryType") == ledger_entry_type:
            return deleted.get("LedgerIndex")
    return None


# ── State updaters ───────────────────────────────────────────────────
# Each takes (workload, tx_json, meta) and updates tracking lists on success.


def _on_trust_set(w, tx, meta):
    account = tx.get("Account", "")
    limit = tx.get("LimitAmount", {})
    if isinstance(limit, dict):
        issuer = limit.get("issuer", "")
        currency = limit.get("currency", "")
        w.trust_lines.append(TrustLine(account_a=account, account_b=issuer, currency=currency))
        log.info("WS: trust line %s <-> %s %s", account, issuer, currency)


def _on_vault_create(w, tx, meta):
    vault_id = _extract_created_id(meta, "Vault")
    if vault_id:
        w.vaults.append(Vault(owner=tx["Account"], vault_id=vault_id))
        log.info("WS: vault %s by %s", vault_id, tx["Account"])


def _on_vault_delete(w, tx, meta):
    vault_id = _extract_deleted_id(meta, "Vault")
    if vault_id:
        w.vaults[:] = [v for v in w.vaults if v.vault_id != vault_id]
        log.info("WS: vault %s deleted", vault_id)


def _on_nftoken_mint(w, tx, meta):
    nftoken_id = meta.get("nftoken_id")
    if nftoken_id:
        account = tx["Account"]
        w.nfts.append(NFT(owner=account, nftoken_id=nftoken_id))
        # Also update per-account tracking if available
        if account in w.accounts:
            w.accounts[account].nfts.add(nftoken_id)
        log.info("WS: minted NFT %s for %s", nftoken_id, account)


def _on_nftoken_burn(w, tx, meta):
    nftoken_id = tx.get("NFTokenID")
    if nftoken_id:
        w.nfts[:] = [n for n in w.nfts if n.nftoken_id != nftoken_id]
        for acc in w.accounts.values():
            acc.nfts.discard(nftoken_id)
        log.info("WS: burned NFT %s", nftoken_id)


def _on_nftoken_create_offer(w, tx, meta):
    offer_id = _extract_created_id(meta, "NFTokenOffer")
    if offer_id:
        nftoken_id = tx.get("NFTokenID", "")
        is_sell = bool(tx.get("Flags", 0) & 1)  # tfSellNFToken = 0x00000001
        w.nft_offers.append(NFTOffer(
            creator=tx["Account"], offer_id=offer_id,
            nftoken_id=nftoken_id, is_sell=is_sell,
        ))
        log.info("WS: NFT offer %s (%s)", offer_id, "sell" if is_sell else "buy")


def _on_nftoken_cancel_offer(w, tx, meta):
    offer_ids = tx.get("NFTokenOffers", [])
    if offer_ids:
        removed = set(offer_ids)
        w.nft_offers[:] = [o for o in w.nft_offers if o.offer_id not in removed]


def _on_mpt_create(w, tx, meta):
    mpt_id = _extract_created_id(meta, "MPTokenIssuance")
    if mpt_id:
        w.mpt_issuances.append(MPTokenIssuance(issuer=tx["Account"], mpt_issuance_id=mpt_id))
        log.info("WS: MPT issuance %s by %s", mpt_id, tx["Account"])


def _on_mpt_destroy(w, tx, meta):
    mpt_id = tx.get("MPTokenIssuanceID")
    if mpt_id:
        w.mpt_issuances[:] = [m for m in w.mpt_issuances if m.mpt_issuance_id != mpt_id]
        log.info("WS: MPT issuance %s destroyed", mpt_id)


def _on_credential_create(w, tx, meta):
    w.credentials.append(Credential(
        issuer=tx["Account"],
        subject=tx.get("Subject", ""),
        credential_type=tx.get("CredentialType", ""),
    ))
    log.info("WS: credential %s -> %s", tx["Account"], tx.get("Subject", ""))


def _on_credential_delete(w, tx, meta):
    subject = tx.get("Subject", "")
    issuer = tx.get("Issuer", tx.get("Account", ""))
    cred_type = tx.get("CredentialType", "")
    w.credentials[:] = [
        c for c in w.credentials
        if not (c.issuer == issuer and c.subject == subject and c.credential_type == cred_type)
    ]


def _on_ticket_create(w, tx, meta):
    account = tx["Account"]
    seq = tx.get("Sequence", 0)
    count = tx.get("TicketCount", 0)
    if account in w.accounts and seq and count:
        tix = set(range(seq + 1, seq + 1 + count))
        w.accounts[account].tickets.update(tix)
        log.info("WS: %d tickets for %s (seqs %d-%d)", count, account, seq + 1, seq + count)


def _on_domain_set(w, tx, meta):
    domain_id = _extract_created_id(meta, "PermissionedDomain")
    if domain_id:
        w.domains.append(PermissionedDomain(owner=tx["Account"], domain_id=domain_id))
        log.info("WS: domain %s by %s", domain_id, tx["Account"])


def _on_domain_delete(w, tx, meta):
    domain_id = _extract_deleted_id(meta, "PermissionedDomain")
    if domain_id:
        w.domains[:] = [d for d in w.domains if d.domain_id != domain_id]


def _on_loan_broker_set(w, tx, meta):
    broker_id = _extract_created_id(meta, "LoanBroker")
    vault_id = tx.get("VaultID", "")
    if broker_id:
        w.loan_brokers.append(LoanBroker(owner=tx["Account"], loan_broker_id=broker_id, vault_id=vault_id))
        log.info("WS: loan broker %s by %s", broker_id, tx["Account"])


def _on_loan_broker_delete(w, tx, meta):
    broker_id = _extract_deleted_id(meta, "LoanBroker")
    if broker_id:
        w.loan_brokers[:] = [b for b in w.loan_brokers if b.loan_broker_id != broker_id]


def _on_loan_set(w, tx, meta):
    loan_id = _extract_created_id(meta, "Loan")
    broker_id = tx.get("LoanBrokerID", "")
    if loan_id:
        w.loans.append(Loan(borrower=tx["Account"], loan_id=loan_id, loan_broker_id=broker_id))
        log.info("WS: loan %s by %s", loan_id, tx["Account"])


def _on_loan_delete(w, tx, meta):
    loan_id = _extract_deleted_id(meta, "Loan")
    if loan_id:
        w.loans[:] = [l for l in w.loans if l.loan_id != loan_id]


# ── Dispatch table ───────────────────────────────────────────────────

_STATE_UPDATERS = {
    "TrustSet": _on_trust_set,
    "VaultCreate": _on_vault_create,
    "VaultDelete": _on_vault_delete,
    "NFTokenMint": _on_nftoken_mint,
    "NFTokenBurn": _on_nftoken_burn,
    "NFTokenCreateOffer": _on_nftoken_create_offer,
    "NFTokenCancelOffer": _on_nftoken_cancel_offer,
    "MPTokenIssuanceCreate": _on_mpt_create,
    "MPTokenIssuanceDestroy": _on_mpt_destroy,
    "CredentialCreate": _on_credential_create,
    "CredentialDelete": _on_credential_delete,
    "TicketCreate": _on_ticket_create,
    "PermissionedDomainSet": _on_domain_set,
    "PermissionedDomainDelete": _on_domain_delete,
    "LoanBrokerSet": _on_loan_broker_set,
    "LoanBrokerDelete": _on_loan_broker_delete,
    "LoanSet": _on_loan_set,
    "LoanDelete": _on_loan_delete,
}


def _handle_validated_tx(workload, msg: dict) -> None:
    """Process a single validated transaction from the WS stream."""
    meta = msg.get("meta", {})
    tx = msg.get("tx_json", {})
    tx_type = tx.get("TransactionType", "")
    engine_result = msg.get("engine_result") or meta.get("TransactionResult", "")
    tx_hash = msg.get("hash", "")
    account = tx.get("Account", "")

    # Only process transactions from our accounts
    if account not in workload.accounts:
        return

    # Build a normalized result dict for tx_result()
    result = {
        "engine_result": engine_result,
        "engine_result_message": "",
        "tx_json": tx,
        "hash": tx_hash,
        "meta": meta,
    }
    tx_result(tx_type, result)

    # Update state on success
    if engine_result == "tesSUCCESS":
        updater = _STATE_UPDATERS.get(tx_type)
        if updater:
            try:
                updater(workload, tx, meta)
            except Exception as e:
                log.error("WS: state update failed for %s: %s", tx_type, e)


async def start_ws_listener(workload, ws_url: str) -> None:
    """Subscribe to validated transactions and process results.

    Runs forever with automatic reconnection. Start as a background task.
    """
    log.info("WS listener connecting to %s", ws_url)
    while True:
        try:
            async with AsyncWebsocketClient(ws_url) as ws:
                await ws.send(Subscribe(streams=[StreamParameter.TRANSACTIONS]))
                log.info("WS listener subscribed to transactions stream")
                async for msg in ws:
                    if msg.get("type") == "transaction" and msg.get("validated"):
                        _handle_validated_tx(workload, msg)
        except Exception as e:
            log.warning("WS listener disconnected: %s, reconnecting in 2s...", e)
            await asyncio.sleep(2)
