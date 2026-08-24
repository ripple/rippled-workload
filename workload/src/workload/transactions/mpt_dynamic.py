"""Dynamic MPT (XLS-0094): DynamicMPTSet submitted as MPTokenIssuanceSet.

Under DynamicMPT an issuance is mutable by default; a create-time ImmutableFlags
(tifMPT*) bit permanently freezes a capability/field. A mutating
MPTokenIssuanceSet then either enables a capability via a set-enable bit in the
Flags field (MPTokenIssuanceSetFlag.TF_MPT_SET_*), rewrites MPTokenMetadata, or
sets TransferFee (fee>0 needs CanTransfer already enabled at issuance). All three
ride the typed xrpl-py fields; only the oversize-metadata malformation needs
submit_raw.
"""

from __future__ import annotations

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import (
    MPTokenIssuanceSet,
    MPTokenIssuanceSetFlag,
)
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import MPTokenIssuance, UserAccount
from workload.randoms import choice
from workload.submit import submit_raw, submit_tx

# TF_MPT_SET_* set-enable bits carried in the Flags field; each turns on the
# matching lsfMPTCan* capability on the issuance (a one-way latch).
_SET_ENABLE_FLAGS = [
    MPTokenIssuanceSetFlag.TF_MPT_SET_CAN_LOCK,
    MPTokenIssuanceSetFlag.TF_MPT_SET_REQUIRE_AUTH,
    MPTokenIssuanceSetFlag.TF_MPT_SET_CAN_ESCROW,
    MPTokenIssuanceSetFlag.TF_MPT_SET_CAN_TRADE,
    MPTokenIssuanceSetFlag.TF_MPT_SET_CAN_TRANSFER,
    MPTokenIssuanceSetFlag.TF_MPT_SET_CAN_CLAWBACK,
]


def _mutable_issuances(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
) -> list[MPTokenIssuance]:
    """Dynamic-cohort issuances we issue that stayed fully mutable (no ImmutableFlags)."""
    return [
        m for m in mpt_issuances if m.dynamic and not m.immutable_flags and m.issuer in accounts
    ]


def _immutable_issuances(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
) -> list[MPTokenIssuance]:
    """Dynamic-cohort issuances frozen at create (ImmutableFlags set): every mutation fails."""
    return [m for m in mpt_issuances if m.dynamic and m.immutable_flags and m.issuer in accounts]


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
        flags=choice(_SET_ENABLE_FLAGS),
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
    if mutation == "metadata":
        # MPTokenMetadata is mutable by default (not frozen via ImmutableFlags).
        txn = MPTokenIssuanceSet(
            account=issuer.address,
            mptoken_issuance_id=mpt.mpt_issuance_id,
            mptoken_metadata=params.mpt_metadata(),
        )
    elif mutation == "transfer_fee" and mpt.can_transfer:
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
            flags=choice(_SET_ENABLE_FLAGS),
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
            flags=choice(_SET_ENABLE_FLAGS),
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
            flags=choice(_SET_ENABLE_FLAGS),
        )
        await submit_tx("DynamicMPTSet", txn, client, src.wallet)
        return

    if mutation == "immutable_mutation":
        # Mutate a field/flag frozen at create via ImmutableFlags -> tecNO_PERMISSION.
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
                flags=choice(_SET_ENABLE_FLAGS),
            )
        )
        await submit_tx("DynamicMPTSet", txn, client, issuer.wallet)
        return

    # oversize_metadata: MPTokenMetadata > 1024 bytes -> temMALFORMED (xrpl-py
    # rejects the length at construction, so inject it raw).
    def _mutate_oversize(d: dict) -> None:
        d.pop("Flags", None)
        d["MPTokenMetadata"] = "AB" * 1025

    await submit_raw("DynamicMPTSet", base, client, wallet, _mutate_oversize)
