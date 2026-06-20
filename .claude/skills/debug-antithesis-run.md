# Debug Antithesis Run

Diagnose issues from an Antithesis experiment. The triage report ships `events(N).log` — download and grep it.

## Grep patterns
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

grep "fatal\|Unable to open\|SIGABRT\|condition.:false" events.log | head -20   # validator crashes
grep "accounts_json" events.log
grep "workload::setup" events.log | grep '"hit": true'                          # did /setup work?
```

## Common issues
| Symptom | Cause | Fix |
|---------|-------|-----|
| `accounts_json_missing` | accounts.json not mounted in workload container | Check compose volume mounts survive the workflow's compose manipulation |
| `endpoint_exception` | Handler name shadows import | Rename inner async def with `_endpoint` suffix |
| All tx assertions never seen | Accounts not loaded / endpoint returns None | Check accounts.json exists, check early returns |
| Validators crash instantly | genesis_ledger.json not found | Confirm it's in each node's volume dir |
| `Unable to open /genesis_ledger.json` | Wrong path | Must be `/opt/xrpld/etc/genesis_ledger.json` |

## SDK pipeline check
- `workload::started` + `workload::sdk_works` present but no tx assertions → SDK works (VoidstarHandler active); txns not submitted (check account loading / exceptions).
- Nothing present → container didn't start, or `/usr/lib/libvoidstar.so` not injected.
</content>
