"""Deterministic setup for the Antithesis first_* phase.

Creates the minimum ledger state needed for all transaction types to operate,
including IOU gateways with token distribution, MPT authorization, and AMM pools.

Account allocation:
  [0..4]   — MPT issuers
  [5..6]   — Confidential MPT issuers (privacy-enabled, XLS-0096)
  [10..16] — Vault creators (also get trust lines + IOU/MPT balances for non-XRP vaults)
  [20..24] — NFT minters
  [30..35] — Credential issuer + subjects
  [40..42] — Ticket holders
  [50..52] — Domain creators
  [60..61] — IOU gateways (DefaultRipple + AllowTrustLineClawback set)
  [62..71] — IOU/MPT holders (trust lines + balances for all currencies)
  [62]     — also creates XRP/USD AMM
  [63]     — also creates USD/EUR AMM
  [72..76] — Confidential MPT holders
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from workload.app import Workload

import xrpl.models
from antithesis.assertions import reachable, unreachable
from antithesis.lifecycle import send_event
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.asyncio.transaction import autofill_and_sign
from xrpl.asyncio.transaction import submit as xrpl_submit
from xrpl.models import IssuedCurrency, IssuedCurrencyAmount
from xrpl.models.amounts import MPTAmount
from xrpl.models.currencies import MPTCurrency
from xrpl.models.requests import GenericRequest
from xrpl.models.transactions import (
    AccountSet,
    AccountSetAsfFlag,
    AMMCreate,
    CredentialCreate,
    LoanBrokerCoverDeposit,
    LoanBrokerSet,
    LoanPay,
    LoanSet,
    MPTokenAuthorize,
    MPTokenIssuanceCreate,
    MPTokenIssuanceCreateFlag,
    MPTokenIssuanceSet,
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
from workload.assertions import assert_no_internal_error_submit
from workload.models import (
    ConfidentialHolder,
    ConfidentialMPTIssuance,
    UserAccount,
)
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
_CONF_MPT_CONVERT_AMOUNT = 5000  # initial public→confidential convert per holder
_CONF_MPT_DISTRIBUTION_AMOUNT = "10000"  # public MPT distribution to conf holders

# Gateway assignments: (account_index, [currencies])
_GATEWAYS = [
    (60, ["USD", "BTC"]),
    (61, ["EUR", "GBP"]),
]

# Accounts that receive IOU/MPT balances
_HOLDER_RANGE = range(62, 72)  # accounts[62..71]
_VAULT_RANGE = range(10, 17)  # accounts[10..16]
_CONF_ISSUER_RANGE = range(5, 7)  # accounts[5..6] — confidential MPT issuers
_CONF_HOLDER_RANGE = range(72, 77)  # accounts[72..76] — confidential MPT holders


def _accounts_list(workload: Workload) -> list[UserAccount]:
    return list(workload.accounts.values())


async def _wait_for_state(
    items: list, expected: int, label: str, timeout: float = 15, poll: float = 1
) -> int:
    """Wait for WS listener to populate a list. Returns actual count. Non-blocking."""
    elapsed = 0.0
    while len(items) < expected and elapsed < timeout:
        await asyncio.sleep(poll)
        elapsed += poll
    if len(items) < expected:
        send_event(
            f"workload::setup_state_partial : {label}",
            {"expected": expected, "got": len(items), "timeout_s": timeout},
        )
    return len(items)


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
                send_event(
                    f"workload::setup_reject : {name}",
                    {
                        "phase": name,
                        "tx_type": tx_type,
                        "account": wallet.address,
                        "sequence": seq_num,
                        "engine_result": engine,
                    },
                )
        except Exception as e:
            send_event(
                f"workload::setup_error : {name}",
                {
                    "phase": name,
                    "tx_type": tx_type,
                    "account": wallet.address,
                    "error": f"{type(e).__name__}: {e}",
                },
            )
    return created


async def _submit_raw_setup(
    name: str,
    tx_json: dict,
    wallet: Wallet,
    client: AsyncJsonRpcClient,
) -> str:
    """Submit a raw JSON-RPC transaction for setup (no xrpl-py model).

    Returns the engine_result string, or empty string on error.
    """
    request = GenericRequest(method="submit", tx_json=tx_json, secret=wallet.seed)
    try:
        response = await client.request(request)
    except Exception as e:
        send_event(
            f"workload::setup_error : {name}",
            {
                "phase": name,
                "tx_type": tx_json.get("TransactionType", "?"),
                "account": wallet.address,
                "error": f"{type(e).__name__}: {e}",
            },
        )
        return ""
    result = response.result
    assert_no_internal_error_submit(name, result)
    engine = result.get("engine_result", "")
    if engine not in ("tesSUCCESS", "terQUEUED", "terPRE_SEQ"):
        send_event(
            f"workload::setup_reject : {name}",
            {
                "phase": name,
                "tx_type": tx_json.get("TransactionType", "?"),
                "account": wallet.address,
                "engine_result": engine,
            },
        )
    return engine


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


async def _probe_node(workload: Workload) -> None:
    """Submit a no-op AccountSet to confirm the node accepts transactions.

    Retries with backoff until accepted. The sync checks in wait_for_network
    verify the node reports 'full', but that doesn't guarantee it will accept
    transaction submissions — autofill and submit have stricter sync requirements.
    """
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
    """Set up Confidential MPT state (XLS-0096).

    Steps:
      1. Create privacy-enabled MPT issuances from accounts[5..6]
      2. Set issuer ElGamal encryption keys via MPTokenIssuanceSet
      3. Authorize holders[72..76] for each issuance
      4. Distribute public MPT balances to holders
      5. Generate ElGamal keypairs for holders
      6. Convert initial amounts to confidential (ConfidentialMPTConvert)
      7. MergeInbox for each holder to move inbox → spending
    """
    import workload.confidential_crypto as cc

    client = workload.client
    seq = workload.seq

    issuer_indices = [i for i in _CONF_ISSUER_RANGE if i < len(accs)]
    holder_indices = [i for i in _CONF_HOLDER_RANGE if i < len(accs)]
    if not issuer_indices or not holder_indices:
        return

    # ── 1. Create privacy-enabled MPT issuances ─────────────────────
    _conf_mpt_flags = (
        MPTokenIssuanceCreateFlag.TF_MPT_CAN_CONFIDENTIAL_AMOUNT
        | MPTokenIssuanceCreateFlag.TF_MPT_CAN_CLAWBACK
        | MPTokenIssuanceCreateFlag.TF_MPT_CAN_TRANSFER
    )
    summary["conf_mpt_issuances"] = await _submit_batch(
        "conf_mpt",
        [
            (
                "MPTokenIssuanceCreate",
                MPTokenIssuanceCreate(account=accs[i].address, flags=_conf_mpt_flags),
                accs[i].wallet,
            )
            for i in issuer_indices
        ],
        client,
        seq,
    )

    # Wait for WS listener to register the new issuances
    total_mpt = len(workload.mpt_issuances)
    expected = total_mpt + summary["conf_mpt_issuances"]
    await _wait_for_state(workload.mpt_issuances, expected, "conf_mpt_issuances")

    # Identify which issuances are ours (created by conf issuer accounts)
    conf_issuer_addrs = {accs[i].address for i in issuer_indices}
    conf_issuances = [m for m in workload.mpt_issuances if m.issuer in conf_issuer_addrs]
    if not conf_issuances:
        return

    # ── 2. Generate issuer ElGamal keys + set via MPTokenIssuanceSet ──
    set_txns = []
    issuer_keys: dict[str, tuple[str, str]] = {}  # addr → (priv, pub)
    for m in conf_issuances:
        priv, pub = cc.generate_keypair()
        issuer_keys[m.issuer] = (priv, pub)
        issuer_acc = workload.accounts.get(m.issuer)
        if not issuer_acc:
            continue
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
    summary["conf_mpt_issuer_keys"] = await _submit_batch(
        "conf_mpt_issuer_keys", set_txns, client, seq
    )

    # ── 3. Authorize holders for each confidential issuance ──────────
    auth_txns = []
    for m in conf_issuances:
        for h_idx in holder_indices:
            holder = accs[h_idx]
            if holder.address == m.issuer:
                continue
            auth_txns.append(
                (
                    "MPTokenAuthorize",
                    MPTokenAuthorize(
                        account=holder.address,
                        mptoken_issuance_id=m.mpt_issuance_id,
                    ),
                    holder.wallet,
                )
            )
    summary["conf_mpt_auth"] = await _submit_batch("conf_mpt_auth", auth_txns, client, seq)

    # ── 4. Distribute public MPT balances to holders ─────────────────
    dist_txns = []
    for m in conf_issuances:
        issuer_acc = workload.accounts.get(m.issuer)
        if not issuer_acc:
            continue
        for h_idx in holder_indices:
            holder = accs[h_idx]
            if holder.address == m.issuer:
                continue
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
    summary["conf_mpt_dist"] = await _submit_batch("conf_mpt_dist", dist_txns, client, seq)

    # ── 5. Generate ElGamal keypairs for holders ─────────────────────
    for h_idx in holder_indices:
        holder = accs[h_idx]
        priv, pub = cc.generate_keypair()
        holder.elgamal_private_key = priv
        holder.elgamal_public_key = pub

    # ── 6. Convert initial amounts to confidential ───────────────────
    # First Convert also registers the holder's encryption key.
    await asyncio.sleep(3)  # let MPT distribution settle
    convert_ok = 0
    for m in conf_issuances:
        issuer_priv, issuer_pub = issuer_keys.get(m.issuer, (None, None))
        if not issuer_priv:
            continue
        for h_idx in holder_indices:
            holder = accs[h_idx]
            if holder.address == m.issuer:
                continue
            amount = _CONF_MPT_CONVERT_AMOUNT
            bf = cc.generate_blinding_factor()

            # Encrypt amount for holder and issuer
            holder_ct = cc.encrypt(holder.elgamal_public_key, amount, bf)
            issuer_ct = cc.encrypt(issuer_pub, amount, bf)

            # Context hash and proof of knowledge
            holder_hex = cc.account_to_hex(holder.address)
            holder_seq = await seq.next_seq(holder.address)
            ctx = cc.convert_context_hash(holder_hex, holder_seq, m.mpt_issuance_id)
            pok = cc.pok_proof(holder.elgamal_private_key, holder.elgamal_public_key, ctx)

            tx_json = {
                "TransactionType": "ConfidentialMPTConvert",
                "Account": holder.address,
                "MPTokenIssuanceID": m.mpt_issuance_id,
                "MPTAmount": amount,
                "HolderEncryptedAmount": holder_ct,
                "IssuerEncryptedAmount": issuer_ct,
                "BlindingFactor": bf,
                "HolderEncryptionKey": holder.elgamal_public_key,
                "ZKProof": pok,
                "Sequence": holder_seq,
            }
            engine = await _submit_raw_setup("conf_mpt_convert", tx_json, holder.wallet, client)
            if engine in ("tesSUCCESS", "terQUEUED", "terPRE_SEQ"):
                convert_ok += 1
    summary["conf_mpt_convert"] = convert_ok

    # ── 7. MergeInbox to move inbox → spending ───────────────────────
    await asyncio.sleep(3)  # let Convert transactions validate
    merge_ok = 0
    for m in conf_issuances:
        for h_idx in holder_indices:
            holder = accs[h_idx]
            if holder.address == m.issuer:
                continue
            tx_json = {
                "TransactionType": "ConfidentialMPTMergeInbox",
                "Account": holder.address,
                "MPTokenIssuanceID": m.mpt_issuance_id,
            }
            engine = await _submit_raw_setup("conf_mpt_merge", tx_json, holder.wallet, client)
            if engine in ("tesSUCCESS", "terQUEUED", "terPRE_SEQ"):
                merge_ok += 1
    summary["conf_mpt_merge"] = merge_ok

    # ── Register ConfidentialMPTIssuance objects in workload ──────────
    for m in conf_issuances:
        priv, pub = issuer_keys.get(m.issuer, ("", ""))
        ci = ConfidentialMPTIssuance(
            issuer=m.issuer,
            mpt_issuance_id=m.mpt_issuance_id,
            issuer_privkey=priv,
            issuer_pubkey=pub,
        )
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
        workload.confidential_mpt_issuances.append(ci)


async def run_setup(workload: Workload) -> dict[str, int]:
    """Submit deterministic setup transactions. State tracking via WS listener."""
    await _probe_node(workload)
    accs = _accounts_list(workload)
    client = workload.client
    seq = workload.seq
    summary: dict[str, int] = {}

    holder_indices = list(_HOLDER_RANGE) + list(_VAULT_RANGE)

    # Record addresses used by setup so AccountDelete won't target them.
    # Indices: gateways (60-61), MPT issuers (0-6), vault creators (10-16),
    # NFT minters (20-24), credential issuer + subjects (30-35), ticket
    # holders (40-42), domain creators (50-52), IOU/MPT holders (62-76).
    _setup_indices: set[int] = (
        set(range(7))
        | set(range(10, 17))
        | set(range(20, 25))
        | set(range(30, 36))
        | set(range(40, 43))
        | set(range(50, 53))
        | set(range(60, 77))
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
    _mpt_flags = (
        MPTokenIssuanceCreateFlag.TF_MPT_CAN_LOCK | MPTokenIssuanceCreateFlag.TF_MPT_CAN_CLAWBACK
    )
    summary["mpt_issuances"] = await _submit_batch(
        "mpt",
        [
            (
                "MPTokenIssuanceCreate",
                MPTokenIssuanceCreate(account=accs[i].address, flags=_mpt_flags),
                accs[i].wallet,
            )
            for i in range(min(5, len(accs)))
        ],
        client,
        seq,
    )

    # ── 5. MPT authorization: holders authorize for each issuance ────
    await _wait_for_state(workload.mpt_issuances, summary["mpt_issuances"], "mpt_issuances")
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

    # ── 6b. Confidential MPT setup (XLS-0096) ───────────────────────
    await _setup_confidential_mpt(workload, accs, summary)

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
    await _wait_for_state(workload.vaults, summary["vaults"], "vaults")
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
    await _wait_for_state(workload.nfts, summary["nfts"], "nfts")
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

    # ── 8c. AMMs: seed pools so Deposit/Withdraw/Vote/Bid/Delete aren't no-ops
    amm_txns = []
    if len(accs) > 63:
        gw0_idx, gw0_currencies = _GATEWAYS[0]  # (60, ["USD", "BTC"])
        gw1_idx, gw1_currencies = _GATEWAYS[1]  # (61, ["EUR", "GBP"])
        gw0_addr = accs[gw0_idx].address if gw0_idx < len(accs) else None
        gw1_addr = accs[gw1_idx].address if gw1_idx < len(accs) else None

        # AMM 1: XRP / USD — created by holder account[62]
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

        # AMM 2: USD / EUR — created by holder account[63]
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
    summary["amms"] = await _submit_batch("amms", amm_txns, client, seq)

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
    await _wait_for_state(workload.loan_brokers, summary["loan_brokers"], "loan_brokers")
    cover_txns = []
    vault_by_id = {v.vault_id: v for v in workload.vaults}
    for broker in workload.loan_brokers[:4]:
        if broker.owner not in workload.accounts:
            continue
        owner = workload.accounts[broker.owner]
        vault = vault_by_id.get(broker.vault_id)
        if vault and isinstance(vault.asset, IssuedCurrency):
            amount = IssuedCurrencyAmount(
                currency=vault.asset.currency,
                issuer=vault.asset.issuer,
                value=_IOU_DISTRIBUTION_AMOUNT,
            )
        elif vault and isinstance(vault.asset, MPTCurrency):
            amount = MPTAmount(
                mpt_issuance_id=vault.asset.mpt_issuance_id, value=_MPT_DISTRIBUTION_AMOUNT
            )
        else:
            amount = _COVER_DEPOSIT
        cover_txns.append(
            (
                "LoanBrokerCoverDeposit",
                LoanBrokerCoverDeposit(
                    account=owner.address,
                    loan_broker_id=broker.loan_broker_id,
                    amount=amount,
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
                        await _wait_for_state(
                            workload.loans, summary["loans"] + 1, "zero_interest_loan"
                        )
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
