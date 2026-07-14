# rippled-workload

Antithesis workload generator for rippled fuzzing. FastAPI server emits random XRPL transactions; Antithesis test-composer shell scripts drive the endpoints.

## Setup

`direnv allow` once — `.envrc` auto-loads the flake devshell (Python, uv, ruff, mypy, basedpyright, `scripts/` on PATH) on every `cd` into the tree, and `uv sync`s the venv. Without direnv, prefix commands with `nix develop --command` (what CI does). With the env active:

```bash
check-imports          # imports resolve (~2s)
check-endpoints        # endpoints register (~3s)
check-fuzz-coverage    # every _faulty wires generative fuzz (~2s)
check-modifier-coverage # every Modifier partitions REGISTRY types (~2s)
```

## Add a transaction type

1. `params.py` — parameter generators for every randomizable field.
2. `transactions/<name>.py` — dispatch + `_valid` + `_faulty`, sharing a `_<name>_base()` builder. `_faulty` MUST include a `"fuzz"` choice (see Generative fuzzing) plus ≥1 curated tec vector:
   ```python
   async def escrow_create(accounts, escrows, client):
       if params.should_send_faulty():
           return await _escrow_create_faulty(...)
       return await _escrow_create_valid(...)
   ```
3. `models.py` — dataclass if the object needs state tracking.
4. `transactions/__init__.py` — `REGISTRY` entry.
5. `test_composer/parallel_driver_<name>_random.sh` — `curl --silent`.
6. `scripts/check-imports` — add the module import.
7. `scripts/check-endpoints` — add the endpoint path to `expected`.
8. `transactions/tickets.py` — classify in `_TICKET_BUILDERS` or `_TICKET_EXCLUDED`. Builders take a `TicketCtx` (src/dst, `common`, workload state) and return a `Transaction` or `None`. Only types that can't be built from the ctx (need object IDs, cosign, circular, batch) go in `_TICKET_EXCLUDED`. Every `REGISTRY` type must be in one; else `check_ticket_coverage` fires `unreachable : ticket_coverage_missing`.
9. `setup.py` — creation logic if other transactions depend on the object.
10. `modifiers.py` — classify the new type for **every** `Modifier`: add it to `supported`, or to `excluded` with a reason. `check-modifier-coverage` fails CI otherwise.
11. Run `check-imports`, `check-endpoints`, `check-fuzz-coverage`, and `check-modifier-coverage`.

## Patterns

### REGISTRY (`transactions/__init__.py`)
5-tuple: `(name, path, handler_fn, args_fn, state_updater | None)`.
- `name` — PascalCase TransactionType (`"VaultCreate"`).
- `path` — endpoint (`"/vault/create/random"`).
- `args_fn` — `lambda w: (w.accounts, w.vaults, ...)`.
- `state_updater` — `def(w, tx, meta)`, or `None`.

### State updaters
WS listener calls on `tesSUCCESS`. Parse `meta["AffectedNodes"]` via `_extract_created_id`/`_extract_deleted_id`. Update **both** the global list (`w.vaults`) and per-account state (`w.accounts[addr].nfts`).

### Handler preconditions
Return early silently on empty state — never raise, never log. Non-XRPL exceptions trip `unreachable()` and fail the test.

### Submission
`submit_tx()` (`submit.py`) always — it wires `tx_submitted()`. Never call `xrpl_submit` directly (exception: `LoanSet` co-signs in `lending.py`).

`submit_raw(name, base, mutate, client, wallet)` for malformations xrpl-py rejects at construction (`tfHybrid` w/o `DomainID`, empty/`>10`/duplicate arrays). Autofills a valid `base`, serializes, applies `mutate(dict)`, signs and submits raw so rippled preflight does the rejecting. `_faulty` only. `mutate` MUST keep the dict encodable — no encode guard (only `submit_fuzzed` catches that).

### Transaction modifiers (`modifiers.py`)
Submit-time decorators applied by `submit_tx` via `apply_modifiers(name, txn, wallet, ctx)` (never `submit_raw`). Each `Modifier` declares `supported: set[str]` / `excluded: dict[str, str]` (a partition over REGISTRY types), a `weight` (fire probability), and `incompatible_with: set[str]` tags. The pipeline runs `MODIFIERS` in registry order (ticket → delegate → sponsor); a modifier fires iff `name in supported`, no already-applied tag is in its `incompatible_with`, `random() < weight`, and its `apply` returns a non-None `ModResult`. Any number stack. `apply_modifiers` returns `(txn, wallet, applied_tags, cosigns)`; `submit_tx` runs each `cosign` over the signed tx (post-sign phase, e.g. sponsor co-sign) before submitting.

