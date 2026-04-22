"""OfferCreate / OfferCancel workload handlers.

Creates DEX offers that trade through AMM pools, and cancels existing offers.
"""

from __future__ import annotations

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models import IssuedCurrencyAmount as IOUAmount
from xrpl.models.currencies import IssuedCurrency
from xrpl.models.transactions import OfferCreate, OfferCancel
from xrpl.models.transactions.offer_create import OfferCreateFlag

import xrpl.models

from workload.models import AMM, TrustLine, UserAccount
from workload.randoms import choice, randint, random
from workload.submit import submit_tx
from workload import params
from workload.transactions.amm import _find_account_with_trust_lines

# ── Helpers ─────────────────────────────────────────────────────────


def _fake_iou() -> IssuedCurrency:
    return IssuedCurrency(currency=params.currency_code(), issuer=params.fake_account())


def _random_flag() -> int:
    """Pick a random OfferCreate flag or no flag."""
    flags = [0, OfferCreateFlag.TF_PASSIVE, OfferCreateFlag.TF_SELL,
             OfferCreateFlag.TF_IMMEDIATE_OR_CANCEL, OfferCreateFlag.TF_FILL_OR_KILL]
    return choice(flags)


def _iou_amount(asset: IssuedCurrency, value: str) -> IOUAmount:
    return IOUAmount(currency=asset.currency, issuer=asset.issuer, value=value)


def _make_offer_amounts(
    amm: AMM,
) -> tuple[str | IOUAmount, str | IOUAmount] | None:
    """Build taker_gets / taker_pays from an AMM's asset pair.

    Randomly picks direction: buy asset1 with asset2, or vice versa.
    Returns (taker_gets, taker_pays) or None if assets are insufficient.
    """
    if len(amm.assets) < 2:
        return None
    a1, a2 = amm.assets[0], amm.assets[1]

    # Randomly pick direction
    if random() < 0.5:
        get_asset, pay_asset = a1, a2
    else:
        get_asset, pay_asset = a2, a1

    # Build amounts — use small amounts to increase chance of filling
    if isinstance(get_asset, xrpl.models.XRP):
        taker_gets = str(randint(100_000, 10_000_000))  # 0.1-10 XRP in drops
    else:
        taker_gets = _iou_amount(get_asset, str(randint(1, 50)))

    if isinstance(pay_asset, xrpl.models.XRP):
        taker_pays = str(randint(100_000, 10_000_000))
    else:
        taker_pays = _iou_amount(pay_asset, str(randint(1, 50)))

    return taker_gets, taker_pays


def _find_account_for_amm(
    accounts: dict[str, UserAccount],
    trust_lines: list[TrustLine],
    amm: AMM,
) -> UserAccount | None:
    """Find an account that has trust lines for the AMM's IOU assets."""
    needed_ious = [a for a in amm.assets if not isinstance(a, xrpl.models.XRP)]
    return _find_account_with_trust_lines(accounts, trust_lines, needed_ious)


# ── OfferCreate ─────────────────────────────────────────────────────


