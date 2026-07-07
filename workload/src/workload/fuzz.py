"""Generative mutation for ``_faulty`` paths: corrupt a valid tx dict, kept encodable+signed."""

from __future__ import annotations

from antithesis.lifecycle import send_event
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.core.binarycodec.definitions.definitions import load_definitions
from xrpl.core.binarycodec.exceptions import XRPLBinaryCodecException
from xrpl.models.transactions.transaction import Transaction
from xrpl.wallet import Wallet

from workload import params
from workload.randoms import choice, randint, random
from workload.submit import submit_raw

# Left intact: mutating these yields shallow auth/sequence/fee rejects instead
# of reaching the transactor logic we want to fuzz.
_PROTECTED = {
    "TransactionType",
    "Account",
    "Sequence",
    "Fee",
    "SigningPubKey",
    "TxnSignature",
    "LastLedgerSequence",
    "NetworkID",
}

# Serialization types we can synthesize an encodable hostile value for. Complex
# types (STObject/STArray/PathSet/Issue/Number/…) are omitted: fabricating them
# from nothing skews toward codec rejects, not the transactor coverage we want.
_INJECTABLE_TYPES = {
    "UInt8",
    "UInt16",
    "UInt32",
    "UInt64",
    "Hash128",
    "Hash160",
    "Hash256",
    "AccountID",
    "Amount",
    "Blob",
}


def _load_injectable() -> list[tuple[str, str]]:
    """Protocol-known signing fields we can inject; sourced from the codec so it
    tracks the linked xrpl-py version instead of a hardcoded list."""
    fields = load_definitions()["FIELDS"]
    return [
        (name, meta["type"])
        for name, meta in fields.items()
        if meta.get("isSerialized")
        and meta.get("isSigningField")
        and meta["type"] in _INJECTABLE_TYPES
        and name not in _PROTECTED
    ]


_INJECTABLE = _load_injectable()


def _is_hex(s: str) -> bool:
    try:
        int(s, 16)
        return True
    except ValueError:
        return False


def _hostile_amount(value: dict) -> dict:
    """Attack one component of an Amount object, staying encodable. IOU carries
    currency/issuer/value; MPT carries mpt_issuance_id/value."""
    out = dict(value)
    if "mpt_issuance_id" in value:
        if choice(["value", "id"]) == "value":
            out["value"] = choice(["0", "-1", "9999999999999999"])
        else:
            out["mpt_issuance_id"] = params.fake_mpt_id()
        return out
    key = choice([k for k in ("value", "currency", "issuer") if k in value])
    if key == "value":
        out["value"] = choice(["0", "-1", "9999999999999999"])
    elif key == "currency":
        # "XRP" and non-standard hex codes are encodable but illegal as an IOU.
        out["currency"] = choice(["XRP", "0" * 40, "F" * 40])
    else:
        out["issuer"] = params.fake_account()
    return out


def _hostile(value: object, depth: int = 0) -> object:
    """Return a hostile-but-encodable variant of ``value``, inferred from shape.
    Recurses one level into nested objects/arrays so inner fields aren't spared."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return choice([0, 1, 0xFFFFFFFF, randint(0, 0xFFFFFFFF)])
    if isinstance(value, dict):
        if "value" in value:
            return _hostile_amount(value)
        if depth >= 3 or not value:
            return value
        # Structural STObject wrapper (e.g. {"Memo": {...}}): recurse into a field.
        k = choice(list(value))
        out = dict(value)
        out[k] = _hostile(value[k], depth + 1)
        return out
    if isinstance(value, list):
        kind = choice(["empty", "oversize", "mutate_elem"])
        if kind == "empty":
            return []
        if kind == "oversize" or not value or depth >= 3:
            return (value or [{}]) * 6
        idx = randint(0, len(value) - 1)
        items = list(value)
        items[idx] = _hostile(items[idx], depth + 1)
        return items
    if isinstance(value, str):
        if len(value) == 64 and _is_hex(value):  # Hash256
            return choice(["0" * 64, "F" * 64, params.fake_id()])
        if value.isdigit():  # XRP drops
            return choice(["0", "99999999999999999"])
        if value.startswith("r") and 25 <= len(value) <= 35:  # AccountID
            return params.fake_account()
        return choice(["", value[::-1]])
    return value


def _hostile_for_type(field_type: str) -> object:
    """Encodable hostile value for a freshly injected field of ``field_type``."""
    if field_type == "UInt8":
        return choice([0, 1, 0xFF])
    if field_type == "UInt16":
        return choice([0, 1, 0xFFFF])
    if field_type == "UInt32":
        return choice([0, 1, 0xFFFFFFFF, randint(0, 0xFFFFFFFF)])
    if field_type == "UInt64":  # JSON-encoded as a hex string
        return choice(["0", "1", "FFFFFFFFFFFFFFFF"])
    if field_type == "Hash128":
        return choice(["0" * 32, "F" * 32])
    if field_type == "Hash160":
        return choice(["0" * 40, "F" * 40])
    if field_type == "Hash256":
        return choice(["0" * 64, "F" * 64, params.fake_id()])
    if field_type == "AccountID":
        return params.fake_account()
    if field_type == "Amount":
        return choice(
            [
                "0",
                "99999999999999999",
                {"currency": "USD", "issuer": params.fake_account(), "value": "-1"},
            ]
        )
    if field_type == "Blob":
        return choice(["", "DEADBEEF", "00" * 128])
    return None


def fuzz_mutate(tx_dict: dict) -> list[str]:
    """Apply 1-3 random mutations to ``tx_dict`` in place; return op descriptions."""
    ops: list[str] = []
    for _ in range(randint(1, 3)):
        # Recompute each round — a prior drop may have removed fields.
        present = [k for k in tx_dict if k not in _PROTECTED]
        if random() < 0.8:
            absent = [(n, t) for n, t in _INJECTABLE if n not in tx_dict]
            # Injection folds into the set-branch: sometimes add an absent known
            # field (reaches preflight's unknown/illegal-field paths for the type)
            # instead of overwriting a present one; set-heavy bias stays intact.
            if absent and (not present or random() < 0.3):
                name, ftype = choice(absent)
                tx_dict[name] = _hostile_for_type(ftype)
                ops.append(f"inject:{name}")
            elif present:
                field = choice(present)
                before = tx_dict[field]
                after = _hostile(before)
                tx_dict[field] = after
                ops.append(f"set:{field}" if after != before else f"noop:{field}")
        elif present:
            field = choice(present)
            tx_dict.pop(field, None)
            ops.append(f"drop:{field}")
    return ops


async def submit_fuzzed(
    name: str,
    base: Transaction,
    client: AsyncJsonRpcClient,
    wallet: Wallet,
) -> dict | None:
    """Fuzz a valid ``base`` and submit raw. Never raises (faulty paths must not):
    an unserializable shape emits ``workload::fuzz_skipped`` and returns None.
    """
    ops: list[str] = []

    def _mutate(d: dict) -> None:
        ops.extend(fuzz_mutate(d))

    try:
        result = await submit_raw(name, base, client, wallet, _mutate)
    except (XRPLBinaryCodecException, ValueError, TypeError, KeyError, OverflowError) as e:
        send_event(
            "workload::fuzz_skipped",
            {"tx_type": name, "ops": "; ".join(ops), "error": type(e).__name__},
        )
        return None
    tx_hash = result.get("tx_json", {}).get("hash", "") or result.get("hash", "")
    send_event(
        "workload::fuzz",
        {
            "tx_type": name,
            "ops": "; ".join(ops),
            "engine_result": result.get("engine_result", ""),
            "hash": tx_hash,
        },
    )
    return result
