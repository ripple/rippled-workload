# Local Testing Guide

Step-by-step guide to run the workload server locally against a standalone rippled node and test transaction endpoints.

## Prerequisites

- **Nix with flakes** — `nix --version`
- **rippled binary** — a local build at `~/rippled/my_build/xrpld` (adjust paths below if different)
- **curl** — for endpoint testing

## Terminal Layout

You need **4 terminals** (or tmux panes):

| Terminal | Purpose              | Must stay running? |
|----------|----------------------|--------------------|
| 1        | rippled (standalone) | Yes                |
| 2        | Ledger auto-closer   | Yes                |
| 3        | Workload server      | Yes                |
| 4        | Testing with curl    | No                 |

---

## Step 1: Enter the Nix Dev Shell

In **all terminals** where you run project commands:

```bash
cd ~/rippled-workload
nix develop
```

## Step 2: Install Python Dependencies (first time only)

```bash
cd workload
uv sync
check-imports    # verify all imports resolve
```

---

## Step 3: Start rippled (Terminal 1)

```bash
cd ~/rippled-workload

# Clean old DB state and start fresh
rm -rf local-test/db && mkdir -p local-test/db

# Start standalone rippled with pre-funded genesis ledger
~/rippled/my_build/xrpld \
  --conf local-test/xrpld.cfg \
  -a \
  --ledgerfile local-test/genesis_ledger.json
```

**Key flags:**
- `-a` — standalone mode (no peers needed)
- `--ledgerfile local-test/genesis_ledger.json` — loads a genesis ledger with all 100 accounts pre-funded and all amendments enabled. No separate funding step needed.

**Wait for:** `NetworkOPs:NFO STATE->full` and both ports open (5005 RPC, 6006 WS).

## Step 4: Start the Ledger Auto-Closer (Terminal 2)

**CRITICAL:** Standalone rippled does NOT auto-close ledgers. Without this, transactions never validate.

```bash
while true; do
  curl -s http://127.0.0.1:5005 \
    -X POST -H "Content-Type: application/json" \
    -d '{"method":"ledger_accept"}' > /dev/null 2>&1
  sleep 1
done
```

## Step 5: Start the Workload Server (Terminal 3)

```bash
cd ~/rippled-workload/workload

XRPLD_NAME=127.0.0.1 \
XRPLD_RPC_PORT=5005 \
XRPLD_WS_PORT=6006 \
ACCOUNTS_JSON=../local-test/accounts.json \
ANTITHESIS_SDK_LOCAL_OUTPUT=../local-test/antithesis_sdk.jsonl \
uv run workload
```

**Startup takes 3–5 minutes.** The setup phase submits ~200 transactions (gateways, trust lines, vaults, NFTs, credentials, loans, etc.).

**Wait for:**

```
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000
```

If it hangs at "Waiting for application startup", check the ledger auto-closer is running.

---

## Step 6: Test Endpoints (Terminal 4)

### Single requests

```bash
curl http://localhost:8000/did/set/random       # DIDSet
curl http://localhost:8000/did/delete/random     # DIDDelete
```

**Expected response:** `null` with HTTP 200 — this is correct (fire-and-forget pattern).

### Verify no errors

In Terminal 3, you should see clean `200 OK` lines:

```
INFO: 127.0.0.1:XXXXX - "GET /did/set/random HTTP/1.1" 200 OK
```

If you see `[WARNING] DIDSet: actNotFound`, the accounts are not funded — verify you used `--ledgerfile local-test/genesis_ledger.json` in Step 3.

---

## Step 7: Stress Testing

### Parallel DIDSet (10 concurrent)

```bash
for i in $(seq 10); do
  curl --silent http://localhost:8000/did/set/random &
done
wait
echo "All 10 DIDSet requests completed"
```

### Parallel DIDDelete (10 concurrent)

```bash
for i in $(seq 10); do
  curl --silent http://localhost:8000/did/delete/random &
done
wait
echo "All 10 DIDDelete requests completed"
```

### Mixed DID stress test (20 concurrent)

