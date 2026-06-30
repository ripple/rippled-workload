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
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import MPTokenIssuance, UserAccount
from workload.randoms import choice, random
from workload.submit import submit_raw, submit_tx

# ── Create ───────────────────────────────────────────────────────────


async def mpt_create(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _mpt_create_faulty(accounts, mpt_issuances, client)
    return await _mpt_create_valid(accounts, mpt_issuances, client)


def _mpt_create_base(
    accounts: dict[str, UserAccount],
) -> tuple[MPTokenIssuanceCreate, Wallet] | None:
    """Valid MPTokenIssuanceCreate + wallet; shared by valid and fuzz."""
    if not accounts:
        return None
    src = accounts[choice(list(accounts))]
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
    return txn, src.wallet


async def _mpt_create_valid(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    built = _mpt_create_base(accounts)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("MPTokenIssuanceCreate", txn, client, wallet)


async def _mpt_create_faulty(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    built = _mpt_create_base(accounts)
    if built is None:
        return
    base, wallet = built

    # A fresh issuance always succeeds, so no reliable tec vector exists here;
    # both curated mutations are temMALFORMED malformations xrpl-py forbids.
    mutation = choice(["fuzz", "oversize_metadata", "zero_max_amount"])
    if mutation == "fuzz":
        await submit_fuzzed("MPTokenIssuanceCreate", base, client, wallet)
        return

    if mutation == "oversize_metadata":
        # MPTokenMetadata > 1024 bytes -> temMALFORMED.
        def _mutate(d: dict) -> None:
            d["MPTokenMetadata"] = "AB" * 1025

    else:  # zero_max_amount -> temMALFORMED (MaximumAmount must be positive).

        def _mutate(d: dict) -> None:
            d["MaximumAmount"] = "0"

    await submit_raw("MPTokenIssuanceCreate", base, client, wallet, _mutate)


# ── Authorize ────────────────────────────────────────────────────────


async def mpt_authorize(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _mpt_authorize_faulty(accounts, mpt_issuances, client)
    return await _mpt_authorize_valid(accounts, mpt_issuances, client)


def _mpt_authorize_base(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
) -> tuple[MPTokenAuthorize, Wallet] | None:
    """Valid MPTokenAuthorize (holder opt-in / issuer auth) + wallet; shared by valid and fuzz."""
    if not mpt_issuances:
        return None
    mpt = choice(mpt_issuances)
    if mpt.issuer not in accounts:
        return None
    other_accounts = [a for a in accounts if a != mpt.issuer]
    if not other_accounts:
        return None

    if choice(["holder_optin", "issuer_auth"]) == "holder_optin":
        # opt-in works for any MPT; issuer_auth needs TF_MPT_REQUIRE_AUTH
        holder = accounts[choice(other_accounts)]
        txn = MPTokenAuthorize(
            account=holder.address,
            mptoken_issuance_id=mpt.mpt_issuance_id,
        )
        return txn, holder.wallet
    issuer = accounts[mpt.issuer]
    holder_id = choice(other_accounts)
    txn = MPTokenAuthorize(
        account=issuer.address,
        mptoken_issuance_id=mpt.mpt_issuance_id,
        holder=holder_id,
    )
    return txn, issuer.wallet


async def _mpt_authorize_valid(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    built = _mpt_authorize_base(accounts, mpt_issuances)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("MPTokenAuthorize", txn, client, wallet)


async def _mpt_authorize_faulty(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    built = _mpt_authorize_base(accounts, mpt_issuances)
    if built is None:
        return
    base, wallet = built

    mutation = choice(["fuzz", "fake_issuance", "self_authorize", "duplicate_optin"])
    if mutation == "fuzz":
        await submit_fuzzed("MPTokenAuthorize", base, client, wallet)
        return

    if mutation == "fake_issuance":
        # Opt in to an issuance that does not exist -> tecOBJECT_NOT_FOUND.
        if not accounts:
            return
        holder = choice(list(accounts.values()))
        txn = MPTokenAuthorize(
            account=holder.address,
            mptoken_issuance_id=params.fake_mpt_id(),
        )
        await submit_tx("MPTokenAuthorize", txn, client, holder.wallet)
        return

    mpt = choice(mpt_issuances)
    if mpt.issuer not in accounts:
        return

    if mutation == "self_authorize":
        # Issuer names itself as Holder -> temMALFORMED.
        issuer = accounts[mpt.issuer]
        txn = MPTokenAuthorize(
            account=issuer.address,
            mptoken_issuance_id=mpt.mpt_issuance_id,
            holder=issuer.address,
        )
        await submit_tx("MPTokenAuthorize", txn, client, issuer.wallet)
        return

    # duplicate_optin: holder already authorized re-opts in -> tecDUPLICATE.
    if not mpt.holders:
        return
    held = choice(list(mpt.holders))
    if held not in accounts:
        return
    holder = accounts[held]
    txn = MPTokenAuthorize(
        account=holder.address,
        mptoken_issuance_id=mpt.mpt_issuance_id,
    )
    await submit_tx("MPTokenAuthorize", txn, client, holder.wallet)


# ── Set ──────────────────────────────────────────────────────────────


async def mpt_issuance_set(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _mpt_issuance_set_faulty(accounts, mpt_issuances, client)
    return await _mpt_issuance_set_valid(accounts, mpt_issuances, client)


def _mpt_issuance_set_base(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
) -> tuple[MPTokenIssuanceSet, Wallet] | None:
    """Valid MPTokenIssuanceSet (issuer lock/unlock) + wallet; shared by valid and fuzz."""
    if not mpt_issuances:
        return None
    mpt = choice(mpt_issuances)
    if mpt.issuer not in accounts:
        return None
    issuer = accounts[mpt.issuer]
    flag = choice(list(MPTokenIssuanceSetFlag))
    txn = MPTokenIssuanceSet(
        account=issuer.address,
        mptoken_issuance_id=mpt.mpt_issuance_id,
        flags=flag,
    )
    return txn, issuer.wallet


async def _mpt_issuance_set_valid(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    built = _mpt_issuance_set_base(accounts, mpt_issuances)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("MPTokenIssuanceSet", txn, client, wallet)


async def _mpt_issuance_set_faulty(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    built = _mpt_issuance_set_base(accounts, mpt_issuances)
    if built is None:
        return
    base, wallet = built

    mutation = choice(["fuzz", "fake_issuance", "non_issuer"])
    if mutation == "fuzz":
        await submit_fuzzed("MPTokenIssuanceSet", base, client, wallet)
        return

    flag = choice(list(MPTokenIssuanceSetFlag))

    if mutation == "fake_issuance":
        # Set flags on an issuance that does not exist -> tecOBJECT_NOT_FOUND.
        if not accounts:
            return
        src = choice(list(accounts.values()))
        txn = MPTokenIssuanceSet(
            account=src.address,
            mptoken_issuance_id=params.fake_mpt_id(),
            flags=flag,
        )
        await submit_tx("MPTokenIssuanceSet", txn, client, src.wallet)
        return

    # non_issuer: an account other than the issuer sets flags -> tecNO_PERMISSION.
    mpt = choice(mpt_issuances)
    others = [a for a in accounts if a != mpt.issuer]
    if not others:
        return
    src = accounts[choice(others)]
    txn = MPTokenIssuanceSet(
        account=src.address,
        mptoken_issuance_id=mpt.mpt_issuance_id,
        flags=flag,
    )
    await submit_tx("MPTokenIssuanceSet", txn, client, src.wallet)


# ── Destroy ──────────────────────────────────────────────────────────


async def mpt_destroy(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _mpt_destroy_faulty(accounts, mpt_issuances, client)
    return await _mpt_destroy_valid(accounts, mpt_issuances, client)


def _mpt_destroy_base(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
) -> tuple[MPTokenIssuanceDestroy, Wallet] | None:
    """Valid MPTokenIssuanceDestroy (issuer destroys own issuance) + wallet."""
    if not mpt_issuances:
        return None
    mpt = choice(mpt_issuances)
    if mpt.issuer not in accounts:
        return None
    issuer = accounts[mpt.issuer]
    txn = MPTokenIssuanceDestroy(
        account=issuer.address,
        mptoken_issuance_id=mpt.mpt_issuance_id,
    )
    return txn, issuer.wallet


async def _mpt_destroy_valid(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    built = _mpt_destroy_base(accounts, mpt_issuances)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("MPTokenIssuanceDestroy", txn, client, wallet)


async def _mpt_destroy_faulty(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    built = _mpt_destroy_base(accounts, mpt_issuances)
    if built is None:
        return
    base, wallet = built

    mutation = choice(["fuzz", "fake_issuance", "non_issuer", "has_obligations"])
    if mutation == "fuzz":
        await submit_fuzzed("MPTokenIssuanceDestroy", base, client, wallet)
        return

    if mutation == "fake_issuance":
        # Destroy an issuance that does not exist -> tecOBJECT_NOT_FOUND.
        if not accounts:
            return
        src = choice(list(accounts.values()))
        txn = MPTokenIssuanceDestroy(
            account=src.address,
            mptoken_issuance_id=params.fake_mpt_id(),
        )
        await submit_tx("MPTokenIssuanceDestroy", txn, client, src.wallet)
        return

    mpt = choice(mpt_issuances)
    if mpt.issuer not in accounts:
        return

    if mutation == "non_issuer":
        # Non-issuer tries to destroy -> tecNO_PERMISSION.
        others = [a for a in accounts if a != mpt.issuer]
        if not others:
            return
        src = accounts[choice(others)]
        txn = MPTokenIssuanceDestroy(
            account=src.address,
            mptoken_issuance_id=mpt.mpt_issuance_id,
        )
        await submit_tx("MPTokenIssuanceDestroy", txn, client, src.wallet)
        return

    # has_obligations: destroy an issuance that still has holders -> tecHAS_OBLIGATIONS.
    with_holders = [m for m in mpt_issuances if m.holders and m.issuer in accounts]
    if not with_holders:
        return
    mpt = choice(with_holders)
    issuer = accounts[mpt.issuer]
    txn = MPTokenIssuanceDestroy(
        account=issuer.address,
        mptoken_issuance_id=mpt.mpt_issuance_id,
    )
    await submit_tx("MPTokenIssuanceDestroy", txn, client, issuer.wallet)
