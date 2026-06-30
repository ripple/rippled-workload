"""SignerListSet handler (NON_DELEGABLE): signer_quorum=0 with no entries deletes the list."""

from __future__ import annotations

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import SignerListSet
from xrpl.models.transactions.signer_list_set import SignerEntry
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
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


def _signer_list_set_base(
    accounts: dict[str, UserAccount],
) -> tuple[SignerListSet, Wallet] | None:
    """Valid SignerListSet (set a multisign list, or remove it) + wallet."""
    if len(accounts) < 3:
        return None

    acct_list = list(accounts.values())
    src = choice(acct_list)

    action = choice(["set", "remove"])

    if action == "set":
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
        txn = SignerListSet(
            account=src.address,
            signer_quorum=0,
        )

    return txn, src.wallet


async def _signer_list_set_valid(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    built = _signer_list_set_base(accounts)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("SignerListSet", txn, client, wallet)


async def _signer_list_set_faulty(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice(
        [
            "fuzz",
            "fake_signers",
            "single_fake_signer",
            "high_quorum_fake_signers",
        ]
    )

    if mutation == "fuzz":
        built = _signer_list_set_base(accounts)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("SignerListSet", base, client, wallet)
        return

    if mutation == "fake_signers":
        count = randint(2, 8)
        entries = [
            SignerEntry(account=params.fake_account(), signer_weight=1) for _ in range(count)
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
        count = randint(3, 8)
        entries = [
            SignerEntry(account=params.fake_account(), signer_weight=1) for _ in range(count)
        ]
        txn = SignerListSet(
            account=src.address,
            signer_quorum=count,
            signer_entries=entries,
        )

    await submit_tx("SignerListSet", txn, client, src.wallet)
