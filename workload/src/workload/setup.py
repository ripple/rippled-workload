"""Deterministic setup for the Antithesis first_* phase.

Creates the minimum ledger state needed for all transaction types to operate,
including IOU gateways with token distribution and MPT authorization.

Account allocation:
  [0..4]   — MPT issuers
  [10..14] — Vault creators (also get trust lines + IOU/MPT balances for non-XRP vaults)
  [20..24] — NFT minters
  [30..35] — Credential issuer + subjects
  [40..42] — Ticket holders
  [50..52] — Domain creators
  [60..61] — IOU gateways (DefaultRipple set)
  [62..71] — IOU/MPT holders (trust lines + balances for all currencies)
"""

import asyncio

import xrpl.models
from workload import logging, params
from workload.submit import submit_tx
from antithesis.assertions import reachable
from xrpl.models import IssuedCurrency, IssuedCurrencyAmount
from xrpl.models.amounts import MPTAmount
from xrpl.models.transactions import (
    AccountSet, AccountSetAsfFlag,
    TrustSet, Payment,
    MPTokenIssuanceCreate, MPTokenAuthorize,
    VaultCreate, VaultDeposit,
    NFTokenMint, NFTokenMintFlag, NFTokenCreateOffer, NFTokenCreateOfferFlag,
    CredentialCreate, TicketCreate, PermissionedDomainSet,
    LoanBrokerSet, LoanSet,
)
from xrpl.models.transactions.deposit_preauth import Credential as XRPLCredential

log = logging.getLogger(__name__)

_SETUP_CREDENTIAL_TYPE = "setup".encode("utf-8").hex()
_TRUSTLINE_LIMIT = "1000000000000000"
_VAULT_ASSETS_MAXIMUM = "1000000000"
_TICKET_COUNT = 5
_IOU_DISTRIBUTION_AMOUNT = "10000"  # tokens per currency per holder
_MPT_DISTRIBUTION_AMOUNT = "10000"

# Gateway assignments: (account_index, [currencies])
_GATEWAYS = [
    (60, ["USD", "BTC"]),
    (61, ["EUR", "GBP"]),
]

# Accounts that receive IOU/MPT balances
_HOLDER_RANGE = range(62, 72)  # accounts[62..71]
_VAULT_RANGE = range(10, 15)   # accounts[10..14] also get balances for non-XRP vaults


def _accounts_list(workload) -> list:
    return list(workload.accounts.values())


