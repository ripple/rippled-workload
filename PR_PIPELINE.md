# PR Pipeline — rippled-workload

Use this checklist for every code change before creating or updating a PR.
It captures everything we've learned from Vlad's reviews, Mounika's Antithesis demo,
codebase patterns, and XRPL domain knowledge.

---

## Phase 0: Before Writing Any Code

### 0.1 Sync with main
- [ ] `git fetch origin main` — check what's been merged since you branched
- [ ] Read recent commit messages: `git log --oneline origin/main -15`
- [ ] **Look for new enforcement systems** (like `check_ticket_coverage()`) that
      will break your PR if you don't account for them
- [ ] Rebase early, rebase often: `git rebase origin/main`

> **Lesson (DID PR comment 1):** Our PR predated the ticket-coverage enforcement.
> It didn't exist when we branched. Rebase catches this before the reviewer does.

### 0.2 Read the XRPL spec thoroughly
- Transaction docs: `xrpl.org/docs/references/protocol/transactions/types/<name>`
- XLS specs (newer features): `github.com/XRPLF/XRPL-Standards` under `XLS-NNNN-<name>/`
- rippled source tests: `<tx_type>_test.cpp` — shows exact error codes and edge cases
- **Document every field, every error code, every flag** before writing handlers

### 0.3 Study existing patterns in the codebase
- [ ] Read 2-3 similar transaction modules in `transactions/` end-to-end
- [ ] Read `submit.py` — understand the `autofill_and_sign` → `submit` pipeline
- [ ] Read `ws_listener.py` — understand how `tx_result()` and state updaters fire
- [ ] Read `tickets.py` — understand `_TICKET_BUILDERS` vs `_TICKET_EXCLUDED`
- [ ] Read `assertions.py` — understand catalog entries, `_META_EXPECTATIONS`, `tx_result()`
- [ ] Read `params.py` — every random value MUST go through params, never inline

---

## Phase 1: Implementation Checklist

### 1.1 Transaction module (`transactions/<name>.py`)
- [ ] Dispatch function: `if params.should_send_faulty(): return await _faulty(...)`
- [ ] `_valid` handler — covers the happy path and interesting valid variations
- [ ] `_faulty` handler — picks ONE random mutation via `choice()`
- [ ] Preconditions: return early silently (`if not items: return`), never raise
- [ ] All submission goes through `submit_tx()` — never call `xrpl_submit` directly
- [ ] No logger calls — only `submit_tx` (which calls `tx_submitted` internally)

### 1.2 Faulty handler deep-dive
**Flag values:**
- [ ] **NEVER use `0x00010000`** — it's `tfFullyCanonicalSig`, silently accepted by rippled
- [ ] Use `0x80000000` or `0x40000000` — high reserved bits that rippled actually rejects
- [ ] Verify your chosen flag value isn't a real flag by checking the XRPL spec

**"Race condition" mutations:**
- [ ] **Two sequential `submit_tx` calls are NOT a race** — `autofill()` assigns
      deterministic incrementing sequence numbers, so both succeed
- [ ] To create a real sequence collision: use `txn.__replace__(sequence=N)` to
      bypass autofill and force the same sequence on both transactions
- [ ] Or just remove fake races — they don't test anything useful

**Other faulty patterns that actually work:**
- `params.fake_id()` — nonexistent object ID
- `params.fake_account()` — nonexistent account address
- Non-owner submission — sign with wrong wallet (→ `tefBAD_AUTH`)
- Zero/negative amounts — `"0"` or overdraw beyond tracked balance
- Mismatched asset types — wrong currency for the operation

### 1.3 Parameter generators (`params.py`)
- [ ] Add generator functions for ALL randomizable fields
- [ ] Never hardcode random values in transaction builders
- [ ] Make generators reusable — ticket builders, batch inner txns, etc. need them too

