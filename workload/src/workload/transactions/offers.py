"""OfferCreate / OfferCancel workload handlers."""

from __future__ import annotations

import xrpl.models
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models import IssuedCurrencyAmount as IOUAmount
from xrpl.models.currencies import IssuedCurrency, MPTCurrency
from xrpl.models.transactions import OfferCancel, OfferCreate
from xrpl.models.transactions.offer_create import OfferCreateFlag
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import AMM, TrustLine, UserAccount
from workload.randoms import choice, randint, random, sample
from workload.submit import submit_tx
from workload.transactions.amm import _find_account_with_trust_lines

# ── Helpers ─────────────────────────────────────────────────────────


def _fake_iou() -> IssuedCurrency:
    return IssuedCurrency(currency=params.currency_code(), issuer=params.fake_account())


def _non_mpt_amms(amms: list[AMM]) -> list[AMM]:
    """IOU/XRP-only AMMs; MPT pools are covered by OfferCreateMPT (mpt_dex.py)."""
    return [a for a in amms if not any(isinstance(x, MPTCurrency) for x in a.assets)]


def _random_flag() -> int:
    """OR together a random subset of OfferCreate flags (XRPL allows combining them)."""
    all_flags = [
        OfferCreateFlag.TF_PASSIVE,
        OfferCreateFlag.TF_SELL,
        OfferCreateFlag.TF_IMMEDIATE_OR_CANCEL,
        OfferCreateFlag.TF_FILL_OR_KILL,
    ]
    count = choice(range(len(all_flags) + 1))  # 0..4 flags
    if count == 0:
        return 0
    picked = sample(all_flags, count)
    result = 0
    for f in picked:
        result |= f
    return result


def _iou_amount(asset: IssuedCurrency, value: str) -> IOUAmount:
    return IOUAmount(currency=asset.currency, issuer=asset.issuer, value=value)


def _make_offer_amounts(
    amm: AMM,
) -> tuple[str | IOUAmount, str | IOUAmount] | None:
    """Build (taker_gets, taker_pays) from an AMM pair, or None if assets insufficient."""
    if len(amm.assets) < 2:
        return None
    a1, a2 = amm.assets[0], amm.assets[1]

    if random() < 0.5:
        get_asset, pay_asset = a1, a2
    else:
        get_asset, pay_asset = a2, a1

    # Offers are non-MPT: the non-XRP leg is always an issued currency.
    # Small amounts raise the chance of filling.
    if isinstance(get_asset, IssuedCurrency):
        taker_gets = _iou_amount(get_asset, str(randint(1, 50)))
    else:
        taker_gets = str(randint(100_000, 10_000_000))  # 0.1-10 XRP in drops

    if isinstance(pay_asset, IssuedCurrency):
        taker_pays = _iou_amount(pay_asset, str(randint(1, 50)))
    else:
        taker_pays = str(randint(100_000, 10_000_000))

    return taker_gets, taker_pays


def _find_account_for_amm(
    accounts: dict[str, UserAccount],
    trust_lines: list[TrustLine],
    amm: AMM,
) -> UserAccount | None:
    needed_ious = [a for a in amm.assets if isinstance(a, IssuedCurrency)]
    return _find_account_with_trust_lines(accounts, trust_lines, needed_ious)


# ── OfferCreate ─────────────────────────────────────────────────────


