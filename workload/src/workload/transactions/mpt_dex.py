"""MPT-on-DEX workload handlers (XLS-82).

OfferCreate with an MPT leg. Registered under the synthetic assertion name
``OfferCreateMPT``; the on-ledger TransactionType stays ``OfferCreate``.
ws_listener fires the matching ``tx_result("OfferCreateMPT", ...)`` when a
validated OfferCreate carries an MPT leg, so the success/failure buckets for
the synthetic name resolve (mirrors the permissioned-DEX precedent).

Direction semantics: ``TakerGets`` is what the offer owner SELLS; ``TakerPays``
is what the owner BUYS. A buy-MPT-for-XRP offer (taker_gets=XRP, taker_pays=MPT)
needs only XRP funding so any account can place it and it rests (the book for
this MPT pair is typically empty), reliably tesSUCCESS. A sell-MPT-for-XRP offer
by the issuer is also reliable (the issuer is funding-exempt on the TakerGets
leg).

XLS-82 gates exercised by the faulty path: both legs must carry lsfMPTCanTrade
(else tecNO_PERMISSION); receiving a require-auth MPT the holder was never
issuer-authorized for → tecNO_AUTH; a globally-locked MPT leg → tecLOCKED; a
nonexistent issuance in TakerPays → tecNO_ISSUER (checkAcceptAsset reads the
issuer embedded in the MPTokenIssuanceID); a non-holder selling MPT it does not
hold → tecUNFUNDED_OFFER; same asset both legs → temREDUNDANT; zero MPT value →
temBAD_OFFER (preflight saTakerPays <= kZero).
"""

from __future__ import annotations

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.amounts import MPTAmount
from xrpl.models.transactions import OfferCreate
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import MPTokenIssuance, UserAccount
from workload.randoms import choice, random
from workload.submit import submit_raw, submit_tx

# ── Cohort filter helpers ───────────────────────────────────────────
# Defensively skip ``locked`` wherever a usable token is required.


def _tradeable(mpts: list[MPTokenIssuance]) -> list[MPTokenIssuance]:
    """Issuances usable on both legs of a resting offer: CanTrade + CanTransfer,
    not require-auth (our setup never issuer-authorizes), not locked."""
    return [
        m
        for m in mpts
        if m.can_trade and m.can_transfer and m.require_auth is False and m.locked is False
    ]


def _no_trade(mpts: list[MPTokenIssuance]) -> list[MPTokenIssuance]:
    """Issuances missing lsfMPTCanTrade → an offer leg referencing them gets
    tecNO_PERMISSION."""
    return [m for m in mpts if m.can_trade is False]


def _require_auth(mpts: list[MPTokenIssuance]) -> list[MPTokenIssuance]:
    """Require-auth issuances (still CanTrade in our setup) whose holders were
    never issuer-authorized → receiving them gets tecNO_AUTH."""
    return [m for m in mpts if m.require_auth and not m.locked]


def _locked(mpts: list[MPTokenIssuance]) -> list[MPTokenIssuance]:
    """Globally-locked issuances → an offer leg referencing them gets tecLOCKED."""
    return [m for m in mpts if m.locked]


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
    """Build a valid MPT offer + its signing wallet. Shared by valid and fuzz.

    ~40%: if we control the issuer, the issuer SELLS the MPT for XRP
    (funding-exempt). Otherwise any account BUYS the MPT for XRP (needs only
    XRP funding, rests in an empty book)."""
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

    # Build the conditional mutation list: only include a vector when its cohort
    # (and the accounts it needs) exists. fake_mpt is always available.
    mutations = ["fake_mpt"]
    if tr:
        mutations += ["fuzz", "redundant", "zero_amount"]
        # unfunded_sell needs a tradeable MPT with at least one account that is
        # neither the issuer nor a tracked holder (so its balance is provably 0).
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
        # Generative: corrupt a valid MPT offer in open-ended ways.
        built = _offer_mpt_base(accounts, mpt_issuances)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("OfferCreateMPT", base, client, wallet)
        return

    if mutation == "not_tradeable":
        # Any account BUYS a no-CanTrade MPT -> tecNO_PERMISSION.
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
        # Any account BUYS a nonexistent MPT issuance -> tecNO_ISSUER.
        # checkAcceptAsset reads the issuer embedded in the fake MPTokenIssuanceID
        # (its trailing 20 bytes) and rejects before canTrade is reached.
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
        # A non-holder SELLS a tradeable MPT it does not hold -> tecUNFUNDED_OFFER.
        # The seller is neither the issuer (funding-exempt) nor a tracked holder,
        # so its balance is provably zero — any positive value suffices.
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
        # A non-issuer BUYS a require-auth MPT it was never authorized for
        # -> tecNO_AUTH (receiving the MPT runs requireAuth). The issuer is
        # exempt and would rest as tesSUCCESS, so exclude it.
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
        # Any account BUYS a globally-locked MPT -> tecLOCKED
        # (MPT path of checkGlobalFrozen).
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
        # Same MPT on both legs -> temREDUNDANT. Build a valid BUY base, then
        # copy the MPT TakerPays onto TakerGets.
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

    # zero_amount — set the MPT leg's value to "0" -> temBAD_OFFER (preflight
    # saTakerPays <= beast::kZero). xrpl-py accepts MPTAmount(value="0"), so the
    # zero is injected via the raw path and rippled preflight is what rejects it.
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
