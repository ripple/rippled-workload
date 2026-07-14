"""Deterministic ledger-state setup for the Antithesis first_* phase."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from workload.app import Workload

    # (txn, workload) -> True once the submission's ledger object is tracked.
    _ExistsFn = Callable[[Any, Workload], bool]

import xrpl.models
from antithesis.assertions import reachable, unreachable
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
    AMMCreate,
    CheckCreate,
    CredentialAccept,
    CredentialCreate,
    DelegateSet,
    LoanBrokerCoverDeposit,
    LoanBrokerSet,
    LoanPay,
    LoanSet,
    MPTokenAuthorize,
    MPTokenIssuanceCreate,
    MPTokenIssuanceCreateFlag,
    MPTokenIssuanceSet,
    MPTokenIssuanceSetFlag,
    NFTokenCreateOffer,
    NFTokenCreateOfferFlag,
    NFTokenMint,
    NFTokenMintFlag,
    Payment,
    PermissionedDomainSet,
    SponsorshipSet,
    TicketCreate,
    Transaction,
    TrustSet,
    VaultCreate,
    VaultDeposit,
)
from xrpl.models.transactions.delegate_set import Permission
from xrpl.models.transactions.deposit_preauth import Credential as XRPLCredential
from xrpl.models.transactions.types import TransactionType
from xrpl.transaction.counterparty_signer import sign_loan_set_by_counterparty
from xrpl.wallet import Wallet

import workload.confidential_crypto as cc
from workload import params
from workload.assertions import assert_no_internal_error_submit
from workload.models import ConfidentialHolder, ConfidentialMPTIssuance, UserAccount
from workload.sequence import SequenceTracker
from workload.submit import submit_tx

# ── Constants ───────────────────────────────────────────────────────────
_SETUP_CREDENTIAL_TYPE = b"setup".hex()
_TRUSTLINE_LIMIT = "1000000000000000"
_VAULT_ASSETS_MAXIMUM = "1000000000"
_TICKET_COUNT = 5
_IOU_DISTRIBUTION_AMOUNT = "10000"
_MPT_DISTRIBUTION_AMOUNT = "10000"
_AMM_XRP_AMOUNT = "100000000"  # 100 XRP in drops
_AMM_IOU_AMOUNT = "5000"  # IOU tokens per side
_AMM_TRADING_FEE = 500  # 0.5%
_COVER_DEPOSIT = "10000000"  # 10 XRP
_LOAN_PRINCIPAL = "1000000"  # 1 XRP
_LOAN_INTEREST = 1000  # 1% annualized
_LOAN_INTERVAL = 3600  # 1 hour
_LOAN_TOTAL = 3  # 3 payments
_LOAN_GRACE = 600  # 10 minutes

# Verify-and-retry bound: each phase re-submits its shortfall and re-polls
# tracked state for up to _RETRY_ROUNDS rounds of _SETTLE_TIMEOUT each (~60s).
_RETRY_ROUNDS = 5
_SETTLE_TIMEOUT = 12.0
_SETTLE_POLL = 1.5
_ACCEPTED = ("tesSUCCESS", "terQUEUED", "terPRE_SEQ")

# Gateway assignments: (account_index, [currencies])
_GATEWAYS = [
    (60, ["USD", "BTC"]),
    (61, ["EUR", "GBP"]),
]

# Accounts that receive IOU/MPT balances
_HOLDER_RANGE = range(62, 72)  # accounts[62..71]
_VAULT_RANGE = range(10, 17)  # accounts[10..16]

# Confidential MPT (XLS-0096): issuers + holders on indices unused elsewhere.
_CONF_ISSUER_RANGE = range(7, 9)  # accounts[7..8]
_CONF_HOLDER_RANGE = range(72, 77)  # accounts[72..76]
_CONF_MPT_DISTRIBUTION_AMOUNT = "100000"
_CONF_MPT_CONVERT_AMOUNT = 5000


def _accounts_list(workload: Workload) -> list[UserAccount]:
    return list(workload.accounts.values())


@dataclass
class _Submission:
    tx_type: str
    txn: Transaction
    wallet: Wallet
    accepted: bool = False  # last submit returned an _ACCEPTED engine_result


async def _submit_round(
    name: str,
    subs: list[_Submission],
    client: AsyncJsonRpcClient,
    seq: SequenceTracker,
) -> None:
    """Submit each pending submission once; record acceptance and emit
    setup_reject/setup_error for observability. Never raises."""
    for s in subs:
        s.accepted = False
        try:
            seq_num = await seq.next_seq(s.wallet.address)
            result = await submit_tx(s.tx_type, s.txn, client, s.wallet, seq=seq_num)
            engine = result.get("engine_result", "")
            s.accepted = engine in _ACCEPTED
            if not s.accepted:
                send_event(
                    f"workload::setup_reject : {name}",
                    {
                        "phase": name,
                        "tx_type": s.tx_type,
                        "account": s.wallet.address,
                        "sequence": seq_num,
                        "engine_result": engine,
                    },
                )
        except Exception as e:
            send_event(
                f"workload::setup_error : {name}",
                {
                    "phase": name,
                    "tx_type": s.tx_type,
                    "account": s.wallet.address,
                    "error": f"{type(e).__name__}: {e}",
                },
            )


async def _run_phase(
    workload: Workload,
    name: str,
    txns: list[tuple[str, Transaction, Wallet]],
    exists: _ExistsFn | None = None,
) -> int:
    """Submit ``txns``, then verify + retry the shortfall until every submission
    is confirmed or the bound (~_RETRY_ROUNDS / ~60s) is hit — then fail loud.

    ``exists`` confirms a submission's ledger object is tracked (async WS state,
    authoritative); ``None`` falls back to submit-time acceptance for pure
    side-effect phases with no object. Between rounds each retried account's
    sequence is re-fetched from the (now settled) ledger so re-submits don't
    collide (tefPAST_SEQ). Fail loud aborts the run rather than proceed on
    partial state."""
    subs = [_Submission(t, txn, w) for t, txn, w in txns]
    if not subs:
        return 0
    client, seq = workload.client, workload.seq
    loop = asyncio.get_event_loop()

    def satisfied(s: _Submission) -> bool:
        return exists(s.txn, workload) if exists is not None else s.accepted

    for round_i in range(_RETRY_ROUNDS):
        pending = [s for s in subs if not satisfied(s)]
        if not pending:
            break
        if round_i > 0:
            for addr in {s.wallet.address for s in pending}:
                seq.reset(addr)
        await _submit_round(name, pending, client, seq)
        deadline = loop.time() + _SETTLE_TIMEOUT
        while loop.time() < deadline and any(not satisfied(s) for s in subs):
            await asyncio.sleep(_SETTLE_POLL)

    got = sum(1 for s in subs if satisfied(s))
    if got < len(subs):
        unreachable(
            "workload::setup_incomplete",
            {"phase": name, "expected": len(subs), "got": got},
        )
        raise RuntimeError(f"setup phase {name} incomplete: {got}/{len(subs)}")
    return got


async def _submit_batch(
    name: str,
    txns: list[tuple[str, Transaction, Wallet]],
    client: AsyncJsonRpcClient,
    seq: SequenceTracker,
) -> int:
    """Best-effort submit (no retry, no fail-loud): for graceful-degradation
    phases (Confidential MPT, designed-partial MPT cohorts) where partial state
    is acceptable. Returns the count accepted into a ledger."""
    subs = [_Submission(t, txn, w) for t, txn, w in txns]
    await _submit_round(name, subs, client, seq)
    return sum(1 for s in subs if s.accepted)


async def _poll(predicate: Callable[[], bool], timeout: float = _SETTLE_TIMEOUT) -> bool:
    """Best-effort wait for ``predicate`` (no fail-loud); returns its final value."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(_SETTLE_POLL)
    return predicate()


