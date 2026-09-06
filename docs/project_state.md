# Project State

## Current Phase

Step 2 complete — aggregation layer.

Step 1 complete — chess domain value objects.

## Last Approved Code Commit

`ffb47c8` — `feat: add position aggregation layer`

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
- The complete test suite currently has 41 passing tests.

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

Still not designed or implemented:

- Chess.com / PGN ingestion
- reference-corpus ingestion
- cohort filtering
- Stockfish integration
- regret calculation
- Top-K weakness ranking
- opening classification
- visual/training UI

Preventing cross-call duplicate ingestion of the same game is the
responsibility of the future ingestion/corpus layer, not `PositionIndex`.

## Next Step

Step 3 has not yet been designed. Architecture discussion with Dvir and the
project reviewer is required before implementation.
