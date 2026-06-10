# rippled-workload

Antithesis workload generator for XRP Ledger (rippled) fuzzing. Generates random XRPL transactions via a FastAPI HTTP server, driven by Antithesis test composer shell scripts.

## Development Setup

### Prerequisites
- Nix with flakes enabled
- direnv (optional, auto-activates devshell)

### Quick Start
```bash
cd rippled-workload
nix develop          # or direnv auto-enters via .envrc
check-imports        # verify all imports resolve (~2s)
check-endpoints      # start app, verify all endpoints register (~3s)
```

## Adding a New Transaction Type

1. **`params.py`** — Add parameter generators for all randomizable fields.
2. **New module** in `transactions/` — dispatch + `_valid` + `_faulty` (stub):
   ```python
   async def escrow_create(accounts, escrows, client):
       if params.should_send_faulty():
           return await _escrow_create_faulty(...)
       return await _escrow_create_valid(...)
   ```
3. **`models.py`** — Add a dataclass if the object needs state tracking.
4. **`transactions/__init__.py`** — Add entry to `REGISTRY` (see Patterns below).
5. **`test_composer/`** — Add `parallel_driver_<name>_random.sh` with `curl --silent`.
6. **`scripts/check-imports`** — Add the new module import.
7. **`scripts/check-endpoints`** — Add the new endpoint path to `expected`.
8. **`transactions/tickets.py`** — Classify the new type in `_TICKET_BUILDERS` or `_TICKET_EXCLUDED`. Builders take a `TicketCtx` (src account, dst, `common`, and workload state: accounts/domains/credentials/amms) and return a `Transaction` or `None` to skip; state-aware types (e.g. permissioned DEX) read `ctx.domains`/`ctx.amms`. Only truly impossible types (need object IDs the ctx can't supply, cosign, circular, batch) go in `_TICKET_EXCLUDED`. Every `REGISTRY` type must be in one or the other, or `check_ticket_coverage` fires `unreachable : ticket_coverage_missing`.
9. **`setup.py`** — Add creation logic if other transactions depend on this object existing.
10. Run `check-imports` and `check-endpoints` before pushing.

## Patterns

### REGISTRY shape
Each entry in `REGISTRY` (`transactions/__init__.py`) is a 5-tuple:
```python
(name, path, handler_fn, args_fn, state_updater | None)
# name: PascalCase XRPL TransactionType ("VaultCreate")
# path: HTTP endpoint ("/vault/create/random")
# handler_fn: async def handler(args..., client) -> None
# args_fn: lambda w: (w.accounts, w.vaults, ...) — extracts Workload state
# state_updater: def updater(w, tx, meta) -> None — or None if not needed
```

### State updaters
Called by WS listener on `tesSUCCESS`. Parse created/deleted objects from `meta["AffectedNodes"]` using `_extract_created_id(meta, entry_type)` / `_extract_deleted_id(meta, entry_type)`. Must update **both** global lists (`w.nfts`, `w.vaults`, etc.) and per-account state (`w.accounts[addr].nfts`) to keep them in sync.

### Handler preconditions
Return early silently when state is empty — never raise, never log. Non-XRPL exceptions trigger `unreachable()` assertions and fail the test.
```python
if not nfts:
    return
```

### Transaction submission
Always use `submit_tx()` from `submit.py` — it wires `tx_submitted()` assertions automatically. Never call `xrpl_submit` directly. Exception: `LoanSet` uses manual co-signing in `lending.py`.

For deliberately malformed transactions that xrpl-py's model validation rejects at **construction** (e.g. `tfHybrid` without `DomainID` → `temINVALID_FLAG`, empty/`>10`/duplicate `AcceptedCredentials` → `temARRAY_EMPTY`/`temARRAY_TOO_LARGE`/`temMALFORMED`), use `submit_raw(name, base, mutate, client, wallet)` from `submit.py`. It autofills a **valid** `base` model (for Sequence/Fee/LastLedgerSequence), serializes to an XRPL dict, applies `mutate(dict)` to introduce the malformation, then signs and submits the raw blob — so rippled's own preflight is what rejects it. It wires `tx_submitted` identically. Use ONLY in `_faulty` paths. `tx_submitted` accepts either a model or the raw dict. (Permissioned DEX is the first adopter; other workloads' faulty handlers can migrate to this for vectors currently unreachable through models.)

### Faulty handlers
Each `_faulty` handler picks ONE random mutation via `choice()`, constructs a deliberately invalid transaction, and submits via `submit_tx` (or `submit_raw` for model-rejected shapes — see above). Must never raise — precondition checks same as `_valid`. Common mutations: `params.fake_id()` (nonexistent object), `params.zero_domain_id()` (all-zero DomainID → `temMALFORMED`), non-owner submission, zero/negative amounts, mismatched asset types, overdraw (`balance + randint(...)`). Keep overdraw/state-aware mutations in `_faulty` only — `_valid` handlers must use amounts within tracked balances.

Bucket coverage caveat: `tem*`/`tef*` vectors never enter a ledger, so they never reach `ws_listener`/`tx_result` — they feed only the `seen` bucket plus the submit-time `no_internal_rippled_error_submit` check, NOT the `success`/`failure` `sometimes` buckets. The `sometimes(failure)` bucket for a type is satisfied only by its tec-producing vectors (which DO validate); make sure each `_faulty` handler keeps at least one tec-producing vector or its failure assertion will starve.

### Generative fuzzing (`fuzz.py`)
The curated mutations above encode *predicted* faults. `fuzz.py` adds **true generative fuzzing** to the faulty path for the unknown-unknowns: `submit_fuzzed(name, base, client, wallet)` takes a VALID base model and applies 1–3 random, type-inferred-but-open-ended mutations to its serialized dict (boundary/zero/max values, hostile hashes/accounts, empty/oversize/duplicated arrays, field drops), keeping it **encodable and validly signed** so it reaches rippled's preflight/preclaim/doApply (not just the binary parser). Auth/sequence/fee fields are left intact (`_PROTECTED`). It rides `submit_raw`, emits `workload::fuzz` with the exact operators applied + engine_result (and `workload::fuzz_skipped` if a shape won't serialize) for reproducible triage. Wired as one `"fuzz"` choice **alongside** the curated mutations in each pdex `_faulty` handler (factor out a `_*_base()` builder so valid + fuzz share it). Permissioned DEX is the first adopter; other workloads can opt in the same way. Randomness flows through `workload.randoms` so Antithesis explores the mutation space and replays counterexamples deterministically.

### LoanSet co-signing
`LoanSet` requires dual signing (borrower + broker). Uses `autofill_and_sign` → `sign_loan_set_by_counterparty` → `xrpl_submit` directly (not `submit_tx`). Calls `tx_submitted("LoanSet", txn, response.result)` after submit so the submit-time tef* internal-error assertion fires on the engine result. See `lending.py:_loan_set_valid`. Setup-phase paths that submit directly (`setup.py` LoanSet co-sign and `_probe_node`) call `assert_no_internal_error_submit(name, resp.result)` instead — they emit their own `setup_*` events rather than the runtime `submitted` event.

### XRPL specifications
Transaction format docs are at `xrpl.org/docs/references/protocol/transactions/types/<name>`. Authoritative XLS specifications (especially for newer features like vaults and lending) live in `github.com/XRPLF/XRPL-Standards` under `XLS-NNNN-<name>/`.

### Setup dependency chain
Gateways → trust_lines → iou_distribution → mpt_issuances → mpt_auth → mpt_distribution → vaults → vault_deposits → holder_vault_deposits → nfts → nft_offers → credentials → credential_accepts → tickets → domains → loan_brokers → cover_deposits → loans → zero_interest_loan_payoff. If gateways fail, almost everything downstream cascades. Setup accepts the credentials it issues (`credential_accepts`) so their subjects become permissioned-domain members — without acceptance `accountInDomain` fails and the permissioned-DEX valid paths starve.

### SequenceTracker
`SequenceTracker` (`sequence.py`) prevents `tefPAST_SEQ` cascades in setup. Lazily fetches each account's sequence from the ledger on first use, then increments in-memory. All `_submit_batch` calls and LoanSet co-signing paths in setup use it. Driver endpoints still use xrpl-py autofill (no tracker needed for one-off calls). **Gotcha:** `TicketCreate` advances the account Sequence by `TicketCount + 1` (the only tx that increases Sequence by >1), but `next_seq` only counts +1 — so after a setup `TicketCreate` on an account reused by a later setup tx, call `seq.advance(addr, TicketCount)` (see the tickets step; domain owners 50-52 are both ticket holders and domain creators).

### Structured transaction events
`tx_submitted()` emits `workload::submitted : {TxType}` and `tx_result()` emits `workload::result : {TxType}` via Antithesis `send_event`. Both include `account`, `sequence`, `tx_type`, and relevant object IDs (`vault_id`, `loan_id`, `nftoken_id`, etc.). Results also include `created_id`/`created_type` and `deleted_id`/`deleted_type` from metadata for object lifecycle tracking.

Inner batch transactions (top-level entries with `tfInnerBatchTxn` set, applied by rippled as side effects of an outer Batch) are tagged by `ws_listener.py` with a dedicated `workload::inner_batch_observed` event for grep-ability. Normal `tx_result()` processing still runs so state updaters can track inner-txn side effects (minted NFTs, created credentials, etc.).

### Synthetic assertion names (permissioned DEX & TicketUse)
Some REGISTRY entries use a synthetic `name` that differs from the on-ledger `TransactionType`, to get dedicated `seen`/`success`/`failure` buckets for a variant of an existing transaction. Permissioned-DEX handlers (`transactions/permissioned_dex.py`) register `OfferCreateDomain`, `OfferCreateHybrid`, `PaymentDomain`, `PaymentDomainXC` — all submit as `OfferCreate`/`Payment` on-ledger. The submit side already uses the synthetic name (handlers pass it to `submit_tx`). The result side must be taught the mapping in `ws_listener.py`: it fires `tx_result(<synthetic>, ...)` when a validated `OfferCreate`/`Payment` carries a `DomainID` (and `tfHybrid` / cross-currency distinguish the sub-variants). This mirrors the existing `TicketUse` mapping. STATE_UPDATERS lookup stays keyed by the real `TransactionType`, so these entries use `None` for their updater (the real-type updater, e.g. `_on_offer_create`, handles state). A synthetic name whose `sometimes(success)` cannot be reliably hit (e.g. `PaymentDomainXC`, which needs resting domain liquidity) goes in `assertions._NO_SUCCESS_TYPES`.

Domain membership is computed dynamically: `permissioned_dex._domain_members` = domain owner + holders of an *accepted* credential matching the domain's `accepted_credentials`. This requires `PermissionedDomain.accepted_credentials` (parsed in `_on_domain_set`) and `Credential.accepted` (flipped by `_on_credential_accept`) to be tracked.

### Logging policy
No logger calls in setup.py or transaction handlers — structured `send_event` calls and assertions cover observability. `setup.py` emits `workload::setup_reject : {phase}` on non-success engine results and `workload::setup_error : {phase}` on exceptions. Only `ws_listener.py` retains warning/error logs for connection issues and state update failures. `sequence.py` has one debug log for tracker initialization.

### Randomness
All randomness goes through `workload.randoms` (backed by `AntithesisRandom`). Parameter generators live in `params.py` — never hardcode values in transaction builders.
