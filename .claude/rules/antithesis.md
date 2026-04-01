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
`anytime_*`, `eventually_*`, `finally_*` deferred — need team input on rippled-specific checks (ledger hash agreement, XRP conservation).

## Assertions in triage reports

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
- Includes both `Supported::yes` and `Supported::no` amendments; excludes retired
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