# ── Phase object-existence predicates (used by _run_phase) ───────────────
def _trust_line_exists(txn: Any, w: Workload) -> bool:
    limit = txn.limit_amount
    pair = {txn.account, limit.issuer}
    return any(
        {tl.account_a, tl.account_b} == pair and tl.currency == limit.currency
        for tl in w.trust_lines
    )


def _mpt_issuance_exists(txn: Any, w: Workload) -> bool:
    return any(m.issuer == txn.account for m in w.mpt_issuances)


def _mpt_authorized(txn: Any, w: Workload) -> bool:
    return any(
        m.mpt_issuance_id == txn.mptoken_issuance_id and txn.account in m.holders
        for m in w.mpt_issuances
    )


def _vault_exists(txn: Any, w: Workload) -> bool:
    return any(v.owner == txn.account for v in w.vaults)


def _nft_exists(txn: Any, w: Workload) -> bool:
    return any(n.owner == txn.account for n in w.nfts)


def _nft_offer_exists(txn: Any, w: Workload) -> bool:
    return any(o.nftoken_id == txn.nftoken_id for o in w.nft_offers)


def _amm_exists(txn: Any, w: Workload) -> bool:
    return any(a.account == txn.account for a in w.amms)


def _credential_exists(txn: Any, w: Workload) -> bool:
    return any(
        c.issuer == txn.account
        and c.subject == txn.subject
        and c.credential_type == txn.credential_type
        for c in w.credentials
    )


def _credential_accepted(txn: Any, w: Workload) -> bool:
    return any(
        c.issuer == txn.issuer
        and c.subject == txn.account
        and c.credential_type == txn.credential_type
        and c.accepted
        for c in w.credentials
    )


def _domain_exists(txn: Any, w: Workload) -> bool:
    return any(d.owner == txn.account for d in w.domains)


def _broker_exists(txn: Any, w: Workload) -> bool:
    return any(b.owner == txn.account and b.vault_id == txn.vault_id for b in w.loan_brokers)


def _cover_deposited(txn: Any, w: Workload) -> bool:
    return any(
        b.loan_broker_id == txn.loan_broker_id and b.cover_balance > 0 for b in w.loan_brokers
    )


def _sponsorship_exists(txn: Any, w: Workload) -> bool:
    return any(s.sponsor == txn.account and s.sponsee == txn.sponsee for s in w.sponsorships)


async def _submit_loan(
    workload: Workload,
    broker_wallet: Wallet,
    broker_owner: str,
    broker_id: str,
    borrower: UserAccount,
    interest_rate: int = _LOAN_INTEREST,
    payment_total: int = _LOAN_TOTAL,
) -> bool:
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
    assert_no_internal_error_submit("LoanSet", resp.result)
    engine = resp.result.get("engine_result", "")
    ok = engine in ("tesSUCCESS", "terQUEUED", "terPRE_SEQ")
    if not ok:
        send_event(
            "workload::setup_reject : loan",
            {
                "phase": "loan",
                "tx_type": "LoanSet",
                "broker": broker_owner,
                "borrower": borrower.address,
                "broker_id": broker_id,
                "engine_result": engine,
            },
        )
    return ok


@dataclass
class _LoanAttempt:
    broker_wallet: Wallet
    broker_owner: str
    broker_id: str
    borrower: UserAccount


async def _run_loans(workload: Workload, attempts: list[_LoanAttempt]) -> int:
    """Co-signed LoanSet analogue of _run_phase: submit, verify against tracked
    loans, retry the shortfall, then fail loud. LoanSet needs a counterparty
    co-sign so it can't ride _submit_round."""
    if not attempts:
        return 0
    loop = asyncio.get_event_loop()

    def landed(a: _LoanAttempt) -> bool:
        return any(
            loan.borrower == a.borrower.address and loan.loan_broker_id == a.broker_id
            for loan in workload.loans
        )

    for round_i in range(_RETRY_ROUNDS):
        pending = [a for a in attempts if not landed(a)]
        if not pending:
            break
        if round_i > 0:
            for a in pending:
                workload.seq.reset(a.borrower.address)
        for a in pending:
            try:
                await _submit_loan(
                    workload, a.broker_wallet, a.broker_owner, a.broker_id, a.borrower
                )
            except Exception as e:
                send_event(
                    "workload::setup_error : loans",
                    {
                        "phase": "loans",
                        "borrower": a.borrower.address,
                        "error": f"{type(e).__name__}: {e}",
                    },
                )
        deadline = loop.time() + _SETTLE_TIMEOUT
        while loop.time() < deadline and any(not landed(a) for a in attempts):
            await asyncio.sleep(_SETTLE_POLL)

    got = sum(1 for a in attempts if landed(a))
    if got < len(attempts):
        unreachable(
            "workload::setup_incomplete",
            {"phase": "loans", "expected": len(attempts), "got": got},
        )
        raise RuntimeError(f"setup phase loans incomplete: {got}/{len(attempts)}")
    return got


