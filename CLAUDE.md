# CLAUDE.md

Permanent instructions for Claude Code working on this repository.

## Project

- Project name: Chess Decision Explorer.
- Python project developed on Ubuntu/Linux.
- Main goal: analyze a user's chess history, identify recurring damaging
  decisions, compare them with a reference population, and later add
  Stockfish and visual/opening-training features.

## Authority and architecture

- Architecture and analysis semantics are decided by Dvir together with the
  project reviewer.
- Claude must not invent, expand, or silently change architecture.
- Before significant work, read `docs/architecture.md` and
  `docs/project_state.md`.
- Existing tests and approved architecture are constraints, not suggestions.

## Scope discipline

- Work only on the explicitly requested step.
- Never begin the next roadmap step automatically.
- Do not add dependencies without explicit approval.
- Do not implement future features unless explicitly requested.
- Prefer minimal, clear implementations over speculative abstractions.

### Current known future features that must NOT be implemented unless requested

- Chess.com ingestion
- Lichess/reference ingestion
- statistics aggregation
- regret / Top-K analysis
- Stockfish
- concurrency / multiprocessing
- persistent databases
- opening classification
- repertoire scoring/training
- web/visual UI

## Git workflow

- Inspect `git status` before changes.
- Never commit unless explicitly authorized.
- Never rewrite Git history without explicit authorization.
- Do not modify unrelated files.

## Testing and review

- Run the complete pytest suite after implementation.
- Do not weaken or delete tests merely to make code pass.
- If an approved test appears incorrect, stop and report it.
- Run `git diff --check`.
- End each implementation task with a concise review handoff:
  - STATUS
  - FILES CHANGED
  - TESTS
  - GIT
  - DESIGN NOTES
  - UNCERTAINTIES / BLOCKERS
  - REVIEW MATERIAL

## Escalation

Stop and ask for architectural review before:

- changing domain semantics
- changing public interfaces in a meaningful way
- adding a dependency
- choosing a new storage/database architecture
- choosing concurrency architecture
- changing analysis/statistical definitions
- weakening requirements
- rewriting Git history
