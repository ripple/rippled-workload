# Coding Rules

## No tech debt
Clean up dead code immediately in the same change. No unused imports, orphaned CLI flags, stale templates, or dead functions left behind.

## No AI co-authored commits
Never add AI Co-Authored-By lines (e.g. Claude, Copilot) to git commits. Human co-authors are fine.

## Brief commit messages
One-liner commit messages only, like all existing commits in this repo.

## Don't shy away from large refactors
If a refactor makes the code better, more idiomatic, or reduces maintenance burden — do it. Don't propose half-measures out of reluctance to touch many files.

## Keep docs in sync with code
When a code change affects project structure, conventions, submission patterns, assertion registration, setup steps, or CI pipeline — update the relevant `.claude/` docs and `CLAUDE.md` in the same change. Stale docs are worse than no docs.

## Always run `check-imports` before pushing
Import errors take 15-20 min to surface in Antithesis CI.
```bash
nix develop --command bash -c "scripts/check-imports && scripts/check-endpoints"
```

## Transaction submission
Use `submit_tx()` from `submit.py` — fire-and-forget via `autofill_and_sign` + `submit`.
Results observed by WS listener (`ws_listener.py`) which fires `tx_result()` assertions.
Exception: `LoanSet` uses manual counterparty co-signing in `lending.py`.

## Test composer scripts
- All `parallel_driver_*.sh` must use `curl --silent`
- `first_setup.sh` calls `/setup` deterministically before fault injection
- Never reference a non-existent endpoint
