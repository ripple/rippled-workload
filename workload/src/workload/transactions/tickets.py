"""TicketCreate generator: stocks an account's ticket pool.

Ticket *use* is a submit-time modifier (``workload.modifiers``): any supported
tx gets ``Sequence=0`` + a ``TicketSequence`` drawn from the account's pool.
"""

import xrpl.models
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import UserAccount
from workload.randoms import choice
from workload.submit import submit_raw, submit_tx


async def ticket_create(accounts: dict[str, UserAccount], client: AsyncJsonRpcClient) -> None:
    if params.should_send_faulty():
        return await _ticket_create_faulty(accounts, client)
    return await _ticket_create_valid(accounts, client)


def _ticket_create_base(
    accounts: dict[str, UserAccount],
) -> tuple[xrpl.models.TicketCreate, Wallet] | None:
    """Valid TicketCreate + wallet; shared by valid and fuzz."""
    if not accounts:
        return None
    account = accounts[choice(list(accounts))]
    txn = xrpl.models.TicketCreate(account=account.address, ticket_count=params.ticket_count())
    return txn, account.wallet


async def _ticket_create_valid(
    accounts: dict[str, UserAccount], client: AsyncJsonRpcClient
) -> None:
    built = _ticket_create_base(accounts)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("TicketCreate", txn, client, wallet)


async def _ticket_create_faulty(
    accounts: dict[str, UserAccount], client: AsyncJsonRpcClient
) -> None:
    built = _ticket_create_base(accounts)
    if built is None:
        return
    base, wallet = built

    if choice(["zero_count", "fuzz"]) == "fuzz":
        await submit_fuzzed("TicketCreate", base, client, wallet)
        return

    # zero_count: TicketCount 0 -> temINVALID_COUNT (xrpl-py forbids it at construction).
    def _mutate(d: dict) -> None:
        d["TicketCount"] = 0

    await submit_raw("TicketCreate", base, client, wallet, _mutate)
