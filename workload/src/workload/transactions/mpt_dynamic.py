"""Dynamic MPT (XLS-0094) mutations submitted as MPTokenIssuanceSet."""

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import (
    MPTokenIssuanceImmutableFlag,
    MPTokenIssuanceSet,
    MPTokenIssuanceSetFlag,
)
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import MPTokenIssuance, UserAccount
from workload.randoms import choice
from workload.submit import submit_raw, submit_tx

_SET_FLAG_PAIRS = (
    (MPTokenIssuanceSetFlag.TF_MPT_SET_CAN_LOCK, MPTokenIssuanceImmutableFlag.TIF_MPT_CAN_LOCK),
    (
        MPTokenIssuanceSetFlag.TF_MPT_SET_REQUIRE_AUTH,
        MPTokenIssuanceImmutableFlag.TIF_MPT_REQUIRE_AUTH,
    ),
    (
        MPTokenIssuanceSetFlag.TF_MPT_SET_CAN_ESCROW,
        MPTokenIssuanceImmutableFlag.TIF_MPT_CAN_ESCROW,
    ),
    (MPTokenIssuanceSetFlag.TF_MPT_SET_CAN_TRADE, MPTokenIssuanceImmutableFlag.TIF_MPT_CAN_TRADE),
    (
        MPTokenIssuanceSetFlag.TF_MPT_SET_CAN_TRANSFER,
        MPTokenIssuanceImmutableFlag.TIF_MPT_CAN_TRANSFER,
    ),
    (
        MPTokenIssuanceSetFlag.TF_MPT_SET_CAN_CLAWBACK,
        MPTokenIssuanceImmutableFlag.TIF_MPT_CAN_CLAWBACK,
    ),
    (
        MPTokenIssuanceSetFlag.TF_MPT_SET_CAN_HOLD_CONFIDENTIAL_BALANCE,
        MPTokenIssuanceImmutableFlag.TIF_MPT_CAN_HOLD_CONFIDENTIAL_BALANCE,
    ),
)
_SET_ENABLE_FLAGS = tuple(flag for flag, _ in _SET_FLAG_PAIRS)
_VALID_SET_FLAG_PAIRS = tuple(
    pair for pair in _SET_FLAG_PAIRS if pair[0] != MPTokenIssuanceSetFlag.TF_MPT_SET_REQUIRE_AUTH
)
_SAFE_IMMUTABLE_FLAGS = (
    MPTokenIssuanceImmutableFlag.TIF_MPT_CAN_LOCK,
    MPTokenIssuanceImmutableFlag.TIF_MPT_CAN_TRANSFER,
)
_ALL_IMMUTABLE_FLAGS = sum(int(flag) for flag in MPTokenIssuanceImmutableFlag)


def _dynamic_issuances(
    accounts: dict[str, UserAccount], mpt_issuances: list[MPTokenIssuance], *, immutable: bool
) -> list[MPTokenIssuance]:
    return [
        m
        for m in mpt_issuances
        if m.dynamic
        and (m.immutable_flags == _ALL_IMMUTABLE_FLAGS) is immutable
        and m.issuer in accounts
    ]


def _available_set_flags(mpt: MPTokenIssuance) -> list[MPTokenIssuanceSetFlag]:
    return [flag for flag, frozen in _VALID_SET_FLAG_PAIRS if not mpt.immutable_flags & int(frozen)]