Coverage: `supported ∪ excluded == {REGISTRY types}`, disjoint — enforced by `scripts/check-modifier-coverage` (CI) + `check_modifier_coverage()` at startup (fires `unreachable : modifier_coverage_missing` per gap). Only **delegate** exists so far (weight 0.10, `_NON_DELEGABLE_NAMES` → excluded; `incompatible_with={"sponsor"}`); `maybe_delegate` is now a pure candidate picker (the Modifier owns probability + non-delegable filtering). Fee sponsorship stays an inline `submit_tx` block (respects the delegate tag) until Phase 3 folds it into a sponsor modifier; `check_ticket_coverage` stays until Phase 2 makes ticket a modifier. See `docs/transaction-modifiers.md`.

### Faulty handlers
One random mutation via `choice()`; never raise; same preconditions as `_valid`. Mutations: `fake_id()`, `zero_domain_id()` (→ `temMALFORMED`), non-owner submit, zero/negative amounts, mismatched assets, overdraw. Keep overdraw/state-aware mutations out of `_valid`.

`tem*`/`tef*` vectors never enter a ledger, so they feed only `seen` + the submit-time `no_internal_rippled_error_submit` check — NOT the `success`/`failure` buckets. Keep ≥1 tec-producing vector per `_faulty` or `sometimes(failure)` starves. When no tec is reachable (e.g. malformations are all tem, or the "fault" is a tesSUCCESS no-op), add the type to `assertions._NO_FAILURE_TYPES` with a one-line reason instead.

### Generative fuzzing (`fuzz.py`)
`submit_fuzzed(name, base, client, wallet)` applies 1–3 type-inferred mutations to a valid base's dict, keeping it encodable and signed so it reaches preflight/preclaim/doApply. Leaves auth/sequence/fee intact (`_PROTECTED`). Rides `submit_raw`; emits `workload::fuzz` (+ `workload::fuzz_skipped`). Each round is set (0.8) or drop (0.2); set folds in **field injection** — adding an absent protocol-known field with a hostile value to hit preflight's unknown/illegal-field paths — and a rare (~5% of set-rounds) **type morph** (`_type_morph`) that swaps a value's type. Only STAmount is polymorphic (XRP drops string ↔ IOU/MPT object), so those morphs stay encodable and reach amount handling; generic cross-type morphs mostly die at the codec (→ `fuzz_skipped`), hence rare. Mutations: boundary/zero/max ints, hostile hashes/accounts, empty/oversize/duplicate arrays, and one-level recursion into nested STObjects and STArray elements (so Memos/SignerEntries inner fields aren't spared). `_hostile_amount` attacks value **and** currency/issuer (or MPT `mpt_issuance_id`) — e.g. `"XRP"` as an IOU code encodes but is illegal. `_INJECTABLE` field names come from the codec's `load_definitions()`, so they track the linked xrpl-py, not a hardcoded list (scalar types only; STObject/PathSet/etc. would just skew skips). ~3% of fuzzed dicts are unencodable → graceful `fuzz_skipped`.

Wired as one `"fuzz"` choice in **every** `_faulty`, alongside curated mutations sharing a `_*_base()` builder — `check-fuzz-coverage` fails CI for any `_faulty` lacking it. Because fuzz rides single-wallet `submit_raw`, it can't sign txns needing a counterparty co-sign: `LoanSet` (broker co-sign) is the sole exclusion, listed in `check-fuzz-coverage`. `Batch` is single-account so it fuzzes the outer dict; multi-account batches would need `BatchSigners`.

### LoanSet co-signing
Dual sign (borrower + broker): `autofill_and_sign` → `sign_loan_set_by_counterparty` → `xrpl_submit`, then `tx_submitted("LoanSet", txn, result)`. Setup direct paths (`setup.py` co-sign, `_probe_node`) call `assert_no_internal_error_submit` and emit `setup_*` instead.

