"""Payment transaction generators for the antithesis workload."""

from workload import logging, params
from workload.assertions import tx_submitted, tx_result
from workload.randoms import sample
from xrpl.asyncio.account import get_next_valid_seq_number
from xrpl.asyncio.transaction import submit_and_wait, sign_and_submit
from xrpl.models.transactions import Payment

log = logging.getLogger(__name__)


async def payment_random(accounts, client):
    if len(accounts) < 2:
        return
    if params.should_send_faulty():
        return await _payment_random_faulty(accounts, client)
    return await _payment_random_valid(accounts, client)


async def _payment_random_valid(accounts, client):
    src_address, dst = sample(list(accounts), 2)
    src = accounts[src_address]
    amount = params.payment_amount()
    payment_txn = Payment(
        account=src.address,
        amount=amount,
        destination=dst,
    )
    tx_submitted("Payment")
    response = await submit_and_wait(payment_txn, client, src.wallet)
    tx_result("Payment", response.result)


async def _payment_random_faulty(accounts, client):
    pass  # TODO: fault injection