async def _submit_batch(name: str, txns: list[tuple], client) -> int:
    """Submit a batch of (tx_type_name, txn, wallet) tuples. Returns count of accepted."""
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
    summary = {}

    # All accounts that need trust lines + balances
    holder_indices = list(_HOLDER_RANGE) + list(_VAULT_RANGE)

    # ── 1. Gateway setup: set DefaultRipple ──────────────────────────
    summary["gateways"] = await _submit_batch("gateways", [
        ("AccountSet", AccountSet(
            account=accs[gw_idx].address,
            set_flag=AccountSetAsfFlag.ASF_DEFAULT_RIPPLE,
        ), accs[gw_idx].wallet)
        for gw_idx, _ in _GATEWAYS
        if gw_idx < len(accs)
    ], client)

    # ── 2. Trust lines: holders → gateways for each currency ─────────
    trust_txns = []
    for gw_idx, currencies in _GATEWAYS:
        if gw_idx >= len(accs):
            continue
        gateway = accs[gw_idx]
        for holder_idx in holder_indices:
            if holder_idx >= len(accs):
                continue
            holder = accs[holder_idx]
            for currency in currencies:
                trust_txns.append(("TrustSet", TrustSet(
                    account=holder.address,
                    limit_amount=IssuedCurrencyAmount(
                        currency=currency,
                        issuer=gateway.address,
                        value=_TRUSTLINE_LIMIT,
                    ),
                ), holder.wallet))
    summary["trust_lines"] = await _submit_batch("trust_lines", trust_txns, client)

    # ── 3. IOU distribution: gateways send tokens to holders ─────────
    iou_txns = []
    for gw_idx, currencies in _GATEWAYS:
        if gw_idx >= len(accs):
            continue
        gateway = accs[gw_idx]
        for holder_idx in holder_indices:
            if holder_idx >= len(accs):
                continue
            holder = accs[holder_idx]
            for currency in currencies:
                iou_txns.append(("Payment", Payment(
                    account=gateway.address,
                    destination=holder.address,
                    amount=IssuedCurrencyAmount(
                        currency=currency,
                        issuer=gateway.address,
                        value=_IOU_DISTRIBUTION_AMOUNT,
                    ),
                ), gateway.wallet))
    summary["iou_distribution"] = await _submit_batch("iou_distribution", iou_txns, client)

    # ── 4. MPT issuances: 5 from accounts[0..4] ─────────────────────
    summary["mpt_issuances"] = await _submit_batch("mpt", [
        ("MPTokenIssuanceCreate", MPTokenIssuanceCreate(account=accs[i].address), accs[i].wallet)
        for i in range(min(5, len(accs)))
    ], client)

    # ── 5. MPT authorization: holders authorize for each issuance ────
    # Wait for WS listener to process MPT issuance results so we have IDs
    await asyncio.sleep(5)
    log.info("Setup: MPT issuances in state: %d", len(workload.mpt_issuances))
    mpt_auth_txns = []
    for mpt in workload.mpt_issuances:
        for holder_idx in holder_indices:
            if holder_idx >= len(accs):
                continue
            holder = accs[holder_idx]
            if holder.address == mpt.issuer:
                continue  # issuer can't authorize themselves
            mpt_auth_txns.append(("MPTokenAuthorize", MPTokenAuthorize(
                account=holder.address,
                mptoken_issuance_id=mpt.mpt_issuance_id,
            ), holder.wallet))
    summary["mpt_authorizations"] = await _submit_batch("mpt_auth", mpt_auth_txns, client)

    # ── 6. MPT distribution: issuers send tokens to authorized holders
    mpt_dist_txns = []
    for mpt in workload.mpt_issuances:
        issuer_acc = next((a for a in accs if a.address == mpt.issuer), None)
        if not issuer_acc:
            continue
        for holder_idx in holder_indices:
            if holder_idx >= len(accs):
                continue
            holder = accs[holder_idx]
            if holder.address == mpt.issuer:
                continue
            mpt_dist_txns.append(("Payment", Payment(
                account=mpt.issuer,
                destination=holder.address,
                amount=MPTAmount(
                    mpt_issuance_id=mpt.mpt_issuance_id,
                    value=_MPT_DISTRIBUTION_AMOUNT,
                ),
            ), issuer_acc.wallet))
    summary["mpt_distribution"] = await _submit_batch("mpt_distribution", mpt_dist_txns, client)

    # ── 7. Vaults: mix of XRP and IOU assets ─────────────────────────
    vault_txns = []
    for i in range(min(5, max(0, len(accs) - 10))):
        src = accs[10 + i]
        if i < 3:
            # First 3 vaults: XRP
            vault_txns.append(("VaultCreate", VaultCreate(
                account=src.address,
                asset=xrpl.models.XRP(),
                assets_maximum=_VAULT_ASSETS_MAXIMUM,
            ), src.wallet))
        else:
            # Last 2 vaults: IOU (use first gateway's first currency)
            gw_idx, currencies = _GATEWAYS[0]
            if gw_idx < len(accs):
                vault_txns.append(("VaultCreate", VaultCreate(
                    account=src.address,
                    asset=IssuedCurrency(
                        currency=currencies[0],
                        issuer=accs[gw_idx].address,
                    ),
                    assets_maximum=_VAULT_ASSETS_MAXIMUM,
                ), src.wallet))
    summary["vaults"] = await _submit_batch("vaults", vault_txns, client)

    # ── 7b. Vault deposits: deposit XRP into first 3 vaults (XRP vaults)
    # IOU vaults (indices 3,4) need IOU amounts — skip for now
    await asyncio.sleep(3)  # wait for WS to populate vault state
    deposit_txns = []
    for vault in workload.vaults[:3]:
        if vault.owner not in workload.accounts:
            continue
        owner = workload.accounts[vault.owner]
        deposit_txns.append(("VaultDeposit", VaultDeposit(
            account=owner.address,
            vault_id=vault.vault_id,
            amount=params.vault_deposit_amount(),
        ), owner.wallet))
    summary["vault_deposits"] = await _submit_batch("vault_deposits", deposit_txns, client)

    # ── 8. NFTs: 5 from accounts[20..24] ─────────────────────────────
    summary["nfts"] = await _submit_batch("nfts", [
        ("NFTokenMint", NFTokenMint(
            account=accs[20 + i].address,
            nftoken_taxon=0,
            flags=NFTokenMintFlag.TF_TRANSFERABLE,
        ), accs[20 + i].wallet)
        for i in range(min(5, max(0, len(accs) - 20)))
    ], client)

    # ── 8b. NFT offers: create sell offers so cancel/accept have state ─
    await asyncio.sleep(3)  # wait for WS to populate NFT state
    nft_offer_txns = []
    for nft in workload.nfts:
        if nft.owner not in workload.accounts:
            continue
        owner = workload.accounts[nft.owner]
        nft_offer_txns.append(("NFTokenCreateOffer", NFTokenCreateOffer(
            account=owner.address,
            nftoken_id=nft.nftoken_id,
            amount=params.nft_offer_amount(),
            flags=NFTokenCreateOfferFlag.TF_SELL_NFTOKEN,
        ), owner.wallet))
    summary["nft_offers"] = await _submit_batch("nft_offers", nft_offer_txns, client)

    # ── 9. Credentials: 5, issuer=accounts[30], subjects=accounts[31..35]
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

    # ── 10. Tickets: 3 accounts[40..42], 5 tickets each ──────────────
    summary["tickets"] = await _submit_batch("tickets", [
        ("TicketCreate", TicketCreate(
            account=accs[40 + i].address,
            ticket_count=_TICKET_COUNT,
        ), accs[40 + i].wallet)
        for i in range(min(3, max(0, len(accs) - 40)))
    ], client)
    summary["tickets"] *= _TICKET_COUNT

    # ── 11. Permissioned domains: 3 from accounts[50..52] ────────────
    if len(accs) > 52:
        cred_issuer = accs[30].address
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

    # ── 12. Loan brokers: create on existing vaults ─────────────────
    await asyncio.sleep(3)  # ensure vaults are in state
    broker_txns = []
    for vault in workload.vaults[:3]:  # use first 3 vaults
        if vault.owner not in workload.accounts:
            continue
        owner = workload.accounts[vault.owner]
        broker_txns.append(("LoanBrokerSet", LoanBrokerSet(
            account=owner.address,
            vault_id=vault.vault_id,
            management_fee_rate=params.loan_broker_management_fee_rate(),
            cover_rate_minimum=params.loan_broker_cover_rate_minimum(),
            cover_rate_liquidation=params.loan_broker_cover_rate_liquidation(),
        ), owner.wallet))
    summary["loan_brokers"] = await _submit_batch("loan_brokers", broker_txns, client)

    # ── 13. Loans: TODO — LoanSet requires counterparty_signature (temBAD_SIGNER)
    # Need to investigate two-party signing for lending protocol
    summary["loans"] = 0

    # Fire reachable assertions
    for key, count in summary.items():
        if count:
            reachable(f"workload::setup_{key}", {"count": count})

    log.info("Setup: submitted — %s", summary)

    # Give WS listener time to process all validated results
    await asyncio.sleep(5)

    log.info("Setup: state after wait — vaults=%d nfts=%d mpt=%d creds=%d "
             "domains=%d trust=%d loans=%d brokers=%d",
             len(workload.vaults), len(workload.nfts), len(workload.mpt_issuances),
             len(workload.credentials), len(workload.domains), len(workload.trust_lines),
             len(workload.loans), len(workload.loan_brokers))
    return summary
