# rippled-workload

Antithesis workload generator for rippled fuzzing. FastAPI server emits random XRPL transactions; Antithesis test-composer shell scripts drive the endpoints.

## Setup

`direnv allow` once — `.envrc` auto-loads the flake devshell (Python, uv, ruff, mypy, basedpyright, `scripts/` on PATH) on every `cd` into the tree, and `uv sync`s the venv. Without direnv, prefix commands with `nix develop --command` (what CI does). With the env active:

```bash
check-imports        # imports resolve (~2s)
check-endpoints      # endpoints register (~3s)
check-fuzz-coverage  # every _faulty wires generative fuzz (~2s)
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
10. Run `check-imports`, `check-endpoints`, and `check-fuzz-coverage`.

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

### Faulty handlers
One random mutation via `choice()`; never raise; same preconditions as `_valid`. Mutations: `fake_id()`, `zero_domain_id()` (→ `temMALFORMED`), non-owner submit, zero/negative amounts, mismatched assets, overdraw. Keep overdraw/state-aware mutations out of `_valid`.

`tem*`/`tef*` vectors never enter a ledger, so they feed only `seen` + the submit-time `no_internal_rippled_error_submit` check — NOT the `success`/`failure` buckets. Keep ≥1 tec-producing vector per `_faulty` or `sometimes(failure)` starves. When no tec is reachable (e.g. malformations are all tem, or the "fault" is a tesSUCCESS no-op), add the type to `assertions._NO_FAILURE_TYPES` with a one-line reason instead.

### Generative fuzzing (`fuzz.py`)
`submit_fuzzed(name, base, client, wallet)` applies 1–3 type-inferred mutations to a valid base's dict (boundary/zero/max, hostile hashes/accounts, empty/oversize/duplicate arrays, field drops), keeping it encodable and signed so it reaches preflight/preclaim/doApply. Leaves auth/sequence/fee intact (`_PROTECTED`). Rides `submit_raw`; emits `workload::fuzz` (+ `workload::fuzz_skipped`).

Wired as one `"fuzz"` choice in **every** `_faulty`, alongside curated mutations sharing a `_*_base()` builder — `check-fuzz-coverage` fails CI for any `_faulty` lacking it. Because fuzz rides single-wallet `submit_raw`, it can't sign txns needing a counterparty co-sign: `LoanSet` (broker co-sign) is the sole exclusion, listed in `check-fuzz-coverage`. `Batch` is single-account so it fuzzes the outer dict; multi-account batches would need `BatchSigners`.

### LoanSet co-signing
Dual sign (borrower + broker): `autofill_and_sign` → `sign_loan_set_by_counterparty` → `xrpl_submit`, then `tx_submitted("LoanSet", txn, result)`. Setup direct paths (`setup.py` co-sign, `_probe_node`) call `assert_no_internal_error_submit` and emit `setup_*` instead.

### Setup dependency chain
gateways → trust_lines → iou_distribution → mpt_issuances → mpt_auth → mpt_distribution → mpt_lock → vaults → vault_deposits → holder_vault_deposits → nfts → nft_offers → credentials → credential_accepts → tickets → domains → loan_brokers → cover_deposits → loans → zero_interest_loan_payoff. Gateway failure cascades. `credential_accepts` makes subjects domain members — without it `accountInDomain` fails and permissioned-DEX valid paths starve.

### MPT cohorts (XLS-82)
Setup step 4 mints flag-distinct cohorts (one issuer each) so MPT-on-DEX paths hit valid + fault gates: `[0]/[1]` tradeable (success), `[2]` no-trade (`tecNO_PERMISSION`), `[3]` no-transfer (`tecNO_AUTH`/`tecPATH_PARTIAL`), `[4]` require-auth never authorized (`tecNO_AUTH`), `[5]` lockable→locked in `mpt_lock` (`tecLOCKED` / `tecPATH_DRY`). `MPTokenIssuance` tracks `can_trade/can_transfer/require_auth/locked/holders`. `w.amms` now holds MPT-paired AMMs — filter `isinstance(asset, IssuedCurrency)` before reading `.currency`/`.issuer`.

### SequenceTracker (`sequence.py`)
Prevents `tefPAST_SEQ` cascades in setup: lazy-fetch each account's sequence, then increment in-memory. Setup batches + LoanSet co-sign use it; drivers use autofill. Gotcha: `TicketCreate` advances Sequence by `count + 1` (only tx >1), but `next_seq` counts +1 — after a setup `TicketCreate` on a reused account call `seq.advance(addr, count)`.

### Structured events
`tx_submitted()` → `workload::submitted : {TxType}`; `tx_result()` → `workload::result : {TxType}`. Both carry account/sequence/tx_type + object IDs; results add `created_id`/`created_type`, `deleted_id`/`deleted_type`. Inner batch txns (`tfInnerBatchTxn`) also emit `workload::inner_batch_observed`; normal `tx_result()` still runs.

### Synthetic assertion names
Some `REGISTRY` entries use a synthetic `name` ≠ on-ledger `TransactionType` for dedicated buckets: `OfferCreateDomain`, `OfferCreateHybrid`, `PaymentDomain`, `PaymentDomainXC` (`permissioned_dex.py`), `OfferCreateMPT`, `PaymentMPT` (`mpt_dex.py`) — all submit as `OfferCreate`/`Payment`. Handlers pass the synthetic name to `submit_tx`; `ws_listener.py` maps the validated tx back: `DomainID` present (+ `tfHybrid`/cross-currency), MPT leg via `_amount_is_mpt`. STATE_UPDATERS stay keyed by real type → these use `None`. Synthetic names whose `sometimes(success)` can't be reliably hit (`PaymentDomainXC`) go in `assertions._NO_SUCCESS_TYPES`; `OfferCreateMPT`/`PaymentMPT` don't (their valid paths rest reliably).

**api_version 2 gotcha:** the WS stream renames a Payment's `Amount` to `DeliverMax` and drops `Amount`. Read delivered amount via `_delivered_amount(tx)` (`DeliverMax`, `Amount` fallback) — `tx["Amount"]` is `None` and misclassifies. `DomainID`/`SendMax`/`TakerGets`/`TakerPays` are unaffected.

`permissioned_dex._domain_members` = owner + holders of an accepted matching credential; needs `PermissionedDomain.accepted_credentials` and `Credential.accepted` tracked.

### Logging
No logger calls in `setup.py` or handlers — `send_event` + assertions cover observability. `setup.py` emits `workload::setup_reject : {phase}` / `setup_error : {phase}`. Only `ws_listener.py` keeps warn/error logs; `sequence.py` one debug log.

### Randomness & specs
All randomness via `workload.randoms` (`AntithesisRandom`); generators in `params.py`, never hardcode. Tx docs: `xrpl.org/docs/references/protocol/transactions/types/<name>`; specs: `github.com/XRPLF/XRPL-Standards` `XLS-NNNN-<name>/`.
</content>
</invoke>