async def mpt_issuance_set_dynamic(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _dynamic_faulty(accounts, mpt_issuances, client)
    return await _dynamic_valid(accounts, mpt_issuances, client)


def _dynamic_base(
    accounts: dict[str, UserAccount], mpt_issuances: list[MPTokenIssuance]
) -> tuple[MPTokenIssuanceSet, Wallet] | None:
    mutable = _dynamic_issuances(accounts, mpt_issuances, immutable=False)
    if not mutable:
        return None
    mpt = choice(mutable)
    flags = _available_set_flags(mpt)
    if not flags:
        return None
    issuer = accounts[mpt.issuer]
    return (
        MPTokenIssuanceSet(
            account=issuer.address,
            mptoken_issuance_id=mpt.mpt_issuance_id,
            flags=choice(flags),
        ),
        issuer.wallet,
    )


async def _dynamic_valid(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    mutable = _dynamic_issuances(accounts, mpt_issuances, immutable=False)
    if not mutable:
        return
    mpt = choice(mutable)
    issuer = accounts[mpt.issuer]
    mutations = []
    if not mpt.immutable_flags & int(MPTokenIssuanceImmutableFlag.TIF_MPT_METADATA):
        mutations += ["metadata"] * 3
    if not mpt.can_hold_confidential and not mpt.immutable_flags & int(
        MPTokenIssuanceImmutableFlag.TIF_MPT_TRANSFER_FEE
    ):
        mutations += ["transfer_fee"] * 3
    if _available_set_flags(mpt):
        mutations.append("flag_enable")
    immutable_flags = [
        flag for flag in _SAFE_IMMUTABLE_FLAGS if not mpt.immutable_flags & int(flag)
    ]
    if immutable_flags:
        mutations.append("immutable")
    if not mutations:
        return
    mutation = choice(mutations)

    if mutation == "transfer_fee":
        txn = MPTokenIssuanceSet(
            account=issuer.address,
            mptoken_issuance_id=mpt.mpt_issuance_id,
            transfer_fee=params.mpt_transfer_fee(),
        )
    elif mutation == "flag_enable":
        flags = _available_set_flags(mpt)
        flag = choice(flags)
        txn = MPTokenIssuanceSet(
            account=issuer.address,
            mptoken_issuance_id=mpt.mpt_issuance_id,
            flags=flag,
            transfer_fee=(
                0
                if flag == MPTokenIssuanceSetFlag.TF_MPT_SET_CAN_HOLD_CONFIDENTIAL_BALANCE
                else None
            ),
        )
    elif mutation == "immutable":
        txn = MPTokenIssuanceSet(
            account=issuer.address,
            mptoken_issuance_id=mpt.mpt_issuance_id,
            immutable_flags=choice(immutable_flags),
        )
    else:
        txn = MPTokenIssuanceSet(
            account=issuer.address,
            mptoken_issuance_id=mpt.mpt_issuance_id,
            mptoken_metadata=params.mpt_metadata(),
        )
    await submit_tx("DynamicMPTSet", txn, client, issuer.wallet)


async def _dynamic_faulty(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    built = _dynamic_base(accounts, mpt_issuances)
    if built is None:
        return
    base, wallet = built
    mutation = choice(["fuzz", "fake_issuance", "non_issuer", "immutable", "oversize_metadata"])
    if mutation == "fuzz":
        await submit_fuzzed("DynamicMPTSet", base, client, wallet)
        return

    if mutation == "fake_issuance":
        src = choice(list(accounts.values()))
        txn = base.__replace__(account=src.address, mptoken_issuance_id=params.fake_mpt_id())
        await submit_tx("DynamicMPTSet", txn, client, src.wallet)
        return

    if mutation == "non_issuer":
        mpt = choice(_dynamic_issuances(accounts, mpt_issuances, immutable=False))
        others = [a for a in accounts.values() if a.address != mpt.issuer]
        if not others:
            return
        src = choice(others)
        txn = base.__replace__(account=src.address, mptoken_issuance_id=mpt.mpt_issuance_id)
        await submit_tx("DynamicMPTSet", txn, client, src.wallet)
        return

    if mutation == "immutable":
        frozen = _dynamic_issuances(accounts, mpt_issuances, immutable=True)
        if not frozen:
            return
        mpt = choice(frozen)
        issuer = accounts[mpt.issuer]
        txn = MPTokenIssuanceSet(
            account=issuer.address,
            mptoken_issuance_id=mpt.mpt_issuance_id,
            mptoken_metadata=params.mpt_metadata(),
        )
        await submit_tx("DynamicMPTSet", txn, client, issuer.wallet)
        return

    def mutate(d: dict) -> None:
        d.pop("Flags", None)
        d["MPTokenMetadata"] = "AB" * 1025

    await submit_raw("DynamicMPTSet", base, client, wallet, mutate)
