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
Return early with `None` when state is empty — never raise exceptions. Non-XRPL exceptions trigger `unreachable()` assertions and fail the test.
```python
if not nfts:
    log.debug("No NFTs to burn")
    return
```

### Transaction submission
Always use `submit_tx()` from `submit.py` — it wires `tx_submitted()` assertions automatically. Never call `xrpl_submit` directly. Exception: `LoanSet` uses manual co-signing in `lending.py`.

### Faulty handlers
Each `_faulty` handler picks ONE random mutation via `choice()`, constructs a deliberately invalid transaction, and submits via `submit_tx`. Must never raise — precondition checks same as `_valid`. Common mutations: `params.fake_id()` (nonexistent object), non-owner submission, zero/negative amounts, mismatched asset types. Use state-aware amounts when balance tracking is available (e.g., overdraw = `balance + randint(...)`).

### LoanSet co-signing
`LoanSet` requires dual signing (borrower + broker). Uses `autofill_and_sign` → `sign_loan_set_by_counterparty` → `xrpl_submit` directly (not `submit_tx`). Calls `tx_submitted("LoanSet", txn)` manually before submit. See `lending.py:_loan_set_valid`.

### XRPL specifications
Transaction format docs are at `xrpl.org/docs/references/protocol/transactions/types/<name>`. Authoritative XLS specifications (especially for newer features like vaults and lending) live in `github.com/XRPLF/XRPL-Standards` under `XLS-NNNN-<name>/`.

### Randomness
All randomness goes through `workload.randoms` (backed by `AntithesisRandom`). Parameter generators live in `params.py` — never hardcode values in transaction builders.