async def _probe_node(workload: Workload) -> None:
    # 'full' sync doesn't guarantee submit acceptance; retry until a no-op AccountSet is accepted.
    max_wait = 300
    start = asyncio.get_event_loop().time()
    attempt = 0
    delay = 2.0
    while True:
        attempt += 1
        try:
            probe = AccountSet(account=workload.funding_wallet.address)
            signed = await autofill_and_sign(probe, workload.client, workload.funding_wallet)
            resp = await xrpl_submit(signed, workload.client)
            assert_no_internal_error_submit("AccountSet", resp.result)
            engine = resp.result.get("engine_result", "")
            if engine in ("tesSUCCESS", "terQUEUED", "terPRE_SEQ"):
                send_event(
                    "workload::setup_probe_ok",
                    {
                        "attempt": attempt,
                        "engine_result": engine,
                        "elapsed_s": int(asyncio.get_event_loop().time() - start),
                    },
                )
                return
            send_event(
                "workload::setup_probe_reject",
                {
                    "attempt": attempt,
                    "engine_result": engine,
                },
            )
        except Exception as e:
            send_event(
                "workload::setup_probe_retry",
                {
                    "attempt": attempt,
                    "error": f"{type(e).__name__}: {e}",
                },
            )
        elapsed = asyncio.get_event_loop().time() - start
        if elapsed >= max_wait:
            unreachable(
                "workload::setup_probe_timeout",
                {"attempts": attempt, "elapsed_s": int(elapsed)},
            )
            raise RuntimeError(f"Probe failed after {attempt} attempts ({int(elapsed)}s)")
        await asyncio.sleep(min(delay, max_wait - elapsed))
        delay = min(delay * 1.5, 10)


