"""Deterministic setup for the Antithesis first_* phase.

Creates the minimum ledger state needed for all transaction types to operate,
including IOU gateways with token distribution and MPT authorization.

Account allocation:
  [0..4]   — MPT issuers
  [10..16] — Vault creators (also get trust lines + IOU/MPT balances for non-XRP vaults)
  [20..24] — NFT minters
  [30..35] — Credential issuer + subjects
  [40..42] — Ticket holders
  [50..52] — Domain creators
  [60..61] — IOU gateways (DefaultRipple + AllowTrustLineClawback set)
  [62..71] — IOU/MPT holders (trust lines + balances for all currencies)
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from workload.app import Workload

import xrpl.models
from antithesis.assertions import reachable
from antithesis.lifecycle import send_event
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.asyncio.transaction import autofill_and_sign
from xrpl.asyncio.transaction import submit as xrpl_submit
from xrpl.models import IssuedCurrency, IssuedCurrencyAmount
from xrpl.models.amounts import MPTAmount
from xrpl.models.currencies import MPTCurrency
from xrpl.models.transactions import (
    AccountSet,
    AccountSetAsfFlag,
    CredentialCreate,
    LoanBrokerCoverDeposit,
    LoanBrokerSet,
    LoanPay,
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
from xrpl.transaction.counterparty_signer import sign_loan_set_by_counterparty
from xrpl.wallet import Wallet

from workload import params
from workload.models import UserAccount
from workload.sequence import SequenceTracker
from workload.submit import submit_tx

# ── Constants ───────────────────────────────────────────────────────────
_SETUP_CREDENTIAL_TYPE = b"setup".hex()
_TRUSTLINE_LIMIT = "1000000000000000"
_VAULT_ASSETS_MAXIMUM = "1000000000"
_TICKET_COUNT = 5
_IOU_DISTRIBUTION_AMOUNT = "10000"
_MPT_DISTRIBUTION_AMOUNT = "10000"
_COVER_DEPOSIT = "10000000"  # 10 XRP
_LOAN_PRINCIPAL = "1000000"  # 1 XRP
_LOAN_INTEREST = 1000  # 1% annualized
_LOAN_INTERVAL = 3600  # 1 hour
_LOAN_TOTAL = 3  # 3 payments
_LOAN_GRACE = 600  # 10 minutes

# Gateway assignments: (account_index, [currencies])
_GATEWAYS = [
    (60, ["USD", "BTC"]),
    (61, ["EUR", "GBP"]),
]

# Accounts that receive IOU/MPT balances
_HOLDER_RANGE = range(62, 72)  # accounts[62..71]
_VAULT_RANGE = range(10, 17)  # accounts[10..16]


def _accounts_list(workload: Workload) -> list[UserAccount]:
    return list(workload.accounts.values())


async def _submit_batch(
    name: str,
    txns: list[tuple[str, object, Wallet]],
    client: AsyncJsonRpcClient,
    seq: SequenceTracker,
) -> int:
    """Submit a batch with local sequence tracking."""
    if not txns:
        return 0
    created = 0
    for tx_type, txn, wallet in txns:
        try:
            seq_num = await seq.next_seq(wallet.address)
            result = await submit_tx(tx_type, txn, client, wallet, seq=seq_num)
            engine = result.get("engine_result", "")
            if engine in ("tesSUCCESS", "terQUEUED", "terPRE_SEQ"):
                created += 1
            else:
                send_event(f"workload::setup_reject : {name}", {
                    "phase": name,
                    "tx_type": tx_type,
                    "account": wallet.address,
                    "sequence": seq_num,
                    "engine_result": engine,
                })
        except Exception as e:
            send_event(f"workload::setup_error : {name}", {
                "phase": name,
                "tx_type": tx_type,
                "account": wallet.address,
                "error": f"{type(e).__name__}: {e}",
            })
    return created


async def _submit_loan(
    workload: Workload,
    broker_wallet: Wallet,
    broker_owner: str,
    broker_id: str,
    borrower: UserAccount,
    interest_rate: int = _LOAN_INTEREST,
    payment_total: int = _LOAN_TOTAL,
) -> bool:
    """Create a co-signed LoanSet. Returns True on success."""
    txn = LoanSet(
        account=borrower.address,
        loan_broker_id=broker_id,
        counterparty=broker_owner,
        principal_requested=_LOAN_PRINCIPAL,
        interest_rate=interest_rate,
        payment_interval=_LOAN_INTERVAL,
        payment_total=payment_total,
        grace_period=_LOAN_GRACE,
        sequence=await workload.seq.next_seq(borrower.address),
    )
    signed = await autofill_and_sign(txn, workload.client, borrower.wallet)
    cosigned = sign_loan_set_by_counterparty(broker_wallet, signed)
    resp = await xrpl_submit(cosigned.tx, workload.client)
    engine = resp.result.get("engine_result", "")
    ok = engine in ("tesSUCCESS", "terQUEUED", "terPRE_SEQ")
    if not ok:
        send_event("workload::setup_reject : loan", {
            "phase": "loan",
            "tx_type": "LoanSet",
            "broker": broker_owner,
            "borrower": borrower.address,
            "broker_id": broker_id,
            "engine_result": engine,
        })
    return ok


async def run_setup(workload: Workload) -> dict[str, int]:
    """Submit deterministic setup transactions. State tracking via WS listener."""
    accs = _accounts_list(workload)
    client = workload.client
    seq = workload.seq
    summary: dict[str, int] = {}

    holder_indices = list(_HOLDER_RANGE) + list(_VAULT_RANGE)

    # ── 1. Gateway setup: DefaultRipple + AllowTrustLineClawback ─────
    gw_txns = []
    for gw_idx, _ in _GATEWAYS:
        if gw_idx >= len(accs):
            continue
        gw = accs[gw_idx]
        gw_txns.append(
            (
                "AccountSet",
                AccountSet(account=gw.address, set_flag=AccountSetAsfFlag.ASF_DEFAULT_RIPPLE),
                gw.wallet,
            )
        )
        gw_txns.append(
            (
                "AccountSet",
                AccountSet(
                    account=gw.address, set_flag=AccountSetAsfFlag.ASF_ALLOW_TRUSTLINE_CLAWBACK
                ),
                gw.wallet,
            )
        )
    summary["gateways"] = await _submit_batch("gateways", gw_txns, client, seq)

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
                                currency=currency, issuer=gateway.address, value=_TRUSTLINE_LIMIT
                            ),
                        ),
                        holder.wallet,
                    )
                )
    summary["trust_lines"] = await _submit_batch("trust_lines", trust_txns, client, seq)

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
    summary["iou_distribution"] = await _submit_batch("iou_distribution", iou_txns, client, seq)

    # ── 4. MPT issuances: 5 from accounts[0..4] ─────────────────────
    summary["mpt_issuances"] = await _submit_batch(
        "mpt",
        [
            (
                "MPTokenIssuanceCreate",
                MPTokenIssuanceCreate(
                    account=accs[i].address, flags=MPTokenIssuanceCreateFlag.TF_MPT_CAN_LOCK
                ),
                accs[i].wallet,
            )
            for i in range(min(5, len(accs)))
        ],
        client,
        seq,
    )

    # ── 5. MPT authorization: holders authorize for each issuance ────
    await asyncio.sleep(5)  # wait for WS to process MPT issuance results
    mpt_auth_txns = []
    for mpt in workload.mpt_issuances:
        for holder_idx in holder_indices:
            if holder_idx >= len(accs):
                continue
            holder = accs[holder_idx]
            if holder.address == mpt.issuer:
                continue
            mpt_auth_txns.append(
                (
                    "MPTokenAuthorize",
                    MPTokenAuthorize(
                        account=holder.address, mptoken_issuance_id=mpt.mpt_issuance_id
                    ),
                    holder.wallet,
                )
            )
    summary["mpt_authorizations"] = await _submit_batch("mpt_auth", mpt_auth_txns, client, seq)

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
                            mpt_issuance_id=mpt.mpt_issuance_id, value=_MPT_DISTRIBUTION_AMOUNT
                        ),
                    ),
                    issuer_acc.wallet,
                )
            )
    summary["mpt_distribution"] = await _submit_batch(
        "mpt_distribution", mpt_dist_txns, client, seq
    )

    # ── 7. Vaults: mix of XRP, IOU, and MPT assets ───────────────────
    vault_txns = []
    for i in range(min(7, max(0, len(accs) - 10))):
        src = accs[10 + i]
        if i < 3:
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
        elif i < 5:
            gw_idx, currencies = _GATEWAYS[0]
            if gw_idx < len(accs):
                vault_txns.append(
                    (
                        "VaultCreate",
                        VaultCreate(
                            account=src.address,
                            asset=IssuedCurrency(
                                currency=currencies[0], issuer=accs[gw_idx].address
                            ),
                            assets_maximum=_VAULT_ASSETS_MAXIMUM,
                        ),
                        src.wallet,
                    )
                )
        else:
            if workload.mpt_issuances:
                mpt_idx = min(i - 5, len(workload.mpt_issuances) - 1)
                vault_txns.append(
                    (
                        "VaultCreate",
                        VaultCreate(
                            account=src.address,
                            asset=MPTCurrency(
                                mpt_issuance_id=workload.mpt_issuances[mpt_idx].mpt_issuance_id
                            ),
                            assets_maximum=_VAULT_ASSETS_MAXIMUM,
                        ),
                        src.wallet,
                    )
                )
    summary["vaults"] = await _submit_batch("vaults", vault_txns, client, seq)

    # ── 7b. Vault deposits: owners deposit into their vaults ─────────
    await asyncio.sleep(3)
    deposit_txns = []
    for vault in workload.vaults:
        if vault.owner not in workload.accounts:
            continue
        owner = workload.accounts[vault.owner]
        if isinstance(vault.asset, IssuedCurrency):
            amount = IssuedCurrencyAmount(
                currency=vault.asset.currency,
                issuer=vault.asset.issuer,
                value=params.iou_amount(),
            )
        elif isinstance(vault.asset, MPTCurrency):
            amount = MPTAmount(
                mpt_issuance_id=vault.asset.mpt_issuance_id, value=params.mpt_amount()
            )
        else:
            amount = params.vault_deposit_amount()
        deposit_txns.append(
            (
                "VaultDeposit",
                VaultDeposit(account=owner.address, vault_id=vault.vault_id, amount=amount),
                owner.wallet,
            )
        )
    summary["vault_deposits"] = await _submit_batch("vault_deposits", deposit_txns, client, seq)

    # ── 7c. Non-owner deposits into IOU vaults (for clawback) ────────
    iou_vaults = [v for v in workload.vaults if isinstance(v.asset, IssuedCurrency)]
    holder_deposit_txns = []
    for vault in iou_vaults:
        for holder_idx in list(_HOLDER_RANGE)[:3]:
            if holder_idx >= len(accs):
                continue
            holder = accs[holder_idx]
            holder_deposit_txns.append(
                (
                    "VaultDeposit",
                    VaultDeposit(
                        account=holder.address,
                        vault_id=vault.vault_id,
                        amount=IssuedCurrencyAmount(
                            currency=vault.asset.currency, issuer=vault.asset.issuer, value="1000"
                        ),
                    ),
                    holder.wallet,
                )
            )
    summary["holder_vault_deposits"] = await _submit_batch(
        "holder_vault_deposits", holder_deposit_txns, client, seq
    )

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
        seq,
    )

    # ── 8b. NFT offers ───────────────────────────────────────────────
    await asyncio.sleep(3)
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
    summary["nft_offers"] = await _submit_batch("nft_offers", nft_offer_txns, client, seq)

    # ── 9. Credentials ───────────────────────────────────────────────
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
            seq,
        )
    else:
        summary["credentials"] = 0

    # ── 10. Tickets ──────────────────────────────────────────────────
    summary["tickets"] = await _submit_batch(
        "tickets",
        [
            (
                "TicketCreate",
                TicketCreate(account=accs[40 + i].address, ticket_count=_TICKET_COUNT),
                accs[40 + i].wallet,
            )
            for i in range(min(3, max(0, len(accs) - 40)))
        ],
        client,
        seq,
    )
    summary["tickets"] *= _TICKET_COUNT

    # ── 11. Permissioned domains ─────────────────────────────────────
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
                                issuer=cred_issuer, credential_type=_SETUP_CREDENTIAL_TYPE
                            )
                        ],
                    ),
                    accs[50 + i].wallet,
                )
                for i in range(3)
            ],
            client,
            seq,
        )
    else:
        summary["domains"] = 0

    # ── 12. Loan brokers (4th has no loans — for cover withdraw) ─────
    await asyncio.sleep(3)
    broker_txns = []
    for vault in workload.vaults[:4]:
        if vault.owner not in workload.accounts:
            continue
        owner = workload.accounts[vault.owner]
        broker_txns.append(
            (
                "LoanBrokerSet",
                LoanBrokerSet(
                    account=owner.address,
                    vault_id=vault.vault_id,
                    management_fee_rate=100,
                    cover_rate_minimum=1000,
                    cover_rate_liquidation=500,
                ),
                owner.wallet,
            )
        )
    summary["loan_brokers"] = await _submit_batch("loan_brokers", broker_txns, client, seq)

    # ── 12b. Broker cover deposits ───────────────────────────────────
    await asyncio.sleep(3)
    cover_txns = []
    for broker in workload.loan_brokers[:4]:
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
    summary["cover_deposits"] = await _submit_batch("cover_deposits", cover_txns, client, seq)

    # ── 13. Loans (co-signed) ────────────────────────────────────────
    await asyncio.sleep(3)
    loan_count = 0
    borrower_indices = list(_HOLDER_RANGE)
    for idx, broker in enumerate(workload.loan_brokers[:3]):
        if broker.owner not in workload.accounts:
            continue
        broker_wallet = workload.accounts[broker.owner].wallet
        b_idx = borrower_indices[idx] if idx < len(borrower_indices) else None
        if b_idx is None or b_idx >= len(accs) or accs[b_idx].address == broker.owner:
            continue
        try:
            if await _submit_loan(
                workload, broker_wallet, broker.owner, broker.loan_broker_id, accs[b_idx]
            ):
                loan_count += 1
        except Exception:
            pass
    summary["loans"] = loan_count

    # ── 13b. Zero-interest loan + payoff (for LoanDelete) ────────────
    await asyncio.sleep(3)
    if workload.loan_brokers:
        broker = workload.loan_brokers[0]
        if broker.owner in workload.accounts:
            b_idx = borrower_indices[3] if len(borrower_indices) > 3 else None
            if b_idx is not None and b_idx < len(accs) and accs[b_idx].address != broker.owner:
                borrower = accs[b_idx]
                broker_wallet = workload.accounts[broker.owner].wallet
                try:
                    ok = await _submit_loan(
                        workload,
                        broker_wallet,
                        broker.owner,
                        broker.loan_broker_id,
                        borrower,
                        interest_rate=0,
                        payment_total=1,
                    )
                    if ok:
                        await asyncio.sleep(3)
                        if workload.loans:
                            zero_loan = workload.loans[-1]
                            summary["loan_payoff"] = await _submit_batch(
                                "loan_payoff",
                                [
                                    (
                                        "LoanPay",
                                        LoanPay(
                                            account=borrower.address,
                                            loan_id=zero_loan.loan_id,
                                            amount=_LOAN_PRINCIPAL,
                                        ),
                                        borrower.wallet,
                                    )
                                ],
                                client,
                                seq,
                            )
                except Exception:
                    pass

    # ── Done ─────────────────────────────────────────────────────────
    for key, count in summary.items():
        if count:
            reachable(f"workload::setup_{key}", {"count": count})

    await asyncio.sleep(5)  # let WS listener process validated results

    return summary
