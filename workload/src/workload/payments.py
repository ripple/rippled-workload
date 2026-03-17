"""Payment transaction generators for the antithesis workload."""

from workload import logging, params
from workload.assertions import tx_submitted, tx_result
from workload.randoms import sample, choice, random
from xrpl.asyncio.transaction import submit_and_wait
from xrpl.models import IssuedCurrencyAmount as IOUAmount
from xrpl.models.amounts import MPTAmount
from xrpl.models.transactions import Payment

log = logging.getLogger(__name__)


async def payment_random(accounts, trust_lines, mpt_issuances, client):
    if len(accounts) < 2:
        return
    if params.should_send_faulty():
        return await _payment_random_faulty(accounts, trust_lines, mpt_issuances, client)
    return await _payment_random_valid(accounts, trust_lines, mpt_issuances, client)


async def _payment_random_valid(accounts, trust_lines, mpt_issuances, client):
    src_address, dst = sample(list(accounts), 2)
    src = accounts[src_address]

    # Pick asset type from available options
    options = ["xrp"]
    if trust_lines:
        options.append("iou")
    if mpt_issuances:
        options.append("mpt")
    asset_type = choice(options)

    if asset_type == "iou":
        amount = _iou_amount(trust_lines)
    elif asset_type == "mpt":
        amount = _mpt_amount(mpt_issuances)
    else:
        amount = params.payment_amount()

    payment_txn = Payment(
        account=src.address,
        amount=amount,
        destination=dst,
    )
    tx_submitted("Payment")
    response = await submit_and_wait(payment_txn, client, src.wallet)
    tx_result("Payment", response.result)


def _iou_amount(trust_lines):
    tl = choice(trust_lines)
    issuer = choice([tl.account_a, tl.account_b])
    return IOUAmount(
        currency=tl.currency,
        issuer=issuer,
        value=params.iou_amount(),
    )


def _mpt_amount(mpt_issuances):
    mpt = choice(mpt_issuances)
    return MPTAmount(
        mpt_issuance_id=mpt.mpt_issuance_id,
        value=params.mpt_amount(),
    )


async def _payment_random_faulty(accounts, trust_lines, mpt_issuances, client):
    pass  # TODO: fault injection