async def _setup_confidential_mpt(
    workload: Workload,
    accs: list[UserAccount],
    summary: dict[str, int],
) -> None:
    """Seed Confidential MPT state; crypto steps run only when cc.CRYPTO_AVAILABLE."""
    client = workload.client
    seq = workload.seq

    issuer_indices = [i for i in _CONF_ISSUER_RANGE if i < len(accs)]
    holder_indices = [i for i in _CONF_HOLDER_RANGE if i < len(accs)]
    if not issuer_indices or not holder_indices:
        return

    # ── 1. Create privacy-enabled MPT issuances (always) ─────────────
    conf_flags = (
        MPTokenIssuanceCreateFlag.TF_MPT_CAN_HOLD_CONFIDENTIAL_BALANCE
        | MPTokenIssuanceCreateFlag.TF_MPT_CAN_CLAWBACK
        | MPTokenIssuanceCreateFlag.TF_MPT_CAN_TRANSFER
    )
    before = len(workload.mpt_issuances)
    summary["conf_mpt_issuances"] = await _submit_batch(
        "conf_mpt",
        [
            (
                "MPTokenIssuanceCreate",
                MPTokenIssuanceCreate(account=accs[i].address, flags=conf_flags),
                accs[i].wallet,
            )
            for i in issuer_indices
        ],
        client,
        seq,
    )
    # Graceful degradation: conf MPT is allowed partial state (crypto/RPC races),
    # so best-effort poll rather than fail loud.
    await _poll(lambda: len(workload.mpt_issuances) >= before + summary["conf_mpt_issuances"])

    conf_issuer_addrs = {accs[i].address for i in issuer_indices}
    conf_issuances = [m for m in workload.mpt_issuances if m.issuer in conf_issuer_addrs]
    if not conf_issuances:
        return

    # Track issuances even without crypto so handlers can find them.
    issuer_keys: dict[str, tuple[str, str]] = {}
    tracked = {ci.mpt_issuance_id for ci in workload.confidential_mpt_issuances}
    for m in conf_issuances:
        if m.mpt_issuance_id in tracked:
            continue
        workload.confidential_mpt_issuances.append(
            ConfidentialMPTIssuance(
                issuer=m.issuer,
                mpt_issuance_id=m.mpt_issuance_id,
                issuer_privkey="",
                issuer_pubkey="",
            )
        )

    if not cc.CRYPTO_AVAILABLE:
        return

    # ── 2. Issuer ElGamal keys: generate + register on-ledger ────────
    set_txns: list[tuple[str, Transaction, Wallet]] = []
    for m in conf_issuances:
        issuer_acc = workload.accounts.get(m.issuer)
        if not issuer_acc:
            continue
        priv, pub = await cc.generate_keypair()
        issuer_keys[m.issuer] = (priv, pub)
        issuer_acc.elgamal_private_key = priv
        issuer_acc.elgamal_public_key = pub
        set_txns.append(
            (
                "MPTokenIssuanceSet",
                MPTokenIssuanceSet(
                    account=m.issuer,
                    mptoken_issuance_id=m.mpt_issuance_id,
                    issuer_encryption_key=pub,
                ),
                issuer_acc.wallet,
            )
        )
    summary["conf_mpt_issuer_keys"] = await _submit_batch("conf_mpt_keys", set_txns, client, seq)

    # ── 3. Authorize holders + 4. distribute public MPT balances ─────
    auth_txns: list[tuple[str, Transaction, Wallet]] = []
    dist_txns: list[tuple[str, Transaction, Wallet]] = []
    for m in conf_issuances:
        issuer_acc = workload.accounts.get(m.issuer)
        if not issuer_acc:
            continue
        for h_idx in holder_indices:
            holder = accs[h_idx]
            if holder.address == m.issuer:
                continue
            auth_txns.append(
                (
                    "MPTokenAuthorize",
                    MPTokenAuthorize(account=holder.address, mptoken_issuance_id=m.mpt_issuance_id),
                    holder.wallet,
                )
            )
            dist_txns.append(
                (
                    "Payment",
                    Payment(
                        account=m.issuer,
                        destination=holder.address,
                        amount=MPTAmount(
                            mpt_issuance_id=m.mpt_issuance_id,
                            value=_CONF_MPT_DISTRIBUTION_AMOUNT,
                        ),
                    ),
                    issuer_acc.wallet,
                )
            )
    summary["conf_mpt_auth"] = await _submit_batch("conf_mpt_auth", auth_txns, client, seq)
    summary["conf_mpt_dist"] = await _submit_batch("conf_mpt_dist", dist_txns, client, seq)

    # ── 5. Holder ElGamal keys ───────────────────────────────────────
    for h_idx in holder_indices:
        holder = accs[h_idx]
        priv, pub = await cc.generate_keypair()
        holder.elgamal_private_key = priv
        holder.elgamal_public_key = pub

    # ── 6. Initial public->confidential Convert (matched sequence) ───
    # First Convert also registers the holder encryption key on its MPToken.
    await asyncio.sleep(3)
    convert_ok = 0
    for m in conf_issuances:
        if m.issuer not in issuer_keys:
            continue
        for h_idx in holder_indices:
            holder = accs[h_idx]
            if holder.address == m.issuer:
                continue
            try:
                holder_seq = await cc.account_sequence(client.url, holder.address)
                base = await cc.build_convert(
                    client.url,
                    holder.wallet,
                    m.mpt_issuance_id,
                    _CONF_MPT_CONVERT_AMOUNT,
                    issuer_keys[m.issuer][1],
                    holder.elgamal_private_key,
                    holder.elgamal_public_key,
                )
                result = await submit_tx(
                    "ConfidentialMPTConvert", base, client, holder.wallet, seq=holder_seq
                )
                seq.reset(holder.address)  # realign tracker to ledger-driven seq
                engine = result.get("engine_result", "")
                if engine in ("tesSUCCESS", "terQUEUED", "terPRE_SEQ"):
                    convert_ok += 1
                else:
                    send_event(
                        "workload::setup_reject : conf_mpt_convert",
                        {
                            "phase": "conf_mpt_convert",
                            "account": holder.address,
                            "engine_result": engine,
                        },
                    )
            except Exception as e:
                send_event(
                    "workload::setup_error : conf_mpt_convert",
                    {
                        "phase": "conf_mpt_convert",
                        "account": holder.address,
                        "error": f"{type(e).__name__}: {e}",
                    },
                )
    summary["conf_mpt_convert"] = convert_ok

    # ── 7. MergeInbox: move converted inbox -> spending balance ──────
    await asyncio.sleep(3)  # Convert must validate before merging its inbox
    merge_txns: list[tuple[str, Transaction, Wallet]] = []
    for m in conf_issuances:
        if m.issuer not in issuer_keys:
            continue
        for h_idx in holder_indices:
            holder = accs[h_idx]
            if holder.address == m.issuer:
                continue
            # Builder does an RPC; a raise here must not abort run_setup (it would
            # starve every later phase's setup_* assert).
            try:
                txn = await cc.build_merge_inbox(client.url, holder.wallet, m.mpt_issuance_id)
            except Exception as e:
                send_event(
                    "workload::setup_error : conf_mpt_merge",
                    {
                        "phase": "conf_mpt_merge",
                        "account": holder.address,
                        "error": f"{type(e).__name__}: {e}",
                    },
                )
                continue
            merge_txns.append(("ConfidentialMPTMergeInbox", txn, holder.wallet))
    summary["conf_mpt_merge"] = await _submit_batch("conf_mpt_merge", merge_txns, client, seq)

    # ── Fill tracked issuances with keys + seeded holder balances ────
    by_id = {ci.mpt_issuance_id: ci for ci in workload.confidential_mpt_issuances}
    for m in conf_issuances:
        ci = by_id.get(m.mpt_issuance_id)
        if ci is None:
            continue
        priv, pub = issuer_keys.get(m.issuer, ("", ""))
        ci.issuer_privkey = priv
        ci.issuer_pubkey = pub
        for h_idx in holder_indices:
            holder = accs[h_idx]
            if holder.address == m.issuer:
                continue
            ci.holders[holder.address] = ConfidentialHolder(
                address=holder.address,
                spending_balance=_CONF_MPT_CONVERT_AMOUNT,
                inbox_balance=0,
                version=0,
            )


# (sponsor_idx, sponsee_idx, mode, require_sign_fee, require_sign_reserve) -- fixed
# pairs (not random) to keep setup deterministic; reuses existing role accounts as
# sponsors so no extra reserved indices are needed.
_SPONSORSHIP_PAIRS: list[tuple[int, int, str, bool, bool]] = [
    (60, 62, "fee", False, False),
    (61, 63, "fee", True, False),
    (30, 64, "reserve", False, False),
    (31, 65, "reserve", False, True),
    (40, 66, "both", False, False),
    (41, 67, "both", True, True),
    (50, 68, "fee", False, False),
    (51, 69, "reserve", False, False),
    (10, 70, "both", False, False),
    (11, 71, "fee", False, False),
]
_TF_SPONSORSHIP_SET_REQUIRE_SIGN_FOR_FEE = 0x00010000
_TF_SPONSORSHIP_SET_REQUIRE_SIGN_FOR_RESERVE = 0x00040000


