# Coding Rules

## Brevity
Code is the documentation. Comment only the WHY, never the WHAT. Drop docstrings that restate the signature; keep ones that record a non-obvious reason or gotcha. No filler prose in `.claude/` docs or `CLAUDE.md` — facts and gotchas only.

## Planning
For non-trivial work, surface open questions and an outline before coding; get answers, then implement. Use Plan Mode for multi-step changes.

## No tech debt
Remove dead code in the same change — no unused imports, orphaned flags, stale templates, or dead functions. No TODOs without agreement.

## Refactors
Take the large refactor if it makes the code more idiomatic or cuts maintenance. No half-measures.

## Docs in sync
A code change that affects structure, conventions, submission patterns, assertion registration, setup steps, or CI must update the relevant `.claude/` docs and `CLAUDE.md` in the same change.

## Commits
One-liner messages, like the existing history. No AI Co-Authored-By lines (human co-authors fine).

## Before pushing
```bash
nix develop --command bash -c "scripts/check-imports && scripts/check-endpoints"
nix develop --command bash -c "cd workload && ruff check src/workload/ && ruff format --check src/workload/"
```
Type annotations on all functions. Line length 100.

## Test composer scripts
`parallel_driver_*.sh` use `curl --silent`. Setup runs at FastAPI startup (no `first_*` scripts). Never reference a non-existent endpoint.
</content>
