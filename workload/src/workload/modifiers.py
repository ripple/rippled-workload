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

from workload.randoms import choice, random
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


# ── Ticket modifier ──────────────────────────────────────────────────
# A ticket is Sequence=0 + TicketSequence consuming one of the account's tracked
# tickets. Orthogonal to delegate/sponsor (rippled checkSeq is independent of
# sfDelegate/sfSponsor -- Phase 1 findings), so incompatible_with is empty.
# Excluded types either fix their own sequence or bind it into a proof, so a
# ticket's Sequence=0 would break them.
_TICKET_EXCLUDED: dict[str, str] = {
    "Batch": "manages inner-tx Sequences + needs Fee=0 (TapBatch); a ticket would desync it",
    "TicketCreate": "circular: new tickets number from account-root seq, not the tx seq",
    "ConfidentialMPTConvert": "proof binds account Sequence; ticket zeroes it -> tecBAD_PROOF",
    "ConfidentialMPTSend": "proof binds account Sequence; ticket zeroes it -> tecBAD_PROOF",
    "ConfidentialMPTConvertBack": "proof binds account Sequence; ticket zeroes it -> tecBAD_PROOF",
    "ConfidentialMPTClawback": "proof binds issuer Sequence; ticket zeroes it -> tecBAD_PROOF",
}
_TICKET_SUPPORTED: set[str] = _TX_NAMES - set(_TICKET_EXCLUDED)


def _apply_ticket(
    name: str, txn: Transaction, wallet: Wallet, ctx: ModifierCtx
) -> ModResult | None:
    acct = ctx.accounts.get(txn.account)
    if acct is None or not acct.tickets:
        return None
    ticket_sequence = choice(sorted(acct.tickets))
    acct.tickets.discard(ticket_sequence)  # optimistic consume: avoid reuse by concurrent submits
    return ModResult(
        txn=txn.__replace__(sequence=0, ticket_sequence=ticket_sequence),
        wallet=wallet,
        tag="ticket",
    )


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


# Registry order matters: ticket → delegate → sponsor (sponsor folds in Phase 3).
MODIFIERS: list[Modifier] = [
    Modifier(
        name="ticket",
        supported=_TICKET_SUPPORTED,
        excluded=_TICKET_EXCLUDED,
        incompatible_with=set(),
        weight=0.10,
        apply=_apply_ticket,
    ),
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
