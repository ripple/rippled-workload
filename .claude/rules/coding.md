# Coding Rules

## Planning before implementation
For non-trivial tasks, start with open questions and an outline — don't jump straight to a complete plan or code. Present your questions, get answers, propose an approach, get feedback, then implement. Use Plan Mode for multi-step work.

## No tech debt
Clean up dead code immediately in the same change. No unused imports, orphaned CLI flags, stale templates, or dead functions left behind.

## No AI co-authored commits
Never add AI Co-Authored-By lines (e.g. Claude, Copilot) to git commits. Human co-authors are fine.

## Brief commit messages
One-liner commit messages only, like all existing commits in this repo.

## Don't shy away from large refactors
If a refactor makes the code better, more idiomatic, or reduces maintenance burden — do it. Don't propose half-measures out of reluctance to touch many files.

## Keep docs in sync with code
When a code change affects project structure, conventions, submission patterns, assertion registration, setup steps, or CI pipeline — update the relevant `.claude/` docs and `CLAUDE.md` in the same change.

## Always run checks before pushing
```bash
nix develop --command bash -c "scripts/check-imports && scripts/check-endpoints"
```

## Python style
Type annotations required on all functions. Line length 100 chars. Run `ruff check` and `ruff format` before committing:
```bash
nix develop --command bash -c "cd workload && ruff check src/workload/ && ruff format --check src/workload/"
```

## Test composer scripts
- All `parallel_driver_*.sh` must use `curl --silent`
- Setup runs during FastAPI startup (before `setup_complete()`) — no `first_*` scripts needed
- Never reference a non-existent endpoint
