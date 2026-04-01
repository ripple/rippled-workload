"""Payment transaction generators for the antithesis workload."""

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models import IssuedCurrencyAmount as IOUAmount
from xrpl.models.amounts import MPTAmount
from xrpl.models.transactions import Payment

from workload import logging, params
from workload.models import MPTokenIssuance, TrustLine, UserAccount
from workload.randoms import choice, sample
from workload.submit import submit_tx

log = logging.getLogger(__name__)


async def payment_random(
    accounts: dict[str, UserAccount],
    trust_lines: list[TrustLine],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _payment_random_faulty(accounts, trust_lines, mpt_issuances, client)
    return await _payment_random_valid(accounts, trust_lines, mpt_issuances, client)


async def _payment_random_valid(
    accounts: dict[str, UserAccount],
    trust_lines: list[TrustLine],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
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
    await submit_tx("Payment", payment_txn, client, src.wallet)


def _iou_amount(trust_lines: list[TrustLine]) -> IOUAmount:
    tl = choice(trust_lines)
    issuer = choice([tl.account_a, tl.account_b])
    return IOUAmount(
        currency=tl.currency,
        issuer=issuer,
        value=params.iou_amount(),
    )


def _mpt_amount(mpt_issuances: list[MPTokenIssuance]) -> MPTAmount:
    mpt = choice(mpt_issuances)
    return MPTAmount(
        mpt_issuance_id=mpt.mpt_issuance_id,
        value=params.mpt_amount(),
    )


async def _payment_random_faulty(
    accounts: dict[str, UserAccount],
    trust_lines: list[TrustLine],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    pass  # TODO: fault injection
