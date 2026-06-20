# Antithesis Rules

## SDK assertions
- `assert_raw()` is the only way to register dynamic-name assertions — f-strings are invisible to the static scanner.
- `TX_TYPES` derives from `REGISTRY` (single source of truth). Each entry gets `seen`/`success`/`failure` at startup via `register_assertions()`.
- `reachable` — reached ≥ once. `sometimes(success)` — succeeds ≥ once. `sometimes(failure)` — fails ≥ once. `always` — invariant. `unreachable` — must never fire (fatal errors).

## SDK handler chain (Python)
`VoidstarHandler` (libvoidstar.so, injected by Antithesis) → `LocalHandler` (`ANTITHESIS_SDK_LOCAL_OUTPUT`) → `NoopHandler` (silent). Outside Antithesis: `export ANTITHESIS_SDK_LOCAL_OUTPUT=/path/file.jsonl`.

## Test composer phases
```
setup_complete() -> [first_*] -> [drivers + anytime_*] -> [eventually_* / finally_*]
                    no faults     faults active           faults stopped
```
Implemented: `first_*`, `parallel_driver_*`.

## Network topology
Workload submits to the non-validating tracking node (`xrpld`), isolated from fault injection. The 6 validators (`val0`–`val4`, `fuzzer`) take faults. Don't retarget submission to a validator — fault jitter would corrupt transaction results.
</content>
