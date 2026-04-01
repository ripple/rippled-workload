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

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from workload.app import Workload

import xrpl.models
from antithesis.assertions import reachable
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models import IssuedCurrency, IssuedCurrencyAmount
from xrpl.models.amounts import MPTAmount
from xrpl.models.transactions import (
    AccountSet,
    AccountSetAsfFlag,
    CredentialCreate,
    LoanBrokerCoverDeposit,
    LoanBrokerSet,
    LoanSet,
    MPTokenAuthorize,
    MPTokenIssuanceCreate,
    MPTokenIssuanceCreateFlag,
    NFTokenCreateOffer,
    NFTokenCreateOfferFlag,
    NFTokenMint,
    NFTokenMintFlag,
    Payment,
    PermissionedDomainSet,
    TicketCreate,
    TrustSet,
    VaultCreate,
    VaultDeposit,
)
from xrpl.models.transactions.deposit_preauth import Credential as XRPLCredential
from xrpl.wallet import Wallet

from workload import logging, params
from workload.models import UserAccount
from workload.submit import submit_tx

log = logging.getLogger(__name__)

_SETUP_CREDENTIAL_TYPE = b"setup".hex()
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
_VAULT_RANGE = range(10, 15)  # accounts[10..14] also get balances for non-XRP vaults


def _accounts_list(workload: Workload) -> list[UserAccount]:
    return list(workload.accounts.values())


async def _submit_batch(
    name: str, txns: list[tuple[str, object, Wallet]], client: AsyncJsonRpcClient
) -> int:
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


