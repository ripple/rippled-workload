"""Raw fuzz band: emit encodings xrpl-py's codec refuses, reaching server paths a
conformant client can't produce. Low-weight escalation from the codec-legal band."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass

import xrpl.core.binarycodec.definitions.definitions as _defs

from workload.randoms import choice, randint

# Above every real transaction type: the codec accepts the sentinel mapping, the server's
# transaction-format lookup still finds no match.
_UNKNOWN_TXN_TYPE_NAME = "zzBrutalUnknownTxnType"
_UNKNOWN_TXN_TYPE_CODE = 60000

# Kept low so the codec-legal band stays dominant: finite submit budget per run, and
# codec-legal blobs reach far deeper on average.
RAW_CHANCE = 0.15

# Corrupted first because nested arrays/objects give field-aware ops the most to hit;
# widened to all types once the band proves out.
_STARTER_TYPES = frozenset(
    {"Batch", "Payment", "OfferCreate", "AMMDeposit", "AMMWithdraw", "SignerListSet", "NFTokenMint"}
)

_MISSING = object()


@dataclass
class Escalation:
    dict_mutate: Callable[[dict], None] | None = None
    encode_ctx: AbstractContextManager[None] | None = None
    blob_mutate: Callable[[bytes], bytes] | None = None


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


# ── Surface 2: parseable but no conformant client emits it; needs a valid signature ──


def _carry_unknown_inner_txn_type(tx_dict: dict) -> None:
    raws = tx_dict.get("RawTransactions")
    if not isinstance(raws, list) or len(raws) < 2:  # target the multi-inner path
        return
    entry = raws[randint(0, len(raws) - 1)]
    inner = entry.get("RawTransaction") if isinstance(entry, dict) else None
    if isinstance(inner, dict):
        inner["TransactionType"] = _UNKNOWN_TXN_TYPE_NAME


def _unknown_inner_txn_type() -> Escalation:
    return Escalation(
        dict_mutate=_carry_unknown_inner_txn_type,
        encode_ctx=_injected_txn_types({_UNKNOWN_TXN_TYPE_NAME: _UNKNOWN_TXN_TYPE_CODE}),
    )


# ── Surface 1: post-sign byte corruption; the deserializer runs before signature checks ──


def _random_bytes(n: int) -> bytes:
    return bytes(randint(0, 255) for _ in range(n))


def _truncate(blob: bytes) -> bytes:
    if len(blob) <= 8:
        return blob
    return blob[: randint(1, len(blob) - 1)]


def _trailing_garbage(blob: bytes) -> bytes:
    return blob + _random_bytes(randint(1, 64))


def _byte_flip(blob: bytes) -> bytes:
    if not blob:
        return blob
    b = bytearray(blob)
    for _ in range(randint(1, 4)):
        b[randint(0, len(b) - 1)] ^= 1 << randint(0, 7)
    return bytes(b)


@dataclass(frozen=True)
class _Operator:
    tag: str
    weight: int
    applies: Callable[[str], bool]
    build: Callable[[], Escalation]


def _blob_op(fn: Callable[[bytes], bytes]) -> Callable[[], Escalation]:
    return lambda: Escalation(blob_mutate=fn)


_OPERATORS: tuple[_Operator, ...] = (
    _Operator("unknown_inner_txn_type", 3, lambda n: n == "Batch", _unknown_inner_txn_type),
    _Operator("truncate", 1, lambda n: n in _STARTER_TYPES, _blob_op(_truncate)),
    _Operator("trailing_garbage", 1, lambda n: n in _STARTER_TYPES, _blob_op(_trailing_garbage)),
    _Operator("byte_flip", 1, lambda n: n in _STARTER_TYPES, _blob_op(_byte_flip)),
)


def escalate(name: str, ops: list[str]) -> Escalation | None:
    """Pick a weighted raw operator applicable to ``name``, else ``None`` so the caller
    falls back to the codec-legal band."""
    pool = [op for op in _OPERATORS if op.applies(name) for _ in range(op.weight)]
    if not pool:
        return None
    op = choice(pool)
    ops.append(f"raw:{op.tag}")
    return op.build()
