"""SetRegularKey workload handler.

SetRegularKey assigns or removes a regular key pair for an account.
When set, both the master key and the regular key can sign transactions.
Setting regular_key to None removes it.
"""

from __future__ import annotations

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import SetRegularKey

from workload import params
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


async def _set_regular_key_valid(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    if len(accounts) < 2:
        return

    acct_list = list(accounts.values())
    src = choice(acct_list)

    # Randomly set or remove the regular key
    action = choice(["set", "remove"])

    if action == "set":
        # Pick another account's address as the regular key
        dst = choice([a for a in acct_list if a.address != src.address])
        txn = SetRegularKey(
            account=src.address,
            regular_key=dst.address,
        )
    else:
        # Remove regular key
        txn = SetRegularKey(
            account=src.address,
        )

    await submit_tx("SetRegularKey", txn, client, src.wallet)


async def _set_regular_key_faulty(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice([
        "set_key_to_self",
        "set_key_to_fake",
        "set_key_empty_string",
    ])

    if mutation == "set_key_to_self":
        txn = SetRegularKey(
            account=src.address,
            regular_key=src.address,
        )
    elif mutation == "set_key_to_fake":
        txn = SetRegularKey(
            account=src.address,
            regular_key=params.fake_account(),
        )
    else:  # set_key_empty_string
        txn = SetRegularKey(
            account=src.address,
            regular_key="",
        )

    await submit_tx("SetRegularKey", txn, client, src.wallet)