async def run_setup(workload: Workload) -> dict[str, int]:
    """Submit deterministic setup transactions. State tracking via WS listener."""
    log.info("Setup: starting (%d accounts available)", len(workload.accounts))
    accs = _accounts_list(workload)
    client = workload.client
    summary = {}

    # All accounts that need trust lines + balances
    holder_indices = list(_HOLDER_RANGE) + list(_VAULT_RANGE)

    # ── 1. Gateway setup: set DefaultRipple ──────────────────────────
    summary["gateways"] = await _submit_batch(
        "gateways",
        [
            (
                "AccountSet",
                AccountSet(
                    account=accs[gw_idx].address,
                    set_flag=AccountSetAsfFlag.ASF_DEFAULT_RIPPLE,
                ),
                accs[gw_idx].wallet,
            )
            for gw_idx, _ in _GATEWAYS
            if gw_idx < len(accs)
        ],
        client,
    )

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
                trust_txns.append(
                    (
                        "TrustSet",
                        TrustSet(
                            account=holder.address,
                            limit_amount=IssuedCurrencyAmount(
                                currency=currency,
                                issuer=gateway.address,
                                value=_TRUSTLINE_LIMIT,
                            ),
                        ),
                        holder.wallet,
                    )
                )
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
                iou_txns.append(
                    (
                        "Payment",
                        Payment(
                            account=gateway.address,
                            destination=holder.address,
                            amount=IssuedCurrencyAmount(
                                currency=currency,
                                issuer=gateway.address,
                                value=_IOU_DISTRIBUTION_AMOUNT,
                            ),
                        ),
                        gateway.wallet,
                    )
                )
    summary["iou_distribution"] = await _submit_batch("iou_distribution", iou_txns, client)

    # ── 4. MPT issuances: 5 from accounts[0..4] ─────────────────────
    summary["mpt_issuances"] = await _submit_batch(
        "mpt",
        [
            (
                "MPTokenIssuanceCreate",
                MPTokenIssuanceCreate(
                    account=accs[i].address,
                    flags=MPTokenIssuanceCreateFlag.TF_MPT_CAN_LOCK,
                ),
                accs[i].wallet,
            )
            for i in range(min(5, len(accs)))
        ],
        client,
    )

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
            mpt_auth_txns.append(
                (
                    "MPTokenAuthorize",
                    MPTokenAuthorize(
                        account=holder.address,
                        mptoken_issuance_id=mpt.mpt_issuance_id,
                    ),
                    holder.wallet,
                )
            )
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
            mpt_dist_txns.append(
                (
                    "Payment",
                    Payment(
                        account=mpt.issuer,
                        destination=holder.address,
                        amount=MPTAmount(
                            mpt_issuance_id=mpt.mpt_issuance_id,
                            value=_MPT_DISTRIBUTION_AMOUNT,
                        ),
                    ),
                    issuer_acc.wallet,
                )
            )
    summary["mpt_distribution"] = await _submit_batch("mpt_distribution", mpt_dist_txns, client)

    # ── 7. Vaults: mix of XRP and IOU assets ─────────────────────────
    vault_txns = []
    for i in range(min(5, max(0, len(accs) - 10))):
        src = accs[10 + i]
        if i < 3:
            # First 3 vaults: XRP
            vault_txns.append(
                (
                    "VaultCreate",
                    VaultCreate(
                        account=src.address,
                        asset=xrpl.models.XRP(),
                        assets_maximum=_VAULT_ASSETS_MAXIMUM,
                    ),
                    src.wallet,
                )
            )
        else:
            # Last 2 vaults: IOU (use first gateway's first currency)
            gw_idx, currencies = _GATEWAYS[0]
            if gw_idx < len(accs):
                vault_txns.append(
                    (
                        "VaultCreate",
                        VaultCreate(
                            account=src.address,
                            asset=IssuedCurrency(
                                currency=currencies[0],
                                issuer=accs[gw_idx].address,
                            ),
                            assets_maximum=_VAULT_ASSETS_MAXIMUM,
                        ),
                        src.wallet,
                    )
                )
    summary["vaults"] = await _submit_batch("vaults", vault_txns, client)

    # ── 7b. Vault deposits: deposit XRP into first 3 vaults (XRP vaults)
    # IOU vaults (indices 3,4) need IOU amounts — skip for now
    await asyncio.sleep(3)  # wait for WS to populate vault state
    deposit_txns = []
    for vault in workload.vaults[:3]:
        if vault.owner not in workload.accounts:
            continue
        owner = workload.accounts[vault.owner]
        deposit_txns.append(
            (
                "VaultDeposit",
                VaultDeposit(
                    account=owner.address,
                    vault_id=vault.vault_id,
                    amount=params.vault_deposit_amount(),
                ),
                owner.wallet,
            )
        )
    summary["vault_deposits"] = await _submit_batch("vault_deposits", deposit_txns, client)

    # ── 8. NFTs: 5 from accounts[20..24] ─────────────────────────────
    summary["nfts"] = await _submit_batch(
        "nfts",
        [
            (
                "NFTokenMint",
                NFTokenMint(
                    account=accs[20 + i].address,
                    nftoken_taxon=0,
                    flags=NFTokenMintFlag.TF_TRANSFERABLE,
                ),
                accs[20 + i].wallet,
            )
            for i in range(min(5, max(0, len(accs) - 20)))
        ],
        client,
    )

    # ── 8b. NFT offers: create sell offers so cancel/accept have state ─
    await asyncio.sleep(3)  # wait for WS to populate NFT state
    nft_offer_txns = []
    for nft in workload.nfts:
        if nft.owner not in workload.accounts:
            continue
        owner = workload.accounts[nft.owner]
        nft_offer_txns.append(
            (
                "NFTokenCreateOffer",
                NFTokenCreateOffer(
                    account=owner.address,
                    nftoken_id=nft.nftoken_id,
                    amount=params.nft_offer_amount(),
                    flags=NFTokenCreateOfferFlag.TF_SELL_NFTOKEN,
                ),
                owner.wallet,
            )
        )
    summary["nft_offers"] = await _submit_batch("nft_offers", nft_offer_txns, client)

    # ── 9. Credentials: 5, issuer=accounts[30], subjects=accounts[31..35]
    if len(accs) > 35:
        issuer = accs[30]
        summary["credentials"] = await _submit_batch(
            "credentials",
            [
                (
                    "CredentialCreate",
                    CredentialCreate(
                        account=issuer.address,
                        subject=accs[31 + i].address,
                        credential_type=_SETUP_CREDENTIAL_TYPE,
                    ),
                    issuer.wallet,
                )
                for i in range(5)
            ],
            client,
        )
    else:
        summary["credentials"] = 0

    # ── 10. Tickets: 3 accounts[40..42], 5 tickets each ──────────────
    summary["tickets"] = await _submit_batch(
        "tickets",
        [
            (
                "TicketCreate",
                TicketCreate(
                    account=accs[40 + i].address,
                    ticket_count=_TICKET_COUNT,
                ),
                accs[40 + i].wallet,
            )
            for i in range(min(3, max(0, len(accs) - 40)))
        ],
        client,
    )
    summary["tickets"] *= _TICKET_COUNT

    # ── 11. Permissioned domains: 3 from accounts[50..52] ────────────
    if len(accs) > 52:
        cred_issuer = accs[30].address
        summary["domains"] = await _submit_batch(
            "domains",
            [
                (
                    "PermissionedDomainSet",
                    PermissionedDomainSet(
                        account=accs[50 + i].address,
                        accepted_credentials=[
                            XRPLCredential(
                                issuer=cred_issuer,
                                credential_type=_SETUP_CREDENTIAL_TYPE,
                            )
                        ],
                    ),
                    accs[50 + i].wallet,
                )
                for i in range(3)
            ],
            client,
        )
    else:
        summary["domains"] = 0

    # ── 12. Loan brokers: create on existing vaults ─────────────────
    await asyncio.sleep(3)  # ensure vaults are in state
    broker_txns = []
    for vault in workload.vaults[:3]:  # use first 3 vaults
        if vault.owner not in workload.accounts:
            continue
        owner = workload.accounts[vault.owner]
        broker_txns.append(
            (
                "LoanBrokerSet",
                LoanBrokerSet(
                    account=owner.address,
                    vault_id=vault.vault_id,
                    management_fee_rate=100,  # 0.1% — low fixed fee
                    cover_rate_minimum=1000,  # 1% — minimal collateral requirement
                    cover_rate_liquidation=500,  # 0.5% — below minimum
                ),
                owner.wallet,
            )
        )
    summary["loan_brokers"] = await _submit_batch("loan_brokers", broker_txns, client)

    # ── 12b. Broker cover deposits: fund first-loss capital so loans can be created
    await asyncio.sleep(3)  # wait for WS to populate broker state
    _COVER_DEPOSIT = "10000000"  # 10 XRP — enough to cover several small loans
    cover_txns = []
    for broker in workload.loan_brokers[:3]:
        if broker.owner not in workload.accounts:
            continue
        owner = workload.accounts[broker.owner]
        cover_txns.append(
            (
                "LoanBrokerCoverDeposit",
                LoanBrokerCoverDeposit(
                    account=owner.address,
                    loan_broker_id=broker.loan_broker_id,
                    amount=_COVER_DEPOSIT,
                ),
                owner.wallet,
            )
        )
    summary["cover_deposits"] = await _submit_batch("cover_deposits", cover_txns, client)

    # ── 13. Loans: create against brokers (requires counterparty co-signing)
    # Use small fixed principal and low cover rates so borrowers can afford collateral.
    from xrpl.asyncio.transaction import autofill_and_sign
    from xrpl.asyncio.transaction import submit as xrpl_submit
    from xrpl.transaction.counterparty_signer import sign_loan_set_by_counterparty

    await asyncio.sleep(3)  # wait for WS to populate broker state
    _SETUP_PRINCIPAL = "1000000"  # 1 XRP — small enough that any account can cover
    _SETUP_INTEREST = 1000  # 1% annualized
    _SETUP_INTERVAL = 3600  # 1 hour
    _SETUP_TOTAL = 3  # 3 payments
    _SETUP_GRACE = 600  # 10 minutes
    loan_count = 0
    # Use holder accounts as borrowers — they have plenty of XRP
    borrower_indices = list(_HOLDER_RANGE)
    for idx, broker in enumerate(workload.loan_brokers[:3]):
        if broker.owner not in workload.accounts:
            continue
        broker_wallet = workload.accounts[broker.owner].wallet
        # Pick a borrower that isn't the broker owner
        b_idx = borrower_indices[idx] if idx < len(borrower_indices) else None
        if b_idx is None or b_idx >= len(accs) or accs[b_idx].address == broker.owner:
            continue
        borrower = accs[b_idx]
        try:
            txn = LoanSet(
                account=borrower.address,
                loan_broker_id=broker.loan_broker_id,
                counterparty=broker.owner,
                principal_requested=_SETUP_PRINCIPAL,
                interest_rate=_SETUP_INTEREST,
                payment_interval=_SETUP_INTERVAL,
                payment_total=_SETUP_TOTAL,
                grace_period=_SETUP_GRACE,
            )
            # 1. Borrower signs
            signed = await autofill_and_sign(txn, client, borrower.wallet)
            # 2. Broker co-signs
            result = sign_loan_set_by_counterparty(broker_wallet, signed)
            # 3. Submit the doubly-signed tx
            resp = await xrpl_submit(result.tx, client)
            engine = resp.result.get("engine_result", "")
            if engine in ("tesSUCCESS", "terQUEUED", "terPRE_SEQ"):
                loan_count += 1
                log.info(
                    "Setup: loan submitted (broker=%s borrower=%s)", broker.owner, borrower.address
                )
            else:
                log.warning(
                    "Setup: loan failed: %s (%s)",
                    engine,
                    resp.result.get("engine_result_message", ""),
                )
        except Exception as exc:
            log.error("Setup: loan creation failed: %s", exc)
    summary["loans"] = loan_count

    # Fire reachable assertions
    for key, count in summary.items():
        if count:
            reachable(f"workload::setup_{key}", {"count": count})

    log.info("Setup: submitted — %s", summary)

    # Give WS listener time to process all validated results
    await asyncio.sleep(5)

    log.info(
        "Setup: state — v=%d nft=%d mpt=%d cred=%d dom=%d tl=%d loan=%d broker=%d",
        len(workload.vaults),
        len(workload.nfts),
        len(workload.mpt_issuances),
        len(workload.credentials),
        len(workload.domains),
        len(workload.trust_lines),
        len(workload.loans),
        len(workload.loan_brokers),
    )
    return summary
