# Transaction Modifiers — design & plan

Status: planned. Branch `transaction-modifiers` off `sponsor-workload`.

## Problem

Three cross-cutting features are each implemented differently:

- **Delegation** — a submit-time modifier (`submit.py` `maybe_delegate`): decorates ~10% of any tx with `Delegate` + swaps the signing wallet.
- **Fee sponsorship** — a submit-time modifier (`submit.py`, prefunded-only `maybe_sponsor`): attaches `Sponsor` + `spfSponsorFee`.
- **Reserve sponsorship** — bespoke per-type workloads (`sponsored_create.py`): 8 `Sponsored*Create` endpoints, drivers, and synthetic assertion buckets.
- **Ticketing** — bespoke per-type builders (`tickets.py` `_TICKET_BUILDERS`): reconstruct each tx type purely to inject `Sequence=0` + `TicketSequence`.

Reserve sponsorship and ticketing are the outliers. Both are *really* submit-time modifiers: ticketing is a sequence-field swap; reserve sponsorship is a two-field attach (+ optional co-sign). The per-type builders/handlers duplicate generation logic the real generators already own, and — critically — they only ever decorate a *freshly built valid* tx, so they miss the combinatorial surface (e.g. a faulty object carrying a valid sponsor, which is exactly where cross-feature bugs and sponsor-accounting invariants live).

## The abstraction

A **transaction modifier** is a submit-time decorator that:
1. applies to a subset of tx types (a `supported`/`excluded` partition over REGISTRY real types),
2. consumes an account-scoped resource, and
3. declines when that resource isn't available for the tx's account.

| modifier | decorates with | resource | signing |
|---|---|---|---|
| delegate | `Delegate` + wallet swap | delegate relationship | delegate signs |
| sponsor | `Sponsor` + `SponsorFlags` (fee/reserve/both) | Sponsorship budget / any account | co-sign in co-signed mode |
| ticket | `Sequence=0` + `TicketSequence` | account's ticket pool | unchanged |

## Framework (`modifiers.py`)

- `Modifier`: `name`, `supported: set[str]`, `excluded: dict[str, str]` (reason per exclusion), `weight`/probability, `incompatible_with: set[str]`, and `apply(name, txn, wallet, ctx) -> ModResult | None` (`None` = declined: resource unavailable for this account).
- `ModResult` carries the two phases: **pre-sign** (field attach / wallet swap / sequence override) and optional **post-sign co-sign** (sponsor co-signed path).
- `MODIFIERS` registry. `submit_tx` runs the pipeline in fixed order `ticket → delegate → sponsor`: offer the tx to each modifier; it applies if supported + resource available + compatible with already-applied modifiers this tx. Then sign, run any post-sign co-sign, submit. `submit_raw` stays modifier-free (as delegation already is — no co-sign path there).

### Composition (decision: stack any compatible modifiers)
Any number of compatible modifiers may stack for maximum coverage surface. Each modifier declares `incompatible_with`.

Compatibility matrix (verify delegate×ticket and sponsor×ticket against rippled `develop` in Phase 1):
- **delegate × sponsor → INVALID** (rippled `terNO_SPONSORSHIP` — the existing `delegate_combo` fault). Excluded as a valid combo; retained as a deliberate fault vector.
- delegate × ticket, sponsor × ticket → expected valid (orthogonal). Confirm before relying.

## Coverage rule (formalized)

Every modifier must satisfy `supported ∪ excluded == {REGISTRY real types}` and `supported ∩ excluded == ∅`.

- **`scripts/check-modifier-coverage`** — CI gate (mirrors `check-fuzz-coverage`): parses REGISTRY + each modifier's sets, fails listing any unclassified `(modifier, type)` pair.
- **Runtime** `check_modifier_coverage()` at startup fires `unreachable : modifier_coverage_missing : <modifier>` for any gap. Subsumes and retires `check_ticket_coverage`.
- **CLAUDE.md "Add a transaction type" checklist** gains: "classify the new type for every Modifier (`supported`, or `excluded` with a reason)." This is the mechanism that forces new workloads to be considered against every modifier.

## The three modifiers

