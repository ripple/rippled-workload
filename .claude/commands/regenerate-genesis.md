# regenerate-genesis

Regenerate genesis ledger with amendments injected for local testing.

## For local testing

```bash
nix develop --command uv run prepare-workload/generate_genesis.py \
  --features-macro <path-to-rippled>/include/xrpl/protocol/detail/features.macro \
  --genesis genesis/genesis_ledger.json \
  --accounts genesis/accounts.json \
  --output-dir local-test
```

Replace `<path-to-rippled>` with the local rippled source checkout. Ask the user if unsure.

This reads the committed `genesis/` files (100 pre-funded accounts, empty amendments), injects amendment hashes from `features.macro`, and writes to `local-test/`.

## Notes

- `genesis/genesis_ledger.json` — committed base with empty amendments (CI injects correct ones per xrpld binary)
- `genesis/accounts.json` — 100 SECP256K1 accounts, committed
- `local-test/` — gitignored, local copies with amendments injected
- Amendment hashes = SHA-512Half of amendment name — pure Python, no xrpld binary needed
- Includes both `Supported::yes` and `Supported::no` features; excludes retired
- Only need to regenerate when rippled adds/removes amendments
