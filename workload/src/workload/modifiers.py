"""Submit-time transaction modifiers.

A modifier decorates a subset of tx types with an account-scoped resource (a
delegate signer swap, a ticket sequence, a fee/reserve sponsor). Each declares a
``supported``/``excluded`` partition over the REGISTRY types (coverage-gated by
``scripts/check-modifier-coverage`` + ``check_modifier_coverage`` at startup), a
fire ``weight``, and ``incompatible_with`` tags. ``submit_tx`` runs the
``MODIFIERS`` pipeline in registry order (ticket → delegate → sponsor); any
number of compatible modifiers may stack.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from xrpl.models.transactions.transaction import Transaction
from xrpl.wallet import Wallet

from workload.randoms import random
from workload.transactions import TX_TYPES
from workload.transactions.delegation import _NON_DELEGABLE_NAMES, maybe_delegate

_TX_NAMES: set[str] = set(TX_TYPES)


@dataclass
class ModifierCtx:
    """Workload state a modifier's ``apply`` may draw resources from."""

    delegates: list[Any]
    accounts: dict[str, Any]
    sponsorships: list[Any]


@dataclass
class ModResult:
    txn: Transaction  # possibly a __replace__'d copy
    wallet: Wallet  # possibly swapped (delegate)
    cosign: Callable[[Any], Any] | None = None  # post-sign hook (Phase 3 sponsor co-sign)
    tag: str = ""  # e.g. "delegate", for events/debug


@dataclass
class Modifier:
    name: str
    supported: set[str]
    excluded: dict[str, str]  # type -> reason
    incompatible_with: set[str]
    weight: float  # fire probability in [0, 1]
    apply: Callable[..., ModResult | None]  # (name, txn, wallet, ctx) -> decoration | None


# ── Delegate modifier ────────────────────────────────────────────────
# Probability + non-delegable filtering are owned here (weight + supported);
# maybe_delegate is now a pure candidate picker.
_DELEGATE_EXCLUDED: dict[str, str] = {
    n: "non-delegable" for n in _NON_DELEGABLE_NAMES if n in _TX_NAMES
}
_DELEGATE_SUPPORTED: set[str] = _TX_NAMES - set(_DELEGATE_EXCLUDED)


def _apply_delegate(
    name: str, txn: Transaction, wallet: Wallet, ctx: ModifierCtx
) -> ModResult | None:
    if not ctx.delegates:
        return None
    delegate_addr, delegate_wallet = maybe_delegate(name, txn.account, ctx.delegates, ctx.accounts)
    if delegate_addr is None or delegate_wallet is None:
        return None
    return ModResult(
        txn=txn.__replace__(delegate=delegate_addr),
        wallet=delegate_wallet,
        tag="delegate",
    )


# Registry order matters: ticket → delegate → sponsor (only delegate exists this phase).
MODIFIERS: list[Modifier] = [
    Modifier(
        name="delegate",
        supported=_DELEGATE_SUPPORTED,
        excluded=_DELEGATE_EXCLUDED,
        incompatible_with={"sponsor"},
        weight=0.10,
        apply=_apply_delegate,
    ),
]


def apply_modifiers(
    name: str, txn: Transaction, wallet: Wallet, ctx: ModifierCtx
) -> tuple[Transaction, Wallet, list[str], list[Callable[[Any], Any]]]:
    """Run the MODIFIERS pipeline. A modifier fires iff the tx is supported, no
    already-applied tag is in its ``incompatible_with``, ``random() < weight``,
    and its ``apply`` returns non-None. Returns the decorated txn, final signing
    wallet, applied tags, and any post-sign co-sign callables (in order)."""
    applied: list[str] = []
    cosigns: list[Callable[[Any], Any]] = []
    for mod in MODIFIERS:
        if name not in mod.supported:
            continue
        if any(tag in mod.incompatible_with for tag in applied):
            continue
        if random() >= mod.weight:
            continue
        result = mod.apply(name, txn, wallet, ctx)
        if result is None:
            continue
        txn = result.txn
        wallet = result.wallet
        if result.cosign is not None:
            cosigns.append(result.cosign)
        applied.append(result.tag or mod.name)
    return txn, wallet, applied, cosigns


def check_modifier_coverage() -> None:
    """Fire ``unreachable`` for any REGISTRY type a modifier fails to classify."""
    from antithesis.assertions import unreachable

    for mod in MODIFIERS:
        classified = set(mod.supported) | set(mod.excluded)
        for missing in _TX_NAMES - classified:
            unreachable(
                "workload::modifier_coverage_missing",
                {"modifier": mod.name, "missing": missing},
            )
