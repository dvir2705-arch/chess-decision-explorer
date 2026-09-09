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

## Aggregation

The aggregation layer folds already-normalized `DecisionObservation` objects
into factual occurrence/outcome statistics. It records only what happened; it
never decides whether a move is good or bad, and it computes no regret or
quality metric.

- **`occurrence_count` vs `distinct_game_count`.** `occurrence_count`
  increments for every observation, including genuine repeats within one
  game. `distinct_game_count` increments at most once per game for a given
  position, and at most once per game for a given decision.

- **Outcome counts are distinct-game based.** Each `PositionStats` and
  `DecisionStats` holds an `OutcomeCounts` (wins / draws / losses) recorded
  once per contributing game, so `wins + draws + losses == distinct_game_count`.
  `score_rate` is derived (`(wins + 0.5 * draws) / game_count`) and is `None`
  when no games were recorded.

- **Choice rate is occurrence based.** `PositionStats.choice_rate(move)` is
  `decision occurrence_count / position occurrence_count`, or `0.0` for a move
  never seen in that position. Choice rates over all of a position's moves sum
  to approximately 1.0.

- **Repeated positions may create multiple decision-level game counts in one
  position-level game.** If a game reaches position P twice and plays a
  different move each time, P gets `distinct_game_count += 1` but each of the
  two decisions also gets `distinct_game_count += 1`. The sum of a position's
  decision `distinct_game_count` values is therefore not required to equal the
  position `distinct_game_count`. The sum of decision `occurrence_count` values
  for a position always equals the position `occurrence_count`.

- **Game-ID sets are temporary.** `PositionIndex.add_game` uses per-call
  temporary sets to gate the once-per-game increments and discards them when
  the call returns. No sets of game IDs are stored in `PositionStats` or
  `DecisionStats`. `add_game` rejects an empty observation collection, rejects
  observations with differing `game_id`, and rejects a game where the same
  position or the same decision appears with conflicting `Outcome` values. The
  decision-level conflict check runs before the position-level one, so a
  repeated identical decision reports the decision conflict rather than the
  position conflict; all consistency checks complete before any state is
  mutated.

- **Cross-call game uniqueness is not the index's job.** `PositionIndex`
  keeps no record of previously submitted `game_id`s and assumes each logical
  `game_id` is passed to `add_game` at most once. Preventing duplicate
  ingestion of the same game across calls belongs to the future
  ingestion/corpus layer, not to this index.

- **One generic index.** There is a single `PositionIndex` implementation.
  Personal and reference populations use separate instances of it; there are
  no distinct personal/reference index classes.

## PGN parsing and personal decision extraction

Parsing a local PGN file and turning it into domain objects is a separate
concern from both the domain value objects and the aggregation layer.

- **`GameRecord`** — a source-neutral, immutable representation of one parsed
  standard-chess game: the two player names (spelling and case preserved),
  optional ratings, raw `GameResult`, optional `time_control` / `date` /
  `site`, the complete `initial_fen` the game replays from, and the PGN main
  line as a tuple of moves. Optional metadata is only lightly normalised:
  meaningful values are preserved as trimmed source text (ratings as `int`),
  while absent, blank, or conventional unknown markers (`?`, `-`,
  `????.??.??`) become `None` and a clearly malformed rating is rejected. It
  is otherwise not interpreted, classified, or filtered — ratings are not
  turned into cohorts, `time_control` is not labelled blitz/rapid. No
  opening/ECO fields.

- **PGN parsing is streaming.** `iter_pgn_records(handle, source_id)` reads
  one game at a time from the handle rather than loading the whole file.

- **`game_id` for local sources is `f"{source_id}:{ordinal}"`**, where the
  ordinal is zero-based and advances once per parsed game. Identity is not
  derived from player names or date. Preventing duplicate ingestion of the
  same game across sources or across calls remains a future
  ingestion/corpus responsibility, not the parser's.

- **Malformed or unfinished games are rejected, not partially indexed.** A
  game is rejected (`ValueError`) if python-chess reports parsing errors, if
  the result is not one of `1-0` / `0-1` / `1/2-1/2` (so `*` is rejected), if
  it uses an unsupported variant or Chess960, or if a rating header is
  clearly non-numeric. A legal custom starting position is preserved through
  `initial_fen`. Only the main line is consumed; comments, annotations and
  side variations are ignored.

