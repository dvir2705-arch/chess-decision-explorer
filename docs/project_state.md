# Project State

## Current Phase

Step 1 complete — chess domain value objects.

## Last Approved Code Commit

`d6c8cbc` — `feat: add chess domain value objects`

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
- 25 tests currently pass.

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

Aggregation has NOT been designed or implemented yet.

No current implementation exists for:

- PositionStats
- DecisionStats
- occurrence aggregation
- distinct-game counting
- personal choice rate
- reference statistics
- regret
- Top-K ranking

## Next Step

Step 2 will first DESIGN aggregation semantics before implementation.

Questions to resolve include:

- occurrence_count vs distinct_game_count
- repeated positions within one game
- position-level vs decision-level aggregation
- what data must be stored vs derived
- preparation for later reference cohorts and Stockfish without prematurely
  adding them

## Working State

Expected Git working tree before Step 2: clean.
