"""Generative, schema-agnostic mutation for ``_faulty`` transaction paths.

True fuzzing for the faulty path: instead of a fixed menu of predicted faults,
take a VALID transaction dict and apply random, open-ended mutations that keep
it encodable and validly signed — so it reaches rippled's preflight / preclaim /
doApply (where bugs live), not just the binary parser.

Used via ``submit_fuzzed()`` from ``_faulty`` handlers, ALONGSIDE the curated
mutations (curated guarantees named TER-code coverage and encodes known-bug
knowledge; this finds the unknown-unknowns). Auth / sequence / fee fields are
left intact so the transaction authenticates and gets past the cheap rejects.

All randomness flows through ``workload.randoms`` so Antithesis explores the
mutation space, and every fuzzed submission emits a ``workload::fuzz`` event
recording the exact operators applied for reproducible triage.

Boundary: fuzz only mutates or drops EXISTING fields, never adds new ones.
New-sfield injection and type-confusion-via-unknown-field are out of scope by
design — adding fields the model didn't emit would break encoding, and keeping
the blob encodable (so it reaches the transactor) is the whole point.
"""

from __future__ import annotations

from antithesis.lifecycle import send_event
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.core.binarycodec.exceptions import XRPLBinaryCodecException
from xrpl.models.transactions.transaction import Transaction
from xrpl.wallet import Wallet

from workload import params
from workload.randoms import choice, randint, random
from workload.submit import submit_raw

# Fields left intact: mutating these yields shallow auth / sequence / fee
# rejects instead of reaching the transactor logic we want to fuzz.
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


def _is_hex(s: str) -> bool:
    try:
        int(s, 16)
        return True
    except ValueError:
        return False


def _hostile(value: object) -> object:
    """Return a hostile-but-encodable variant of ``value``, inferred from shape."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return choice([0, 1, 0xFFFFFFFF, randint(0, 0xFFFFFFFF)])
    if isinstance(value, dict):
        # Amount-like object (IOU/MPT): attack the numeric value, stay encodable.
        if "value" in value:
            return {**value, "value": choice(["0", "-1", "9999999999999999"])}
        return value
    if isinstance(value, list):
        # STArray: empty or oversize + duplicated.
        return choice([[], (value or [{}]) * 6])
    if isinstance(value, str):
        if len(value) == 64 and _is_hex(value):  # Hash256
            return choice(["0" * 64, "F" * 64, params.fake_id()])
        if value.isdigit():  # XRP drops
            return choice(["0", "99999999999999999"])
        if value.startswith("r") and 25 <= len(value) <= 35:  # AccountID
            return params.fake_account()
        return choice(["", value[::-1]])
    return value


def fuzz_mutate(tx_dict: dict) -> list[str]:
    """Apply 1-3 random mutations to ``tx_dict`` in place; return op descriptions."""
    ops: list[str] = []
    for _ in range(randint(1, 3)):
        # Recompute each round — a prior drop may have removed fields.
        fields = [k for k in tx_dict if k not in _PROTECTED]
        if not fields:
            break
        field = choice(fields)
        if random() < 0.8:
            before = tx_dict[field]
            after = _hostile(before)
            tx_dict[field] = after
            ops.append(f"set:{field}" if after != before else f"noop:{field}")
        else:
            tx_dict.pop(field, None)
            ops.append(f"drop:{field}")
    return ops


async def submit_fuzzed(
    name: str,
    base: Transaction,
    client: AsyncJsonRpcClient,
    wallet: Wallet,
) -> dict | None:
    """Apply ``fuzz_mutate`` to a valid ``base`` and submit via the raw path.

    Emits ``workload::fuzz`` with the operators applied + engine_result. If a
    hostile shape can't be serialized, emits ``workload::fuzz_skipped`` and
    returns None — never raises (faulty paths must not).
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