- **Decision extraction is separate from parsing.**
  `extract_player_observations(game, player_name)` replays the game from
  `initial_fen` and emits `DecisionObservation` objects for only the
  requested player's turns. Player matching is case-insensitive
  (`casefold()`); a player who is neither side, or an ambiguous identity, is
  rejected. For each ply where the side to move is the requested player, the
  position is captured *before* the move is pushed, `ply_index` is the
  zero-based half-move index, and the observation's `Outcome` is the game's
  final result normalised to that player's colour. Every move is checked for
  legality during replay; if replay cannot complete, the call raises rather
  than returning partial observations. Aggregation is not performed here.

## Personal cohort analysis

Turning a stream of `GameRecord` objects into the player's own analysis is a
separate concern again, living in `personal.py`. It never changes the
semantics of `domain.py`, `aggregation.py`, or `ingestion.py`, and
`PositionIndex` stays completely unaware of rating and time control.

- **Time-control classification.** A raw PGN `TimeControl` value is mapped to
  a `TimeControlCategory` (`RAPID` / `BLITZ` / `BULLET` / `UNKNOWN`) using
  Chess.com's estimated-duration model:

  `estimated_seconds = base_seconds + 40 * increment_seconds`

  Boundaries: `< 180` → BULLET, `180 <= t < 600` → BLITZ, `t >= 600` → RAPID.
  Only plain `integer` or `integer+integer` strings are recognised; `None`,
  blank, and any other shape (correspondence/daily, `-`, ...) is `UNKNOWN`.
  Classification never guesses and never raises on merely unsupported values.

- **Personal rating means the actor's own rating in that game** — the White
  rating when the player was White, the Black rating when Black. It is not a
  peak rating or an external rating.

- **`PersonalGameContext`** bundles the resolved `player_color`, that side's
  `personal_rating` (or `None`), and the `time_control_category`. Player
  matching is case-insensitive (`casefold()`); a name matching neither side
  or both sides is rejected.

- **v1 eligibility policy (`PersonalAnalysisPolicy`).** Thresholds are
  configuration, not scattered constants, and may change in a later version:

  - Rapid minimum rating `>= 800`
  - Blitz minimum rating `>= 501`
  - Bullet minimum rating `>= 600`

  The product decision for Blitz is "> 500"; the implementation keeps the
  uniform `rating >= minimum` rule, so the configured Blitz minimum is 501.
  Each threshold is validated at construction as a non-negative `int`
  (`bool` and other non-`int` types are rejected with `ValueError`); values
  are validated, never coerced. The policy stays frozen and slotted.

- **`GameDisposition`** is the initial eligibility outcome: `CORE`, `LEGACY`,
  `MISSING_RATING`, `UNKNOWN_TIME_CONTROL`. Order of decision: unknown time
  control first, then missing rating, then rating vs. the category threshold
  (`>=` → CORE, else LEGACY). `zero_decision` is **not** a disposition — it is
  a downstream status a game can only reach *after* it has been classified
  CORE and then produced no personal decisions.

- **Separate indexes per category.** Rapid, Blitz, and Bullet each get their
  own `PositionIndex` and `CohortStats`; the indexes are never merged and
  there is no combined all-core index. A position is present in a cohort's
  index only if a game of that cohort contributed it. LEGACY, missing-rating,
  and unknown-time-control games never reach any `PositionIndex`.

- **Accounting invariants.** For a known category:

  `games_seen == core_eligible_games + legacy_games + missing_rating_games`
  `core_eligible_games == indexed_games + zero_decision_games`

  `personal_decisions` is the number of `DecisionObservation` objects actually
  inserted into that cohort's index; a zero-decision CORE game adds nothing to
  it and is not counted as indexed. UNKNOWN-time-control games are accumulated
  in a separate `CohortStats` with no index, where only `games_seen` and
  `unknown_time_control_games` advance (in lockstep) — the documented
  exception to the first invariant. Dataset-level totals are always derived
  from the per-cohort stats, never stored separately, so they cannot drift.

- **CORE processing is per-game transactional with respect to `CohortStats`.**
  For a CORE game every step that can raise — observation extraction, and
  `PositionIndex.add_game` when there are observations — runs before any
  counter is touched. A game that fails during extraction contributes
  nothing; a game that fails during indexing contributes nothing to
  `CohortStats` (and `PositionIndex` remains atomic in its own right); a
  zero-decision CORE game is accounted only after a successful extraction;
  the indexed counters are committed only after a successful
  `PositionIndex.add_game`. LEGACY, missing-rating, and unknown-time-control
  games do no downstream work and keep simple immediate accounting.

- **Raw data is never deleted because of cohort filtering.** Cohort
  classification only decides what feeds which index; the underlying
  `GameRecord` stream and any stored PGN files are untouched.

## Out of scope for now

The following are known future directions but are explicitly not part of
the current foundation and have no design yet:

- Stockfish evaluation
- Parallel processing
- Databases
- A web-based visual interface
