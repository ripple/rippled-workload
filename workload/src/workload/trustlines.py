"""Trust line transaction generators for the antithesis workload.

A trust line (RippleState) is a symmetric relationship between two accounts
for a specific currency. Either side can create it via TrustSet. The currency
is identified as (currency_code, issuer_address) where "issuer" is contextual —
if account A holds USD.B, then B is the issuer from A's perspective.
"""

from workload import logging, params
from workload.assertions import tx_submitted, tx_result
from workload.models import TrustLine
from workload.randoms import sample, choice
from xrpl.asyncio.transaction import submit_and_wait
from xrpl.models import IssuedCurrencyAmount as IOUAmount
from xrpl.models.transactions import TrustSet

log = logging.getLogger(__name__)


async def trustline_create(accounts, trust_lines, client):
    if len(accounts) < 2:
        return
    if params.should_send_faulty():
        return await _trustline_create_faulty(accounts, trust_lines, client)
    return await _trustline_create_valid(accounts, trust_lines, client)


async def _trustline_create_valid(accounts, trust_lines, client):
    account_id, other_id = sample(list(accounts), 2)
    account = accounts[account_id]
    currency = params.currency_code()
    # The submitter sets a trust line limit for currency issued by the other account
    txn = TrustSet(
        account=account.address,
        limit_amount=IOUAmount(
            currency=currency,
            issuer=other_id,
            value=params.trustline_limit(),
        ),
    )
    tx_submitted("TrustSet", txn)
    response = await submit_and_wait(txn, client, account.wallet)
    result = response.result
    tx_result("TrustSet", result)
    if result.get("engine_result") == "tesSUCCESS":
        trust_lines.append(TrustLine(
            account_a=account_id,
            account_b=other_id,
            currency=currency,
        ))
        log.info("Trust line: %s <-> %s for %s", account_id, other_id, currency)


async def _trustline_create_faulty(accounts, trust_lines, client):
    pass  # TODO: fault injection
