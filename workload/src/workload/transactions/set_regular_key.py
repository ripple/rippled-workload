"""SetRegularKey handler; omitting regular_key removes it."""

from __future__ import annotations

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import SetRegularKey
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import UserAccount
from workload.randoms import choice
from workload.submit import submit_tx


async def set_regular_key(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _set_regular_key_faulty(accounts, client)
    return await _set_regular_key_valid(accounts, client)


def _set_regular_key_base(
    accounts: dict[str, UserAccount],
) -> tuple[SetRegularKey, Wallet] | None:
    """Valid SetRegularKey (set a peer as regular key, or remove it) + wallet."""
    if len(accounts) < 2:
        return None

    acct_list = list(accounts.values())
    src = choice(acct_list)

    action = choice(["set", "remove"])

    if action == "set":
        dst = choice([a for a in acct_list if a.address != src.address])
        txn = SetRegularKey(
            account=src.address,
            regular_key=dst.address,
        )
    else:
        txn = SetRegularKey(
            account=src.address,
        )

    return txn, src.wallet


async def _set_regular_key_valid(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    built = _set_regular_key_base(accounts)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("SetRegularKey", txn, client, wallet)


async def _set_regular_key_faulty(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice(
        [
            "fuzz",
            "set_key_to_self",
            "set_key_to_fake",
        ]
    )

    if mutation == "fuzz":
        built = _set_regular_key_base(accounts)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("SetRegularKey", base, client, wallet)
        return

    if mutation == "set_key_to_self":
        txn = SetRegularKey(
            account=src.address,
            regular_key=src.address,
        )
    else:  # set_key_to_fake
        txn = SetRegularKey(
            account=src.address,
            regular_key=params.fake_account(),
        )

    await submit_tx("SetRegularKey", txn, client, src.wallet)
