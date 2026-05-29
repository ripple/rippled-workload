---
name: fetch-antithesis-results
description: Fetch an Antithesis run's failing assertions + full event stream from the REST API and analyze it
disable-model-invocation: true
argument-hint: "[latest | latest-failing | gh=<id> | match=<substr> | <run_id>] [save_dir=<path>]"
allowed-tools: [Bash(python3 scripts/antithesis_fetch.py *), Bash(python3 *)]
---

Pull an Antithesis experiment's results via the read REST API — no manual triage-report download —
then hand the saved artifacts to the `experiment-analyst` agent.

## Auth

The fetch script needs a Bearer token: `$ANTITHESIS_API_KEY`, or a key file (`$ANTITHESIS_KEY_FILE`,
default `~/antithesis.key`; a bare token or a `pass: <token>` line). If neither is present, tell the
user to set it and stop. Tenant defaults to `ripple` (`$ANTITHESIS_TENANT`).

## Steps

1. Parse `$ARGUMENTS`. First token is the run **selector** (default `latest` if omitted):
   - `latest` — most recent run
   - `latest-failing` — most recent run with status `incomplete`
   - `gh=<id>` — run launched by that GitHub Actions run id (matches `run=<id>` in the description)
   - `match=<substr>` — most recent run whose description contains the substring (e.g. a branch name)
   - a literal `<run_id>`
   Optional `save_dir=<path>` overrides where artifacts are written.

2. From the rippled-workload repo root, run the fetch (it resolves the run, saves artifacts, and
   prints a summary):
   ```
   python3 scripts/antithesis_fetch.py fetch <selector> [--save-dir <path>]
   ```
   This writes to `<save_dir|~/Downloads/antithesis>/<run_id>/`:
   - `run.json` — status, commits/refs, `links.triage_report` (signed URL)
   - `properties.json` — every assertion outcome
   - `failing.txt` — the non-passing assertions with source `file:line`
   - `events.ndjson` — the full event stream via `/logs` at the latest moment: SDK events
     (`workload::result : <Tx>`, `val_health`, `fault`, …) plus all container stdout/stderr

3. Print the script's summary (status, triage link, failing count) to the user.

4. Hand off to the **experiment-analyst** agent for root-cause analysis. Give it the saved directory
   path and point it at `failing.txt`, `properties.json`, and `events.ndjson`. Tell it to separate
   genuinely failing invariants (`always`/`AlwaysOrUnreachable` with `condition:false`, e.g. a
   rippled or sidecar assertion) from unsatisfied **coverage** assertions that merely show as
   "Failing" when not yet exercised (`workload::failure|success|seen : <Tx>`, `fuzzer::seen|faulted`).

## Notes

- `events.ndjson` is pure NDJSON — one JSON object per line, **no log prefix**. Parse with
  `json.loads(line)`; SDK event names are top-level keys (e.g. `"workload::result : Payment"`).
- For a deep dive on a specific failing assertion, its counterexample carries a `moment`
  (`input_hash`,`vtime`); fetch the surrounding window with
  `python3 scripts/antithesis_fetch.py logs <run_id> --input-hash H --vtime V [--begin-input-hash H0 --begin-vtime V0]`.