async def _setup_sponsorships(
    workload: Workload, accs: list[UserAccount], summary: dict[str, int]
) -> None:
    """Prefund ~10 Sponsorship objects (mixing fee-only/reserve-only/both,
    a couple with require-sign flags) so driver prefunded paths have
    working budgets from the first tick. Generous fee pools/reserve
    counts -- params.sponsorship_* alone can hand back a thin or zero
    budget, which is the point for driver-time exploration but not here."""
    txns: list[tuple[str, Transaction, Wallet]] = []
    for sponsor_idx, sponsee_idx, mode, req_fee, req_reserve in _SPONSORSHIP_PAIRS:
        if sponsor_idx >= len(accs) or sponsee_idx >= len(accs):
            continue
        sponsor = accs[sponsor_idx]
        sponsee = accs[sponsee_idx]
        flags = 0
        if req_fee:
            flags |= _TF_SPONSORSHIP_SET_REQUIRE_SIGN_FOR_FEE
        if req_reserve:
            flags |= _TF_SPONSORSHIP_SET_REQUIRE_SIGN_FOR_RESERVE
        fee_amount = params.sponsorship_fee_amount() if mode in ("fee", "both") else None
        remaining_owner_count = (
            max(5, params.sponsorship_reserve_count()) if mode in ("reserve", "both") else None
        )
        txns.append(
            (
                "SponsorshipSet",
                SponsorshipSet(
                    account=sponsor.address,
                    sponsee=sponsee.address,
                    fee_amount=fee_amount,
                    max_fee=params.sponsorship_max_fee() if fee_amount else None,
                    remaining_owner_count=remaining_owner_count,
                    flags=flags,
                ),
                sponsor.wallet,
            )
        )
    summary["sponsorships"] = await _run_phase(workload, "sponsorships", txns, _sponsorship_exists)


# ── Cross-resource accounts (Phase 4) ────────────────────────────────────
# Modifier combos fire only when one account holds multiple resources; the base
# setup places tickets / delegations / sponsorships on disjoint account ranges
# (~0 overlap). This pool gives ~20 accounts ALL of: a ticket pool, a delegate
# authorized for common types, and a prefunded sponsee Sponsorship -- so
# ticket+delegate and ticket+sponsor become reachable. Seeding is best-effort
# (thin combos, not correctness); it never fails the run. Counts + the coverage
# model's predicted hits live in docs/transaction-modifiers.md and are checked by
# scripts/modifier-coverage-model.
_CROSS_RESOURCE_RICH_RANGE = range(77, 97)  # accounts[77..96]: ticket + delegate-source + sponsee
_CROSS_RESOURCE_DELEGATE_INDICES = (97, 98)  # authorized delegates (perm chunk A / chunk B)
_CROSS_RESOURCE_FUNDED_SPONSOR_INDEX = 99  # funds a fee+reserve Sponsorship for every rich acct
_CROSS_RESOURCE_EXHAUSTED_SPONSOR_INDEX = 6  # fee-only (reserve budget 0) -> prefunded_exhausted
_CROSS_RESOURCE_TICKET_SEED = 60  # generous so the ticket pool outlasts driver consumption
_CROSS_RESOURCE_EXHAUSTED_COUNT = 4  # rich accts that also get an exhausted-reserve sponsorship
_DELEGATE_PERMS_MAX = 10  # rippled PERMISSIONS_MAX_LENGTH per DelegateSet

# Delegate permissions granted to the rich-account delegates. Sponsor-supported
# types are excluded on purpose: those decorate via the sponsor modifier
# (ticket+sponsor), leaving these for ticket+delegate so the two combos don't
# compete. Front-loaded with types a fresh account can submit unconditionally
# (creators) so the delegation is active early; object-dependent types activate
# as the account accumulates objects. Filtered against the live delegable set at
# runtime so a xrpl-py delegability change just thins combos, never aborts setup.
_CROSS_RESOURCE_PERM_ORDER = [
    "Payment",
    "OfferCreate",
    "NFTokenMint",
    "DIDSet",
    "MPTokenIssuanceCreate",
    "VaultCreate",
    "NFTokenBurn",
    "NFTokenModify",
    "NFTokenCreateOffer",
    "OfferCancel",
    "DIDDelete",
    "MPTokenIssuanceSet",
    "MPTokenIssuanceDestroy",
    "VaultDeposit",
    "VaultSet",
    "VaultWithdraw",
    "VaultDelete",
    "PermissionedDomainSet",
    "PermissionedDomainDelete",
    "NFTokenAcceptOffer",
]


def _cross_resource_delegate_perms() -> list[str]:
    """Curated permission names filtered to those actually delegable +
    ticket-supported + non-sponsor (single source of truth: the modifier sets)."""
    from workload.modifiers import _SPONSOR_SUPPORTED, _TICKET_SUPPORTED
    from workload.transactions import TX_TYPES
    from workload.transactions.delegation import DELEGABLE_TX_TYPES

    delegable = {t.value for t in DELEGABLE_TX_TYPES}
    eligible = (delegable & _TICKET_SUPPORTED & set(TX_TYPES)) - _SPONSOR_SUPPORTED
    return [t for t in _CROSS_RESOURCE_PERM_ORDER if t in eligible]


