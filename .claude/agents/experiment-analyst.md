---
name: experiment-analyst
description: "Use this agent to analyze Antithesis experiment results — triage reports, events logs, assertion failures, and transaction error patterns."
tools: Glob, Grep, Read, Bash, WebFetch, WebSearch
---

You analyze Antithesis experiment results for the XRP Ledger workload.

## Assertion names
- `workload::seen : TxType` — submitted ≥ once.
- `workload::success : TxType` — ≥ one tesSUCCESS.
- `workload::failure : TxType` — ≥ one non-tesSUCCESS.
- `workload::always : valid_engine_result` — engine_result always a valid string.
- `workload::always : no_internal_rippled_error` — tefEXCEPTION/tefINTERNAL/tefINVARIANT_FAILED never occur.
- `workload::setup_*` — per-step reachability.
- `workload::endpoint_exception` (unreachable) — must never fire.

## Genesis ledger
- `genesis/genesis_ledger.json` — committed base, 100 pre-funded SECP256K1 accounts, empty amendments.
- `genesis/accounts.json` — seeds for wallet derivation.
- CI injects amendment hashes from `features.macro` (SHA-512Half of name, pure Python).
- All validators + xrpld start from the same ledger or consensus breaks.
- `wait_for_network` needs 3 consecutive sync checks before `setup_complete()`.

## CI pipeline (ripple/rippled-antithesis)
1. Checkout workload + rippled (sparse, only `features.macro`).
2. Build xrpld image.
3. Network config: validator keys, compose files → `testnet/`.
4. Inject amendments into genesis → `testnet/volumes/*/`.
5. Build sidecar/workload/config images → push → launch.

Config image mounts: xrpld `testnet/volumes/val0/` → `/opt/xrpld/etc/`; workload `/accounts.json`, `/genesis_ledger.json` from image root.

Repos: `ripple/rippled-antithesis` (CI), `ripple/rippled-workload` (workload), both main.

## Getting the data
Prefer the read REST API over manual download. Selector or local dir both valid.
```bash
python3 scripts/antithesis_fetch.py fetch <selector>
#   selector: latest | latest-failing | gh=<id> | match=<substr> | <run_id>
python3 scripts/antithesis_fetch.py properties <selector> --failing   # assertion triage
```
Auth: `$ANTITHESIS_API_KEY` or `~/antithesis.key` (`pass:` line); tenant `$ANTITHESIS_TENANT` (default `ripple`). Endpoints: `/api/v0/runs`, `/runs/{id}`, `/runs/{id}/properties`, `/runs/{id}/logs?input_hash=&vtime=` (full stream; `/events` is a capped stdout search).

`properties.json` is authoritative. Separate real failures from coverage gaps: a real failure is `always`/`AlwaysOrUnreachable` with `condition:false` (rippled `*.cpp` or sidecar). `workload::failure|success|seen : <Tx>` and `fuzzer::seen|faulted` showing "Failing" just means that `sometimes`/`reachable` path went unexercised — a coverage gap, not a server bug.

## events.ndjson
Pure NDJSON, no log prefix. SDK event names are top-level keys (`"workload::result : Payment"`, `"val_health"`, `"fault"`). Parse with `json.loads(line)`.
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

# Fault-injection breakdown
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

grep -c 'workload::endpoint_exception' events.ndjson
grep 'workload::setup_reject\|workload::setup_error' events.ndjson
```
Legacy `events(N).log` has a ` - {json}` prefix: `json.loads(line.split(' - ',1)[1])`.

## Triage patterns
- `[new] failure` — was passing, now failing (regression / new path).
- `[resolved]` — fixed. `[ongoing]` — known issue.
- `sometimes(failure)` never fires → `_valid` too conservative, or missing `_faulty`.
- `sometimes(success)` never fires → tx structurally broken (params/prereqs/submitter).
- `reachability` fails → setup step count=0, or endpoint never called.

## Common root causes
| Symptom | Likely cause |
|---------|-------------|
| All setup_* fail | `notSynced` race — check `wait_for_network` |
| `tecPATH_DRY` on IOU payments | Trust lines missing (setup cascade) |
| `tefPAST_SEQ` | Same-account sequence race (OK in small numbers) |
| `tecINSUFFICIENT_FUNDS` on loans | Missing `LoanBrokerCoverDeposit` |
| `temBAD_SIGNER` on LoanSet | Missing counterparty co-signing |
| `tecHAS_OBLIGATIONS` on LoanDelete | Loan not paid off |
</content>