- **Delegate** — wrap `maybe_delegate`; `_NON_DELEGABLE_NAMES` becomes `excluded`, rest `supported`. Wallet swap in pre-sign phase.
- **Ticket** — delete `_TICKET_BUILDERS` + the domain ticket builders. Modifier: if `txn.account` owns a free ticket, set `Sequence=0` + `TicketSequence` and consume it. `TicketCreate` stays a normal workload (stocks the pool). The old "can't build from ctx" exclusions evaporate (we decorate the real generator's tx); only genuine protocol exclusions remain (Batch/inner-batch).
- **Sponsor** — unified modifier from `sponsored_create.py`. `supported` = reserve-sponsorable allow-list (mirror rippled `preflight1Sponsor`); rest `excluded` with reasons. Modes: **valid** (prefunded no-cosign / co-signed) and **faulty** (today's `_sponsored_create_faulty` vectors: missing/invalid flags, nonexistent sponsor, sponsor==account, garbage co-sign, exhausted budget, disallowed type). Folds the fee `maybe_sponsor` in (fee/reserve/both). Co-sign in post-sign phase. Deletes `sponsored_create.py`, its 8 REGISTRY entries, 8 drivers, synthetic names.

## Assertions → per-modifier dimensions

- Sponsor: drop `seen/success/failure : Sponsored*`; add cross-type `sometimes` dimensions — `sponsor_reserve_succeeded / _failed / _exhausted`, `sponsor_disallowed_type_rejected` — plus existing fee dimensions. `ws_listener` routes validated txns carrying `Sponsor`+reserve flag to them. This solves the flaky-bucket + missing-quadrant problems structurally.
- Ticket: ticketed txns validate under their real type's buckets (no per-type loss); add `sometimes : ticket_used`.
- Delegate: add `sometimes : delegate_used`.

## Probability of composition (decision: evaluate it)

Combos are only reachable if the **same account holds multiple resources**. Today setup assigns primitives to disjoint index ranges, so overlap ≈ 0 → a ticket+sponsor combo would essentially never fire regardless of probabilities.

Deliverable: a small **coverage model** (documented calc or tiny script) — `per-modifier probability × resource-availability × account-overlap × expected txn volume → predicted hits per combo` — used to tune probabilities so every combo clears a safe `sometimes` margin (≥ ~10 hits/run) without drowning plain txns. Rough sizing: a 30-min run submits O(10–50k) txns; at ~10% overlap and ~12% per-modifier fire, `P(ticket+sponsor) ≈ 0.0014` → ~30 hits over 20k txns (enough). The actionable output feeds setup: place cross-resource accounts (see below).

## Setup robustness (decision: make it fully robust)

Today setup is best-effort (`_wait_for_state` times out → `setup_state_partial` → proceeds with partial state), which is the root of recurring assertion starvation. Make it deterministic:
- Each phase **verifies then retries** until its objects exist (bounded, generous), retrying submits that didn't land (`terQUEUED`/`tefPAST_SEQ`/dropped).
- **Fail loud**: if a primitive can't be created after retries, `unreachable` — abort, don't proceed on partial state.
- Fail-loud threshold (proposed): per-phase up to ~5 retry rounds / ~60s, then `unreachable`.
- (Phase 4) place **cross-resource accounts** — accounts that own a ticket *and* are a sponsorship sponsee *and* have a delegate — plus a stock of unsponsored objects and some exhausted-budget sponsorships, so composition and the sponsor modifier are reachable.

## Phasing

0. **Setup robustness** — deterministic verify-and-retry + fail-loud on the *existing* setup (no modifier knowledge needed). Stabilizes the current sponsor branch too.
1. **Framework + coverage gate** (`check-modifier-coverage`, runtime `unreachable`, CLAUDE.md checklist) + migrate **delegate**. Verify the compatibility matrix against `develop`.
2. Migrate **ticket** to a pure decorator; delete `_TICKET_BUILDERS` + domain builders; `ticket_used` dimension.
3. **Sponsor** unified modifier (fee+reserve, valid+faulty, co-sign); dimension assertions; delete `sponsored_create.py` + per-type sponsored workloads.
4. **Composition tuning** — coverage model, set probabilities, add cross-resource accounts to setup, confirm every combo clears its `sometimes` margin; docs.

Each phase must leave all gates green: `check-imports`, `check-endpoints`, `check-fuzz-coverage`, `check-modifier-coverage`, `ruff check`, `ruff format --check`, `mypy`, `basedpyright`.

## Open items
- Confirm the setup fail-loud threshold value with the owner.

## Compatibility findings (Phase 1, rippled `develop`)

Read `libxrpl/tx/Transactor.cpp` (`preflight1`, `preflight1Sponsor`, `checkSponsor`, `checkSeq`) + `SponsorshipTransfer.cpp`.

- **delegate × ticket → valid.** Ticket/Sequence handling (`checkSeq`, ~L711–757) is fully independent of `sfSponsor`/`sfDelegate`: a ticket just replaces the account Sequence (`Sequence=0` + `TicketSequence`), no coupling. `preflight1` only rejects `Ticket + AccountTxnID`.
- **sponsor × ticket → valid.** Same reason — orthogonal; nothing in `checkSponsor` or `preflight1Sponsor` reads the sequence/ticket fields.
- **delegate × sponsor:** more precise than "always invalid." `checkSponsor` (L416–418) returns `temINVALID` only when the sponsor is a **reserve** sponsor and `sfDelegate` is present (`isFieldPresent(sfDelegate) && isReserveSponsored(tx)`). A **fee**-only sponsor + delegate is *not* rejected there; it instead requires the Sponsorship object to be keyed on `(sponsor, initiator=delegate)`, else `terNO_PERMISSION` (L437). So the framework keeps delegate and sponsor mutually exclusive (`incompatible_with={"sponsor"}`); the reserve-sponsor+delegate `temINVALID` remains a deliberate fault vector (note: the previously-cited `terNO_SPONSORSHIP` is now `temINVALID` on `develop`).