async def offer_create(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    offers: list[dict],
    trust_lines: list[TrustLine],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _offer_create_faulty(accounts, amms, trust_lines, client)
    return await _offer_create_valid(accounts, amms, trust_lines, client)


async def _offer_create_valid(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    trust_lines: list[TrustLine],
    client: AsyncJsonRpcClient,
) -> None:
    if not amms:
        return

    amm = choice(amms)
    src = _find_account_for_amm(accounts, trust_lines, amm)
    if not src:
        return

    pair = _make_offer_amounts(amm)
    if not pair:
        return
    taker_gets, taker_pays = pair
    flag = _random_flag()
    txn = OfferCreate(
        account=src.address,
        taker_gets=taker_gets,
        taker_pays=taker_pays,
        flags=flag,
    )
    await submit_tx("OfferCreate", txn, client, src.wallet)


async def _offer_create_faulty(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    trust_lines: list[TrustLine],
    client: AsyncJsonRpcClient,
) -> None:
    src = choice(list(accounts.values()))

    mutation = choice([
        "non_existent_asset", "same_asset_both", "zero_amount",
        "negative_iou_amount", "crossed_offer",
    ])
    if mutation == "non_existent_asset":
        # Offer with a fake IOU nobody has issued
        fake = _fake_iou()
        taker_gets = _iou_amount(fake, str(randint(1, 1_000)))
        taker_pays = str(randint(1_000_000, 100_000_000))
        txn = OfferCreate(
            account=src.address,
            taker_gets=taker_gets,
            taker_pays=taker_pays,
        )

    elif mutation == "same_asset_both":
        # Both sides are XRP (tecINSUF_RESERVE_OFFER or malformed)
        txn = OfferCreate(
            account=src.address,
            taker_gets=str(randint(1_000_000, 100_000_000)),
            taker_pays=str(randint(1_000_000, 100_000_000)),
        )

    elif mutation == "zero_amount":
        # Zero taker_gets or taker_pays
        if not amms:
            return
        amm = choice(amms)
        if len(amm.assets) < 2:
            return
        a2 = amm.assets[1] if isinstance(amm.assets[0], xrpl.models.XRP) else amm.assets[0]
        taker_gets = "0"
        taker_pays = _iou_amount(a2, str(randint(1, 1_000)))
        txn = OfferCreate(
            account=src.address,
            taker_gets=taker_gets,
            taker_pays=taker_pays,
        )

    elif mutation == "negative_iou_amount":
        # Negative IOU amount — passes xrpl-py, rejected by rippled
        if not amms:
            return
        amm = choice(amms)
        if len(amm.assets) < 2:
            return
        a2 = amm.assets[1] if isinstance(amm.assets[0], xrpl.models.XRP) else amm.assets[0]
        taker_gets = str(randint(1_000_000, 100_000_000))
        taker_pays = _iou_amount(a2, "-1")
        txn = OfferCreate(
            account=src.address,
            taker_gets=taker_gets,
            taker_pays=taker_pays,
        )

    else:  # crossed_offer
        # Create an offer that crosses itself (same account, opposite direction)
        if not amms:
            return
        amm = choice(amms)
        pair = _make_offer_amounts(amm)
        if not pair:
            return
        taker_gets, taker_pays = pair
        # Swap to create a self-crossing offer
        txn = OfferCreate(
            account=src.address,
            taker_gets=taker_pays,
            taker_pays=taker_gets,
        )

    await submit_tx("OfferCreate", txn, client, src.wallet)


# ── OfferCancel ─────────────────────────────────────────────────────


async def offer_cancel(
    accounts: dict[str, UserAccount],
    offers: list[dict],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _offer_cancel_faulty(accounts, client)
    return await _offer_cancel_valid(accounts, offers, client)


async def _offer_cancel_valid(
    accounts: dict[str, UserAccount],
    offers: list[dict],
    client: AsyncJsonRpcClient,
) -> None:
    if not offers:
        return
    offer = choice(offers)
    acct = accounts.get(offer["account"])
    if not acct:
        return

    txn = OfferCancel(
        account=acct.address,
        offer_sequence=offer["sequence"],
    )
    await submit_tx("OfferCancel", txn, client, acct.wallet)


async def _offer_cancel_faulty(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    src = choice(list(accounts.values()))

    mutation = choice([
        "non_existent_sequence", "cancel_others_offer",
    ])

    if mutation == "non_existent_sequence":
        # Cancel an offer that doesn't exist
        txn = OfferCancel(
            account=src.address,
            offer_sequence=randint(900_000, 999_999),
        )

    else:  # cancel_others_offer
        # Try to cancel someone else's offer (wrong account)
        txn = OfferCancel(
            account=src.address,
            offer_sequence=randint(1, 100),
        )

    await submit_tx("OfferCancel", txn, client, src.wallet)