### Setup dependency chain
gateways → trust_lines → iou_distribution → mpt_issuances → mpt_auth → mpt_distribution → mpt_lock → confidential_mpt (XLS-0096 privacy issuances + seeded balances) → vaults → vault_deposits → holder_vault_deposits → nfts → nft_offers → credentials → credential_accepts → tickets → domains → loan_brokers → cover_deposits → loans → zero_interest_loan_payoff. Gateway failure cascades. `credential_accepts` makes subjects domain members — without it `accountInDomain` fails and permissioned-DEX valid paths starve.

### MPT cohorts (XLS-82)
Setup step 4 mints flag-distinct cohorts (one issuer each) so MPT-on-DEX paths hit valid + fault gates: `[0]/[1]` tradeable (success), `[2]` no-trade (`tecNO_PERMISSION`), `[3]` no-transfer (`tecNO_AUTH`/`tecPATH_PARTIAL`), `[4]` require-auth never authorized (`tecNO_AUTH`), `[5]` lockable→locked in `mpt_lock` (`tecLOCKED` / `tecPATH_DRY`). `MPTokenIssuance` tracks `can_trade/can_transfer/require_auth/locked/holders`. `w.amms` now holds MPT-paired AMMs — filter `isinstance(asset, IssuedCurrency)` before reading `.currency`/`.issuer`.

### SequenceTracker (`sequence.py`)
Prevents `tefPAST_SEQ` cascades in setup: lazy-fetch each account's sequence, then increment in-memory. Setup batches + LoanSet co-sign use it; drivers use autofill. Gotcha: `TicketCreate` advances Sequence by `count + 1` (only tx >1), but `next_seq` counts +1 — after a setup `TicketCreate` on a reused account call `seq.advance(addr, count)`.

### Structured events
`tx_submitted()` → `workload::submitted : {TxType}`; `tx_result()` → `workload::result : {TxType}`. Both carry account/sequence/tx_type + object IDs; results add `created_id`/`created_type`, `deleted_id`/`deleted_type`. Inner batch txns (`tfInnerBatchTxn`) also emit `workload::inner_batch_observed`; normal `tx_result()` still runs.

### Synthetic assertion names
Some `REGISTRY` entries use a synthetic `name` ≠ on-ledger `TransactionType` for dedicated buckets: `OfferCreateDomain`, `OfferCreateHybrid`, `PaymentDomain`, `PaymentDomainXC` (`permissioned_dex.py`), `OfferCreateMPT`, `PaymentMPT` (`mpt_dex.py`), `SponsorshipSetDelete`, `SponsorshipTransferAccount`, `PaymentSponsoredAccount` (`sponsorship.py`) — all submit as `OfferCreate`/`Payment`/`SponsorshipSet`/`SponsorshipTransfer`. Handlers pass the synthetic name to `submit_tx`; `ws_listener.py` maps the validated tx back: `DomainID` present (+ `tfHybrid`/cross-currency), MPT leg via `_amount_is_mpt`, `tfDeleteObject`/absent `ObjectID`/`tfSponsorCreatedAccount` for the Sponsor buckets. STATE_UPDATERS stay keyed by real type → these use `None`, except `PaymentSponsoredAccount`, whose actual state update rides the real `"Payment"` row's updater (`_on_payment_maybe_sponsored_account`) since `"Payment"` itself isn't gated. Synthetic names whose `sometimes(success)` can't be reliably hit (`PaymentDomainXC`) go in `assertions._NO_SUCCESS_TYPES`; `OfferCreateMPT`/`PaymentMPT` don't (their valid paths rest reliably).

