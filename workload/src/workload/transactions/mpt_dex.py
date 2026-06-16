"""MPT-on-DEX workload handlers (XLS-82).

Two synthetic assertion names share this module; the on-ledger TransactionType
stays ``OfferCreate`` / ``Payment`` in each case. ws_listener fires the matching
``tx_result(...)`` when a validated transaction carries an MPT leg, so the
success/failure buckets for the synthetic name resolve (mirrors the
permissioned-DEX precedent).

OfferCreateMPT — an OfferCreate with an MPT leg.

Direction semantics: ``TakerGets`` is what the offer owner SELLS; ``TakerPays``
is what the owner BUYS. A buy-MPT-for-XRP offer (taker_gets=XRP, taker_pays=MPT)
needs only XRP funding so any account can place it and it rests (the book for
this MPT pair is typically empty), reliably tesSUCCESS. A sell-MPT-for-XRP offer
by the issuer is also reliable (the issuer is funding-exempt on the TakerGets
leg).

XLS-82 gates exercised by the OfferCreate faulty path: both legs must carry
lsfMPTCanTrade (else tecNO_PERMISSION); receiving a require-auth MPT the holder
was never issuer-authorized for → tecNO_AUTH; a globally-locked MPT leg →
tecLOCKED; a nonexistent issuance in TakerPays → tecNO_ISSUER (checkAcceptAsset
reads the issuer embedded in the MPTokenIssuanceID); a non-holder selling MPT it
does not hold → tecUNFUNDED_OFFER; same asset both legs → temREDUNDANT; zero MPT
value → temBAD_OFFER (preflight saTakerPays <= kZero).

PaymentMPT — a Payment carrying an MPT. The reliable valid surface is a direct
holder→holder transfer of a tradeable MPT (CanTransfer set, both parties
opted-in and funded by setup) → tesSUCCESS. With featureMPTokensV2 enabled a
direct MPT payment routes through the payment engine (MPTEndpointPaymentStep),
not the legacy V1 direct path, so its failure codes are the engine's:

* dst with no MPToken (unauthorized receiver) → tecNO_AUTH
  (MPTokenHelpers.cpp requireAuth, MPToken_test.cpp:1336-1342)
* CanTransfer unset, holder→holder → tecNO_AUTH
  (MPTokenHelpers.cpp canTransfer, MPToken_test.cpp:1321)
* require-auth MPT to an unauthorized holder → tecNO_AUTH (requireAuth)
* globally/individually locked, holder→holder → tecPATH_DRY under MPTokensV2
  (terLOCKED from MPTEndpointStep::check is a retry code mapped to tecPATH_DRY
  in Payment::doApply; MPToken_test.cpp:1379-1385 — NOT tecLOCKED)
* deliver more than the source holds → tecPATH_PARTIAL
  (Payment::doApply / MPToken_test.cpp:1359-1362)
* nonexistent issuance → tecOBJECT_NOT_FOUND (requireAuth reads the issuance
  keylet; MPToken_test.cpp:1656,1685)
* zero/negative MPT Amount → temBAD_AMOUNT (preflight dstAmount <= kZero;
  MPToken_test.cpp:1025-1029)

A cross-token best-effort variant (SendMax in XRP, deliver MPT) routes through
the order book and ends tecPATH_DRY with no resting liquidity
(MPToken_test.cpp:4862 family); the direct path guarantees the success bucket,
so PaymentMPT is NOT in assertions._NO_SUCCESS_TYPES.
"""

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


def _no_transfer(mpts: list[MPTokenIssuance]) -> list[MPTokenIssuance]:
    """Tradeable issuances missing lsfMPTCanTransfer (the idx3 setup cohort):
    holders are opted-in and funded, but a holder→holder transfer fails
    canTransfer → tecNO_AUTH. CanTrade is set so they are not in _no_trade."""
    return [
        m
        for m in mpts
        if m.can_trade and m.can_transfer is False and m.require_auth is False and m.locked is False
    ]


