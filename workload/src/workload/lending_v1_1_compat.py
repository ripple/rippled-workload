"""Closed-ended Vault compat for LendingProtocolV1_1 (XLS-65, rippled PR #587).

PR #587 adds three VaultCreate transaction fields -- sfVaultKind (UInt8, nth
22), sfSubscriptionDate (UInt32, nth 75) and sfRedemptionDate (UInt32, nth 76)
-- plus a Vault ledger field sfLEVersion (UInt8, nth 6). The pinned xrpl-py
branch carries neither the codec definitions nor the model fields, so this
module injects the field headers into the live binarycodec maps and extends the
model. TEMPORARY -- delete once xrpl-py's pre-3.3-release-group catches up,
then revert imports to xrpl.models.

sfLEVersion needs no codec entry: it is protocol-written, never a transaction
field, and reaches the workload only as JSON metadata. VaultDelete's optional
sfMemoData deletion reason needs no codec entry either -- MemoData is a
long-standing Blob field -- only the model extension below.

AMENDMENT GATING. featureLendingProtocolV1_1 is Supported::No on rippled
develop; generate_genesis.py now enables it anyway (matching Dockerfile.xrpld's
Supported::No rewrite), but a run against a node without it would get
temDISABLED on any of the three fields. So every closed-ended path is gated on
enabled(), which flips only after a validated Vault came back carrying
LEVersion >= 1 (rippled stamps it on every VaultCreate once the amendment is
active, so the setup vault phase settles this before any driver runs).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

import xrpl.models.transactions as _models
from xrpl.core.binarycodec.definitions import definitions as _defs
from xrpl.core.binarycodec.definitions.field_header import FieldHeader
from xrpl.core.binarycodec.definitions.field_info import FieldInfo
from xrpl.models.transactions import VaultCreate as _UpstreamVaultCreate
from xrpl.models.transactions import VaultDelete as _UpstreamVaultDelete


def _register_field(name: str, type_name: str, nth: int) -> None:
    if name in _defs._FIELD_INFO_MAP:
        return  # xrpl-py caught up -- keep its definition, this shim is now dead
    header = FieldHeader(_defs._TYPE_ORDINAL_MAP[type_name], nth)
    if header in _defs._FIELD_HEADER_NAME_MAP:
        # A silent collision would decode as the wrong field on every response.
        raise RuntimeError(
            f"field header {type_name}/{nth} already taken by"
            f" {_defs._FIELD_HEADER_NAME_MAP[header]}"
        )
    _defs._DEFINITIONS["FIELDS"][name] = {
        "nth": nth,
        "isVLEncoded": False,
        "isSerialized": True,
        "isSigningField": True,
        "type": type_name,
    }
    _defs._FIELD_INFO_MAP[name] = FieldInfo(nth, False, True, True, type_name)
    _defs._FIELD_HEADER_NAME_MAP[header] = name


_register_field("VaultKind", "UInt8", 22)
_register_field("SubscriptionDate", "UInt32", 75)
_register_field("RedemptionDate", "UInt32", 76)


class VaultKind(IntEnum):
    """rippled's VaultKind (Protocol.h); absent sfVaultKind means OpenEnded."""

    OPEN_ENDED = 0
    CLOSED_ENDED = 1


class VaultPhase(IntEnum):
    """Lifecycle phase of a Vault. Open-ended vaults are always NO_PHASE."""

    NO_PHASE = 0
    SUBSCRIPTION = 1
    INVESTMENT = 2
    REDEMPTION = 3


# Bounds on RedemptionDate - SubscriptionDate that VaultCreate preflight
# enforces (rippled Protocol.h kMin/kMaxInvestmentPeriod): min <= gap < max.
MIN_INVESTMENT_PERIOD = 60
MAX_INVESTMENT_PERIOD = 946_708_560


def vault_phase(
    kind: int | None,
    subscription_date: int | None,
    redemption_date: int | None,
    now: int,
) -> VaultPhase:
    """rippled's getVaultPhase (VaultHelpers.cpp) over tracked Vault state.
    Subscription includes now == SubscriptionDate; Investment starts strictly
    after it and runs through RedemptionDate."""
    if (
        int(kind or 0) != VaultKind.CLOSED_ENDED
        or subscription_date is None
        or redemption_date is None
    ):
        return VaultPhase.NO_PHASE
    if now <= subscription_date:
        return VaultPhase.SUBSCRIPTION
    if now <= redemption_date:
        return VaultPhase.INVESTMENT
    return VaultPhase.REDEMPTION


@dataclass(frozen=True, kw_only=True)
class VaultCreate(_UpstreamVaultCreate):
    """Upstream model plus the PR #587 closed-ended fields. Validation stays
    server-side so faulty handlers can build the malformed combinations."""

    # Optional[...] not `| None`: xrpl-py's BaseModel._check_type introspects the
    # annotation at construction and crashes on a PEP 604 UnionType.
    vault_kind: Optional[int] = None  # noqa: UP045
    """0 open-ended (default when absent), 1 closed-ended."""

    subscription_date: Optional[int] = None  # noqa: UP045
    """Ripple-epoch second the Subscription phase ends. Closed-ended only."""

    redemption_date: Optional[int] = None  # noqa: UP045
    """Ripple-epoch second the Investment phase ends. Closed-ended only."""


@dataclass(frozen=True, kw_only=True)
class VaultDelete(_UpstreamVaultDelete):
    """Upstream model plus the V1.1 sfMemoData deletion reason. Length stays
    unvalidated so faulty handlers can build the out-of-range cases."""

    memo_data: Optional[str] = None  # noqa: UP045
    """Hex deletion reason, 1-256 bytes (rippled kMaxDataPayloadLength)."""


# autofill/sign round-trip every tx through Transaction.from_dict, which
# resolves the class by live getattr on this module namespace -- without this
# rebind it lands on the upstream class and rejects the new kwargs.
_models.VaultCreate = VaultCreate
_models.VaultDelete = VaultDelete


_enabled = False


def enabled() -> bool:
    """True once a validated Vault proved featureLendingProtocolV1_1 is active."""
    return _enabled


def note_vault_le_version(le_version: int | str | None) -> None:
    """Latch the amendment from a created Vault's LEVersion. Called from the
    VaultCreate state updater, so the flag is set by a validated ledger entry
    rather than an amendment-table read the public port may not serve."""
    global _enabled
    if _enabled:
        return
    try:
        if int(le_version or 0) >= 1:
            _enabled = True
    except (TypeError, ValueError):
        return
