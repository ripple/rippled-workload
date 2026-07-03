"""Reserve-sponsored object creation (XLS-68 Sponsor amendment).

A creator tx carrying the common fields Sponsor + SponsorFlags=spfSponsorReserve
(optionally |spfSponsorFee) moves the new object's reserve onto the sponsor
instead of the owner (rippled's preflight1Sponsor allow-lists which tx types may
set spfSponsorReserve; see Transactor.cpp). Two paths, same as sponsorship.py's
SponsorshipTransfer Create: co-signed (SponsorSignature, any account) or
prefunded (an existing Sponsorship budget, no signature needed) -- both reuse
sponsorship.py's ``_pick_reserve_sponsor``. Eight synthetic endpoints bucket by
real TransactionType (``Sponsored{RealType}``); ws_listener.py's generic
reserve-sponsor rule fires that assertion and records ``w.sponsored_objects``
from the validated tx, so no per-type state updater is needed here.

Per-type valid-path param logic is reused directly from each real builder
(checks/escrow/payment_channels/trustlines/credentials); SignerListSet (create
branch only -- reserve makes no sense on the delete branch), DepositPreauth (no
existing builder), and MPTokenAuthorize (holder self opt-in only -- issuer-auth
authorizes an existing MPToken rather than creating one) are built inline.
"""

from __future__ import annotations

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.asyncio.transaction import autofill_and_sign
from xrpl.asyncio.transaction import submit as xrpl_submit
from xrpl.models import IssuedCurrencyAmount as IOUAmount
from xrpl.models.transactions import (
    DepositPreauth,
    MPTokenAuthorize,
    OfferCreate,
    SignerListSet,
    Transaction,
)
from xrpl.models.transactions.signer_list_set import SignerEntry
from xrpl.transaction import sign_as_sponsor
from xrpl.wallet import Wallet

from workload import params
from workload.assertions import tx_submitted
from workload.fuzz import submit_fuzzed
from workload.models import MPTokenIssuance, Sponsorship, UserAccount
from workload.randoms import choice, randint, sample
from workload.submit import submit_raw, submit_tx
from workload.transactions.checks import _check_create_base
from workload.transactions.credentials import _credential_create_base
from workload.transactions.escrow import _escrow_create_base
from workload.transactions.nft import _nftoken_mint_base
from workload.transactions.payment_channels import _channel_create_base
from workload.transactions.sponsorship import _pick_reserve_sponsor
from workload.transactions.trustlines import _trustline_create_base

_Built = tuple[Transaction, Wallet]

# ── Per-type valid-path builders ─────────────────────────────────────


def _sponsored_check_base(accounts: dict[str, UserAccount]) -> _Built | None:
    return _check_create_base(accounts)


def _sponsored_escrow_base(accounts: dict[str, UserAccount]) -> _Built | None:
    built = _escrow_create_base(accounts)
    if built is None:
        return None
    txn, wallet, _fulfillment = built
    return txn, wallet


def _sponsored_channel_base(accounts: dict[str, UserAccount]) -> _Built | None:
    return _channel_create_base(accounts)


def _sponsored_trustline_base(accounts: dict[str, UserAccount]) -> _Built | None:
    return _trustline_create_base(accounts)


def _sponsored_credential_base(accounts: dict[str, UserAccount]) -> _Built | None:
    return _credential_create_base(accounts)


def _sponsored_signer_list_base(accounts: dict[str, UserAccount]) -> _Built | None:
    """Create branch only, unlike signer_list_set.py's 50/50 set/remove -- deleting
    a list frees reserve rather than consuming it, so a sponsor there is a no-op."""
    if len(accounts) < 3:
        return None
    acct_list = list(accounts.values())
    src = choice(acct_list)
    others = [a for a in acct_list if a.address != src.address]
    count = min(randint(2, 5), len(others))
    entries = [
        SignerEntry(account=s.address, signer_weight=randint(1, 3)) for s in sample(others, count)
    ]
    quorum = randint(1, sum(e.signer_weight for e in entries))
    txn = SignerListSet(account=src.address, signer_quorum=quorum, signer_entries=entries)
    return txn, src.wallet


