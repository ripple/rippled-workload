"""Dynamic MPT (XLS-0094): DynamicMPTSet submitted as MPTokenIssuanceSet.

The model is opt-in: an issuance is immutable unless a create-time MutableFlags
(tmfMPT*) bit declares a capability/field as mutable. A mutating
MPTokenIssuanceSet then either enables a capability via a MutableFlags set-enable
bit (each requires the matching CAN_ENABLE_* declared at create), rewrites
MPTokenMetadata (requires CAN_MUTATE_METADATA), or sets TransferFee (requires
CAN_MUTATE_TRANSFER_FEE and an already-enabled CanTransfer). All three ride the
typed xrpl-py fields; only the oversize-metadata malformation needs submit_raw.
"""

from __future__ import annotations

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import (
    MPTokenIssuanceCreateMutableFlag,
    MPTokenIssuanceSet,
    MPTokenIssuanceSetMutableFlag,
)
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import MPTokenIssuance, UserAccount
from workload.randoms import choice
from workload.submit import submit_raw, submit_tx

# tmfMPTSet* set-enable bits: which capability a mutation turns on. Each requires
# the matching TMF_MPT_CAN_ENABLE_* declared in the create-time MutableFlags.
_SET_ENABLE_FLAGS = [
    MPTokenIssuanceSetMutableFlag.TMF_MPT_SET_CAN_LOCK,
    MPTokenIssuanceSetMutableFlag.TMF_MPT_SET_REQUIRE_AUTH,
    MPTokenIssuanceSetMutableFlag.TMF_MPT_SET_CAN_ESCROW,
    MPTokenIssuanceSetMutableFlag.TMF_MPT_SET_CAN_TRADE,
    MPTokenIssuanceSetMutableFlag.TMF_MPT_SET_CAN_TRANSFER,
    MPTokenIssuanceSetMutableFlag.TMF_MPT_SET_CAN_CLAWBACK,
]

# create-time MutableFlags bits that must be present to mutate metadata / fee.
_TMF_CAN_MUTATE_METADATA = int(MPTokenIssuanceCreateMutableFlag.TMF_MPT_CAN_MUTATE_METADATA)
_TMF_CAN_MUTATE_TRANSFER_FEE = int(MPTokenIssuanceCreateMutableFlag.TMF_MPT_CAN_MUTATE_TRANSFER_FEE)


def _mutable_issuances(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
) -> list[MPTokenIssuance]:
    """Issuances we issue that opted into mutation (non-zero create-time MutableFlags)."""
    return [m for m in mpt_issuances if m.mutable_flags and m.issuer in accounts]


def _immutable_issuances(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
) -> list[MPTokenIssuance]:
    """Issuances we issue that opted out (zero create-time MutableFlags): every mutation fails."""
    return [m for m in mpt_issuances if not m.mutable_flags and m.issuer in accounts]


