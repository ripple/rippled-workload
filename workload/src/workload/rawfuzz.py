"""Raw fuzz band: emit encodings xrpl-py's codec refuses, reaching server paths a
conformant client can't produce. Low-weight escalation from the codec-legal band."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass

import xrpl.core.binarycodec.definitions.definitions as _defs

from workload import assembler
from workload.randoms import choice, randint

# Above every real transaction type: the codec accepts the sentinel mapping, the server's
# transaction-format lookup still finds no match.
_UNKNOWN_TXN_TYPE_NAME = "zzBrutalUnknownTxnType"
_UNKNOWN_TXN_TYPE_CODE = 60000

# Kept low so the codec-legal band stays dominant: finite submit budget per run, and
# codec-legal blobs reach far deeper on average.
RAW_CHANCE = 0.15

# Only these carry a top-level STArray/STObject unconditionally, so drop_end_marker has a
# terminator to strip. Every other raw op is byte- or field-generic and applies to any type.
_CONTAINER_TYPES = frozenset({"Batch", "SignerListSet"})

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


# Field-aware ops: parse the blob into field spans, splice one, reassemble. Structure the
# codec enforces (canonical order, known field codes, honest VL prefixes) but rippled's
# deserializer must still reject without crashing.


def _field_id(type_code: int, field_code: int) -> bytes:
    if type_code < 16 and field_code < 16:
        return bytes([(type_code << 4) | field_code])
    if type_code < 16:
        return bytes([type_code << 4, field_code])
    if field_code < 16:
        return bytes([field_code, type_code])
    return bytes([0, type_code, field_code])


def _duplicate_field(blob: bytes) -> bytes:
    fields = assembler.parse(blob)
    if not fields:
        return blob
    i = randint(0, len(fields) - 1)
    fields.insert(i, fields[i])
    return assembler.reassemble(fields)


def _reorder_fields(blob: bytes) -> bytes:
    fields = assembler.parse(blob)
    if len(fields) < 2:
        return blob
    i = randint(0, len(fields) - 1)
    j = randint(0, len(fields) - 2)
    if j >= i:  # keep j distinct from i so the swap always changes byte order
        j += 1
    fields[i], fields[j] = fields[j], fields[i]
    return assembler.reassemble(fields)


def _unknown_field_code(blob: bytes) -> bytes:
    fields = assembler.parse(blob)
    # UInt32 type (code 2) with an unregistered field code so the parser hits an unknown field.
    injected = assembler.Field("", "UInt32", False, _field_id(2, 255), _random_bytes(4))
    fields.insert(randint(0, len(fields)), injected)
    return assembler.reassemble(fields)


def _vl_length_lie(blob: bytes) -> bytes:
    fields = assembler.parse(blob)
    vl = [f for f in fields if f.is_vl and f.value]
    if not vl:
        return blob
    f = choice(vl)
    content = f.value[assembler.vl_prefix_len(f) :]
    f.value = bytes([randint(0, 192)]) + content  # single-byte prefix lying about the length
    return assembler.reassemble(fields)


def _drop_end_marker(blob: bytes) -> bytes:
    fields = assembler.parse(blob)
    containers = [f for f in fields if f.type_name in ("STArray", "STObject") and f.value]
    if not containers:
        return blob
    target = choice(containers)
    target.value = target.value[:-1]  # drop the terminating end-marker byte
    return assembler.reassemble(fields)


@dataclass(frozen=True)
class _Operator:
    tag: str
    weight: int
    applies: Callable[[str], bool]
    build: Callable[[], Escalation]


def _blob_op(fn: Callable[[bytes], bytes]) -> Callable[[], Escalation]:
    return lambda: Escalation(blob_mutate=fn)


def _any(_: str) -> bool:
    return True


def _has_container(n: str) -> bool:
    return n in _CONTAINER_TYPES


_OPERATORS: tuple[_Operator, ...] = (
    _Operator("unknown_inner_txn_type", 3, lambda n: n == "Batch", _unknown_inner_txn_type),
    _Operator("truncate", 1, _any, _blob_op(_truncate)),
    _Operator("trailing_garbage", 1, _any, _blob_op(_trailing_garbage)),
    _Operator("byte_flip", 1, _any, _blob_op(_byte_flip)),
    _Operator("duplicate_field", 1, _any, _blob_op(_duplicate_field)),
    _Operator("reorder_fields", 1, _any, _blob_op(_reorder_fields)),
    _Operator("unknown_field_code", 1, _any, _blob_op(_unknown_field_code)),
    _Operator("vl_length_lie", 1, _any, _blob_op(_vl_length_lie)),
    _Operator("drop_end_marker", 1, _has_container, _blob_op(_drop_end_marker)),
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