```bash
for i in $(seq 15); do curl --silent http://localhost:8000/did/set/random & done
for i in $(seq 5); do curl --silent http://localhost:8000/did/delete/random & done
wait
echo "Mixed stress test completed"
```

### Sustained load test (60 seconds)

```bash
echo "Running 60-second sustained load test..."
END=$((SECONDS + 60))
COUNT=0
while [ $SECONDS -lt $END ]; do
  curl --silent http://localhost:8000/did/set/random > /dev/null &
  curl --silent http://localhost:8000/did/delete/random > /dev/null &
  COUNT=$((COUNT + 2))
  sleep 0.5
done
wait
echo "Submitted $COUNT requests in 60 seconds"
```


### All-transaction stress test

```bash
for i in $(seq 10); do curl --silent http://localhost:8000/did/set/random & done
for i in $(seq 10); do curl --silent http://localhost:8000/did/delete/random & done
for i in $(seq 5); do curl --silent http://localhost:8000/nft/mint/random & done
for i in $(seq 5); do curl --silent http://localhost:8000/payment/random & done
for i in $(seq 5); do curl --silent http://localhost:8000/delegate/set/random & done
for i in $(seq 5); do curl --silent http://localhost:8000/trustline/create/random & done
wait
echo "All-transaction stress test completed"
```

---

## Step 8: Verify Results

### Check Antithesis SDK assertions

```bash
grep -o '"message":"[^"]*DID[^"]*"' local-test/antithesis_sdk.jsonl \
  | sort | uniq -c | sort -rn
```

Expected output:

```
  N "message":"workload::seen : DIDSet"
  N "message":"workload::success : DIDSet"
  N "message":"workload::seen : DIDDelete"
  N "message":"workload::success : DIDDelete"
```

### Check for failures

```bash
python3 -c "
import json
for line in open('local-test/antithesis_sdk.jsonl'):
    d = json.loads(line)
    a = d.get('antithesis_assert', {})
    msg = a.get('message', '')
    if 'DID' in msg:
        er = a.get('details', {}).get('engine_result', '')
        if er and er != 'tesSUCCESS':
            print(f'{msg}: {er}')
" || echo "No DID failures found"
```

### Verify on the ledger

```bash
curl -s http://127.0.0.1:5005 \
  -X POST -H "Content-Type: application/json" \
  -d '{
    "method": "account_tx",
    "params": [{"account": "rEr9mSQdaGhqSBBkzTBW4xrPkKXbZQRtbF",
                "ledger_index_min": -1, "ledger_index_max": -1}]
  }' | python3 -m json.tool | grep -A2 '"TransactionType"'
```

---

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `actNotFound: Account not found` | Accounts not in genesis ledger | Use `--ledgerfile local-test/genesis_ledger.json` |
| `Address already in use` (port 5005/6006/8000) | Old process still running | `lsof -i :5005 -i :6006 -i :8000` then `kill <PID>` |
| Startup hangs forever | Ledger auto-closer not running | Start the `ledger_accept` loop (Step 4) |
| `Amendment not enabled` | Genesis ledger missing amendments | Re-run `generate_genesis.py` with your rippled's `features.macro` |

## Cleanup

```bash
# Ctrl+C in Terminals 3, 2, 1
# Or kill by port:
lsof -ti :5005 -ti :6006 -ti :8000 | xargs kill 2>/dev/null
```

To start fresh, repeat from Step 3 (`rm -rf local-test/db` clears old state).

---

## New Workload Checklist

Follow these standards when adding a new transaction type workload. Use **DID (`transactions/did.py`)** as the gold-standard reference implementation.

### 1. XRPL Spec Research

Before writing any code, read the authoritative specification:

- **Transaction docs:** `xrpl.org/docs/references/protocol/transactions/types/<name>`
- **XLS specs** (for newer features): `github.com/XRPLF/XRPL-Standards` under `XLS-NNNN-<name>/`
- **Identify all error codes** the transaction can return (e.g., `tecEMPTY_DID`, `tecNO_ENTRY`, `tefBAD_AUTH`, `temINVALID_FLAG`)
- **Identify required vs optional fields** and their constraints (hex-encoded, max length, etc.)