### 1.4 Model (`models.py`)
- [ ] Add a `@dataclass` if the object needs state tracking across transactions
- [ ] Follow existing naming: field names match XRPL ledger entry field names

### 1.5 State updaters (`transactions/__init__.py`)
- [ ] Import the model class and the handler functions
- [ ] Write `_on_<action>` updater — called on `tesSUCCESS` only
- [ ] Use `_extract_created_id(meta, "LedgerEntryType")` / `_extract_deleted_id(...)`
- [ ] Update BOTH global lists (`w.items`) and per-account state (`w.accounts[addr].items`)
- [ ] **State updaters must NOT call assertion functions directly** — use `_META_EXPECTATIONS`

### 1.6 Registry entry (`transactions/__init__.py` → `REGISTRY`)
- [ ] 5-tuple: `(name, path, handler_fn, args_fn, state_updater | None)`
- [ ] `name` = PascalCase XRPL TransactionType (must match exactly)
- [ ] `path` = HTTP endpoint (e.g., `/did/set/random`)
- [ ] `args_fn` = lambda extracting state from Workload (e.g., `lambda w: (w.accounts, w.client)`)

### 1.7 Workload state (`app.py`)
- [ ] Add `self.<items> = []` to `Workload.__init__` if you added a new model

### 1.8 Assertions (`assertions.py`)
- [ ] **DO NOT add per-transaction assertion functions** — they don't scale
- [ ] Add entry to `_META_EXPECTATIONS` table instead (one row per tx type)
- [ ] Format: `"TxType": ("Created", "Modified", "LedgerEntryType")` or `"TxType": ("Deleted", "LedgerEntryType")`
- [ ] The shared `meta_matches_tx_type` assertion in `tx_result()` handles it automatically
- [ ] Catalog entry for `meta_matches_tx_type` is already registered — no new catalog entries needed

### 1.9 Ticket coverage (`transactions/tickets.py`)
- [ ] **Every REGISTRY type must be in `_TICKET_BUILDERS` OR `_TICKET_EXCLUDED`**
- [ ] If stateless (no pre-existing objects needed) → add to `_TICKET_BUILDERS`
  - Builder signature: `lambda dst, common: TxType(field=params.generator(), **common)`
  - Import the xrpl-py transaction model at the top of tickets.py
- [ ] If needs existing objects → add to `_TICKET_EXCLUDED` with a comment explaining why
- [ ] `check_ticket_coverage()` fires `unreachable()` at startup for missing types

### 1.10 Driver scripts (`test_composer/all_transactions/`)
- [ ] Create `parallel_driver_<name>_random.sh` for each endpoint
- [ ] Content: `#!/usr/bin/env bash` + `curl --silent http://workload:8000/<path>`
- [ ] Must be executable: `chmod +x`

### 1.11 Check scripts
- [ ] `scripts/check-imports` — add import line for your new module
- [ ] `scripts/check-endpoints` — add your endpoints to the expected list

---

## Phase 2: Before Creating/Updating the PR

### 2.1 Run all checks locally
```bash
scripts/check-imports       # all imports resolve
scripts/check-endpoints     # all endpoints register, ticket coverage passes
```

### 2.2 Verify with ruff (if available in devshell)
```bash
cd workload && ruff check src/workload/ && ruff format --check src/workload/
```

### 2.3 Rebase onto latest main (again)
```bash
git fetch origin main
git rebase origin/main
# Re-run checks after rebase
scripts/check-imports && scripts/check-endpoints
```

### 2.4 Self-review checklist
- [ ] No hardcoded random values — everything goes through `params.py`
- [ ] No per-tx assertion functions — use `_META_EXPECTATIONS` table
- [ ] No fake races (sequential `submit_tx` calls to same account)
- [ ] Flag values: `0x80000000` not `0x00010000`
- [ ] Faulty handlers never raise — same preconditions as valid handlers
- [ ] No logger calls in handlers — only structured `send_event` / `submit_tx`
- [ ] No unused imports or dead code
- [ ] Commit message is one line