async def _setup_cross_resource(
    workload: Workload, accs: list[UserAccount], summary: dict[str, int]
) -> None:
    client, seq = workload.client, workload.seq
    rich = [accs[i] for i in _CROSS_RESOURCE_RICH_RANGE if i < len(accs)]
    if not rich:
        return
    rich_addrs = {r.address for r in rich}
    delegates = [accs[i] for i in _CROSS_RESOURCE_DELEGATE_INDICES if i < len(accs)]
    perms = _cross_resource_delegate_perms()
    perm_chunks = [
        perms[i : i + _DELEGATE_PERMS_MAX] for i in range(0, len(perms), _DELEGATE_PERMS_MAX)
    ]

    # ── 1. Delegations (rich = source). Submitted BEFORE tickets exist so the
    #      ticket modifier can't zero these txns' Sequence and desync the tracker.
    delegate_txns: list[tuple[str, Transaction, Wallet]] = []
    for r in rich:
        for d_acc, chunk in zip(delegates, perm_chunks, strict=False):
            if not chunk:
                continue
            delegate_txns.append(
                (
                    "DelegateSet",
                    DelegateSet(
                        account=r.address,
                        authorize=d_acc.address,
                        permissions=[
                            Permission(permission_value=TransactionType(p)) for p in chunk
                        ],
                    ),
                    r.wallet,
                )
            )
    n_deleg = await _submit_batch("cross_resource_delegates", delegate_txns, client, seq)

    # ── 2. Prefunded Sponsorships: every rich acct a fee+reserve sponsee (co-sign-
    #      free paths), plus a few fee-only (reserve budget 0) for prefunded_exhausted.
    sponsor_txns: list[tuple[str, Transaction, Wallet]] = []
    funded = (
        accs[_CROSS_RESOURCE_FUNDED_SPONSOR_INDEX]
        if len(accs) > _CROSS_RESOURCE_FUNDED_SPONSOR_INDEX
        else None
    )
    if funded is not None:
        for r in rich:
            sponsor_txns.append(
                (
                    "SponsorshipSet",
                    SponsorshipSet(
                        account=funded.address,
                        sponsee=r.address,
                        fee_amount=params.sponsorship_fee_amount(),
                        max_fee=params.sponsorship_max_fee(),
                        remaining_owner_count=max(5, params.sponsorship_reserve_count()),
                    ),
                    funded.wallet,
                )
            )
    exhausted = (
        accs[_CROSS_RESOURCE_EXHAUSTED_SPONSOR_INDEX]
        if len(accs) > _CROSS_RESOURCE_EXHAUSTED_SPONSOR_INDEX
        else None
    )
    if exhausted is not None:
        # Omitting remaining_owner_count -> RemainingOwnerCount 0: a valid fee-only
        # Sponsorship that a reserve-flag tx drains to tecINSUFFICIENT_RESERVE.
        for r in rich[:_CROSS_RESOURCE_EXHAUSTED_COUNT]:
            sponsor_txns.append(
                (
                    "SponsorshipSet",
                    SponsorshipSet(
                        account=exhausted.address,
                        sponsee=r.address,
                        fee_amount=params.sponsorship_fee_amount(),
                    ),
                    exhausted.wallet,
                )
            )
    n_spons = await _submit_batch("cross_resource_sponsorships", sponsor_txns, client, seq)

    # ── 3. Unsponsored sponsorable objects: one Check per rich acct so
    #      SponsorshipTransfer has owned candidates from the first driver tick.
    check_txns: list[tuple[str, Transaction, Wallet]] = []
    if funded is not None:
        for r in rich:
            check_txns.append(
                (
                    "CheckCreate",
                    CheckCreate(
                        account=r.address,
                        destination=funded.address,
                        send_max=params.check_send_max(),
                    ),
                    r.wallet,
                )
            )
    n_checks = await _submit_batch("cross_resource_checks", check_txns, client, seq)

    # ── 4. Tickets LAST: after this the ticket modifier may decorate rich-acct
    #      submits, so no further cross-resource txn from a rich acct may follow.
    ticket_txns: list[tuple[str, Transaction, Wallet]] = [
        (
            "TicketCreate",
            TicketCreate(account=r.address, ticket_count=_CROSS_RESOURCE_TICKET_SEED),
            r.wallet,
        )
        for r in rich
    ]
    n_tickets = await _submit_batch("cross_resource_tickets", ticket_txns, client, seq)
    for r in rich:
        seq.advance(r.address, _CROSS_RESOURCE_TICKET_SEED)

    # Let the WS listener track the seeded state (modifier ctx reads tracked
    # delegates / sponsorships / per-account tickets).
    await _poll(
        lambda: all(
            workload.accounts[r.address].tickets for r in rich if r.address in workload.accounts
        )
    )
    summary["cross_resource_rich"] = len(rich)
    tracked_deleg = sum(1 for d in workload.delegates if d.source in rich_addrs)
    tracked_spons = sum(1 for s in workload.sponsorships if s.sponsee in rich_addrs)
    tracked_tickets = sum(1 for r in rich if workload.accounts[r.address].tickets)
    send_event(
        "workload::setup_cross_resource",
        {
            "rich": len(rich),
            "perms": len(perms),
            "delegations_accepted": n_deleg,
            "delegations_tracked": tracked_deleg,
            "sponsorships_accepted": n_spons,
            "sponsorships_tracked": tracked_spons,
            "checks_accepted": n_checks,
            "tickets_accepted": n_tickets,
            "tickets_tracked": tracked_tickets,
        },
    )
    # Under-seeded: combos just get thinner (not a correctness failure), but flag
    # it so a starved sometimes(combo) is diagnosable rather than mysterious.
    under_seeded = (
        tracked_deleg < len(delegate_txns)
        or tracked_spons < len(rich)
        or tracked_tickets < len(rich)
    )
    if under_seeded:
        send_event(
            "workload::setup_cross_resource_partial",
            {
                "rich": len(rich),
                "delegations_tracked": tracked_deleg,
                "delegations_expected": len(delegate_txns),
                "sponsorships_tracked": tracked_spons,
                "tickets_tracked": tracked_tickets,
            },
        )


