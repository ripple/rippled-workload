"""Clawback workload handler — IOUs and MPTs.

Clawback lets an issuer/gateway reclaim tokens from a holder.
- IOU clawback: gateway is `account`, `amount.issuer` is the holder
- MPT clawback: MPT issuer is `account`, `holder` field names the holder

Only issuers with `lsfAllowTrustLineClawback` (gateways) or MPT issuers
can clawback.  We use `w.trust_lines` and `w.mpt_issuances` to find
valid (issuer, holder) pairs.
"""

from __future__ import annotations

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.amounts import IssuedCurrencyAmount, MPTAmount
from xrpl.models.transactions import Clawback

from workload import params
from workload.models import MPTokenIssuance, TrustLine, UserAccount
from workload.randoms import choice, randint
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


async def _clawback_valid(
    accounts: dict[str, UserAccount],
    trust_lines: list[TrustLine],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return

    # Pick IOU or MPT clawback based on available state
    can_iou = bool(trust_lines)
    can_mpt = bool(mpt_issuances)
    if not can_iou and not can_mpt:
        return

    flavours = []
    if can_iou:
        flavours.append("iou")
    if can_mpt:
        flavours.append("mpt")
    flavour = choice(flavours)

    if flavour == "iou":
        await _clawback_iou_valid(accounts, trust_lines, client)
    else:
        await _clawback_mpt_valid(accounts, mpt_issuances, client)


async def _clawback_iou_valid(
    accounts: dict[str, UserAccount],
    trust_lines: list[TrustLine],
    client: AsyncJsonRpcClient,
) -> None:
    tl = choice(trust_lines)
    # account_b is the gateway/issuer, account_a is the holder
    gateway = accounts.get(tl.account_b)
    if not gateway:
        return

    txn = Clawback(
        account=gateway.address,
        amount=IssuedCurrencyAmount(
            currency=tl.currency,
            issuer=tl.account_a,  # holder
            value=params.clawback_iou_amount(),
        ),
    )
    await submit_tx("Clawback", txn, client, gateway.wallet)


async def _clawback_mpt_valid(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    mpt = choice(mpt_issuances)
    issuer = accounts.get(mpt.issuer)
    if not issuer:
        return

    # Find a holder — any account that isn't the issuer
    holders = [a for a in accounts.values() if a.address != mpt.issuer]
    if not holders:
        return
    holder = choice(holders)

    txn = Clawback(
        account=issuer.address,
        amount=MPTAmount(
            mpt_issuance_id=mpt.mpt_issuance_id,
            value=params.clawback_mpt_amount(),
        ),
        holder=holder.address,
    )
    await submit_tx("Clawback", txn, client, issuer.wallet)


async def _clawback_faulty(
    accounts: dict[str, UserAccount],
    trust_lines: list[TrustLine],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice([
        "non_issuer_iou",
        "zero_iou_amount",
        "fake_holder_mpt",
        "non_issuer_mpt",
        "fake_mpt_issuance",
    ])

    if mutation == "non_issuer_iou":
        # Non-gateway tries to claw back IOU
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
        # Claw back from non-existent holder
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
        # Non-issuer tries to claw back MPT
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
