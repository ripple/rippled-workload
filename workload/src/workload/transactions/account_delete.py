"""AccountDelete workload handler.

AccountDelete removes an account from the ledger, sending remaining XRP
to a destination.  Constraints enforced by rippled:
  - Account's sequence must be ≥ current ledger seq - 256
  - Account must own ZERO directory objects (trust lines, offers, escrows,
    checks, channels, NFTs, vaults, etc.)
  - Costs the owner reserve (typically 5 XRP) as fee

This is a NON_DELEGABLE transaction — blocked by XRPL protocol.

SAFETY: The valid path only targets accounts outside the critical setup
ranges (gateways, MPT issuers, vault creators, holders, NFT minters,
credential subjects, ticket holders, domain creators).  Most submissions
will be rejected by rippled because even "unused" accounts may have
acquired objects from driver calls.  The fuzzing value is exercising
the transaction path itself.
"""

from __future__ import annotations

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import AccountDelete

from workload import params
from workload.models import UserAccount
from workload.randoms import choice, randint
from workload.submit import submit_tx



# Account indices used by setup — NEVER target these for deletion.
_PROTECTED_INDICES: set[int] = set(
    list(range(0, 5))       # MPT issuers
    + list(range(10, 17))   # Vault creators
    + list(range(20, 25))   # NFT minters
    + list(range(30, 36))   # Credential issuer + subjects
    + list(range(40, 43))   # Ticket holders
    + list(range(50, 53))   # Domain creators
    + list(range(60, 72))   # Gateways + IOU/MPT holders
)


async def account_delete(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _account_delete_faulty(accounts, client)
    return await _account_delete_valid(accounts, client)


async def _account_delete_valid(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    if len(accounts) < 2:
        return

    acct_list = list(accounts.values())

    # Find expendable accounts (not in protected ranges)
    expendable = [
        a for i, a in enumerate(acct_list)
        if i not in _PROTECTED_INDICES
    ]
    if not expendable:
        return

    src = choice(expendable)
    # Destination must differ from source — pick any other account
    dst = choice([a for a in acct_list if a.address != src.address])

    txn = AccountDelete(
        account=src.address,
        destination=dst.address,
    )
    await submit_tx("AccountDelete", txn, client, src.wallet)


async def _account_delete_faulty(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice([
        "self_destination",
        "fake_destination",
        "delete_with_dest_tag",
    ])

    if mutation == "self_destination":
        # Can't delete to self
        txn = AccountDelete(
            account=src.address,
            destination=src.address,
        )
    elif mutation == "fake_destination":
        # Destination doesn't exist
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
