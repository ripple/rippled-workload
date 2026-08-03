"""SponsorshipSet compat for xrpld 3.3.0-rc5+ (xrpld-private #335).

rc5 renamed the SponsorshipSet tx fields sfFeeAmount -> sfFeeAmountDelta
(Amount, nth 34) and sfRemainingOwnerCount -> sfRemainingOwnerCountDelta
(Int32, nth 2) and switched both to DELTA semantics: added to the existing
object's value (negative deltas clamp at zero), must be positive on create,
non-zero when present (else temBAD_AMOUNT / temINVALID). The pinned xrpl-py
branch has neither the model fields nor the codec definitions, so this
module injects the field headers into the live binarycodec maps and extends
the model. TEMPORARY -- delete once xrpl-py's pre-3.3-release-group catches
up, then revert imports to xrpl.models.

The Sponsorship LEDGER OBJECT keeps sfFeeAmount/sfRemainingOwnerCount as
absolutes -- only the tx fields changed, so meta-driven state tracking
(_on_sponsorship_set) needs no delta arithmetic.

xrpl-py's codec cannot encode negative XRP amounts, so a negative
fee-amount delta is unreachable from this workload; negative count deltas
(Int32) encode fine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from xrpl.core.binarycodec.definitions import definitions as _defs
from xrpl.core.binarycodec.definitions.field_header import FieldHeader
from xrpl.core.binarycodec.definitions.field_info import FieldInfo
from xrpl.models.amounts import Amount
from xrpl.models.transactions import SponsorshipSet as _UpstreamSponsorshipSet


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


_register_field("FeeAmountDelta", "Amount", 34)
_register_field("RemainingOwnerCountDelta", "Int32", 2)


@dataclass(frozen=True, kw_only=True)
class SponsorshipSet(_UpstreamSponsorshipSet):
    """Upstream model plus the rc5 delta fields. The inherited absolute
    fields (fee_amount/remaining_owner_count) stay constructible but rippled
    now rejects them at the template, so nothing here sets them."""

    # Optional[...] not `| None`: xrpl-py's BaseModel._check_type introspects the
    # annotation at construction and crashes on a PEP 604 UnionType.
    fee_amount_delta: Optional[Amount] = None  # noqa: UP045
    """XRP drops added to the Sponsorship's fee pool (delta; non-zero,
    positive on create)."""

    remaining_owner_count_delta: Optional[int] = None  # noqa: UP045
    """Owner-reserve count added to the Sponsorship (delta; non-zero,
    positive on create; negative clamps at zero on update)."""