def _controlled_holders(mpt: MPTokenIssuance, accounts: dict[str, UserAccount]) -> list[str]:
    """Holders of ``mpt`` that we control (and so are funded by setup)."""
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
    """Build a reliable direct holder→holder MPT transfer + its wallet. Shared
    by the valid and fuzz paths.

    Picks a tradeable issuance with at least two controlled (hence funded)
    holders and moves a modest value (<=1000, well within the 10000 setup
    distribution) between them → reliable tesSUCCESS. No SendMax, no Paths."""
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
    # ~30%: a cross-token best-effort routed payment (sender pays XRP via
    # SendMax, delivers MPT through the order book). Usually tecPATH_DRY with no
    # resting liquidity — that exercises the routing/step code; the direct path
    # below guarantees the success bucket.
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

    # Conditional mutation list: include a vector only when its cohort + the
    # accounts it needs exist. fake_mpt builds its own Payment from scratch, so
    # it is always available; zero_amount and fuzz need a buildable base.
    mutations = ["fake_mpt"]
    if base is not None:
        mutations += ["zero_amount", "fuzz"]
    # dest_not_authorized needs a tradeable MPT with a controlled holder.
    if any(_controlled_holders(m, accounts) for m in tr):
        mutations.append("dest_not_authorized")
    # overdraw needs >=2 controlled holders so the dst is authorized and the
    # failure is the funds check (tecPATH_PARTIAL), not the receiver auth check.
    if any(len(_controlled_holders(m, accounts)) >= 2 for m in tr):
        mutations.append("overdraw")
    # no_transfer needs >=2 controlled holders (neither is the issuer by cohort).
    if any(len(_controlled_holders(m, accounts)) >= 2 for m in nx):
        mutations.append("no_transfer")
    # locked needs >=2 controlled holders (funded pre-lock).
    if any(len(_controlled_holders(m, accounts)) >= 2 for m in lk):
        mutations.append("locked")
    # require_auth needs the issuer (controlled) + an unauthorized non-issuer dst.
    if any(m.issuer in accounts and len(accounts) >= 2 for m in ra):
        mutations.append("require_auth")
    mutation = choice(mutations)

    if mutation == "fuzz":
        # Generative: corrupt a valid direct MPT payment in open-ended ways.
        b, wallet = base  # type: ignore[misc]
        await submit_fuzzed("PaymentMPT", b, client, wallet)
        return

    if mutation == "fake_mpt":
        # Deliver a nonexistent issuance -> tecOBJECT_NOT_FOUND (requireAuth
        # reads the issuance keylet; MPToken_test.cpp:1656,1685). fake_mpt_id is
        # well-formed (random sequence + random issuer), not the bad-asset
        # sentinel that would yield temBAD_CURRENCY.
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
        # Deliver a tradeable MPT to a dst that holds no MPToken -> tecNO_AUTH
        # (requireAuth on the receiver; MPToken_test.cpp:1336-1342). src is a
        # controlled (funded) holder of that issuance.
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
        # CanTransfer unset, holder->holder where neither is the issuer
        # -> tecNO_AUTH (canTransfer; MPToken_test.cpp:1321).
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
        # Globally-locked MPT, holder->holder (both funded pre-lock)
        # -> tecPATH_DRY under MPTokensV2 (terLOCKED retry-mapped in
        # Payment::doApply; MPToken_test.cpp:1379-1385 — NOT tecLOCKED).
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
        # Issuer delivers a require-auth MPT to a holder that was never
        # issuer-authorized -> tecNO_AUTH (requireAuth on the receiver). Our
        # setup never issuer-authorizes require-auth cohort holders.
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
        # Deliver far more than the source holds, holder->holder (both authorized)
        # -> tecPATH_PARTIAL (Payment::doApply; MPToken_test.cpp:1359-1362). The
        # floor (>10x the 10000 setup distribution) exceeds any balance the
        # source could plausibly accumulate from modest transfers in a session.
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

    # zero_amount — set the MPT Amount's value to "0" -> temBAD_AMOUNT (preflight
    # dstAmount <= kZero; MPToken_test.cpp:1025-1029). xrpl-py accepts
    # MPTAmount(value="0"), so the zero is injected via the raw path and
    # rippled preflight is what rejects it. Only in the list when base is built.
    b, wallet = base  # type: ignore[misc]

    def mutate_zero(d: dict) -> None:
        v = d.get("Amount")
        if isinstance(v, dict) and "mpt_issuance_id" in v:
            v["value"] = "0"

    await submit_raw("PaymentMPT", b, client, wallet, mutate_zero)
