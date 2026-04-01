# Debug Antithesis Run

How to diagnose issues from an Antithesis experiment.

## Get the events log

Antithesis provides `events(N).log` in the triage report. Download and search it.

## Key patterns to grep

```bash
# Workload assertion failures
grep "workload::" events.log | grep '"condition":false\|"hit":true.*Unreachable'

# Endpoint exceptions (workload code broken)
grep "endpoint_exception" events.log | python3 -c "
import sys, json
for line in sys.stdin:
    try:
        d = json.loads(line.split(' - ', 1)[1])
        print(d['antithesis_assert']['details'])
    except: pass
"

# Validator crashes
grep "fatal\|Unable to open\|SIGABRT\|condition.:false" events.log | head -20

# accounts.json missing
grep "accounts_json" events.log

# Setup assertions (did /setup work?)
grep "workload::setup" events.log | grep '"hit": true'
```

## Common issues and fixes

| Symptom | Cause | Fix |
|---------|-------|-----|
| `accounts_json_missing` fires | accounts.json not mounted in workload container | Check docker-compose volume mounts survive the workflow's compose manipulation |
| `endpoint_exception` fires | Handler name shadows import | Rename inner async def with `_endpoint` suffix |
| All tx assertions fail (never seen) | Accounts not loaded / endpoint returning None | Check accounts.json exists, check for early returns |
| Validators crash instantly | genesis_ledger.json not found | Confirm genesis_ledger.json is in each node's volume dir |
| `Unable to open /genesis_ledger.json` | Wrong path in xrpld command | Path must be `/opt/xrpld/etc/genesis_ledger.json` (in node volume) |

## Verify SDK pipeline working

If `workload::started` and `workload::sdk_works` appear in triage but tx assertions don't:
- SDK is working (VoidstarHandler active)
- Transactions aren't being submitted — check account loading, check for exceptions

If nothing appears:
- Check if workload container even started
- VoidstarHandler may not be getting injected — check `/usr/lib/libvoidstar.so` exists in container
