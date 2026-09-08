"""Temporary xrpl-py compatibility for LendingProtocolV1_1 (PR #1034)."""

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
        return
    header = FieldHeader(_defs._TYPE_ORDINAL_MAP[type_name], nth)
    if header in _defs._FIELD_HEADER_NAME_MAP:
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
    OPEN_ENDED = 0
    CLOSED_ENDED = 1


class VaultPhase(IntEnum):
    NO_PHASE = 0
    SUBSCRIPTION = 1
    INVESTMENT = 2
    REDEMPTION = 3


# rippled Protocol.h kMinInvestmentPeriod.
MIN_INVESTMENT_PERIOD = 180


def vault_phase(
    kind: int | None,
    subscription_date: int | None,
    redemption_date: int | None,
    now: int,
) -> VaultPhase:
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
    """VaultCreate with server-validated closed-ended fields."""

    # Optional[...] not `| None`: xrpl-py's BaseModel._check_type introspects the
    # annotation at construction and crashes on a PEP 604 UnionType.
    vault_kind: Optional[int] = None  # noqa: UP045
    subscription_date: Optional[int] = None  # noqa: UP045
    redemption_date: Optional[int] = None  # noqa: UP045


@dataclass(frozen=True, kw_only=True)
class VaultDelete(_UpstreamVaultDelete):
    """VaultDelete with server-validated MemoData."""

    memo_data: Optional[str] = None  # noqa: UP045


# Transaction.from_dict resolves these classes from the live module namespace.
_models.VaultCreate = VaultCreate
_models.VaultDelete = VaultDelete


_enabled = False


def enabled() -> bool:
    return _enabled


def note_vault_le_version(le_version: int | str | None) -> None:
    global _enabled
    if _enabled:
        return
    try:
        if int(le_version or 0) >= 1:
            _enabled = True
    except (TypeError, ValueError):
        return