def _sponsored_deposit_preauth_base(accounts: dict[str, UserAccount]) -> _Built | None:
    if len(accounts) < 2:
        return None
    src_id, auth_id = sample(list(accounts), 2)
    src = accounts[src_id]
    return DepositPreauth(account=src.address, authorize=auth_id), src.wallet


def _sponsored_mpt_base(
    accounts: dict[str, UserAccount], mpt_issuances: list[MPTokenIssuance]
) -> _Built | None:
    if not mpt_issuances:
        return None
    mpt = choice(mpt_issuances)
    holders = [a for a in accounts if a != mpt.issuer]
    if not holders:
        return None
    holder = accounts[choice(holders)]
    return MPTokenAuthorize(
        account=holder.address, mptoken_issuance_id=mpt.mpt_issuance_id
    ), holder.wallet


# ── Shared valid/faulty engine ────────────────────────────────────────


async def _submit_cosigned(
    name: str,
    sponsee_wallet: Wallet,
    sponsor_wallet: Wallet,
    txn: Transaction,
    client: AsyncJsonRpcClient,
) -> None:
    signed = await autofill_and_sign(txn, client, sponsee_wallet)
    sponsor_result = sign_as_sponsor(sponsor_wallet, signed)
    response = await xrpl_submit(sponsor_result.tx, client)
    tx_submitted(name, txn, response.result)


async def _sponsored_create_valid(
    name: str,
    built: _Built | None,
    accounts: dict[str, UserAccount],
    sponsorships: list[Sponsorship],
    client: AsyncJsonRpcClient,
) -> None:
    if built is None:
        return
    txn, wallet = built
    sponsor_addr, prefunded = _pick_reserve_sponsor(wallet.address, accounts, sponsorships)
    if sponsor_addr is None:
        return
    sponsored = txn.__replace__(sponsor=sponsor_addr, sponsor_flags=params.sponsor_reserve_flags())
    if prefunded:
        await submit_tx(name, sponsored, client, wallet)
        return
    await _submit_cosigned(name, wallet, accounts[sponsor_addr].wallet, sponsored, client)


