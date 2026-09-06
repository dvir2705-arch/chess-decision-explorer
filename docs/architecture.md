# Architecture Decisions

This document records the architectural decisions made for Chess Decision
Explorer so far. It describes decisions only — not implementation details
that have not been decided yet.

## 1. The position is the universal unit

A chess position is a concept independent of any dataset it happens to be
observed in. The same position may appear in the player's personal game
history and in the reference corpus; it must be represented and identified
the same way regardless of where it came from.

## 2. Personal data and reference data are logically separate

Statistics about the player's own games and statistics about the reference
population must be kept separate from each other. The player's history is
compared *against* the reference corpus; the two are not merged into a
single pool of statistics.

## 3. Analytical position identity

For the purpose of indexing and comparing positions, a position's identity
is defined by:

- piece placement
- side to move
- castling rights
- relevant en-passant state

Halfmove clock and fullmove number are explicitly excluded from this
identity, since they do not affect the strategic content of a position but
would otherwise cause identical positions to be treated as different.

## 4. The analysis core is independent of interfaces

The core analysis logic must not depend on the CLI or on any future visual
interface. Interfaces are consumers of the core, not part of it.

## 5. Technology choices

The project is implemented in Python, using the `python-chess` library for
chess rules, board representation, and PGN handling.

## 6. Reference data will be processed as streams

Reference datasets are expected to be large. They will eventually be
processed as streams rather than loaded entirely into memory. The streaming
approach itself has not been designed yet.

## Domain model

The core domain is a small chain of immutable value objects:

- **`PositionKey`** — the analytical identity of a position *before* a move
  is made (see decision 3 above), derived from a `chess.Board`.
- **`MoveKey`** — the move played, in canonical UCI notation.
- **`DecisionKey`** — the pair `(PositionKey, MoveKey)`: the decision faced
  and the choice made from it.
- **`DecisionObservation`** — one actual occurrence of a `DecisionKey` in one
  game, identified by `game_id` and `ply_index`, together with its
  `Outcome`.

A separate distinction is kept between the raw result of a game and what
that result meant for the player who made a given decision:

- **`GameResult`** — the raw, game-level result (white win, black win,
  draw), independent of any particular player.
- **`Outcome`** — the same result reinterpreted from the perspective of the
  player who made the decision (win, draw, loss), with a `points` property
  (1.0 / 0.5 / 0.0). `GameResult` exposes a conversion to `Outcome` given
  the acting player's color.

This keeps "what happened in the game" and "what it meant for this player's
decision" as distinct concepts, so the same `GameResult` can yield different
`Outcome`s depending on which side's decision is being evaluated.

## Out of scope for now

The following are known future directions but are explicitly not part of
the current foundation and have no design yet:

- Stockfish evaluation
- Parallel processing
- Databases
- A web-based visual interface
