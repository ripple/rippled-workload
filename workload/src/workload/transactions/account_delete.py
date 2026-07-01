"""AccountDelete (NON_DELEGABLE); valid path skips protected_accounts to not break setup."""

from __future__ import annotations

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import AccountDelete
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import UserAccount
from workload.randoms import choice, randint
from workload.submit import submit_tx


async def account_delete(
    accounts: dict[str, UserAccount],
    protected_accounts: set[str],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _account_delete_faulty(accounts, protected_accounts, client)
    return await _account_delete_valid(accounts, protected_accounts, client)


def _account_delete_base(
    accounts: dict[str, UserAccount],
    protected_accounts: set[str],
) -> tuple[AccountDelete, Wallet] | None:
    """Valid AccountDelete (delete an expendable account, funds to a peer) + wallet."""
    if len(accounts) < 2:
        return None

    acct_list = list(accounts.values())

    expendable = [a for a in acct_list if a.address not in protected_accounts]
    if not expendable:
        return None

    src = choice(expendable)
    dst = choice([a for a in acct_list if a.address != src.address])

    txn = AccountDelete(
        account=src.address,
        destination=dst.address,
    )
    return txn, src.wallet


async def _account_delete_valid(
    accounts: dict[str, UserAccount],
    protected_accounts: set[str],
    client: AsyncJsonRpcClient,
) -> None:
    built = _account_delete_base(accounts, protected_accounts)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("AccountDelete", txn, client, wallet)


async def _account_delete_faulty(
    accounts: dict[str, UserAccount],
    protected_accounts: set[str],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice(
        [
            "fuzz",
            "self_destination",
            "fake_destination",
            "delete_with_dest_tag",
        ]
    )

    if mutation == "fuzz":
        built = _account_delete_base(accounts, protected_accounts)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("AccountDelete", base, client, wallet)
        return

    if mutation == "self_destination":
        txn = AccountDelete(
            account=src.address,
            destination=src.address,
        )
    elif mutation == "fake_destination":
        txn = AccountDelete(
            account=src.address,
            destination=params.fake_account(),
        )
    else:  # delete_with_dest_tag — unusual but valid syntax
        dst = choice(list(accounts.values()))
        txn = AccountDelete(
            account=src.address,
            destination=dst.address,
            destination_tag=randint(1, 2**32 - 1),
        )

    await submit_tx("AccountDelete", txn, client, src.wallet)