`sponsored_create.py` adds eight more: `Sponsored{CheckCreate,EscrowCreate,PaymentChannelCreate,TrustSet,CredentialCreate,SignerListSet,DepositPreauth,MPTokenAuthorize}`, submitting as the bracketed real type with common fields `Sponsor`/`SponsorFlags=spfSponsorReserve[|spfSponsorFee]` set (rippled's `preflight1Sponsor` allow-lists reserve sponsorship per tx type; see `Transactor.cpp`). Unlike the other synthetic buckets above, `ws_listener.py` maps these back with **one generic rule** (`tx_type in _RESERVE_SPONSOR_TX_TYPES and SponsorFlags & spfSponsorReserve → "Sponsored{tx_type}"`) instead of a per-type `elif`, since the discriminating field (`SponsorFlags`) is a common `Transaction` field, not type-specific — `SponsorshipTransfer` also carries it (for its own Create/Reassign) but sits outside `_RESERVE_SPONSOR_TX_TYPES`, so it keeps its dedicated bucket above instead of colliding. STATE_UPDATERS stay `None`; the created object is tracked twice independently: the real type's own updater (e.g. `_on_check_create`) as always, and generically by `ws_listener._on_reserve_sponsored_create`, which parses the `CreatedNode`'s `Sponsor`/`HighSponsor`/`LowSponsor` field (present only when the reserve, not just the fee, was actually sponsored) into `w.sponsored_objects`. `DepositPreauth` has no bare (non-sponsored) endpoint/REGISTRY row in this workload, so its `tx_result`/`seen`/`success`/`failure` triple isn't pre-registered by `register_assertions()` — a known, accepted gap given the task's 8-endpoint scope.

**api_version 2 gotcha:** the WS stream renames a Payment's `Amount` to `DeliverMax` and drops `Amount`. Read delivered amount via `_delivered_amount(tx)` (`DeliverMax`, `Amount` fallback) — `tx["Amount"]` is `None` and misclassifies. `DomainID`/`SendMax`/`TakerGets`/`TakerPays` are unaffected.

`permissioned_dex._domain_members` = owner + holders of an accepted matching credential; needs `PermissionedDomain.accepted_credentials` and `Credential.accepted` tracked.

### Sponsor-specific assertions (`assertions.py`, gated on `features.SPONSOR`)
`workload::always : no_sponsored_queue` — rippled's TxQ rejects only FEE-sponsored txns (`TxQ.cpp`: `sfSponsor && isFeeSponsored`), so a submit carrying `Sponsor` + `spfSponsorFee` must never yield `terQUEUED` — reserve-only sponsorship queues legitimately and is excluded; fired from `_assert_sponsor_submit_signals` (submit-side only — `ter*` never validates, so `tx_result`'s stream would silently miss it). Five `sometimes` reachability signals ride the same submit-time hook plus `tx_result`'s validated side: `sponsor_fee_prefunded_used`/`sponsor_fee_cosigned_used` (validated `tesSUCCESS` + `Sponsor`+`spfSponsorFee`, split on `SponsorSignature` presence — needs validation, since "did the fee sponsor actually land" isn't certain pre-ledger), `sponsor_reserve_budget_exhausted` (`terNO_SPONSORSHIP` or `tecINSUFFICIENT_RESERVE` on a `Sponsor`-bearing submit), `sponsor_no_permission_seen` (`tecNO_SPONSOR_PERMISSION`, any tx), `sponsor_has_obligations_seen` (`AccountDelete` + `tecHAS_OBLIGATIONS`). `_META_EXPECTATIONS["SponsorshipSet"] = ("Created", "Modified", "Deleted", "Sponsorship")` covers create/refill/delete in one entry (`SponsorshipTransfer` skipped — its target ledger-entry type varies between `Sponsorship`/account/object types, so no single expected type fits).

