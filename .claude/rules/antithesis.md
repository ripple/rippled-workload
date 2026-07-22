# Antithesis Rules

## SDK assertions
- `assert_raw()` is the only way to register dynamic-name assertions — f-strings are invisible to the static scanner.
- `TX_TYPES` derives from `REGISTRY` (single source of truth). Each entry gets `seen`/`success`/`failure` at startup via `register_assertions()`.
- `reachable` — reached ≥ once. `sometimes(success)` — succeeds ≥ once. `sometimes(failure)` — fails ≥ once. `always` — invariant. `unreachable` — must never fire (fatal errors).
- `_META_EXPECTATIONS` (`assertions.py`) — per-type `always` that a tesSUCCESS touched the expected `LedgerEntryType` under the allowed node op(s). Rows are verified against the rippled transactor (guaranteed on **every** success); create-or-update types list `Created`+`Modified`. Skips inner-batch txns (effects consolidate into the outer Batch's meta). Also skips the **update** path of `_IDEMPOTENT_UPDATE_MARKER` types (keyed by the tx field that marks an update, e.g. `PermissionedDomainSet`→`DomainID`): re-setting unchanged data is a legitimate tesSUCCESS no-op — rippled drops the byte-identical modify from metadata (`ApplyStateTable.cpp`), so no node appears. The invariant stays enforced on the create path.
- `network_functional_after_faults` — `sometimes` liveness fed by the `/probe/network` endpoint (`probe.py`, driven by `eventually_network_probe.sh`): a payment must validate tesSUCCESS once faults stop.
- Sidecar (`sidecar/sidecar.py`) `always`: consensus safety (agree on hash per index), no unrecovered stall (frozen past `--max-stall`), **close_time tracks wall clock** (a freshly closed ledger's `close_time` within `MAX_TIME_SKEW_SECS` — checked only when `ledger_index` advances, so a manipulated close_time is caught on the ledger it rides while fault-induced halts, which close no new ledger, don't false-fire; no interval budget), and **XRP supply never increases** (`total_coins` monotone non-increasing as a validator's ledger advances — fees only burn).

## SDK handler chain (Python)
`VoidstarHandler` (libvoidstar.so, injected by Antithesis) → `LocalHandler` (`ANTITHESIS_SDK_LOCAL_OUTPUT`) → `NoopHandler` (silent). Outside Antithesis: `export ANTITHESIS_SDK_LOCAL_OUTPUT=/path/file.jsonl`.

## Test composer phases
```
setup_complete() -> [first_*] -> [drivers + anytime_*] -> [eventually_* / finally_*]
                    no faults     faults active           faults stopped
```
Implemented: `parallel_driver_*` (drivers) and `eventually_network_probe.sh` (post-fault liveness probe). Composer picks scripts by filename prefix, so an `eventually_*` in the driver folder runs only in the eventually phase.

## Network topology
Workload submits to the non-validating tracking node (`xrpld`), isolated from fault injection. The 6 validators (`val0`–`val4`, `fuzzer`) take faults. Don't retarget submission to a validator — fault jitter would corrupt transaction results.
</content>
