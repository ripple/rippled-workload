"""MPT-on-DEX (XLS-82): OfferCreateMPT, PaymentMPT submitted as OfferCreate/Payment."""

from __future__ import annotations

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.amounts import MPTAmount
from xrpl.models.transactions import OfferCreate, Payment
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import MPTokenIssuance, UserAccount
from workload.randoms import choice, randint, random, sample
from workload.submit import submit_raw, submit_tx

# ── Cohort filter helpers ───────────────────────────────────────────


def _tradeable(mpts: list[MPTokenIssuance]) -> list[MPTokenIssuance]:
    """Usable on both offer legs: CanTrade + CanTransfer, not require-auth, not locked."""
    return [
        m
        for m in mpts
        if m.can_trade and m.can_transfer and m.require_auth is False and m.locked is False
    ]


def _no_trade(mpts: list[MPTokenIssuance]) -> list[MPTokenIssuance]:
    """Missing CanTrade → offer leg gets tecNO_PERMISSION."""
    return [m for m in mpts if m.can_trade is False]


def _require_auth(mpts: list[MPTokenIssuance]) -> list[MPTokenIssuance]:
    """Require-auth, holders never authorized → receiving gets tecNO_AUTH."""
    return [m for m in mpts if m.require_auth and not m.locked]


def _locked(mpts: list[MPTokenIssuance]) -> list[MPTokenIssuance]:
    """Globally-locked → offer leg gets tecLOCKED."""
    return [m for m in mpts if m.locked]


def _no_transfer(mpts: list[MPTokenIssuance]) -> list[MPTokenIssuance]:
    """CanTrade but not CanTransfer → holder→holder transfer fails canTransfer (tecNO_AUTH)."""
    return [
        m
        for m in mpts
        if m.can_trade and m.can_transfer is False and m.require_auth is False and m.locked is False
    ]


def _controlled_holders(mpt: MPTokenIssuance, accounts: dict[str, UserAccount]) -> list[str]:
    """Holders we control (hence funded by setup)."""
    return [h for h in mpt.holders if h in accounts]


# ── Dispatch ────────────────────────────────────────────────────────