### 2. Implementation Files

| File | What to add |
|------|------------|
| `params.py` | Random parameter generators for all fields (hex, amounts, flags, etc.) |
| `transactions/<name>.py` | `handler()` → `_valid()` + `_faulty()` with real mutations |
| `models.py` | Dataclass if the object needs state tracking |
| `transactions/__init__.py` | REGISTRY entry (5-tuple) + state updater function |
| `assertions.py` | `_META_EXPECTATIONS` entry for the new tx type |
| `test_composer/` | `parallel_driver_<name>_random.sh` with `curl --silent` |
| `scripts/check-imports` | Add the new module import |
| `setup.py` | Creation logic if other transactions depend on this object |

### 3. Valid Handler (`_valid`)

- Pick a random account from `accounts`
- Use `params.*` generators for all field values — **never hardcode**
- Use `submit_tx()` from `submit.py` — **never call `xrpl_submit` directly**
- Return early silently when state is empty — **never raise, never log**
- Keep amounts within tracked balances — overdraw belongs in `_faulty` only

### 4. Faulty Handler (`_faulty`) — DO NOT LEAVE AS `pass`

This is the most important part for fuzz testing. Each `_faulty` handler must:

- Pick **ONE random mutation** via `choice()`
- Construct a deliberately invalid transaction
- Submit via `submit_tx()` — **must never raise**
- Have the same precondition guards as `_valid`

**Standard mutation categories** (use all that apply):

| Mutation | What it does | Expected error |
|----------|-------------|----------------|
| `non_owner_submission` | Sign with wrong wallet | `tefBAD_AUTH` |
| `fake_id()` | Use `params.fake_id()` for object IDs | `tecNO_ENTRY` / `tecOBJECT_NOT_FOUND` |
| `invalid_flags` | Set reserved flag bits (`0x80000000`) | `temINVALID_FLAG` |
| `zero_amount` | Use `"0"` for amount fields | `temBAD_AMOUNT` |
| `overdraw` | `balance + randint(1, 1_000_000)` | `tecINSUFFICIENT_FUNDS` |
| `mismatched_asset` | Wrong asset type for the object | `temBAD_CURRENCY` |
| `non_owner` | Submit as non-owner of the object | `tecNO_PERMISSION` |
| `empty_field` | Empty string for required fields | `tecEMPTY_DID` / `temMALFORMED` |

**Example** (from DID — the reference implementation):

```python
async def _did_set_faulty(accounts, client):
    if not accounts:
        return
    mutation = choice([
        "non_owner_submission",
        "invalid_flags",
        "single_empty_field",
    ])
    if mutation == "non_owner_submission":
        owner, impostor = sample(list(accounts.values()), 2)
        txn = DIDSet(account=owner.address, uri=params.did_hex_field())
        await submit_tx("DIDSet", txn, client, impostor.wallet)
    elif mutation == "invalid_flags":
        src = choice(list(accounts.values()))
        txn = DIDSet(account=src.address, uri=params.did_hex_field(), flags=0x80000000)
        await submit_tx("DIDSet", txn, client, src.wallet)
    elif mutation == "single_empty_field":
        src = choice(list(accounts.values()))
        field = choice(["uri", "data", "did_document"])
        txn = DIDSet(account=src.address, **{field: ""})
        await submit_tx("DIDSet", txn, client, src.wallet)
```

### 5. State Updater

Called by the WS listener on `tesSUCCESS`. Must:

- Parse created/deleted objects from `meta["AffectedNodes"]`
- Use `_extract_created_id(meta, entry_type)` / `_extract_deleted_id(meta, entry_type)`
- Update **both** global lists (`w.dids`, `w.vaults`, etc.) **and** per-account state
- Be idempotent — the same tx might be observed more than once

### 6. Meta Expectations

Add an entry to `_META_EXPECTATIONS` in `assertions.py` for every tx type:

```python
_META_EXPECTATIONS = {
    "DIDSet": ("Created", "Modified", "DID"),
    "DIDDelete": ("Deleted", "DID"),
    "YourNewTx": ("Created", "YourLedgerEntryType"),
}
```

