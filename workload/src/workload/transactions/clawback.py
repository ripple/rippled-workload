"""Clawback workload handler — IOUs and MPTs."""

from __future__ import annotations

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.amounts import IssuedCurrencyAmount, MPTAmount
from xrpl.models.transactions import Clawback
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import MPTokenIssuance, TrustLine, UserAccount
from workload.randoms import choice
from workload.submit import submit_tx


async def clawback(
    accounts: dict[str, UserAccount],
    trust_lines: list[TrustLine],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _clawback_faulty(accounts, trust_lines, mpt_issuances, client)
    return await _clawback_valid(accounts, trust_lines, mpt_issuances, client)


def _clawback_base(
    accounts: dict[str, UserAccount],
    trust_lines: list[TrustLine],
    mpt_issuances: list[MPTokenIssuance],
) -> tuple[Clawback, Wallet] | None:
    """Valid Clawback (issuer claws back an IOU or MPT from a holder) + wallet."""
    if not accounts:
        return None

    can_iou = bool(trust_lines)
    can_mpt = bool(mpt_issuances)
    if not can_iou and not can_mpt:
        return None

    flavours = []
    if can_iou:
        flavours.append("iou")
    if can_mpt:
        flavours.append("mpt")
    flavour = choice(flavours)

    if flavour == "iou":
        return _clawback_iou_base(accounts, trust_lines)
    return _clawback_mpt_base(accounts, mpt_issuances)


def _clawback_iou_base(
    accounts: dict[str, UserAccount],
    trust_lines: list[TrustLine],
) -> tuple[Clawback, Wallet] | None:
    tl = choice(trust_lines)
    # account_b is the gateway/issuer, account_a is the holder
    gateway = accounts.get(tl.account_b)
    if not gateway:
        return None

    txn = Clawback(
        account=gateway.address,
        amount=IssuedCurrencyAmount(
            currency=tl.currency,
            issuer=tl.account_a,  # holder
            value=params.clawback_iou_amount(),
        ),
    )
    return txn, gateway.wallet


def _clawback_mpt_base(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
) -> tuple[Clawback, Wallet] | None:
    mpt = choice(mpt_issuances)
    issuer = accounts.get(mpt.issuer)
    if not issuer:
        return None

    holders = [a for a in accounts.values() if a.address != mpt.issuer]
    if not holders:
        return None
    holder = choice(holders)

    txn = Clawback(
        account=issuer.address,
        amount=MPTAmount(
            mpt_issuance_id=mpt.mpt_issuance_id,
            value=params.clawback_mpt_amount(),
        ),
        holder=holder.address,
    )
    return txn, issuer.wallet


async def _clawback_valid(
    accounts: dict[str, UserAccount],
    trust_lines: list[TrustLine],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    built = _clawback_base(accounts, trust_lines, mpt_issuances)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("Clawback", txn, client, wallet)


async def _clawback_faulty(
    accounts: dict[str, UserAccount],
    trust_lines: list[TrustLine],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice(
        [
            "fuzz",
            "non_issuer_iou",
            "zero_iou_amount",
            "fake_holder_mpt",
            "non_issuer_mpt",
            "fake_mpt_issuance",
        ]
    )

    if mutation == "fuzz":
        built = _clawback_base(accounts, trust_lines, mpt_issuances)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("Clawback", base, client, wallet)
        return

    if mutation == "non_issuer_iou":
        dst = choice(list(accounts.values()))
        txn = Clawback(
            account=src.address,
            amount=IssuedCurrencyAmount(
                currency="USD",
                issuer=dst.address,
                value=params.clawback_iou_amount(),
            ),
        )
    elif mutation == "zero_iou_amount":
        dst = choice(list(accounts.values()))
        txn = Clawback(
            account=src.address,
            amount=IssuedCurrencyAmount(
                currency="USD",
                issuer=dst.address,
                value="0",
            ),
        )
    elif mutation == "fake_holder_mpt":
        if mpt_issuances:
            mpt = choice(mpt_issuances)
            issuer = accounts.get(mpt.issuer) or src
        else:
            issuer = src
        txn = Clawback(
            account=issuer.address,
            amount=MPTAmount(
                mpt_issuance_id=params.fake_id()[:48],
                value=params.clawback_mpt_amount(),
            ),
            holder=params.fake_account(),
        )
    elif mutation == "non_issuer_mpt":
        txn = Clawback(
            account=src.address,
            amount=MPTAmount(
                mpt_issuance_id=params.fake_id()[:48],
                value=params.clawback_mpt_amount(),
            ),
            holder=choice(list(accounts.values())).address,
        )
    else:  # fake_mpt_issuance
        txn = Clawback(
            account=src.address,
            amount=MPTAmount(
                mpt_issuance_id=params.fake_id()[:48],
                value=params.clawback_mpt_amount(),
            ),
            holder=choice(list(accounts.values())).address,
        )

    await submit_tx("Clawback", txn, client, src.wallet)