async def _sponsored_create_faulty(
    name: str,
    built: _Built | None,
    accounts: dict[str, UserAccount],
    sponsorships: list[Sponsorship],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    mutation = choice(
        [
            "fuzz",
            "sponsor_without_flags",
            "flags_without_sponsor",
            "invalid_flag_bits",
            "sponsor_equals_account",
            "nonexistent_sponsor",
            "disallowed_type",
            "delegate_combo",
            "garbage_signature",
            "prefunded_exhausted",
        ]
    )

    if mutation == "fuzz":
        if built is None:
            return
        base, wallet = built
        others = [a for a in accounts if a != wallet.address]
        if not others:
            return
        sponsored = base.__replace__(
            sponsor=choice(others), sponsor_flags=params.sponsor_reserve_flags()
        )
        await submit_fuzzed(name, sponsored, client, wallet)
        return

    if mutation == "sponsor_without_flags":
        # xrpl-py builds this fine (SponsorFlags requires Sponsor, not vice versa) ->
        # rippled's preflight1Sponsor "no sponsor flags" branch -> temINVALID_FLAG.
        if built is None:
            return
        base, wallet = built
        others = [a for a in accounts if a != wallet.address]
        if not others:
            return
        txn = base.__replace__(sponsor=choice(others))
        await submit_tx(name, txn, client, wallet)
        return

    if mutation == "flags_without_sponsor":
        # xrpl-py rejects this at construction -> raw submit for rippled's own check.
        if built is None:
            return
        base, wallet = built
        others = [a for a in accounts if a != wallet.address]
        if not others:
            return
        valid = base.__replace__(
            sponsor=choice(others), sponsor_flags=params.sponsor_reserve_flags()
        )

        def _mutate(d: dict) -> None:
            d.pop("Sponsor", None)

        await submit_raw(name, valid, client, wallet, _mutate)
        return

    if mutation == "invalid_flag_bits":
        # xrpl-py rejects any bit outside 0x1/0x2 at construction -> raw submit for
        # rippled's spfSponsorFlagMask check.
        if built is None:
            return
        base, wallet = built
        others = [a for a in accounts if a != wallet.address]
        if not others:
            return
        valid = base.__replace__(
            sponsor=choice(others), sponsor_flags=params.sponsor_reserve_flags()
        )

        def _mutate(d: dict) -> None:
            d["SponsorFlags"] = choice([0x4, 0x80000000, 0xFFFFFFFF])

        await submit_raw(name, valid, client, wallet, _mutate)
        return

    if mutation == "sponsor_equals_account":
        # xrpl-py rejects sponsor==account at construction -> raw submit for temMALFORMED.
        if built is None:
            return
        base, wallet = built
        others = [a for a in accounts if a != wallet.address]
        if not others:
            return
        valid = base.__replace__(
            sponsor=choice(others), sponsor_flags=params.sponsor_reserve_flags()
        )

        def _mutate(d: dict) -> None:
            d["Sponsor"] = d["Account"]

        await submit_raw(name, valid, client, wallet, _mutate)
        return

    if mutation == "nonexistent_sponsor":
        # Syntactically valid, unfunded AccountID -> checkSponsor's terNO_ACCOUNT.
        if built is None:
            return
        base, wallet = built
        txn = base.__replace__(
            sponsor=params.fake_account(), sponsor_flags=params.sponsor_reserve_flags()
        )
        await submit_tx(name, txn, client, wallet)
        return

    if mutation == "disallowed_type":
        # OfferCreate/NFTokenMint sit outside rippled's reserve-sponsor allow-list;
        # preflight1Sponsor runs before either type's own preflight -> temINVALID_FLAG.
        if len(accounts) < 2:
            return
        if choice([True, False]):
            src_addr, sponsor_addr = sample(list(accounts), 2)
            src = accounts[src_addr]
            txn = OfferCreate(
                account=src.address,
                taker_gets=params.offer_xrp_drops(),
                taker_pays=IOUAmount(
                    currency=params.currency_code(),
                    issuer=params.fake_account(),
                    value=params.offer_iou_value(),
                ),
                sponsor=sponsor_addr,
                sponsor_flags=params.sponsor_reserve_flags(),
            )
            wallet = src.wallet
        else:
            nft_built = _nftoken_mint_base(accounts)
            if nft_built is None:
                return
            nft_txn, wallet = nft_built
            others = [a for a in accounts if a != nft_txn.account]
            if not others:
                return
            txn = nft_txn.__replace__(
                sponsor=choice(others), sponsor_flags=params.sponsor_reserve_flags()
            )
        await submit_tx(name, txn, client, wallet)
        return

    if mutation == "delegate_combo":
        # Reserve sponsor + Delegate together -> checkSponsor's terNO_SPONSORSHIP,
        # checked before the delegate relationship itself is even validated. On the
        # (non-delegable) SignerListSet endpoint this instead trips preflight1's
        # earlier delegate gate -> temINVALID; still a reachable tem-class fault.
        if built is None:
            return
        base, wallet = built
        others = [a for a in accounts if a != wallet.address]
        if not others:
            return
        txn = base.__replace__(
            delegate=choice(others),
            sponsor=choice(others),
            sponsor_flags=params.sponsor_reserve_flags(),
        )
        await submit_tx(name, txn, client, wallet)
        return

    if mutation == "garbage_signature":
        # Sponsor co-signs with a key that doesn't match the claimed Sponsor account.
        if built is None:
            return
        base, wallet = built
        candidates = [a for a in accounts if a != wallet.address]
        if len(candidates) < 2:
            return
        sponsor_addr, wrong_addr = sample(candidates, 2)
        txn = base.__replace__(sponsor=sponsor_addr, sponsor_flags=params.sponsor_reserve_flags())
        await _submit_cosigned(name, wallet, accounts[wrong_addr].wallet, txn, client)
        return

    # prefunded_exhausted: a tracked Sponsorship with no reserve budget left still
    # gets picked without a co-sign -> tecINSUFFICIENT_RESERVE.
    if built is None:
        return
    base, wallet = built
    exhausted = [
        s
        for s in sponsorships
        if s.sponsee == wallet.address
        and s.sponsor != wallet.address
        and s.sponsor in accounts
        and s.remaining_owner_count == 0
        and not s.require_sign_for_reserve
    ]
    if not exhausted:
        return
    s = choice(exhausted)
    txn = base.__replace__(sponsor=s.sponsor, sponsor_flags=params.SPF_SPONSOR_RESERVE)
    await submit_tx(name, txn, client, wallet)


# ── Endpoints ─────────────────────────────────────────────────────────


async def sponsored_check_create(
    accounts: dict[str, UserAccount], sponsorships: list[Sponsorship], client: AsyncJsonRpcClient
) -> None:
    built = _sponsored_check_base(accounts)
    if params.should_send_faulty():
        return await _sponsored_create_faulty(
            "SponsoredCheckCreate", built, accounts, sponsorships, client
        )
    return await _sponsored_create_valid(
        "SponsoredCheckCreate", built, accounts, sponsorships, client
    )


async def sponsored_escrow_create(
    accounts: dict[str, UserAccount], sponsorships: list[Sponsorship], client: AsyncJsonRpcClient
) -> None:
    built = _sponsored_escrow_base(accounts)
    if params.should_send_faulty():
        return await _sponsored_create_faulty(
            "SponsoredEscrowCreate", built, accounts, sponsorships, client
        )
    return await _sponsored_create_valid(
        "SponsoredEscrowCreate", built, accounts, sponsorships, client
    )


async def sponsored_channel_create(
    accounts: dict[str, UserAccount], sponsorships: list[Sponsorship], client: AsyncJsonRpcClient
) -> None:
    built = _sponsored_channel_base(accounts)
    if params.should_send_faulty():
        return await _sponsored_create_faulty(
            "SponsoredPaymentChannelCreate", built, accounts, sponsorships, client
        )
    return await _sponsored_create_valid(
        "SponsoredPaymentChannelCreate", built, accounts, sponsorships, client
    )


async def sponsored_trustline_create(
    accounts: dict[str, UserAccount], sponsorships: list[Sponsorship], client: AsyncJsonRpcClient
) -> None:
    built = _sponsored_trustline_base(accounts)
    if params.should_send_faulty():
        return await _sponsored_create_faulty(
            "SponsoredTrustSet", built, accounts, sponsorships, client
        )
    return await _sponsored_create_valid("SponsoredTrustSet", built, accounts, sponsorships, client)


async def sponsored_credential_create(
    accounts: dict[str, UserAccount], sponsorships: list[Sponsorship], client: AsyncJsonRpcClient
) -> None:
    built = _sponsored_credential_base(accounts)
    if params.should_send_faulty():
        return await _sponsored_create_faulty(
            "SponsoredCredentialCreate", built, accounts, sponsorships, client
        )
    return await _sponsored_create_valid(
        "SponsoredCredentialCreate", built, accounts, sponsorships, client
    )


async def sponsored_signer_list_set(
    accounts: dict[str, UserAccount], sponsorships: list[Sponsorship], client: AsyncJsonRpcClient
) -> None:
    built = _sponsored_signer_list_base(accounts)
    if params.should_send_faulty():
        return await _sponsored_create_faulty(
            "SponsoredSignerListSet", built, accounts, sponsorships, client
        )
    return await _sponsored_create_valid(
        "SponsoredSignerListSet", built, accounts, sponsorships, client
    )


async def sponsored_deposit_preauth(
    accounts: dict[str, UserAccount], sponsorships: list[Sponsorship], client: AsyncJsonRpcClient
) -> None:
    built = _sponsored_deposit_preauth_base(accounts)
    if params.should_send_faulty():
        return await _sponsored_create_faulty(
            "SponsoredDepositPreauth", built, accounts, sponsorships, client
        )
    return await _sponsored_create_valid(
        "SponsoredDepositPreauth", built, accounts, sponsorships, client
    )


async def sponsored_mpt_authorize(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    sponsorships: list[Sponsorship],
    client: AsyncJsonRpcClient,
) -> None:
    built = _sponsored_mpt_base(accounts, mpt_issuances)
    if params.should_send_faulty():
        return await _sponsored_create_faulty(
            "SponsoredMPTokenAuthorize", built, accounts, sponsorships, client
        )
    return await _sponsored_create_valid(
        "SponsoredMPTokenAuthorize", built, accounts, sponsorships, client
    )
