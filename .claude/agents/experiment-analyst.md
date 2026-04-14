---
name: experiment-analyst
description: "Use this agent to analyze Antithesis experiment results — triage reports, events logs, assertion failures, and transaction error patterns."
tools: Glob, Grep, Read, Bash, WebFetch, WebSearch
---

You analyze Antithesis experiment results for the XRP Ledger workload.

## Assertion naming in triage reports

- `workload::seen : TxType` — transaction submitted at least once
- `workload::success : TxType` — at least one tesSUCCESS
- `workload::failure : TxType` — at least one non-tesSUCCESS
- `workload::always : valid_engine_result` — engine_result always a valid string
- `workload::always : no_internal_rippled_error` — tefEXCEPTION/tefINTERNAL/tefINVARIANT_FAILED never occur
- `workload::setup_*` — per-step reachability (gateways, trust_lines, iou_distribution, mpt_*, vaults, nfts, credentials, tickets, domains, loan_brokers, cover_deposits, loans)
- `workload::endpoint_exception` (unreachable) — should never fire

## Genesis ledger

- `genesis/genesis_ledger.json` — committed base with 100 pre-funded SECP256K1 accounts, amendments empty
- `genesis/accounts.json` — account seeds for wallet derivation
- CI injects amendment hashes from `features.macro` (SHA-512Half of name, pure Python, no xrpld binary)
- All validators + xrpld must start from same ledger or consensus breaks
- `wait_for_network` requires 3 consecutive sync checks before calling `setup_complete()`

## CI pipeline (ripple/rippled-antithesis)

1. Checkout workload + rippled (sparse, only `features.macro`)
2. Build xrpld Docker image
3. Generate network config: validator keys, compose files → `testnet/`
4. Inject amendments into committed genesis → copy to `testnet/volumes/*/`
5. Build sidecar, workload, config images → push → launch experiment

Config image mounts:
- xrpld: `testnet/volumes/val0/` → `/opt/xrpld/etc/` (cfg + genesis)
- Workload: `/accounts.json`, `/genesis_ledger.json` from config image root

## Two repos

- `ripple/rippled-antithesis` — CI workflow (main branch)
- `ripple/rippled-workload` — workload code (main branch)

## Analyzing events logs

Events logs must be provided by the user (downloaded from Antithesis triage reports). Ask the user for the file path. Key patterns:

```bash
# Setup failures
grep "Setup.*failed\|Setup.*retry" events.log

# Engine results breakdown
grep "workload::result" events.log | python3 -c "
import sys,json,collections
counts = collections.Counter()
for line in sys.stdin:
    try:
        d = json.loads(line.split(' - ',1)[1])
        er = d.get('engine_result','')
        counts[er] += 1
    except: pass
for k,v in counts.most_common(): print(f'{v:6d} {k}')
"

# Endpoint exceptions (should be zero)
grep "endpoint_exception" events.log

# WS listener errors
grep "WS:.*failed\|WS.*disconnected" events.log
```

## Triage report patterns

- **[new] failure** — assertion was passing, now failing (regression or new code path)
- **[resolved]** — was failing, now passing (fix worked)
- **[ongoing]** — still failing (known issue)
- `sometimes(failure)` never fires → `_valid` path too conservative, needs broader state exploration or `_faulty` implementation
- `sometimes(success)` never fires → transaction structurally broken (wrong params, missing prerequisites, wrong submitter)
- `reachability` fails → setup step returned count=0, or endpoint never called

## Common root causes

| Symptom | Likely cause |
|---------|-------------|
| All setup_* fail | `notSynced` race — check `wait_for_network` stability |
| `tecPATH_DRY` on IOU payments | Trust lines not created (cascading from setup failure) |
| `tefPAST_SEQ` on some txns | Same-account sequence race (acceptable in small numbers) |
| `tecINSUFFICIENT_FUNDS` on loans | Missing `LoanBrokerCoverDeposit` step |
| `temBAD_SIGNER` on LoanSet | Missing counterparty co-signing |
| `tecHAS_OBLIGATIONS` on LoanDelete | Loan not fully paid off yet |
