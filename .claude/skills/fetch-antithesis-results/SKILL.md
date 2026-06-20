---
name: fetch-antithesis-results
description: Fetch an Antithesis run's failing assertions + full event stream from the REST API and analyze it
disable-model-invocation: true
argument-hint: "[latest | latest-failing | gh=<id> | match=<substr> | <run_id>] [save_dir=<path>]"
allowed-tools: [Bash(python3 scripts/antithesis_fetch.py *), Bash(python3 *)]
---

Pull an Antithesis run's results via the read REST API, then hand the artifacts to the `experiment-analyst` agent.

## Auth
Bearer token from `$ANTITHESIS_API_KEY` or a key file (`$ANTITHESIS_KEY_FILE`, default `~/antithesis.key`; bare token or `pass: <token>` line). If neither exists, tell the user and stop. Tenant `$ANTITHESIS_TENANT` (default `ripple`).

## Steps
1. Parse `$ARGUMENTS`. First token is the selector (default `latest`): `latest` | `latest-failing` (status `incomplete`) | `gh=<id>` (GitHub Actions run id, matches `run=<id>` in description) | `match=<substr>` (latest run whose description contains it) | `<run_id>`. Optional `save_dir=<path>`.
2. From the repo root:
   ```
   python3 scripts/antithesis_fetch.py fetch <selector> [--save-dir <path>]
   ```
   Writes to `<save_dir|~/Downloads/antithesis>/<run_id>/`: `run.json` (status, refs, `links.triage_report`), `properties.json` (every assertion outcome), `failing.txt` (non-passing + source `file:line`), `events.ndjson` (full `/logs` stream + container stdout/stderr).
3. Print the script summary (status, triage link, failing count).
4. Hand off to **experiment-analyst** with the saved dir. Tell it to separate real failures (`always`/`AlwaysOrUnreachable` + `condition:false`, rippled/sidecar) from coverage gaps (`workload::failure|success|seen : <Tx>`, `fuzzer::seen|faulted`).

## Notes
- `events.ndjson` is pure NDJSON, no log prefix; event names are top-level keys (`"workload::result : Payment"`).
- Deep dive on a failing assertion: its counterexample carries a `moment` (`input_hash`,`vtime`); fetch the window with `python3 scripts/antithesis_fetch.py logs <run_id> --input-hash H --vtime V [--begin-input-hash H0 --begin-vtime V0]`.
</content>
