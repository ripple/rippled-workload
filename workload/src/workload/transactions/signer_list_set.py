"""SignerListSet workload handler.

SignerListSet configures multi-signing on an account.  A signer list
defines which accounts can co-sign transactions and the quorum needed.
Setting signer_quorum=0 with no entries deletes the list.

This is a NON_DELEGABLE transaction — delegation is blocked by the
XRPL protocol itself.
"""

from __future__ import annotations

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import SignerListSet
from xrpl.models.transactions.signer_list_set import SignerEntry

from workload import params
from workload.models import UserAccount
from workload.randoms import choice, randint, sample
from workload.submit import submit_tx



async def signer_list_set(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _signer_list_set_faulty(accounts, client)
    return await _signer_list_set_valid(accounts, client)


async def _signer_list_set_valid(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    if len(accounts) < 3:
        return

    acct_list = list(accounts.values())
    src = choice(acct_list)

    action = choice(["set", "remove"])

    if action == "set":
        # Pick 2-5 unique signers (not self)
        others = [a for a in acct_list if a.address != src.address]
        count = min(randint(2, 5), len(others))
        signers = sample(others, count)

        entries = [
            SignerEntry(
                account=s.address,
                signer_weight=randint(1, 3),
            )
            for s in signers
        ]

        total_weight = sum(e.signer_weight for e in entries)
        quorum = randint(1, total_weight)

        txn = SignerListSet(
            account=src.address,
            signer_quorum=quorum,
            signer_entries=entries,
        )
    else:
        # Delete the signer list
        txn = SignerListSet(
            account=src.address,
            signer_quorum=0,
        )

    await submit_tx("SignerListSet", txn, client, src.wallet)


async def _signer_list_set_faulty(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice([
        "fake_signers",
        "single_fake_signer",
        "high_quorum_fake_signers",
    ])

    if mutation == "fake_signers":
        # Many non-existent signer accounts
        count = randint(2, 8)
        entries = [
            SignerEntry(account=params.fake_account(), signer_weight=1)
            for _ in range(count)
        ]
        txn = SignerListSet(
            account=src.address,
            signer_quorum=randint(1, count),
            signer_entries=entries,
        )
    elif mutation == "single_fake_signer":
        txn = SignerListSet(
            account=src.address,
            signer_quorum=1,
            signer_entries=[
                SignerEntry(account=params.fake_account(), signer_weight=1),
            ],
        )
    else:  # high_quorum_fake_signers
        # Quorum equals total weight — hard to meet
        count = randint(3, 8)
        entries = [
            SignerEntry(account=params.fake_account(), signer_weight=1)
            for _ in range(count)
        ]
        txn = SignerListSet(
            account=src.address,
            signer_quorum=count,
            signer_entries=entries,
        )

    await submit_tx("SignerListSet", txn, client, src.wallet)
