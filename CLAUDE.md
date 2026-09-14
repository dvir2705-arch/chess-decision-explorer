# CLAUDE.md

Permanent instructions for Claude Code working on this repository.

## Project

- Project name: Chess Decision Explorer.
- Python project developed on Ubuntu/Linux.
- Main goal: analyze a player's chess history, identify recurring decisions, evaluate damaging choices with Stockfish evidence, and later combine those results with rating-matched human reference data and training workflows.
- The current repository already includes the domain model, aggregation, streaming local PGN ingestion, personal Rapid/Blitz/Bullet cohorts, Stockfish/UCI evaluation, a two-level in-memory/SQLite cache, and the Step 4B move-damage assessment foundation.
- Step 4B is still uncalibrated. Step 4C recurrence-based ranking is not implemented.

## Authority and architecture

- Architecture and analysis semantics are decided by Dvir together with the project reviewer.
- Claude must not invent, expand, or silently change architecture.
- Before significant work, read `docs/architecture.md` and `docs/project_state.md`.
- Existing tests and approved architecture are constraints, not suggestions.

## Scope discipline

- Work only on the explicitly requested step.
- Never begin the next roadmap step automatically.
- Do not add dependencies without explicit approval.
- Do not implement future features unless explicitly requested.
- Prefer minimal, clear implementations over speculative abstractions.

### Current future work that must NOT be implemented unless requested

- calibration of Step 4B production thresholds and search budgets
- Step 4C recurrence × admitted-engine-damage ranking
- Chess.com API ingestion beyond the implemented local-PGN path
- Lichess/reference-corpus ingestion and rating-matched human evidence
- opening classification and repertoire analysis
- targeted training and longitudinal progress tracking
- concurrency / multiprocessing architecture
- cloud infrastructure or alternate production databases
- web / visual UI
- cross-position pattern intelligence / ML similarity methods

## Git workflow

- Inspect `git status` before changes.
- Never commit unless explicitly authorized.
- Never rewrite Git history without explicit authorization.
- Do not modify unrelated files.
- Do not include private session URLs, local machine paths, credentials, tokens, or personal datasets in commit messages or tracked files.

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
