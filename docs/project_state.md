# Project State

## Current Phase

Step 3C complete and approved — personal analysis cohorts (`personal.py`):
time-control classification, validated immutable analysis policy,
disposition classification, and separate Rapid / Blitz / Bullet CORE
position indexes with per-game transactional accounting.

Step 3B complete — real-data smoke test passed successfully.

Step 3A complete — local PGN parsing and personal decision extraction.

Step 2 complete — aggregation layer.

Step 1 complete — chess domain value objects.

## Last Approved Code Commit

`feat: add personal analysis cohorts` — Step 3C, approved after
architectural review (supersedes `a6e0590 feat: add local PGN ingestion and
decision extraction`).

## Completed

- Step 0: Python/Linux repository foundation.
- Step 1: immutable chess domain value objects:
  - PositionKey
  - MoveKey
  - DecisionKey
  - GameResult
  - Outcome
  - DecisionObservation
- Position identity uses python-chess EPD with legal en-passant normalization.
- Step 2: aggregation layer:
  - `OutcomeCounts`, `DecisionStats`, `PositionStats`
  - one generic `PositionIndex` with a game-oriented `add_game` API
  - occurrence_count vs distinct_game_count
  - distinct-game-based outcome counting
  - occurrence-based choice rate
  - atomic outcome-consistency validation (all checks pass before any
    mutation; decision conflict reported before position conflict)
  - no permanent storage of game IDs
- Step 3A: local PGN parsing and personal decision extraction:
  - immutable, slotted, source-neutral `GameRecord`
  - streaming local PGN parsing, one game at a time
    (`iter_pgn_records(handle, source_id)`)
  - local game identity is `source_id` + zero-based ordinal
  - main-line-only move storage (comments, annotations, side variations
    ignored)
  - standard chess and legal custom initial FEN both supported and replayable
  - strict rejection of malformed, unfinished (`*`), unsupported-variant and
    Chess960 games
  - factual optional-metadata preservation / normalization (meaningful values
    kept as trimmed source text, ratings as int; blank / conventional unknown
    markers normalized to None; otherwise not interpreted or classified)
  - `extract_player_observations(game, player_name)` for a single player's
    decisions
  - case-insensitive player matching (`casefold()`); absent or ambiguous
    player rejected
  - position captured before the move is pushed
  - zero-based `ply_index` half-move index
  - actor-relative `Outcome` normalization via `GameResult.to_outcome`
  - complete replay validation before any observations are returned (raise or
    full list, never partial)
  - extracted observations integrate directly with `PositionIndex.add_game`
- Step 3B: real-data smoke test completed successfully:
  - observed raw personal dataset: 5128 games across 60 completed monthly
    PGN files
  - raw personal data remains outside Git
  - real-data edge case: 4 games have zero personal decisions overall, and
    only 1 of those 4 falls inside the current Rapid CORE cohort
- Step 3C: personal analysis cohorts in `personal.py` (complete and
  approved):
  - `TimeControlCategory` (RAPID / BLITZ / BULLET / UNKNOWN) classified via
    the Chess.com estimated-duration model
    (`base_seconds + 40 * increment_seconds`); non-`int`/`int+int` values
    (including correspondence/daily) are `UNKNOWN`, never guessed or raised on
  - `PersonalGameContext` + `resolve_personal_game_context` — actor's own
    colour, that side's own rating, and the game's category; case-insensitive
    matching, absent/ambiguous player rejected
  - `PersonalAnalysisPolicy` — immutable (frozen + slotted) v1 policy,
    thresholds Rapid >= 800 / Blitz >= 501 / Bullet >= 600, each validated at
    construction as a non-negative `int` (no coercion; `bool` rejected)
  - `GameDisposition` (CORE / LEGACY / MISSING_RATING / UNKNOWN_TIME_CONTROL);
    zero-decision is a downstream status, not a disposition
  - separate CORE `PositionIndex` per time-control category (Rapid / Blitz /
    Bullet); no combined all-core index; `PositionIndex` stays rating- and
    time-control-agnostic
  - `CohortStats` per category and `DatasetTotals` derived from the per-cohort
    stats (never stored twice)
  - zero-decision accounting: a CORE game with no personal decisions is
    counted as `zero_decision` and is not indexed
  - `build_personal_analysis(games, player_name, policy)` — source-independent;
    CORE accounting is per-game transactional: extraction and, when there are
    observations, `PositionIndex.add_game` run before any `CohortStats`
    counter is committed, so a CORE game that fails downstream contributes
    nothing
  - raw data is never deleted by cohort filtering
  - `domain.py`, `aggregation.py`, `ingestion.py` unchanged
- The complete test suite has 138 passing tests
  (75 prior + 63 for Step 3C), plus one known unrelated python-chess
  `chess.engine` deprecation warning.

## Core Product Direction

The system will eventually rank the user's most damaging recurring chess
decisions.

A decision is:
`(position before move, move chosen)`

The long-term analysis keeps three evidence sources conceptually separate:

- Stockfish / engine evidence
- reference-population evidence
- personal historical evidence

Do not invent a weighted combination yet.

The long-term product may evolve into a personalized opening/repertoire
trainer with:

- opening classification
- White/Black opening statistics
- recurring weakness maps
- board-based training

Opening identity must remain separate from PositionKey.

## Current Design Boundary

Aggregation is implemented and approved (see the Aggregation section of
`docs/architecture.md`).

Local PGN parsing and personal decision extraction are implemented and
approved (see the PGN parsing section of `docs/architecture.md`).

Personal cohort analysis is implemented and approved (see the Personal
cohort analysis section of `docs/architecture.md`): time-control
classification, validated `PersonalAnalysisPolicy` thresholds,
CORE/LEGACY/missing/unknown disposition, per-game transactional CORE
accounting, and separate per-cohort `PositionIndex` instances for
Rapid / Blitz / Bullet.

Not implemented:

- Chess.com network/API ingestion
- reference-corpus ingestion
- rating bands inside CORE
- legacy / combined PositionIndexes

Remaining future work: Stockfish integration, regret calculation, Top-K
weakness ranking, opening classification, visual/training UI.

Preventing cross-source / cross-call duplicate ingestion of the same game is
the responsibility of the future ingestion/corpus layer, not `PositionIndex`
and not the local PGN parser.

Future architectural rule (not implemented now): a source ordinal represents
a logical PGN record's position in its source. If future tolerant corpus
ingestion skips malformed records, each skipped record must still consume its
source ordinal so that game identity stays reproducible.

## Next Step

Step 3C is complete and approved. Step 4 has not started.

The next step is architectural planning only — to be designed with Dvir and
the project reviewer before any implementation. No Step 4 architecture has
been decided yet. Stockfish, reference-corpus ingestion, regret / Top-K
ranking, opening classification, persistence, and UI all remain out of scope
until explicitly planned and approved.
