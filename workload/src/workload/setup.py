"""Deterministic setup for the Antithesis first_* phase.

Creates the minimum ledger state needed for all transaction types to operate.
Called via the /setup endpoint before fault injection begins.

All account selection is index-based (no randomness). All parameters are
hardcoded safe values (no params.* randomizers, no should_send_faulty()).
State tracking is handled by ws_listener.py — setup just submits
transactions and counts preliminary successes.
"""

import asyncio

import xrpl.models
from workload import logging
from workload.submit import submit_tx
from antithesis.assertions import reachable
from xrpl.models import IssuedCurrencyAmount
from xrpl.models.transactions import (
    TrustSet,
    MPTokenIssuanceCreate,
    VaultCreate,
    NFTokenMint,
    NFTokenMintFlag,
    CredentialCreate,
    TicketCreate,
    PermissionedDomainSet,
)
from xrpl.models.transactions.deposit_preauth import Credential as XRPLCredential

log = logging.getLogger(__name__)

_SETUP_CREDENTIAL_TYPE = "setup".encode("utf-8").hex()
_TRUSTLINE_LIMIT = "1000000000000000"
_VAULT_ASSETS_MAXIMUM = "1000000000"
_TICKET_COUNT = 5


def _accounts_list(workload) -> list:
    return list(workload.accounts.values())


async def _submit_batch(name: str, txns: list[tuple], client) -> int:
    """Submit a batch of (tx_type_name, txn, wallet) tuples. Returns count of accepted submissions."""
    created = 0
    for i, (tx_type, txn, wallet) in enumerate(txns):
        try:
            result = await submit_tx(tx_type, txn, client, wallet)
            engine = result.get("engine_result", "")
            if engine in ("tesSUCCESS", "terQUEUED", "terPRE_SEQ"):
                created += 1
            else:
                log.warning("Setup %s[%d]: %s", name, i, engine)
        except Exception as exc:
            log.error("Setup %s[%d] failed: %s", name, i, exc)
    return created


async def run_setup(workload) -> dict:
    """Submit deterministic setup transactions. State tracking via WS listener."""
    log.info("Setup: starting (%d accounts available)", len(workload.accounts))
    accs = _accounts_list(workload)
    client = workload.client
    codes = workload.currency_codes
    summary = {}

    # Trust lines: 10 pairs from accounts[0..19]
    summary["trust_lines"] = await _submit_batch("trust_lines", [
        ("TrustSet", TrustSet(
            account=accs[i].address,
            limit_amount=IssuedCurrencyAmount(
                currency=codes[(i // 2) % len(codes)],
                issuer=accs[i + 1].address,
                value=_TRUSTLINE_LIMIT,
            ),
        ), accs[i].wallet)
        for i in range(0, min(20, len(accs) - 1), 2)
    ], client)

    # MPT issuances: 5 from accounts[0..4]
    summary["mpt_issuances"] = await _submit_batch("mpt", [
        ("MPTokenIssuanceCreate", MPTokenIssuanceCreate(account=accs[i].address), accs[i].wallet)
        for i in range(min(5, len(accs)))
    ], client)

    # Vaults: 5 from accounts[10..14]
    summary["vaults"] = await _submit_batch("vaults", [
        ("VaultCreate", VaultCreate(
            account=accs[10 + i].address,
            asset=xrpl.models.XRP(),
            assets_maximum=_VAULT_ASSETS_MAXIMUM,
        ), accs[10 + i].wallet)
        for i in range(min(5, max(0, len(accs) - 10)))
    ], client)

    # NFTs: 5 from accounts[20..24]
    summary["nfts"] = await _submit_batch("nfts", [
        ("NFTokenMint", NFTokenMint(
            account=accs[20 + i].address,
            nftoken_taxon=0,
            flags=NFTokenMintFlag.TF_TRANSFERABLE,
        ), accs[20 + i].wallet)
        for i in range(min(5, max(0, len(accs) - 20)))
    ], client)

    # Credentials: 5, issuer=accounts[30], subjects=accounts[31..35]
    if len(accs) > 35:
        issuer = accs[30]
        summary["credentials"] = await _submit_batch("credentials", [
            ("CredentialCreate", CredentialCreate(
                account=issuer.address,
                subject=accs[31 + i].address,
                credential_type=_SETUP_CREDENTIAL_TYPE,
            ), issuer.wallet)
            for i in range(5)
        ], client)
    else:
        summary["credentials"] = 0

    # Tickets: 3 accounts[40..42], 5 tickets each
    summary["tickets"] = await _submit_batch("tickets", [
        ("TicketCreate", TicketCreate(
            account=accs[40 + i].address,
            ticket_count=_TICKET_COUNT,
        ), accs[40 + i].wallet)
        for i in range(min(3, max(0, len(accs) - 40)))
    ], client)
    summary["tickets"] *= _TICKET_COUNT  # each TicketCreate makes 5 tickets

    # Permissioned domains: 3 from accounts[50..52]
    if len(accs) > 52:
        cred_issuer = accs[30].address if len(accs) > 30 else accs[50].address
        summary["domains"] = await _submit_batch("domains", [
            ("PermissionedDomainSet", PermissionedDomainSet(
                account=accs[50 + i].address,
                accepted_credentials=[XRPLCredential(
                    issuer=cred_issuer,
                    credential_type=_SETUP_CREDENTIAL_TYPE,
                )],
            ), accs[50 + i].wallet)
            for i in range(3)
        ], client)
    else:
        summary["domains"] = 0

    # Fire reachable assertions for each type that had successful submissions
    for key, count in summary.items():
        if count:
            reachable(f"workload::setup_{key}", {"count": count})

    log.info("Setup: submitted — %s", summary)

    # Give WS listener time to process validated results before parallel drivers start
    await asyncio.sleep(5)

    log.info("Setup: state after wait — vaults=%d nfts=%d mpt=%d creds=%d domains=%d trust=%d",
             len(workload.vaults), len(workload.nfts), len(workload.mpt_issuances),
             len(workload.credentials), len(workload.domains), len(workload.trust_lines))
    return summary
