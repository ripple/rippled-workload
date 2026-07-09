"""Raw fuzz band: emit encodings xrpl-py's codec refuses, so rippled's C++
deserializer and post-parse logic take the hit.

The codec-legal band (``fuzz.py``) mutates the tx dict and stays within what xrpl-py
will encode: every blob it emits parses at the top level and reaches doApply, the
highest-yield surface per submit. This module is the low-weight escalation that leaves
that band by corrupting *through* the codec's own gatekeeping — the class of inputs a
conformant client library can never produce.

v1 operator: the Batch inner-transaction carrier. A validly-signed outer Batch whose
inner carries an unregistered ``TransactionType`` (60000). The outer parses and its
signature verifies; the inner survives outer deserialization (the raw-transaction
field has no inner-object template to reject an unknown type) and only detonates
later, when batch fee recomputation reconstructs the inner as a full transaction and
throws on the unknown type. xrpl-py cannot emit this: its codec maps transaction-type
names to codes and rejects unknown names at encode time. We register a sentinel name
-> 60000 in the codec's transaction-type map for the duration of the encode; the
field is a plain UInt16, so the sentinel serializes to the code and the rest of the
blob stays canonical.

Byte- and field-level corruption (bad length prefixes, unregistered field codes,
truncation) is a separate future band that assembles the blob field-by-field rather
than riding xrpl-py's encoder; it is not wired here yet.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

import xrpl.core.binarycodec.definitions.definitions as _defs

from workload.randoms import randint

# Sentinel transaction-type name -> unregistered on-ledger code. 60000 sits well above
# any real transaction type, so rippled's transaction-format lookup returns null and
# the deferred inner-transaction reconstruction throws.
_UNKNOWN_TXN_TYPE_NAME = "zzBrutalUnknownTxnType"
_UNKNOWN_TXN_TYPE_CODE = 60000

# Fraction of submit_fuzzed calls that attempt the raw band. Kept low so the codec-legal
# band stays dominant — Antithesis has a finite submit budget per run and codec-legal
# blobs reach far deeper on average.
RAW_CHANCE = 0.15

_MISSING = object()


@contextmanager
def _injected_txn_types(mapping: dict[str, int]) -> Iterator[None]:
    """Temporarily add ``name -> code`` entries to the codec's transaction-type map so
    ``encode`` emits codes it would otherwise reject, restoring the map on exit.

    Safe under the workload's single-process asyncio: ``encode`` is synchronous and the
    sentinel names are unused elsewhere, so a concurrent encode on another task is
    unaffected even while the entry is live.
    """
    tt = _defs._DEFINITIONS["TRANSACTION_TYPES"]
    original = {name: tt.get(name, _MISSING) for name in mapping}
    tt.update(mapping)
    try:
        yield
    finally:
        for name, prev in original.items():
            if prev is _MISSING:
                tt.pop(name, None)
            else:
                tt[name] = prev


def _carry_unknown_inner_txn_type(tx_dict: dict, ops: list[str]) -> None:
    """Overwrite one inner batch transaction's ``TransactionType`` with the unknown-type
    sentinel. Requires >=2 inners (the fee-recompute path the throw lives on)."""
    raws = tx_dict.get("RawTransactions")
    if not isinstance(raws, list) or len(raws) < 2:
        return
    entry = raws[randint(0, len(raws) - 1)]
    inner = entry.get("RawTransaction") if isinstance(entry, dict) else None
    if not isinstance(inner, dict):
        return
    inner["TransactionType"] = _UNKNOWN_TXN_TYPE_NAME
    ops.append("raw:unknown_inner_txn_type")


def escalate(
    name: str, ops: list[str]
) -> tuple[Callable[[dict], None], AbstractContextManager[None]] | None:
    """If a raw operator applies to transaction ``name``, return ``(mutate, encode_ctx)``
    for ``submit_raw``; else ``None`` so the caller falls back to the codec-legal band.
    The ``encode_ctx`` registers the sentinel codes the mutation needs for the duration
    of the encode. v1 covers Batch only."""
    if name == "Batch":
        ctx = _injected_txn_types({_UNKNOWN_TXN_TYPE_NAME: _UNKNOWN_TXN_TYPE_CODE})
        return (lambda tx_dict: _carry_unknown_inner_txn_type(tx_dict, ops), ctx)
    return None
