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
7. **`setup.py`** — Add creation logic if other transactions depend on this object existing.
8. Run `check-imports` and `check-endpoints` before pushing.

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

### Faulty handlers
Each `_faulty` handler picks ONE random mutation via `choice()`, constructs a deliberately invalid transaction, and submits via `submit_tx`. Must never raise — precondition checks same as `_valid`. Common mutations: `params.fake_id()` (nonexistent object), non-owner submission, zero/negative amounts, mismatched asset types, overdraw (`balance + randint(...)`). Keep overdraw/state-aware mutations in `_faulty` only — `_valid` handlers must use amounts within tracked balances.

### LoanSet co-signing
`LoanSet` requires dual signing (borrower + broker). Uses `autofill_and_sign` → `sign_loan_set_by_counterparty` → `xrpl_submit` directly (not `submit_tx`). Calls `tx_submitted("LoanSet", txn, response.result)` after submit so the submit-time tef* internal-error assertion fires on the engine result. See `lending.py:_loan_set_valid`. Setup-phase paths that submit directly (`setup.py` LoanSet co-sign and `_probe_node`) call `assert_no_internal_error_submit(name, resp.result)` instead — they emit their own `setup_*` events rather than the runtime `submitted` event.

### XRPL specifications
Transaction format docs are at `xrpl.org/docs/references/protocol/transactions/types/<name>`. Authoritative XLS specifications (especially for newer features like vaults and lending) live in `github.com/XRPLF/XRPL-Standards` under `XLS-NNNN-<name>/`.

### Setup dependency chain
Gateways → trust_lines → iou_distribution → mpt_issuances → mpt_auth → mpt_distribution → vaults → vault_deposits → holder_vault_deposits → nfts → nft_offers → credentials → tickets → domains → loan_brokers → cover_deposits → loans → zero_interest_loan_payoff. If gateways fail, almost everything downstream cascades.

### SequenceTracker
`SequenceTracker` (`sequence.py`) prevents `tefPAST_SEQ` cascades in setup. Lazily fetches each account's sequence from the ledger on first use, then increments in-memory. All `_submit_batch` calls and LoanSet co-signing paths in setup use it. Driver endpoints still use xrpl-py autofill (no tracker needed for one-off calls).

### Structured transaction events
`tx_submitted()` emits `workload::submitted : {TxType}` and `tx_result()` emits `workload::result : {TxType}` via Antithesis `send_event`. Both include `account`, `sequence`, `tx_type`, and relevant object IDs (`vault_id`, `loan_id`, `nftoken_id`, etc.). Results also include `created_id`/`created_type` and `deleted_id`/`deleted_type` from metadata for object lifecycle tracking.

Inner batch transactions (top-level entries with `tfInnerBatchTxn` set, applied by rippled as side effects of an outer Batch) are tagged by `ws_listener.py` with a dedicated `workload::inner_batch_observed` event for grep-ability. Normal `tx_result()` processing still runs so state updaters can track inner-txn side effects (minted NFTs, created credentials, etc.).

**Inner batch caveat for `_META_EXPECTATIONS`:** The `meta_matches_tx_type` assertion in `assertions.py` is automatically skipped for inner batch transactions because their `AffectedNodes` may be empty or incomplete — the actual ledger changes are consolidated in the outer Batch's metadata. This is handled centrally in `tx_result()` via the `_TF_INNER_BATCH_TXN` flag check, so new entries added to `_META_EXPECTATIONS` are protected automatically without any per-type handling.

### Logging policy
No logger calls in setup.py or transaction handlers — structured `send_event` calls and assertions cover observability. `setup.py` emits `workload::setup_reject : {phase}` on non-success engine results and `workload::setup_error : {phase}` on exceptions. Only `ws_listener.py` retains warning/error logs for connection issues and state update failures. `sequence.py` has one debug log for tracker initialization.

### Randomness
All randomness goes through `workload.randoms` (backed by `AntithesisRandom`). Parameter generators live in `params.py` — never hardcode values in transaction builders.
