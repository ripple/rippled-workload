"""MPToken transaction generators for the antithesis workload."""

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import (
    MPTokenAuthorize,
    MPTokenIssuanceCreate,
    MPTokenIssuanceDestroy,
    MPTokenIssuanceSet,
)
from xrpl.models.transactions.mptoken_issuance_create import MPTokenIssuanceCreateFlag
from xrpl.models.transactions.mptoken_issuance_set import MPTokenIssuanceSetFlag

from workload import params
from workload.models import MPTokenIssuance, UserAccount
from workload.randoms import choice, random


from workload.submit import submit_tx

# ── Create ───────────────────────────────────────────────────────────


async def mpt_create(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    return await _mpt_create_valid(accounts, mpt_issuances, client)


async def _mpt_create_valid(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    src_address = choice(list(accounts))
    src = accounts[src_address]
    flags = MPTokenIssuanceCreateFlag.TF_MPT_CAN_LOCK
    if random() < 0.30:
        flags |= MPTokenIssuanceCreateFlag.TF_MPT_REQUIRE_AUTH
    if random() < 0.30:
        flags |= MPTokenIssuanceCreateFlag.TF_MPT_CAN_CLAWBACK
    txn = MPTokenIssuanceCreate(
        account=src.address,
        maximum_amount=params.mpt_maximum_amount(),
        mptoken_metadata=params.mpt_metadata(),
        flags=flags,
    )
    await submit_tx("MPTokenIssuanceCreate", txn, client, src.wallet)


# ── Authorize ────────────────────────────────────────────────────────


async def mpt_authorize(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _mpt_authorize_faulty(accounts, mpt_issuances, client)
    return await _mpt_authorize_valid(accounts, mpt_issuances, client)


async def _mpt_authorize_valid(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not mpt_issuances:
        return
    mpt = choice(mpt_issuances)
    if mpt.issuer not in accounts:
        return

    other_accounts = [a for a in accounts if a != mpt.issuer]
    if not other_accounts:
        return

    mode = choice(["holder_optin", "issuer_auth"])

    if mode == "holder_optin":
        # Holder self-authorization (opt-in) — works for any MPT
        holder = accounts[choice(other_accounts)]
        txn = MPTokenAuthorize(
            account=holder.address,
            mptoken_issuance_id=mpt.mpt_issuance_id,
        )
        await submit_tx("MPTokenAuthorize", txn, client, holder.wallet)
    else:
        # Issuer authorizes a holder — only works with TF_MPT_REQUIRE_AUTH
        issuer = accounts[mpt.issuer]
        holder_id = choice(other_accounts)
        txn = MPTokenAuthorize(
            account=issuer.address,
            mptoken_issuance_id=mpt.mpt_issuance_id,
            holder=holder_id,
        )
        await submit_tx("MPTokenAuthorize", txn, client, issuer.wallet)


async def _mpt_authorize_faulty(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    pass  # TODO: fault injection


# ── Set ──────────────────────────────────────────────────────────────


async def mpt_issuance_set(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _mpt_issuance_set_faulty(accounts, mpt_issuances, client)
    return await _mpt_issuance_set_valid(accounts, mpt_issuances, client)


async def _mpt_issuance_set_valid(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not mpt_issuances:
        return
    mpt = choice(mpt_issuances)
    if mpt.issuer not in accounts:
        return
    issuer = accounts[mpt.issuer]
    flag = choice(list(MPTokenIssuanceSetFlag))
    txn = MPTokenIssuanceSet(
        account=issuer.address,
        mptoken_issuance_id=mpt.mpt_issuance_id,
        flags=flag,
    )
    await submit_tx("MPTokenIssuanceSet", txn, client, issuer.wallet)


async def _mpt_issuance_set_faulty(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    pass  # TODO: fault injection


# ── Destroy ──────────────────────────────────────────────────────────


async def mpt_destroy(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _mpt_destroy_faulty(accounts, mpt_issuances, client)
    return await _mpt_destroy_valid(accounts, mpt_issuances, client)


async def _mpt_destroy_valid(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not mpt_issuances:
        return
    mpt = choice(mpt_issuances)
    if mpt.issuer not in accounts:
        return
    issuer = accounts[mpt.issuer]
    txn = MPTokenIssuanceDestroy(
        account=issuer.address,
        mptoken_issuance_id=mpt.mpt_issuance_id,
    )
    await submit_tx("MPTokenIssuanceDestroy", txn, client, issuer.wallet)


async def _mpt_destroy_faulty(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    pass  # TODO: fault injection