---

## Phase 3: After PR Review — Common Reviewer Catches

These are the patterns Vlad (and other reviewers) consistently check for.
Internalize them so they don't come up in review.

### 3.1 Scalability
> "Does this pattern work for 30 transaction types, or just 2?"

- Lookup tables > per-type functions
- Shared assertion IDs > per-type assertion IDs
- One-row additions > new function definitions

### 3.2 Domain correctness
> "Does this actually test what you think it tests?"

- Verify flag values against rippled source — not all non-zero flags are invalid
- Verify that "race" mutations actually produce failures
- Verify error codes by checking rippled test files (`<TxType>_test.cpp`)
- `tfFullyCanonicalSig` (0x00010000) is a real legacy flag — rippled accepts it silently

### 3.3 Submission mechanics
> "Do you understand what happens between your code and rippled?"

- `submit_tx()` → `autofill_and_sign()` → `submit()` → fires `tx_submitted()`
- `autofill()` fetches current sequence from ledger → assigns deterministic sequence
- Two calls to same account = sequence N and N+1, both valid — NOT a race
- `SequenceTracker` is only for setup batch paths — driver endpoints use autofill
- `tx_submitted()` takes optional `result` param for submit-time tef* detection

### 3.4 Timing / integration
> "Does your change work with everything else that landed on main?"

- Rebase before final push — always
- Check for new startup enforcement (ticket coverage, amendment checks, etc.)
- Check for signature changes in shared functions (`submit_tx`, `tx_submitted`, etc.)

---

## Deep Knowledge Reference

### A. How `submit_tx` works (end-to-end)

```
Your handler code
  → submit_tx(name, txn, client, wallet)
    → autofill_and_sign(txn, client, wallet)
      → RPC: account_info → gets current sequence number
      → RPC: server_info → gets fee, ledger info
      → signs the transaction with wallet
    → submit(signed, client)
      → RPC: submit → rippled returns preliminary engine_result
    → tx_submitted(name, txn) → fires "workload::seen" assertion
    → returns result dict

Meanwhile, in background:
  ws_listener.py subscribes to "transactions" stream
    → receives validated tx from closed ledger
    → calls tx_result(tx_type, result) → fires success/failure/always assertions
    → if tesSUCCESS: calls STATE_UPDATERS[tx_type](workload, tx, meta)
```

**Key insight:** `autofill` makes an RPC call to get the account's current sequence.
Two sequential `submit_tx` calls for the same account will get sequences N and N+1.
They will NOT collide. This is why "race" mutations are fake.

### B. rippled Flag Internals

| Flag value | Name | rippled behavior |
|------------|------|------------------|
| `0x00010000` | `tfFullyCanonicalSig` | Legacy flag — silently accepted and masked away |
| `0x80000000` | High reserved bit | **Rejected** with `temINVALID_FLAG` |
| `0x40000000` | Unassigned high bit | **Rejected** with `temINVALID_FLAG` |

**Rule:** To test flag rejection, use `0x80000000`. Never use `0x00010000`.

Each transaction type has its own set of valid flags defined in rippled source
(e.g., `TF_SELL_NFTOKEN`, `TF_TRANSFERABLE`). The "universal" flags like
`tfFullyCanonicalSig` are always accepted. Only truly unrecognized bits trigger
`temINVALID_FLAG`.

### C. Assertion Types (Antithesis)

| Type | Meaning | When to use |
|------|---------|-------------|
| `reachability` / `Reachable` | Must be reached at least once | "workload::seen : TxType" |
| `sometimes` / `Sometimes` | Must be true at least once AND false at least once | Success + failure paths |
| `always` / `Always` | Must hold every single time | Invariants, meta checks |
| `unreachable` | Must NEVER be reached | Fatal errors, missing config |

**Catalog entries** = registered at startup with `hit=False` so Antithesis knows
they exist. Runtime assertions use `hit=True`.

