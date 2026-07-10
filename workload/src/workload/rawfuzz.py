"""Raw fuzz band: emit encodings xrpl-py's codec refuses, reaching server paths a
conformant client can't produce. Low-weight escalation from the codec-legal band."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

import xrpl.core.binarycodec.definitions.definitions as _defs

from workload.randoms import randint

# Above every real transaction type: the codec accepts the sentinel mapping, the server's
# transaction-format lookup still finds no match.
_UNKNOWN_TXN_TYPE_NAME = "zzBrutalUnknownTxnType"
_UNKNOWN_TXN_TYPE_CODE = 60000

# Kept low so the codec-legal band stays dominant: finite submit budget per run, and
# codec-legal blobs reach far deeper on average.
RAW_CHANCE = 0.15

_MISSING = object()


@contextmanager
def _injected_txn_types(mapping: dict[str, int]) -> Iterator[None]:
    # Safe under single-process asyncio: encode is synchronous and the sentinel names are
    # unused elsewhere, so a concurrent encode on another task is unaffected while live.
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
    raws = tx_dict.get("RawTransactions")
    if not isinstance(raws, list) or len(raws) < 2:  # target the multi-inner path
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
    # None => caller falls back to the codec-legal band. v1 covers Batch only.
    if name == "Batch":
        ctx = _injected_txn_types({_UNKNOWN_TXN_TYPE_NAME: _UNKNOWN_TXN_TYPE_CODE})
        return (lambda tx_dict: _carry_unknown_inner_txn_type(tx_dict, ops), ctx)
    return None
