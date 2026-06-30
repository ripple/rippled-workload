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
With the devshell active (direnv auto-loads it; else `nix develop`):
```bash
check-imports && check-endpoints
cd workload && ruff check src/workload/ && ruff format --check src/workload/ && mypy && basedpyright src/workload
```
Type annotations on all functions. Line length 100. Both type checkers must pass (CI in `.github/workflows/checks.yml` runs the same, wrapped in `nix develop --command`):
- **basedpyright** — the xrpl-aware gate; resolves xrpl-py via its `py.typed` (config: `pyrightconfig.json`, `venvPath=workload`, `typeCheckingMode=standard`). Zed uses it as the editor LSP.
- **mypy** — complementary flow/annotation check (no-any-return, unbound, missing annotations); xrpl resolves as `Any` (`ignore_missing_imports`, config in `workload/pyproject.toml`).

Prefer real narrowing/typed helpers over `# type: ignore` and `cast`; reserve casts for genuinely untyped deps (e.g. `antithesis`, wrapped once in `randoms.py`).

## Test composer scripts
`parallel_driver_*.sh` use `curl --silent`. Setup runs at FastAPI startup (no `first_*` scripts). Never reference a non-existent endpoint.
</content>