async def mpt_issuance_set_dynamic(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _mpt_issuance_set_dynamic_faulty(accounts, mpt_issuances, client)
    return await _mpt_issuance_set_dynamic_valid(accounts, mpt_issuances, client)


def _mpt_issuance_set_dynamic_base(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
) -> tuple[MPTokenIssuanceSet, Wallet] | None:
    """Valid set-enable dynamic mutation + wallet; shared by valid and fuzz."""
    dyn = _mutable_issuances(accounts, mpt_issuances)
    if not dyn:
        return None
    mpt = choice(dyn)
    issuer = accounts[mpt.issuer]
    txn = MPTokenIssuanceSet(
        account=issuer.address,
        mptoken_issuance_id=mpt.mpt_issuance_id,
        mutable_flags=choice(_SET_ENABLE_FLAGS),
    )
    return txn, issuer.wallet


async def _mpt_issuance_set_dynamic_valid(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    dyn = _mutable_issuances(accounts, mpt_issuances)
    if not dyn:
        return
    mpt = choice(dyn)
    issuer = accounts[mpt.issuer]

    mutation = choice(["flag_enable", "metadata", "transfer_fee"])
    if mutation == "metadata" and (mpt.mutable_flags & _TMF_CAN_MUTATE_METADATA):
        txn = MPTokenIssuanceSet(
            account=issuer.address,
            mptoken_issuance_id=mpt.mpt_issuance_id,
            mptoken_metadata=params.mpt_metadata(),
        )
    elif (
        mutation == "transfer_fee"
        and (mpt.mutable_flags & _TMF_CAN_MUTATE_TRANSFER_FEE)
        and mpt.can_transfer
    ):
        # fee>0 needs CanTransfer already enabled at issuance (preclaim rule).
        txn = MPTokenIssuanceSet(
            account=issuer.address,
            mptoken_issuance_id=mpt.mpt_issuance_id,
            transfer_fee=params.mpt_transfer_fee(),
        )
    else:
        # set-enable latch; re-enabling an already-set capability is a no-op tesSUCCESS.
        txn = MPTokenIssuanceSet(
            account=issuer.address,
            mptoken_issuance_id=mpt.mpt_issuance_id,
            mutable_flags=choice(_SET_ENABLE_FLAGS),
        )
    await submit_tx("DynamicMPTSet", txn, client, issuer.wallet)


async def _mpt_issuance_set_dynamic_faulty(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    built = _mpt_issuance_set_dynamic_base(accounts, mpt_issuances)
    if built is None:
        return
    base, wallet = built

    mutation = choice(
        ["fuzz", "fake_issuance", "non_issuer", "immutable_mutation", "oversize_metadata"]
    )
    if mutation == "fuzz":
        await submit_fuzzed("DynamicMPTSet", base, client, wallet)
        return

    if mutation == "fake_issuance":
        # Mutate a non-existent issuance -> tecOBJECT_NOT_FOUND.
        if not accounts:
            return
        src = choice(list(accounts.values()))
        txn = MPTokenIssuanceSet(
            account=src.address,
            mptoken_issuance_id=params.fake_mpt_id(),
            mutable_flags=choice(_SET_ENABLE_FLAGS),
        )
        await submit_tx("DynamicMPTSet", txn, client, src.wallet)
        return

    if mutation == "non_issuer":
        # Non-issuer submits a dynamic mutation -> tecNO_PERMISSION.
        dyn = _mutable_issuances(accounts, mpt_issuances)
        if not dyn:
            return
        mpt = choice(dyn)
        others = [a for a in accounts if a != mpt.issuer]
        if not others:
            return
        src = accounts[choice(others)]
        txn = MPTokenIssuanceSet(
            account=src.address,
            mptoken_issuance_id=mpt.mpt_issuance_id,
            mutable_flags=choice(_SET_ENABLE_FLAGS),
        )
        await submit_tx("DynamicMPTSet", txn, client, src.wallet)
        return

    if mutation == "immutable_mutation":
        # Mutate an issuance that opted out at create -> tecNO_PERMISSION.
        immutable = _immutable_issuances(accounts, mpt_issuances)
        if not immutable:
            return
        mpt = choice(immutable)
        issuer = accounts[mpt.issuer]
        txn = (
            MPTokenIssuanceSet(
                account=issuer.address,
                mptoken_issuance_id=mpt.mpt_issuance_id,
                mptoken_metadata=params.mpt_metadata(),
            )
            if choice([True, False])
            else MPTokenIssuanceSet(
                account=issuer.address,
                mptoken_issuance_id=mpt.mpt_issuance_id,
                mutable_flags=choice(_SET_ENABLE_FLAGS),
            )
        )
        await submit_tx("DynamicMPTSet", txn, client, issuer.wallet)
        return

    # oversize_metadata: MPTokenMetadata > 1024 bytes -> temMALFORMED (xrpl-py
    # rejects the length at construction, so inject it raw).
    def _mutate_oversize(d: dict) -> None:
        d.pop("MutableFlags", None)
        d["MPTokenMetadata"] = "AB" * 1025

    await submit_raw("DynamicMPTSet", base, client, wallet, _mutate_oversize)
