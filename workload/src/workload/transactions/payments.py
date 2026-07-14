"""Payment transaction generators."""

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models import IssuedCurrencyAmount as IOUAmount
from xrpl.models.amounts import MPTAmount
from xrpl.models.transactions import Payment
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import MPTokenIssuance, TrustLine, UserAccount
from workload.randoms import choice, randint, sample
from workload.submit import submit_tx


async def payment_random(
    accounts: dict[str, UserAccount],
    trust_lines: list[TrustLine],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _payment_random_faulty(accounts, trust_lines, mpt_issuances, client)
    return await _payment_random_valid(accounts, trust_lines, mpt_issuances, client)


def _payment_random_base(
    accounts: dict[str, UserAccount],
    trust_lines: list[TrustLine],
    mpt_issuances: list[MPTokenIssuance],
) -> tuple[Payment, Wallet] | None:
    """Valid Payment (XRP/IOU/MPT) + wallet; shared by valid and fuzz."""
    if len(accounts) < 2:
        return None
    src_address, dst = sample(list(accounts), 2)
    src = accounts[src_address]

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

    txn = Payment(account=src.address, amount=amount, destination=dst)
    return txn, src.wallet


async def _payment_random_valid(
    accounts: dict[str, UserAccount],
    trust_lines: list[TrustLine],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    built = _payment_random_base(accounts, trust_lines, mpt_issuances)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("Payment", txn, client, wallet)


async def payment_fund_new(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    """State-tree bloat driver (synthetic name PaymentFundNew, on-ledger Payment):
    fund a brand-new account every call so each ledger grows the state SHAMap with
    fresh AccountRoots. This widens the window in which a diverging/lagging validator
    holds an incompletely-acquired state tree — the condition under which
    RCLConsensus::timerEntry throws SHAMapMissingNode and aborts the process.
    Valid-only: an under-funded fund-new creates nothing, defeating the purpose."""
    if not accounts:
        return
    src = accounts[choice(list(accounts))]
    txn = Payment(
        account=src.address,
        amount=params.new_account_funding_amount(),
        destination=params.fake_account(),
    )
    await submit_tx("PaymentFundNew", txn, client, src.wallet)


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
    if len(accounts) < 2:
        return
    src_address, dst = sample(list(accounts), 2)
    src = accounts[src_address]

    mutation = choice(
        [
            "overdraw_xrp",
            "underfund_new_account",
            "fuzz",
        ]
    )
    if mutation == "fuzz":
        built = _payment_random_base(accounts, trust_lines, mpt_issuances)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("Payment", base, client, wallet)
        return

    if mutation == "overdraw_xrp":
        # Send far more XRP than any funded account holds -> tecUNFUNDED_PAYMENT.
        txn = Payment(
            account=src.address,
            amount=str(randint(10**17, 10**18)),
            destination=dst,
        )

    else:  # underfund_new_account
        # Pay a brand-new account less than the base reserve -> tecNO_DST_INSUF_XRP.
        txn = Payment(
            account=src.address,
            amount=str(randint(1, 1_000)),
            destination=params.fake_account(),
        )

    await submit_tx("Payment", txn, client, src.wallet)