async def run_setup(workload: Workload) -> dict[str, int]:
    await _probe_node(workload)
    accs = _accounts_list(workload)
    client = workload.client
    seq = workload.seq
    summary: dict[str, int] = {}

    holder_indices = list(_HOLDER_RANGE) + list(_VAULT_RANGE)

    # Record setup-owned addresses so AccountDelete won't target them.
    _setup_indices: set[int] = (
        set(range(6))
        | {7, 8}
        | set(range(10, 17))
        | set(range(20, 25))
        | set(range(30, 36))
        | set(range(40, 43))
        | set(range(50, 53))
        | set(range(60, 72))
        | set(range(72, 77))
        # Cross-resource pool (Phase 4): rich accts + their delegates/sponsors must
        # not be deleted mid-run, else the combo source vanishes.
        | set(_CROSS_RESOURCE_RICH_RANGE)
        | set(_CROSS_RESOURCE_DELEGATE_INDICES)
        | {_CROSS_RESOURCE_FUNDED_SPONSOR_INDEX, _CROSS_RESOURCE_EXHAUSTED_SPONSOR_INDEX}
    )
    workload.protected_accounts.update(accs[i].address for i in _setup_indices if i < len(accs))

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
    summary["gateways"] = await _run_phase(workload, "gateways", gw_txns)

    # ── 1b. Sponsorships (XLS-68): prefund fee/reserve pools ─────────
    # Independent of everything else -- accounts are funded at genesis -- so
    # this runs early. Fail-loud: driver sponsor paths starve without budgets.
    await _setup_sponsorships(workload, accs, summary)

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
    summary["trust_lines"] = await _run_phase(
        workload, "trust_lines", trust_txns, _trust_line_exists
    )

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
    summary["iou_distribution"] = await _run_phase(workload, "iou_distribution", iou_txns)

    # ── 4. MPT issuances: 6 flag-distinct cohorts, one issuer each ───
    # Flag-distinct cohorts so valid DEX/AMM paths AND XLS-82 fault gates
    # (tecNO_PERMISSION/tecNO_AUTH/tecLOCKED) are all reachable; [5] is the
    # lockable cohort locked in step 6b.
    _LOCK = MPTokenIssuanceCreateFlag.TF_MPT_CAN_LOCK
    _CLAW = MPTokenIssuanceCreateFlag.TF_MPT_CAN_CLAWBACK
    _TRADE = MPTokenIssuanceCreateFlag.TF_MPT_CAN_TRADE
    _XFER = MPTokenIssuanceCreateFlag.TF_MPT_CAN_TRANSFER
    _AUTH = MPTokenIssuanceCreateFlag.TF_MPT_REQUIRE_AUTH
    _mpt_cohorts: list[tuple[int, int]] = [
        (0, _LOCK | _CLAW | _TRADE | _XFER),  # tradeable
        (1, _LOCK | _CLAW | _TRADE | _XFER),  # tradeable
        (2, _LOCK),  # no-trade
        (3, _LOCK | _TRADE),  # no-transfer
        (4, _LOCK | _TRADE | _XFER | _AUTH),  # require-auth
        (5, _LOCK | _CLAW | _TRADE | _XFER),  # lockable (locked in 6b)
    ]
    summary["mpt_issuances"] = await _run_phase(
        workload,
        "mpt_issuances",
        [
            (
                "MPTokenIssuanceCreate",
                MPTokenIssuanceCreate(account=accs[i].address, flags=flags),
                accs[i].wallet,
            )
            for i, flags in _mpt_cohorts
            if i < len(accs)
        ],
        _mpt_issuance_exists,
    )

    # ── 5. MPT authorization: holders authorize for each issuance ────
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
    summary["mpt_authorizations"] = await _run_phase(
        workload, "mpt_authorizations", mpt_auth_txns, _mpt_authorized
    )

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

    # ── 6b. MPT lock: lock the lockable issuance ─────────────────────
    # Lock accs[5]'s cohort to make locked-MPT gates reachable (tecLOCKED on
    # offers/AMM, tecPATH_DRY on MPTokensV2 payments). After distribution so
    # holders are funded before the freeze.
    lock_count = 0
    if len(accs) > 5:
        lockable = next((m for m in workload.mpt_issuances if m.issuer == accs[5].address), None)
        if lockable is not None:
            lock_count = await _submit_batch(
                "mpt_lock",
                [
                    (
                        "MPTokenIssuanceSet",
                        MPTokenIssuanceSet(
                            account=accs[5].address,
                            mptoken_issuance_id=lockable.mpt_issuance_id,
                            flags=MPTokenIssuanceSetFlag.TF_MPT_LOCK,
                        ),
                        accs[5].wallet,
                    )
                ],
                client,
                seq,
            )
            if lock_count:
                lockable.locked = True
    summary["mpt_lock"] = lock_count

    # ── 6c. Confidential MPT (XLS-0096): privacy issuances + seeded balances
    # Needs authorized funded holders, so runs after public MPT distribution.
    # Contained: a raise here must not abort run_setup — the setup_* asserts for
    # every later phase only fire in the summary loop at the end.
    try:
        await _setup_confidential_mpt(workload, accs, summary)
    except Exception as e:
        send_event(
            "workload::setup_error : conf_mpt",
            {"phase": "conf_mpt", "error": f"{type(e).__name__}: {e}"},
        )

    # ── 7. Vaults: 4 XRP (loan brokers), 2 IOU, 2 MPT ────────────────
    vault_txns = []
    for i in range(min(8, max(0, len(accs) - 10))):
        src = accs[10 + i]
        if i < 4:
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
        elif i < 6:
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
            # A vault can only back a transferable, unlocked, non-require-auth MPT; the
            # flag-distinct fault cohorts (require-auth [4] → tecNO_AUTH, locked [5] →
            # tecLOCKED) can't, so exclude them or the fail-loud phase aborts on them.
            vault_mpts = [
                m
                for m in workload.mpt_issuances
                if m.can_transfer and not m.require_auth and not m.locked
            ]
            if vault_mpts:
                mpt = vault_mpts[min(i - 6, len(vault_mpts) - 1)]
                vault_txns.append(
                    (
                        "VaultCreate",
                        VaultCreate(
                            account=src.address,
                            asset=MPTCurrency(mpt_issuance_id=mpt.mpt_issuance_id),
                            assets_maximum=_VAULT_ASSETS_MAXIMUM,
                        ),
                        src.wallet,
                    )
                )
    summary["vaults"] = await _run_phase(workload, "vaults", vault_txns, _vault_exists)

    # ── 7b. Vault deposits: owners deposit into their vaults ─────────
    # Best-effort: the second MPT vault's owner (accs[17], outside the funded
    # holder ranges) has no MPT balance, so its deposit fails by design.
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
    holder_deposit_txns = []
    for vault in workload.vaults:
        asset = vault.asset
        if not isinstance(asset, IssuedCurrency):
            continue
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
                            currency=asset.currency, issuer=asset.issuer, value="1000"
                        ),
                    ),
                    holder.wallet,
                )
            )
    summary["holder_vault_deposits"] = await _submit_batch(
        "holder_vault_deposits", holder_deposit_txns, client, seq
    )

    # ── 8. NFTs: 5 from accounts[20..24] ─────────────────────────────
    summary["nfts"] = await _run_phase(
        workload,
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
        _nft_exists,
    )

    # ── 8b. NFT offers ───────────────────────────────────────────────
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
    summary["nft_offers"] = await _run_phase(
        workload, "nft_offers", nft_offer_txns, _nft_offer_exists
    )

    # ── 8c. AMMs: seed pools so Deposit/Withdraw/Vote/Bid/Delete aren't no-ops
    amm_txns = []
    if len(accs) > 63:
        gw0_idx, gw0_currencies = _GATEWAYS[0]  # (60, ["USD", "BTC"])
        gw1_idx, gw1_currencies = _GATEWAYS[1]  # (61, ["EUR", "GBP"])
        gw0_addr = accs[gw0_idx].address if gw0_idx < len(accs) else None
        gw1_addr = accs[gw1_idx].address if gw1_idx < len(accs) else None

        if gw0_addr:
            amm_txns.append(
                (
                    "AMMCreate",
                    AMMCreate(
                        account=accs[62].address,
                        amount=_AMM_XRP_AMOUNT,
                        amount2=IssuedCurrencyAmount(
                            currency=gw0_currencies[0],
                            issuer=gw0_addr,
                            value=_AMM_IOU_AMOUNT,
                        ),
                        trading_fee=_AMM_TRADING_FEE,
                    ),
                    accs[62].wallet,
                )
            )

        if gw0_addr and gw1_addr:
            amm_txns.append(
                (
                    "AMMCreate",
                    AMMCreate(
                        account=accs[63].address,
                        amount=IssuedCurrencyAmount(
                            currency=gw0_currencies[0],
                            issuer=gw0_addr,
                            value=_AMM_IOU_AMOUNT,
                        ),
                        amount2=IssuedCurrencyAmount(
                            currency=gw1_currencies[0],
                            issuer=gw1_addr,
                            value=_AMM_IOU_AMOUNT,
                        ),
                        trading_fee=_AMM_TRADING_FEE,
                    ),
                    accs[63].wallet,
                )
            )
    summary["amms"] = await _run_phase(workload, "amms", amm_txns, _amm_exists)

    # ── 9. Credentials ───────────────────────────────────────────────
    if len(accs) > 35:
        issuer = accs[30]
        summary["credentials"] = await _run_phase(
            workload,
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
            _credential_exists,
        )
        # Subjects accept so they become members of the step-11 domains; setup
        # credentials never expire, so membership stays stable.
        summary["credential_accepts"] = await _run_phase(
            workload,
            "credential_accepts",
            [
                (
                    "CredentialAccept",
                    CredentialAccept(
                        account=accs[31 + i].address,
                        issuer=issuer.address,
                        credential_type=_SETUP_CREDENTIAL_TYPE,
                    ),
                    accs[31 + i].wallet,
                )
                for i in range(5)
            ],
            _credential_accepted,
        )
    else:
        summary["credentials"] = 0
        summary["credential_accepts"] = 0

    # ── 10. Tickets ──────────────────────────────────────────────────
    # 50-52 (domain owners) also get tickets so the ticket x permissioned-DEX
    # valid path (ticket holder that is also a domain member) is reachable.
    ticket_indices = [i for i in (40, 41, 42, 50, 51, 52) if i < len(accs)]
    summary["tickets"] = await _submit_batch(
        "tickets",
        [
            (
                "TicketCreate",
                TicketCreate(account=accs[i].address, ticket_count=_TICKET_COUNT),
                accs[i].wallet,
            )
            for i in ticket_indices
        ],
        client,
        seq,
    )
    summary["tickets"] *= _TICKET_COUNT
    # TicketCreate advances Sequence by TicketCount + 1 but next_seq counted
    # only +1; realign the tracker for accounts reused later (domains in step 11).
    for i in ticket_indices:
        seq.advance(accs[i].address, _TICKET_COUNT)

    # ── 11. Permissioned domains ─────────────────────────────────────
    if len(accs) > 52:
        cred_issuer = accs[30].address
        summary["domains"] = await _run_phase(
            workload,
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
            _domain_exists,
        )
    else:
        summary["domains"] = 0

    # ── 12. Loan brokers (4th has no loans — for cover withdraw) ─────
    # XRP vaults only: setup loans/cover are XRP, owners always hold enough.
    await asyncio.sleep(3)
    broker_txns = []
    xrp_vaults = [v for v in workload.vaults if isinstance(v.asset, xrpl.models.XRP)]
    for vault in xrp_vaults[:4]:
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
    summary["loan_brokers"] = await _run_phase(
        workload, "loan_brokers", broker_txns, _broker_exists
    )

    # ── 12b. Broker cover deposits ───────────────────────────────────
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
    summary["cover_deposits"] = await _run_phase(
        workload, "cover_deposits", cover_txns, _cover_deposited
    )

    # ── 13. Loans (co-signed) ────────────────────────────────────────
    borrower_indices = list(_HOLDER_RANGE)
    loan_attempts: list[_LoanAttempt] = []
    for idx, broker in enumerate(workload.loan_brokers[:3]):
        if broker.owner not in workload.accounts:
            continue
        b_idx = borrower_indices[idx] if idx < len(borrower_indices) else None
        if b_idx is None or b_idx >= len(accs) or accs[b_idx].address == broker.owner:
            continue
        loan_attempts.append(
            _LoanAttempt(
                broker_wallet=workload.accounts[broker.owner].wallet,
                broker_owner=broker.owner,
                broker_id=broker.loan_broker_id,
                borrower=accs[b_idx],
            )
        )
    summary["loans"] = await _run_loans(workload, loan_attempts)

    # ── 13b. Zero-interest loan + payoff (for LoanDelete) ────────────
    # Auxiliary (LoanDelete reachability): best-effort, must not abort the run.
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
                        await _poll(lambda: len(workload.loans) >= summary["loans"] + 1)
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

    # ── 14. Cross-resource pool (Phase 4): overlap for modifier combos ───
    # Best-effort (thin combos, not correctness); contained so a raise can't
    # abort the run. Runs last so its late ticket seeding can't desync any
    # earlier phase's sequence tracking.
    try:
        await _setup_cross_resource(workload, accs, summary)
    except Exception as e:
        send_event(
            "workload::setup_error : cross_resource",
            {"phase": "cross_resource", "error": f"{type(e).__name__}: {e}"},
        )

    # ── Done ─────────────────────────────────────────────────────────
    for key, count in summary.items():
        if count:
            reachable(f"workload::setup_{key}", {"count": count})

    await asyncio.sleep(5)  # let WS listener process validated results

    return summary
