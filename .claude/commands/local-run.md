# local-run

Start a standalone xrpld + workload for local testing.

## Prerequisites

Genesis ledger must exist in `local-test/` with amendments injected:
```bash
nix develop --command uv run prepare-workload/generate_genesis.py \
  --features-macro <path-to-rippled>/include/xrpl/protocol/detail/features.macro \
  --genesis genesis/genesis_ledger.json \
  --accounts genesis/accounts.json \
  --output-dir local-test
```

Replace `<path-to-rippled>` with the local rippled source checkout. Ask the user if unsure.

## Run

```bash
lsof -ti:5005 -ti:8000 2>/dev/null | xargs kill 2>/dev/null; sleep 1
nix develop --command bash -c "./local-test/run.sh"
```

## What it does

1. Starts xrpld standalone from `local-test/genesis_ledger.json`
2. Loads 100 pre-funded accounts from `local-test/accounts.json`
3. Starts workload on `:8000`
4. Calls `/setup` to seed state (trust lines, MPTs, vaults, NFTs, etc.)
5. Calls `/payment/random` as a smoke test
6. Prints SDK assertions that fired (`hit=true`) from `local-test/antithesis_sdk.jsonl`

## Verifying assertions

```bash
grep '"hit": true' local-test/antithesis_sdk.jsonl | \
  python3 -c "import sys,json; [print(d['message']) for l in sys.stdin for d in [json.loads(l).get('antithesis_assert',{})]]"
```

## Hitting endpoints manually

```bash
curl -s http://127.0.0.1:8000/setup | python3 -m json.tool
curl -s http://127.0.0.1:8000/payment/random
curl -s http://127.0.0.1:8000/accounts | python3 -c "import sys,json; print(len(json.load(sys.stdin)))"
```
