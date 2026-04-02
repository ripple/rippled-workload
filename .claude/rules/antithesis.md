# Antithesis Rules

## SDK assertions

- `assert_raw()` is the only reliable way to register assertions with dynamic names — f-strings are invisible to the static scanner
- `TX_TYPES` is derived from `REGISTRY` in `transactions/__init__.py` — the single source of truth. Every entry gets `seen`, `success`, and `failure` catalog entries registered at startup via `register_assertions()`
- `reachable` — must be reached at least once (tx submitted)
- `sometimes(success)` — must succeed at least once
- `sometimes(failure)` — must fail at least once (verifies error paths exercised)
- `always` — must hold every time evaluated (invariants)
- `unreachable` — must never be reached (fatal errors like missing accounts.json)

## SDK handler chain (Python)

`VoidstarHandler` (/usr/lib/libvoidstar.so, injected by Antithesis) → `LocalHandler` (ANTITHESIS_SDK_LOCAL_OUTPUT env var) → `NoopHandler` (silent, discards everything)

Outside Antithesis: `export ANTITHESIS_SDK_LOCAL_OUTPUT=/path/to/file.jsonl`

## Test composer phases

```
setup_complete() -> [first_*] -> [drivers + anytime_*] -> [eventually_* / finally_*]
                    no faults     faults active           faults stopped
```

Current state: only `first_*` and `parallel_driver_*` implemented.

## Network topology

The workload submits to a **non-validating tracking node** (`xrpld`), intentionally isolated from Antithesis fault injection. The 6 validators (`val0`–`val4` + `fuzzer`) are subject to faults. Do not change the submission target — submitting to a validator would introduce fault-induced jitter into transaction results.
