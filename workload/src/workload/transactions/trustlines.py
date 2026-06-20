"""Trust line generators; "issuer" is contextual — if A holds USD.B, B is issuer from A's view."""

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models import IssuedCurrencyAmount as IOUAmount
from xrpl.models.transactions import TrustSet

from workload import logging, params
from workload.models import TrustLine, UserAccount
from workload.randoms import sample
from workload.submit import submit_tx

log = logging.getLogger(__name__)


async def trustline_create(
    accounts: dict[str, UserAccount], trust_lines: list[TrustLine], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _trustline_create_faulty(accounts, trust_lines, client)
    return await _trustline_create_valid(accounts, trust_lines, client)


async def _trustline_create_valid(
    accounts: dict[str, UserAccount], trust_lines: list[TrustLine], client: AsyncJsonRpcClient
) -> None:
    account_id, other_id = sample(list(accounts), 2)
    account = accounts[account_id]
    currency = params.currency_code()
    txn = TrustSet(
        account=account.address,
        limit_amount=IOUAmount(
            currency=currency,
            issuer=other_id,
            value=params.trustline_limit(),
        ),
    )
    await submit_tx("TrustSet", txn, client, account.wallet)


async def _trustline_create_faulty(
    accounts: dict[str, UserAccount], trust_lines: list[TrustLine], client: AsyncJsonRpcClient
) -> None:
    pass  # TODO: fault injection
