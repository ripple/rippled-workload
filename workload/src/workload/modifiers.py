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
from xrpl.transaction import sign_as_sponsor
from xrpl.wallet import Wallet

from workload import params
from workload.randoms import choice, random, sample
from workload.transactions import TX_TYPES
from workload.transactions.delegation import _NON_DELEGABLE_NAMES, maybe_delegate
from workload.transactions.sponsorship import _pick_reserve_sponsor, pick_prefunded_fee_sponsor

_TX_NAMES: set[str] = set(TX_TYPES)

# The type-agnostic /sponsor/malformation endpoint rides submit_raw, which is
# modifier-free, so every modifier excludes it (classified, never decorated).
_RAW_ONLY_REASON = "raw sponsor-malformation endpoint (submit_raw is modifier-free)"


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
    "SponsorMalformation": _RAW_ONLY_REASON,
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
_DELEGATE_EXCLUDED["SponsorMalformation"] = _RAW_ONLY_REASON
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


# ── Sponsor modifier ─────────────────────────────────────────────────
# XLS-68 fee/reserve sponsorship, folding in the former submit_tx fee-sponsor
# block and the eight bespoke sponsored_create.py workloads. ``supported`` is the
# object-creating subset of rippled's isReserveSponsorAllowed allow-list
# (SponsorHelpers.cpp) that this workload attaches reserve sponsors to; everything
# else is excluded. incompatible_with={"delegate"}: a reserve sponsor + sfDelegate
# is temINVALID (Transactor.cpp checkSponsor). Codec-rejected sponsor faults (bad
# flag bits, sponsor==account, flags-without-sponsor, disallowed type) can't
# decorate a Transaction model, so they live in the /sponsor/malformation raw
# endpoint, not here.
_SPONSOR_SUPPORTED: set[str] = {
    "CheckCreate",
    "EscrowCreate",
    "PaymentChannelCreate",
    "TrustSet",
    "CredentialCreate",
    "SignerListSet",
    "MPTokenAuthorize",
    "DepositPreauth",
}
_SPONSOR_EXCLUDED: dict[str, str] = dict.fromkeys(
    _TX_NAMES - _SPONSOR_SUPPORTED,
    "not a reserve-sponsorable object-creator this modifier decorates",
)
_SPONSOR_EXCLUDED["SponsorMalformation"] = _RAW_ONLY_REASON


def _cosign_sponsor(sponsor_wallet: Wallet) -> Callable[[Any], Any]:
    """Post-sign hook: add the sponsor's SponsorSignature over the sponsee-signed
    canonical data. submit_tx submits the returned tx as-is (xrpl-py's submit()
    re-encodes, never re-signs), so the sponsor signature reaches rippled intact."""
    return lambda signed: sign_as_sponsor(sponsor_wallet, signed).tx


def _sponsor_valid(
    txn: Transaction, wallet: Wallet, ctx: ModifierCtx, others: list[str]
) -> ModResult | None:
    flags = params.sponsor_flags()  # weighted fee / both / reserve
    reserve = bool(flags & params.SPF_SPONSOR_RESERVE)
    fee = bool(flags & params.SPF_SPONSOR_FEE)

    if reserve and fee:
        # A co-signing sponsor covers both buckets; a prefunded Sponsorship would
        # need matching fee+reserve budgets, so keep "both" on the co-sign path.
        sponsor_addr = choice(others)
        return ModResult(
            txn=txn.__replace__(sponsor=sponsor_addr, sponsor_flags=flags),
            wallet=wallet,
            cosign=_cosign_sponsor(ctx.accounts[sponsor_addr].wallet),
            tag="sponsor",
        )
    if reserve:
        reserve_addr, prefunded = _pick_reserve_sponsor(txn.account, ctx.accounts, ctx.sponsorships)
        if reserve_addr is None:
            return None
        decorated = txn.__replace__(sponsor=reserve_addr, sponsor_flags=params.SPF_SPONSOR_RESERVE)
        if prefunded:
            return ModResult(txn=decorated, wallet=wallet, tag="sponsor")
        return ModResult(
            txn=decorated,
            wallet=wallet,
            cosign=_cosign_sponsor(ctx.accounts[reserve_addr].wallet),
            tag="sponsor",
        )
    # fee-only: prefer a prefunded fee Sponsorship (no co-sign), else co-sign.
    prefunded_fee = pick_prefunded_fee_sponsor(txn.account, ctx.sponsorships)
    if prefunded_fee is not None:
        return ModResult(
            txn=txn.__replace__(sponsor=prefunded_fee, sponsor_flags=params.SPF_SPONSOR_FEE),
            wallet=wallet,
            tag="sponsor",
        )
    sponsor_addr = choice(others)
    return ModResult(
        txn=txn.__replace__(sponsor=sponsor_addr, sponsor_flags=params.SPF_SPONSOR_FEE),
        wallet=wallet,
        cosign=_cosign_sponsor(ctx.accounts[sponsor_addr].wallet),
        tag="sponsor",
    )