async def offer_create(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    trust_lines: list[TrustLine],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _offer_create_faulty(accounts, amms, trust_lines, client)
    return await _offer_create_valid(accounts, amms, trust_lines, client)


def _offer_create_base(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    trust_lines: list[TrustLine],
) -> tuple[OfferCreate, Wallet] | None:
    """Valid OfferCreate against an IOU/XRP AMM pair + wallet; shared by valid and fuzz."""
    iou_amms = _non_mpt_amms(amms)
    if not iou_amms:
        return None
    amm = choice(iou_amms)
    src = _find_account_for_amm(accounts, trust_lines, amm)
    if not src:
        return None
    pair = _make_offer_amounts(amm)
    if not pair:
        return None
    taker_gets, taker_pays = pair
    txn = OfferCreate(
        account=src.address,
        taker_gets=taker_gets,
        taker_pays=taker_pays,
        flags=_random_flag(),
    )
    return txn, src.wallet


async def _offer_create_valid(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    trust_lines: list[TrustLine],
    client: AsyncJsonRpcClient,
) -> None:
    built = _offer_create_base(accounts, amms, trust_lines)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("OfferCreate", txn, client, wallet)


async def _offer_create_faulty(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    trust_lines: list[TrustLine],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice(
        [
            "non_existent_asset",
            "same_asset_both",
            "zero_amount",
            "negative_iou_amount",
            "crossed_offer",
            "fuzz",
        ]
    )
    if mutation == "fuzz":
        built = _offer_create_base(accounts, amms, trust_lines)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("OfferCreate", base, client, wallet)
        return

    if mutation == "non_existent_asset":
        fake = _fake_iou()
        taker_gets = _iou_amount(fake, str(randint(1, 1_000)))
        taker_pays = str(randint(1_000_000, 100_000_000))
        txn = OfferCreate(
            account=src.address,
            taker_gets=taker_gets,
            taker_pays=taker_pays,
        )

    elif mutation == "same_asset_both":
        # Both sides XRP → tecINSUF_RESERVE_OFFER or malformed.
        txn = OfferCreate(
            account=src.address,
            taker_gets=str(randint(1_000_000, 100_000_000)),
            taker_pays=str(randint(1_000_000, 100_000_000)),
        )

    elif mutation == "zero_amount":
        iou_amms = _non_mpt_amms(amms)
        if not iou_amms:
            return
        amm = choice(iou_amms)
        if len(amm.assets) < 2:
            return
        a2 = amm.assets[1] if isinstance(amm.assets[0], xrpl.models.XRP) else amm.assets[0]
        if not isinstance(a2, IssuedCurrency):
            return
        taker_gets = "0"
        taker_pays = _iou_amount(a2, str(randint(1, 1_000)))
        txn = OfferCreate(
            account=src.address,
            taker_gets=taker_gets,
            taker_pays=taker_pays,
        )

    elif mutation == "negative_iou_amount":
        # Passes xrpl-py construction; rippled rejects it.
        iou_amms = _non_mpt_amms(amms)
        if not iou_amms:
            return
        amm = choice(iou_amms)
        if len(amm.assets) < 2:
            return
        a2 = amm.assets[1] if isinstance(amm.assets[0], xrpl.models.XRP) else amm.assets[0]
        if not isinstance(a2, IssuedCurrency):
            return
        taker_gets = str(randint(1_000_000, 100_000_000))
        taker_pays = _iou_amount(a2, "-1")
        txn = OfferCreate(
            account=src.address,
            taker_gets=taker_gets,
            taker_pays=taker_pays,
        )

    else:  # crossed_offer
        iou_amms = _non_mpt_amms(amms)
        if not iou_amms:
            return
        amm = choice(iou_amms)
        pair = _make_offer_amounts(amm)
        if not pair:
            return
        taker_gets, taker_pays = pair
        # Swap sides → self-crossing offer.
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
        return await _offer_cancel_faulty(accounts, offers, client)
    return await _offer_cancel_valid(accounts, offers, client)


def _offer_cancel_base(
    accounts: dict[str, UserAccount],
    offers: list[dict],
) -> tuple[OfferCancel, Wallet] | None:
    """Valid OfferCancel of a tracked resting offer + wallet; shared by valid and fuzz."""
    if not offers:
        return None
    offer = choice(offers)
    acct = accounts.get(offer["account"])
    if not acct:
        return None
    txn = OfferCancel(account=acct.address, offer_sequence=offer["sequence"])
    return txn, acct.wallet


async def _offer_cancel_valid(
    accounts: dict[str, UserAccount],
    offers: list[dict],
    client: AsyncJsonRpcClient,
) -> None:
    built = _offer_cancel_base(accounts, offers)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("OfferCancel", txn, client, wallet)


async def _offer_cancel_faulty(
    accounts: dict[str, UserAccount],
    offers: list[dict],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice(
        [
            "non_existent_sequence",
            "cancel_others_offer",
            "fuzz",
        ]
    )
    if mutation == "fuzz":
        built = _offer_cancel_base(accounts, offers)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("OfferCancel", base, client, wallet)
        return

    if mutation == "non_existent_sequence":
        txn = OfferCancel(
            account=src.address,
            offer_sequence=randint(900_000, 999_999),
        )

    else:  # cancel_others_offer
        # OfferCancel only touches the submitter's own offers, so a foreign
        # sequence is just a non-existent one from src's view.
        txn = OfferCancel(
            account=src.address,
            offer_sequence=randint(1, 100),
        )

    await submit_tx("OfferCancel", txn, client, src.wallet)
