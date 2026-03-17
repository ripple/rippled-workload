"""Payment transaction generators for the antithesis workload."""

from workload import logging, params
from workload.assertions import tx_submitted, tx_result
from workload.randoms import sample, choice, random
from xrpl.asyncio.transaction import submit_and_wait
from xrpl.models import IssuedCurrencyAmount
from xrpl.models.transactions import Payment

log = logging.getLogger(__name__)


async def payment_random(accounts, trust_lines, client):
    if len(accounts) < 2:
        return
    if params.should_send_faulty():
        return await _payment_random_faulty(accounts, trust_lines, client)
    return await _payment_random_valid(accounts, trust_lines, client)


async def _payment_random_valid(accounts, trust_lines, client):
    src_address, dst = sample(list(accounts), 2)
    src = accounts[src_address]

    # 50% chance to send IOU if trust lines exist
    if trust_lines and random() < 0.5:
        tl = choice(trust_lines)
        # Currency is (currency_code, issuer). Either side of the trust line
        # can be the issuer. The sender/recipient don't have to be on this
        # trust line — rippling through the issuer.
        issuer = choice([tl.account_a, tl.account_b])
        amount = IssuedCurrencyAmount(
            currency=tl.currency,
            issuer=issuer,
            value=params.iou_amount(),
        )
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


async def _payment_random_faulty(accounts, trust_lines, client):
    pass  # TODO: fault injection