async def offer_create_mpt(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _offer_create_mpt_faulty(accounts, mpt_issuances, client)
    return await _offer_create_mpt_valid(accounts, mpt_issuances, client)


def _offer_mpt_base(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
) -> tuple[OfferCreate, Wallet] | None:
    """Valid MPT offer + wallet; shared by valid and fuzz. ~40% issuer sells
    (funding-exempt), else any account buys MPT for XRP (rests in empty book)."""
    tr = _tradeable(mpt_issuances)
    if not tr or not accounts:
        return None
    mpt = choice(tr)
    mpt_amt = MPTAmount(mpt_issuance_id=mpt.mpt_issuance_id, value=params.mpt_offer_value())
    xrp = params.offer_xrp_drops()
    if random() < 0.4 and mpt.issuer in accounts:
        issuer = accounts[mpt.issuer]
        base = OfferCreate(account=issuer.address, taker_gets=mpt_amt, taker_pays=xrp)
        return base, issuer.wallet
    acct = accounts[choice(list(accounts))]
    base = OfferCreate(account=acct.address, taker_gets=xrp, taker_pays=mpt_amt)
    return base, acct.wallet


async def _offer_create_mpt_valid(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    built = _offer_mpt_base(accounts, mpt_issuances)
    if built is None:
        return
    base, wallet = built
    await submit_tx("OfferCreateMPT", base, client, wallet)


async def _offer_create_mpt_faulty(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    tr = _tradeable(mpt_issuances)
    nt = _no_trade(mpt_issuances)
    ra = _require_auth(mpt_issuances)
    lk = _locked(mpt_issuances)
    xrp = params.offer_xrp_drops()

    # Include a vector only when its cohort + accounts exist; fake_mpt always available.
    mutations = ["fake_mpt"]
    if tr:
        mutations += ["fuzz", "redundant", "zero_amount"]
        # unfunded_sell needs a non-issuer non-holder (provably zero balance).
        if any(any(a != m.issuer and a not in m.holders for a in accounts) for m in tr):
            mutations.append("unfunded_sell")
    if nt:
        mutations.append("not_tradeable")
    if ra:
        mutations.append("require_auth")
    if lk:
        mutations.append("locked")
    mutation = choice(mutations)

    if mutation == "fuzz":
        built = _offer_mpt_base(accounts, mpt_issuances)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("OfferCreateMPT", base, client, wallet)
        return

    if mutation == "not_tradeable":
        # Buy a no-CanTrade MPT -> tecNO_PERMISSION.
        mpt = choice(nt)
        acct = accounts[choice(list(accounts))]
        base = OfferCreate(
            account=acct.address,
            taker_gets=xrp,
            taker_pays=MPTAmount(
                mpt_issuance_id=mpt.mpt_issuance_id, value=params.mpt_offer_value()
            ),
        )
        await submit_raw("OfferCreateMPT", base, client, acct.wallet)
        return

    if mutation == "fake_mpt":
        # Buy a nonexistent issuance -> tecNO_ISSUER (checkAcceptAsset reads the
        # issuer embedded in the fake ID before canTrade).
        acct = accounts[choice(list(accounts))]
        base = OfferCreate(
            account=acct.address,
            taker_gets=xrp,
            taker_pays=MPTAmount(
                mpt_issuance_id=params.fake_mpt_id(), value=params.mpt_offer_value()
            ),
        )
        await submit_raw("OfferCreateMPT", base, client, acct.wallet)
        return

    if mutation == "unfunded_sell":
        # Non-holder sells a tradeable MPT it does not hold -> tecUNFUNDED_OFFER.
        mpt = choice(tr)
        non_holders = [a for a in accounts if a != mpt.issuer and a not in mpt.holders]
        if not non_holders:
            return
        acct = accounts[choice(non_holders)]
        base = OfferCreate(
            account=acct.address,
            taker_gets=MPTAmount(
                mpt_issuance_id=mpt.mpt_issuance_id, value=params.mpt_offer_value()
            ),
            taker_pays=xrp,
        )
        await submit_raw("OfferCreateMPT", base, client, acct.wallet)
        return

    if mutation == "require_auth":
        # Non-issuer buys a require-auth MPT it was never authorized for ->
        # tecNO_AUTH. Issuer is exempt (would rest tesSUCCESS), so exclude it.
        mpt = choice(ra)
        buyers = [a for a in accounts if a != mpt.issuer]
        if not buyers:
            return
        acct = accounts[choice(buyers)]
        base = OfferCreate(
            account=acct.address,
            taker_gets=xrp,
            taker_pays=MPTAmount(
                mpt_issuance_id=mpt.mpt_issuance_id, value=params.mpt_offer_value()
            ),
        )
        await submit_raw("OfferCreateMPT", base, client, acct.wallet)
        return

    if mutation == "locked":
        # Buy a globally-locked MPT -> tecLOCKED.
        mpt = choice(lk)
        acct = accounts[choice(list(accounts))]
        base = OfferCreate(
            account=acct.address,
            taker_gets=xrp,
            taker_pays=MPTAmount(
                mpt_issuance_id=mpt.mpt_issuance_id, value=params.mpt_offer_value()
            ),
        )
        await submit_raw("OfferCreateMPT", base, client, acct.wallet)
        return

    if mutation == "redundant":
        # Same MPT on both legs -> temREDUNDANT.
        mpt = choice(tr)
        acct = accounts[choice(list(accounts))]
        base = OfferCreate(
            account=acct.address,
            taker_gets=xrp,
            taker_pays=MPTAmount(
                mpt_issuance_id=mpt.mpt_issuance_id, value=params.mpt_offer_value()
            ),
        )

        def mutate(d: dict) -> None:
            d["TakerGets"] = d["TakerPays"]

        await submit_raw("OfferCreateMPT", base, client, acct.wallet, mutate)
        return

    # zero_amount — MPT leg value "0" -> temBAD_OFFER. xrpl-py accepts value="0",
    # so the zero goes via the raw path for rippled preflight to reject.
    mpt = choice(tr)
    acct = accounts[choice(list(accounts))]
    base = OfferCreate(
        account=acct.address,
        taker_gets=xrp,
        taker_pays=MPTAmount(mpt_issuance_id=mpt.mpt_issuance_id, value=params.mpt_offer_value()),
    )

    def mutate_zero(d: dict) -> None:
        for leg in ("TakerGets", "TakerPays"):
            v = d.get(leg)
            if isinstance(v, dict) and "mpt_issuance_id" in v:
                v["value"] = "0"

    await submit_raw("OfferCreateMPT", base, client, acct.wallet, mutate_zero)


# ── PaymentMPT — a Payment carrying an MPT ──────────────────────────


async def payment_mpt(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _payment_mpt_faulty(accounts, mpt_issuances, client)
    return await _payment_mpt_valid(accounts, mpt_issuances, client)


def _payment_mpt_base(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
) -> tuple[Payment, Wallet] | None:
    """Reliable direct holder→holder MPT transfer + wallet; shared by valid and fuzz.
    Tradeable issuance with ≥2 controlled holders, modest value -> reliable tesSUCCESS."""
    tr = _tradeable(mpt_issuances)
    if not tr or not accounts:
        return None
    candidates = [m for m in tr if len(_controlled_holders(m, accounts)) >= 2]
    if not candidates:
        return None
    mpt = choice(candidates)
    src_id, dst_id = sample(_controlled_holders(mpt, accounts), 2)
    src = accounts[src_id]
    base = Payment(
        account=src.address,
        destination=dst_id,
        amount=MPTAmount(mpt_issuance_id=mpt.mpt_issuance_id, value=params.mpt_offer_value()),
    )
    return base, src.wallet


async def _payment_mpt_valid(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    built = _payment_mpt_base(accounts, mpt_issuances)
    # ~30%: cross-token routed payment (XRP via SendMax, deliver MPT through the
    # book). Usually tecPATH_DRY; the direct path below guarantees the success bucket.
    if built is not None and random() < 0.3:
        tr = _tradeable(mpt_issuances)
        routable = [m for m in tr if _controlled_holders(m, accounts)]
        if routable:
            mpt = choice(routable)
            src = accounts[choice(_controlled_holders(mpt, accounts))]
            dst_id = choice([a for a in accounts if a != src.address])
            xc = Payment(
                account=src.address,
                destination=dst_id,
                amount=MPTAmount(
                    mpt_issuance_id=mpt.mpt_issuance_id, value=params.mpt_offer_value()
                ),
                send_max=params.offer_xrp_drops(),
            )
            await submit_tx("PaymentMPT", xc, client, src.wallet)
            return
    if built is None:
        return
    base, wallet = built
    await submit_tx("PaymentMPT", base, client, wallet)


async def _payment_mpt_faulty(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    tr = _tradeable(mpt_issuances)
    ra = _require_auth(mpt_issuances)
    lk = _locked(mpt_issuances)
    nx = _no_transfer(mpt_issuances)
    base = _payment_mpt_base(accounts, mpt_issuances)

    # Include a vector only when its cohort + accounts exist. fake_mpt builds its
    # own Payment so it is always available; zero_amount/fuzz need a buildable base.
    mutations = ["fake_mpt"]
    if base is not None:
        mutations += ["zero_amount", "fuzz"]
    if any(_controlled_holders(m, accounts) for m in tr):
        mutations.append("dest_not_authorized")
    # overdraw needs >=2 controlled holders so dst is authorized and the failure
    # is the funds check (tecPATH_PARTIAL), not the receiver auth check.
    if any(len(_controlled_holders(m, accounts)) >= 2 for m in tr):
        mutations.append("overdraw")
    if any(len(_controlled_holders(m, accounts)) >= 2 for m in nx):
        mutations.append("no_transfer")
    if any(len(_controlled_holders(m, accounts)) >= 2 for m in lk):
        mutations.append("locked")
    # require_auth needs the issuer + an unauthorized non-issuer dst.
    if any(m.issuer in accounts and len(accounts) >= 2 for m in ra):
        mutations.append("require_auth")
    mutation = choice(mutations)

    if mutation == "fuzz":
        b, wallet = base  # type: ignore[misc]
        await submit_fuzzed("PaymentMPT", b, client, wallet)
        return

    if mutation == "fake_mpt":
        # Deliver a nonexistent issuance -> tecOBJECT_NOT_FOUND. fake_mpt_id is
        # well-formed, not the bad-asset sentinel that yields temBAD_CURRENCY.
        src_id, dst_id = sample(list(accounts), 2)
        src = accounts[src_id]
        txn = Payment(
            account=src.address,
            destination=dst_id,
            amount=MPTAmount(mpt_issuance_id=params.fake_mpt_id(), value=params.mpt_offer_value()),
        )
        await submit_raw("PaymentMPT", txn, client, src.wallet)
        return

    if mutation == "dest_not_authorized":
        # Deliver to a dst that holds no MPToken -> tecNO_AUTH (requireAuth on
        # the receiver). src is a controlled holder.
        usable = [m for m in tr if _controlled_holders(m, accounts)]
        mpt = choice(usable)
        holders = _controlled_holders(mpt, accounts)
        src = accounts[choice(holders)]
        outsiders = [a for a in accounts if a not in set(mpt.holders) and a != src.address]
        if not outsiders:
            return
        dst_id = choice(outsiders)
        txn = Payment(
            account=src.address,
            destination=dst_id,
            amount=MPTAmount(mpt_issuance_id=mpt.mpt_issuance_id, value=params.mpt_offer_value()),
        )
        await submit_raw("PaymentMPT", txn, client, src.wallet)
        return

    if mutation == "no_transfer":
        # CanTransfer unset, holder->holder, neither the issuer -> tecNO_AUTH.
        usable = [m for m in nx if len(_controlled_holders(m, accounts)) >= 2]
        mpt = choice(usable)
        src_id, dst_id = sample(_controlled_holders(mpt, accounts), 2)
        src = accounts[src_id]
        txn = Payment(
            account=src.address,
            destination=dst_id,
            amount=MPTAmount(mpt_issuance_id=mpt.mpt_issuance_id, value=params.mpt_offer_value()),
        )
        await submit_raw("PaymentMPT", txn, client, src.wallet)
        return

    if mutation == "locked":
        # Globally-locked, holder->holder -> tecPATH_DRY under MPTokensV2
        # (terLOCKED retry-mapped in Payment::doApply — NOT tecLOCKED).
        usable = [m for m in lk if len(_controlled_holders(m, accounts)) >= 2]
        mpt = choice(usable)
        src_id, dst_id = sample(_controlled_holders(mpt, accounts), 2)
        src = accounts[src_id]
        txn = Payment(
            account=src.address,
            destination=dst_id,
            amount=MPTAmount(mpt_issuance_id=mpt.mpt_issuance_id, value=params.mpt_offer_value()),
        )
        await submit_raw("PaymentMPT", txn, client, src.wallet)
        return

    if mutation == "require_auth":
        # Issuer delivers a require-auth MPT to a never-authorized holder ->
        # tecNO_AUTH. Setup never authorizes require-auth cohort holders.
        usable = [m for m in ra if m.issuer in accounts]
        mpt = choice(usable)
        src = accounts[mpt.issuer]
        dst_choices = [a for a in accounts if a != mpt.issuer]
        if not dst_choices:
            return
        dst_id = choice(dst_choices)
        txn = Payment(
            account=src.address,
            destination=dst_id,
            amount=MPTAmount(mpt_issuance_id=mpt.mpt_issuance_id, value=params.mpt_offer_value()),
        )
        await submit_raw("PaymentMPT", txn, client, src.wallet)
        return

    if mutation == "overdraw":
        # Deliver more than the source holds (both authorized) -> tecPATH_PARTIAL.
        # Floor >10x the setup distribution exceeds any plausible session balance.
        usable = [m for m in tr if len(_controlled_holders(m, accounts)) >= 2]
        mpt = choice(usable)
        src_id, dst_id = sample(_controlled_holders(mpt, accounts), 2)
        src = accounts[src_id]
        txn = Payment(
            account=src.address,
            destination=dst_id,
            amount=MPTAmount(
                mpt_issuance_id=mpt.mpt_issuance_id, value=str(randint(100001, 10**9))
            ),
        )
        await submit_raw("PaymentMPT", txn, client, src.wallet)
        return

    # zero_amount — MPT Amount value "0" -> temBAD_AMOUNT. xrpl-py accepts
    # value="0", so the zero goes via the raw path for rippled preflight to reject.
    b, wallet = base  # type: ignore[misc]

    def mutate_zero(d: dict) -> None:
        v = d.get("Amount")
        if isinstance(v, dict) and "mpt_issuance_id" in v:
            v["value"] = "0"

    await submit_raw("PaymentMPT", b, client, wallet, mutate_zero)