This fires an `always` assertion: if rippled returns `tesSUCCESS` but the metadata doesn't contain the expected ledger entry operation, that's a bug. **Low effort, high value.**

### 7. Assertions Wiring

The REGISTRY entry automatically gets these assertions (via `register_assertions()`):

- `workload::seen : TxType` — **reachable**, must be hit (tx was submitted)
- `workload::success : TxType` — **sometimes**, must succeed at least once
- `workload::failure : TxType` — **sometimes**, must fail at least once

If your `_faulty` handler is `pass`, the `sometimes(failure)` assertion will fail in Antithesis because no error path is ever exercised. **This is why filling in `_faulty` is critical.**

Add tx type to `_NO_FAILURE_TYPES` only if it's genuinely impossible to produce a failure (very rare). Add to `_NO_SUCCESS_TYPES` only if it never succeeds in the test environment.

### 8. Local Fuzz Testing Workflow

Before pushing a new workload:

```bash
# 1. Run checks
check-imports
check-endpoints

# 2. Start the 3-terminal test stack (Steps 3-5 above)

# 3. Verify valid path works
curl http://localhost:8000/<your-endpoint>/random
# Should return null with 200 OK, no warnings in Terminal 3

# 4. Temporarily bump faulty rate for focused testing
# Edit params.py: return random() < 0.60
# Restart workload server

# 5. Run sustained load test (60 seconds)
END=$((SECONDS + 60)); COUNT=0
while [ $SECONDS -lt $END ]; do
  curl --silent http://localhost:8000/<your-endpoint>/random > /dev/null &
  COUNT=$((COUNT + 1)); sleep 0.5
done
wait
echo "Submitted $COUNT requests"

# 6. Verify both success AND failure assertions were hit
grep -o '"message":"[^"]*YourTxType[^"]*"' local-test/antithesis_sdk.jsonl \
  | sort | uniq -c | sort -rn

# Expected output should include ALL of:
#   workload::seen : YourTxType
#   workload::success : YourTxType
#   workload::failure : YourTxType

# 7. Check meta assertions passed
grep "meta_matches_tx_type" local-test/antithesis_sdk.jsonl \
  | grep "YourTxType" | head -5

# 8. Reset faulty rate before committing
# Edit params.py: return random() < 0.01

# 9. Final clean run at normal faulty rate
# Restart workload server, run 60-second test again
```

### 9. Antithesis-Specific Best Practices

- **All randomness through `workload.randoms`** — backed by `AntithesisRandom`, so Antithesis can guide exploration toward interesting states. Never use `import random`.
- **`send_event()` for observability** — emit events for mutation choices so Antithesis reports show which paths were explored.
- **`always` assertions catch real bugs** — invariants like "no internal rippled error", "meta matches tx type", and "no duplicate objects" are what find actual rippled regressions under fault injection.
- **`sometimes(failure)` with `must_hit=True`** — forces Antithesis to find at least one failure path. If your `_faulty` handler is empty, this assertion will fail and the Antithesis report will flag it.
- **Network topology matters** — the workload submits to a non-validating tracking node (isolated from faults). The 6 validators are subject to Antithesis fault injection. Never change the submission target.

### 10. Common PR Review Issues to Avoid

| Issue | Fix |
|-------|-----|
| `_faulty` handler is `pass # TODO` | Fill in with real mutations (see section 4) |
| Hardcoded values in transaction builders | Use `params.*` generators |
| Missing `_META_EXPECTATIONS` entry | Add expected ledger entry operations |
| Calling `xrpl_submit` directly | Use `submit_tx()` from `submit.py` |
| Missing early-return for empty state | Add `if not objects: return` |
| Logger calls in handlers | Use `send_event()` instead |
| Missing REGISTRY entry | Add 5-tuple to `transactions/__init__.py` |
| Missing `check-imports` entry | Add module import to `scripts/check-imports` |
| Missing test composer script | Add `parallel_driver_<name>_random.sh` |
| Using `import random` | Use `from workload.randoms import choice, random, ...` |