"""Trust line generators; "issuer" is contextual — if A holds USD.B, B is issuer from A's view."""

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models import IssuedCurrencyAmount as IOUAmount
from xrpl.models.transactions import TrustSet
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import TrustLine, UserAccount
from workload.randoms import choice, sample
from workload.submit import submit_raw, submit_tx


async def trustline_create(
    accounts: dict[str, UserAccount], trust_lines: list[TrustLine], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _trustline_create_faulty(accounts, trust_lines, client)
    return await _trustline_create_valid(accounts, trust_lines, client)


def _trustline_create_base(
    accounts: dict[str, UserAccount],
) -> tuple[TrustSet, Wallet] | None:
    """Valid TrustSet (account trusts another as IOU issuer) + wallet; shared by valid and fuzz."""
    if len(accounts) < 2:
        return None
    account_id, other_id = sample(list(accounts), 2)
    account = accounts[account_id]
    txn = TrustSet(
        account=account.address,
        limit_amount=IOUAmount(
            currency=params.currency_code(),
            issuer=other_id,
            value=params.trustline_limit(),
        ),
    )
    return txn, account.wallet


async def _trustline_create_valid(
    accounts: dict[str, UserAccount], trust_lines: list[TrustLine], client: AsyncJsonRpcClient
) -> None:
    built = _trustline_create_base(accounts)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("TrustSet", txn, client, wallet)


async def _trustline_create_faulty(
    accounts: dict[str, UserAccount], trust_lines: list[TrustLine], client: AsyncJsonRpcClient
) -> None:
    built = _trustline_create_base(accounts)
    if built is None:
        return
    base, wallet = built

    if choice(["self_trust", "fuzz"]) == "fuzz":
        await submit_fuzzed("TrustSet", base, client, wallet)
        return

    # self_trust: issuer == account -> temDST_IS_SRC (xrpl-py forbids it at construction).
    def _mutate(d: dict) -> None:
        d["LimitAmount"]["issuer"] = d["Account"]

    await submit_raw("TrustSet", base, client, wallet, _mutate)
