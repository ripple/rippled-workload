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

### Scripts
| Script | What it does |
|--------|-------------|
| `scripts/check-imports` | Imports every module — catches `ImportError`, `ModuleNotFoundError` |
| `scripts/check-endpoints` | Starts FastAPI with mocked network, lists all registered routes, verifies expected endpoints exist |

**Always run `check-imports` before pushing.** Import errors take 15-20 min to surface through the Antithesis CI pipeline.

## Project Structure

```
workload/src/workload/
├── app.py              # FastAPI app, Workload class, data-driven endpoint registration
├── params.py           # Centralized random parameter generators
├── randoms.py          # Single RNG instance (AntithesisRandom)
├── assertions.py       # Antithesis assertion helpers (tx_submitted, tx_result, register_assertions)
├── models.py           # Dataclasses for state tracking (Vault, Credential, Loan, etc.)
├── submit.py           # Fire-and-forget transaction submission (autofill_and_sign + submit)
├── ws_listener.py      # WebSocket observer — fires tx_result assertions, updates state
├── setup.py            # Deterministic setup for first_* phase (seeds all required state)
├── config.py / config.json  # Configuration
├── check_xrpld_sync_state.py  # Network sync check (server_info polling)
├── transactions/       # Transaction generator package
│   ├── __init__.py     # REGISTRY (single source of truth), TX_TYPES, STATE_UPDATERS
│   ├── account_set.py  # AccountSet
│   ├── batch.py        # Batch (multi-type inner transactions)
│   ├── credentials.py  # CredentialCreate/Accept/Delete
│   ├── delegation.py   # DelegateSet
│   ├── domains.py      # PermissionedDomainSet/Delete
│   ├── lending.py      # LoanBrokerSet/Delete/CoverDeposit/CoverWithdraw, LoanSet/Delete/Manage/Pay
│   ├── mpt.py          # MPTokenIssuanceCreate/Destroy/Set, MPTokenAuthorize
│   ├── nft.py          # NFTokenMint/Burn/Modify, NFTokenCreateOffer/CancelOffer/AcceptOffer
│   ├── payments.py     # Payment (XRP/IOU/MPT)
│   ├── tickets.py      # TicketCreate, TicketUse
│   ├── trustlines.py   # TrustSet
│   └── vaults.py       # VaultCreate/Deposit/Withdraw/Set/Delete/Clawback
test_composer/all_transactions/
├── first_setup.sh            # Seeds state before fault injection
└── parallel_driver_*.sh      # 35 shell scripts (one per transaction type)
genesis/
├── genesis_ledger.json       # Committed base (CI injects amendment hashes)
└── accounts.json             # 100 pre-funded SECP256K1 accounts
prepare-workload/
└── generate_genesis.py       # Pure Python genesis generator (SHA-512Half of amendment names)
```

## Conventions

### Adding a New Transaction Type

1. **`params.py`** — Add parameter generators for all randomizable fields.
2. **New module** in `transactions/` (e.g., `escrow.py`) — dispatch + `_valid` + `_faulty` (stub):
   ```python
   async def escrow_create(accounts, escrows, client):
       if params.should_send_faulty():
           return await _escrow_create_faulty(...)
       return await _escrow_create_valid(...)
   ```
3. **`models.py`** — Add a dataclass if the object needs state tracking.
4. **`transactions/__init__.py`** — Add entry to `REGISTRY` table (name, path, handler, args, state_updater). This auto-registers the endpoint, assertion catalog entry, and WS listener handler.
5. **`test_composer/`** — Add `parallel_driver_<name>_random.sh` with `curl --silent`.
6. **`scripts/check-imports`** — Add the new module import.
7. **`setup.py`** — Add creation logic if other transactions depend on this object existing.
8. Run `check-imports` and `check-endpoints` before pushing.

### Randomness

All randomness goes through `workload.randoms` (backed by `AntithesisRandom`). Never create separate RNG instances. Parameter generators live in `params.py` — never hardcode values in transaction builders.

### Faulty Transactions

Each module has `_faulty` stubs controlled by `params.should_send_faulty()` (1% chance). Currently most are `pass # TODO` — full audit planned (see plan file).
