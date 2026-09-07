# Project State

## Current Phase

Step 3A complete — local PGN parsing and personal decision extraction.

Step 2 complete — aggregation layer.

Step 1 complete — chess domain value objects.

## Last Approved Code Commit

`a6e0590` — `feat: add local PGN ingestion and decision extraction`

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
- The complete test suite currently has 75 passing tests.

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

Not implemented:

- Chess.com network/API ingestion
- reference-corpus ingestion
- cohort filtering

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

Step 3B has not yet been implemented. It will be designed with Dvir and the
project reviewer before any Chess.com network access or real-data download is
added.