def _sponsor_faulty(
    txn: Transaction, wallet: Wallet, ctx: ModifierCtx, others: list[str]
) -> ModResult | None:
    """Model-expressible sponsor faults (xrpl-py constructs them; rippled rejects).
    Codec-rejected faults live in the /sponsor/malformation endpoint instead."""
    vector = choice(["nonexistent_sponsor", "prefunded_exhausted", "garbage_signature"])
    if vector == "nonexistent_sponsor":
        # Syntactically valid, unfunded AccountID -> checkSponsor terNO_ACCOUNT.
        return ModResult(
            txn=txn.__replace__(
                sponsor=params.fake_account(), sponsor_flags=params.sponsor_reserve_flags()
            ),
            wallet=wallet,
            tag="sponsor",
        )
    if vector == "prefunded_exhausted":
        # A tracked Sponsorship with no reserve budget, used without a co-sign ->
        # tecINSUFFICIENT_RESERVE.
        exhausted = [
            s
            for s in ctx.sponsorships
            if s.sponsee == txn.account
            and s.sponsor != txn.account
            and s.sponsor in ctx.accounts
            and s.remaining_owner_count == 0
            and not s.require_sign_for_reserve
        ]
        if not exhausted:
            return None
        s = choice(exhausted)
        return ModResult(
            txn=txn.__replace__(sponsor=s.sponsor, sponsor_flags=params.SPF_SPONSOR_RESERVE),
            wallet=wallet,
            tag="sponsor",
        )
    # garbage_signature: co-sign with a wallet that doesn't match the claimed Sponsor.
    if len(others) < 2:
        return None
    sponsor_addr, wrong_addr = sample(others, 2)
    return ModResult(
        txn=txn.__replace__(sponsor=sponsor_addr, sponsor_flags=params.sponsor_reserve_flags()),
        wallet=wallet,
        cosign=_cosign_sponsor(ctx.accounts[wrong_addr].wallet),
        tag="sponsor",
    )


def _apply_sponsor(
    name: str, txn: Transaction, wallet: Wallet, ctx: ModifierCtx
) -> ModResult | None:
    if txn.account not in ctx.accounts:
        return None
    others = [a for a in ctx.accounts if a != txn.account]
    if not others:  # every path needs at least one other account to sponsor
        return None
    if random() < 0.2:
        return _sponsor_faulty(txn, wallet, ctx, others)
    return _sponsor_valid(txn, wallet, ctx, others)


# Registry order matters: ticket → delegate → sponsor.
MODIFIERS: list[Modifier] = [
    # Weights tuned by scripts/modifier-coverage-model so both valid combos
    # (ticket+sponsor, ticket+delegate) clear a safe sometimes margin (>=~10
    # hits/run) off the cross-resource setup pool, while plain (unmodified)
    # submission stays dominant (~85%). See docs/transaction-modifiers.md.
    Modifier(
        name="ticket",
        supported=_TICKET_SUPPORTED,
        excluded=_TICKET_EXCLUDED,
        incompatible_with=set(),
        weight=0.20,
        apply=_apply_ticket,
    ),
    Modifier(
        name="delegate",
        supported=_DELEGATE_SUPPORTED,
        excluded=_DELEGATE_EXCLUDED,
        incompatible_with={"sponsor"},
        weight=0.20,
        apply=_apply_delegate,
    ),
    Modifier(
        name="sponsor",
        supported=_SPONSOR_SUPPORTED,
        excluded=_SPONSOR_EXCLUDED,
        incompatible_with={"delegate"},
        weight=0.15,
        apply=_apply_sponsor,
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
