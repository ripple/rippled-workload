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

## Getting the data (REST API)

Prefer fetching from the Antithesis read API over a manual download. Either a **run selector** or a
**local file/dir** is a valid input:

```bash
# Resolve + save run.json, properties.json, failing.txt, events.ndjson under ~/Downloads/antithesis/<run_id>/
python3 scripts/antithesis_fetch.py fetch <selector>
#   selector: latest | latest-failing | gh=<github_run_id> | match=<substr> | <run_id>
python3 scripts/antithesis_fetch.py properties <selector> --failing   # quick assertion triage
```

Auth: `$ANTITHESIS_API_KEY` or `~/antithesis.key` (`pass:` line); tenant `$ANTITHESIS_TENANT`
(default `ripple`). Endpoints: `GET /api/v0/runs`, `/runs/{id}`, `/runs/{id}/properties`,
`/runs/{id}/logs?input_hash=&vtime=` (the full event stream; `/events` is only a capped stdout
text-search). The script is in the rippled-workload repo at `scripts/antithesis_fetch.py`.

`properties.json` is the authoritative assertion view. **Separate real failures from coverage gaps:**
a genuinely failing invariant is `always`/`AlwaysOrUnreachable` with `condition:false` (e.g. a
rippled `src/.../*.cpp` or sidecar assertion). Names like `workload::failure|success|seen : <Tx>` and
`fuzzer::seen|faulted : <msg>` show "Failing" merely because that `sometimes`/`reachable` path was
never exercised — that's a coverage gap (too-conservative `_valid`, missing `_faulty`, or unreached
setup), not a server bug.

## Analyzing events.ndjson

`events.ndjson` is pure NDJSON (one JSON object per line, **no log prefix**). SDK event names are
top-level keys (`"workload::result : Payment"`, `"val_health"`, `"fault"`, …). Parse with
`json.loads(line)` directly:

```bash
# Engine-result breakdown
python3 -c "
import json,collections,sys
er=collections.Counter()
for line in open(sys.argv[1]):
    try: d=json.loads(line)
    except: continue
    for k,v in d.items():
        if k.startswith('workload::result') and isinstance(v,dict) and v.get('engine_result'):
            er[v['engine_result']]+=1
for k,v in er.most_common(): print(f'{v:6d} {k}')
" events.ndjson

# Fault-injection breakdown (which faults ran)
python3 -c "
import json,collections,sys
f=collections.Counter()
for line in open(sys.argv[1]):
    try: d=json.loads(line)
    except: continue
    if 'fault' in d:
        x=d['fault']; f[x.get('type') if isinstance(x,dict) else x]+=1
print(dict(f.most_common()))
" events.ndjson

# Endpoint exceptions (should be zero) / setup
grep -c 'workload::endpoint_exception' events.ndjson
grep 'workload::setup_reject\|workload::setup_error' events.ndjson
```

Legacy fallback: an old downloaded `events(N).log` has a ` - {json}` prefix and event-name-as-text —
parse those lines with `json.loads(line.split(' - ',1)[1])`.

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
