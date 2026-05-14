"""DID transaction generators for the antithesis workload."""

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import DIDDelete, DIDSet

from workload import params
from workload.models import DID, UserAccount
from workload.randoms import choice, random, sample
from workload.submit import submit_tx

VALID_FIELD_COMBOS = [
    ("uri",),
    ("data",),
    ("did_document",),
    ("uri", "data"),
    ("uri", "did_document"),
    ("data", "did_document"),
    ("uri", "data", "did_document"),
]


# ── Set ──────────────────────────────────────────────────────────────


async def did_set(accounts: dict[str, UserAccount], client: AsyncJsonRpcClient) -> None:
    if params.should_send_faulty():
        return await _did_set_faulty(accounts, client)
    return await _did_set_valid(accounts, client)


async def _did_set_valid(accounts: dict[str, UserAccount], client: AsyncJsonRpcClient) -> None:
    src = choice(list(accounts.values()))

    if random() < 0.10:
        # Partial clear: keep one field, clear the rest.
        keep_field = choice(["uri", "data", "did_document"])
        all_fields = {"uri": "", "data": "", "did_document": ""}
        all_fields[keep_field] = params.did_hex_field()
        txn = DIDSet(account=src.address, **all_fields)
        await submit_tx("DIDSet", txn, client, src.wallet)
        return

    combo = choice(VALID_FIELD_COMBOS)
    fields = {f: params.did_hex_field() for f in combo}
    txn = DIDSet(account=src.address, **fields)
    await submit_tx("DIDSet", txn, client, src.wallet)


async def _did_set_faulty(accounts: dict[str, UserAccount], client: AsyncJsonRpcClient) -> None:
    if not accounts:
        return
    mutation = choice(
        [
            "non_owner_submission",
            "invalid_flags",
            "single_empty_field",
        ]
    )
    if mutation == "non_owner_submission":
        accounts_list = list(accounts.values())
        if len(accounts_list) < 2:
            return
        owner, impostor = sample(accounts_list, 2)
        txn = DIDSet(account=owner.address, uri=params.did_hex_field())
        await submit_tx("DIDSet", txn, client, impostor.wallet)
    elif mutation == "invalid_flags":
        src = choice(list(accounts.values()))
        txn = DIDSet(account=src.address, uri=params.did_hex_field(), flags=0x80000000)
        await submit_tx("DIDSet", txn, client, src.wallet)
    elif mutation == "single_empty_field":
        src = choice(list(accounts.values()))
        field = choice(["uri", "data", "did_document"])
        txn = DIDSet(account=src.address, **{field: ""})
        await submit_tx("DIDSet", txn, client, src.wallet)


# ── Delete ───────────────────────────────────────────────────────────


async def did_delete(
    accounts: dict[str, UserAccount], dids: list[DID], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _did_delete_faulty(accounts, dids, client)
    return await _did_delete_valid(accounts, dids, client)


async def _did_delete_valid(
    accounts: dict[str, UserAccount], dids: list[DID], client: AsyncJsonRpcClient
) -> None:
    if not dids:
        return
    target = choice(dids)
    if target.account not in accounts:
        return
    owner = accounts[target.account]
    txn = DIDDelete(account=owner.address)
    await submit_tx("DIDDelete", txn, client, owner.wallet)


async def _did_delete_faulty(
    accounts: dict[str, UserAccount], dids: list[DID], client: AsyncJsonRpcClient
) -> None:
    if not accounts:
        return
    mutation = choice(
        [
            "delete_no_did",
            "non_owner_submission",
            "invalid_flags",
        ]
    )
    accounts_list = list(accounts.values())
    if mutation == "delete_no_did":
        did_owners = {d.account for d in dids}
        candidates = [a for a in accounts_list if a.address not in did_owners]
        if not candidates:
            return
        target = choice(candidates)
        txn = DIDDelete(account=target.address)
        await submit_tx("DIDDelete", txn, client, target.wallet)
    elif mutation == "non_owner_submission":
        if not dids or len(accounts_list) < 2:
            return
        target_did = choice(dids)
        if target_did.account not in accounts:
            return
        impostors = [a for a in accounts_list if a.address != target_did.account]
        if not impostors:
            return
        impostor = choice(impostors)
        txn = DIDDelete(account=target_did.account)
        await submit_tx("DIDDelete", txn, client, impostor.wallet)
    elif mutation == "invalid_flags":
        src = choice(accounts_list)
        txn = DIDDelete(account=src.address, flags=0x80000000)
        await submit_tx("DIDDelete", txn, client, src.wallet)