**Shared assertions scale better** than per-type assertions:
- Bad: `assert_did_set_meta()`, `assert_vault_create_meta()`, ... (30 functions)
- Good: `_META_EXPECTATIONS` table + one `meta_matches_tx_type` check in `tx_result()`

### D. Setup Dependency Chain

```
gateways → trust_lines → iou_distribution → mpt_issuances → mpt_auth
→ mpt_distribution → vaults → vault_deposits → holder_vault_deposits
→ nfts → nft_offers → credentials → tickets → domains → loan_brokers
→ cover_deposits → loans → zero_interest_loan_payoff
```

If your transaction type needs objects created during setup, add a setup phase.
If it's stateless (like DIDSet — any account can create one), no setup needed.

### E. xrpl-py Client-Side Validation Limitations

xrpl-py validates fields before sending to rippled. This BLOCKS certain faulty
mutations from ever reaching the server:

| Blocked mutation | xrpl-py behavior | Workaround |
|------------------|-------------------|-----------|
| Odd-length hex strings | Raises locally | Need `submit_raw_tx()` bypass (not yet built) |
| Fields > max bytes | Raises locally | Need `submit_raw_tx()` bypass |
| All fields empty | Raises locally | Need `submit_raw_tx()` bypass |
| Invalid field types | Raises locally | Need `submit_raw_tx()` bypass |

**This applies to ALL transaction types**, not just DID. A future `submit_raw_tx()`
will bypass xrpl-py validation and send raw JSON-RPC to exercise these paths.

### F. Antithesis Report Quick-Scan Order

```
1. Correctness section     → Any failures?        → FILE BUG (crash in rippled)
2. Always assertions       → Any violations?       → INVESTIGATE (invariant broken)
3. Unreachability          → Any reached?           → INVESTIGATE (should-never-happen happened)
4. Sometimes assertions    → Both success+failure?  → GOOD (fault injection working)
5. Reachability            → All endpoints hit?     → GOOD (all tx types exercised)
6. Setup phases            → All completed?         → GOOD
```

### G. Triggering an Antithesis Run

1. Go to `github.com/ripple/rippled-antithesis` → Actions → `start-experiment`
2. Click "Run workflow"
3. Set `workload_commit` to your branch name (e.g., `manasip/did-workload`)
4. Set `recipient` to your email
5. Duration: `0.5` hours for quick validation, `23` for full scheduled run

### H. Key Contacts

| Person | Role | Notes |
|--------|------|-------|
| Vlad (vvysokikh1) | Primary reviewer | Framework/C++ expertise, deep rippled knowledge |
| Mounika (mounikakun) | On maternity leave from May 2026 | Created the original workload framework |

---

## Mistakes We've Made (and Learned From)

| # | What we did wrong | Why we missed it | How to prevent it |
|---|-------------------|------------------|-------------------|
| 1 | Didn't add ticket coverage entries | `check_ticket_coverage()` was merged to main after we branched | Rebase onto main before creating PR; read recent main commits |
| 2 | Created per-tx assertion functions | Seemed natural for 2 tx types; didn't think about 30 | Ask: "Will this pattern survive 10x more types?" Use tables, not functions |
| 3 | Used `0x00010000` as invalid flag | Assumed any non-zero flag is invalid | Check rippled source / XLS spec for actual flag definitions |
| 4 | Created fake "race" mutations | Didn't trace through `submit_tx` → `autofill` → sequence assignment | Trace the full call chain before assuming behavior |
| 5 | Put random hex generator in `did.py` instead of `params.py` | Seemed DID-specific | Every random value goes through `params.py` — ticket builders need it too |
| 6 | DID_CHEATSHEET.md showed `double_set_race` result as "Sequence conflict / both succeed" | We actually observed it both succeeding and still called it a "race" | If both succeed, it's not testing failure paths — remove or fix it |