### SponsorshipAudit (`sponsorship.py` + `app.py`)
Read-only `ledger_entry`/`account_info` cross-check of tracked sponsor state against the validated ledger — not a transaction, so no `REGISTRY` row (no `engine_result` to feed `seen`/`success`/`failure`, and `register_assertions()`'s reachability entry would starve waiting for a hit that structurally never comes). `app.py`'s `create_app()` wires `/sponsorship/audit/random` directly onto `_make_endpoint` (same `XRPLException`/timeout handling as REGISTRY rows) instead, invisible to `TX_TYPES`/`check_ticket_coverage`/the fuzz-coverage scan. Picks one random tracked `Sponsorship` and one random `w.sponsored_accounts` entry; a genuine `entryNotFound`/`actNotFound` against the validated ledger prunes the stale tracking entry (state legitimately drifts — deletes reaching us through a path the WS listener doesn't parse) rather than failing anything. Consistency is a `sometimes` (`sponsorship_audit_object_consistent`/`sponsorship_audit_account_consistent`), deliberately not an `always`: only a systematic break (the bucket never satisfying across a whole run) is worth triaging, and an `always` here would be flaky by construction.

### Logging
No logger calls in `setup.py` or handlers — `send_event` + assertions cover observability. `setup.py` emits `workload::setup_reject : {phase}` / `setup_error : {phase}`. Only `ws_listener.py` keeps warn/error logs; `sequence.py` one debug log.

### Confidential MPT (XLS-0096)
Unconditionally on: the pinned `pre-3.3-release-group` xrpl-py carries the `ConfidentialMPT*` models and xrpld `develop` carries the `ConfidentialTransfer` amendment. `Dockerfile.workload` always runs the crypto build (`scripts/setup-confidential-crypto.sh`).

Five real on-ledger handlers (`transactions/confidential_mpt.py`: MergeInbox, Convert, Send, ConvertBack, Clawback) — true `ConfidentialMPT*` type, real-type `STATE_UPDATERS`, no synthetic-name mapping.

**Packaging.** Models come from xrpl-py's `pre-3.3-release-group` branch (git-pinned, in the core wheel; converges the pre-release sponsor + confidential WIP branches). Proof generation is `xrpl.ext.confidential` — the separate `xrpl-py-confidential` dist, EXCLUDED from the core wheel — so `uv sync` gets models but not proofs. `scripts/setup-confidential-crypto.sh` (in `Dockerfile.workload`) copies `xrpl/ext/confidential` into the venv, fetches `libmpt-crypto.so` from `XRPLF/mpt-crypto`'s public release, and compiles `_mpt_crypto` (fail-loud). Import is guarded: absent add-on → `CRYPTO_AVAILABLE=False`.

**Version coherence (not hardcoded).** The script reads the target from the branch's `MPT_CRYPTO_VERSION` and cross-checks it against rippled's `conanfile.py` `mpt-crypto/*` pin (`ARG XRPLD_COMMIT`, default `develop`); divergence fails the build — mismatched proof formats → rippled rejects → `success` starves. Currently `0.4.0-rc2`.

`cc.CRYPTO_AVAILABLE` gates **valid** paths only. Faulty paths aren't gated: trivial-on-curve fixed-length blobs (66B ciphertext, 33B key, bogus proof) reach preclaim → `tecBAD_PROOF`/`temMALFORMED`/`tecOBJECT_NOT_FOUND` with no real crypto. Models validate the lengths rippled enforces, so `confidential_crypto.py` holds the wire-size constants and `params.py` builds faulty bases at them.

Builders (`prepare_confidential_*`) take ElGamal keys explicitly + a **sync** `client` (they set the confidential fee = `base_fee × 10`; autofill preserves it). The sync client calls `asyncio.run()` internally — illegal on the running loop — so every `cc.*` builder/ledger-read is **async**, dispatched to a single worker thread (off-loop + serializes the shared secp256k1 context). `cc.build_*` thread `client.url` + keys from tracked state; Clawback also needs the holder's on-ledger `IssuerEncryptedBalance`.

**Skippable build failures:** every valid path wraps its `cc.*` section in `except cc.BUILD_SKIP_ERRORS: return` — `(ValueError, RuntimeError, KeyError)`. Builders surface degraded RPC responses as stdlib errors (fee → `KeyError 'drops'`, missing balance → `ValueError`) and proof races as `RuntimeError` (native `-1` when the tracked amount > ledger balance — the range proof can't prove the negative remainder). These are fault-injection weather; `sometimes(success)` still catches systematic breakage.

**Clawback drift:** the equality proof must match the on-ledger `IssuerEncryptedBalance` exactly and tracked state drifts under concurrent sends/converts, so `_clawback_valid` `cc.decrypt`s the encrypted balance and claws that — never the tracked amount (→ `tecBAD_PROOF`).

**Faulty fee gotcha:** faulty bases must set `fee=params.confidential_fee()` — autofill's base fee draws `telINSUF_FEE_P` (confidential txns cost 10×), and `tel*` never validates, starving `sometimes(failure)`.

Setup (`_setup_confidential_mpt`, chain step 6c): privacy issuances (`TF_MPT_CAN_HOLD_CONFIDENTIAL_BALANCE|CAN_CLAWBACK|CAN_TRANSFER`) on `[7..8]`, holders `[72..76]`; crypto steps gated on `CRYPTO_AVAILABLE`. `MPTokenIssuanceSet(issuer_encryption_key=...)` before Convert. Convert binds account Sequence into the proof, so setup/`_convert_valid` stamp `cc.account_sequence` on submit or `tecBAD_PROOF`.

Caveat: `sometimes(success)` + `conf_mpt_version_monotonic` only fire against an XLS-0096 `xrpld` whose mpt-crypto pin matches.

### Randomness & specs
All randomness via `workload.randoms` (`AntithesisRandom`); generators in `params.py`, never hardcode. Tx docs: `xrpl.org/docs/references/protocol/transactions/types/<name>`; specs: `github.com/XRPLF/XRPL-Standards` `XLS-NNNN-<name>/`.
