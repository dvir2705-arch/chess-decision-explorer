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

Halfmove clock, fullmove number, and prior repetition history are
deliberately excluded from this identity because its role is
*recurring-position identity*: including them would cause the same recurring
position to be treated as many different ones. This exclusion is a scoping
choice, not a claim that those dimensions are irrelevant. Halfmove clock,
fullmove number, and repetition history can affect the *exact game-state
evaluation* of a single occurrence through draw-rule context (the
50-/75-move rule and threefold/fivefold repetition). Step 4A intentionally
evaluates a canonical recurring position (`CANONICAL_POSITION_V1`) rather
than reproducing the exact historical draw context of each occurrence; a
future exact-historical evaluation mode may handle that separately, without
changing `PositionKey`.

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

## Engine evaluation (Step 4A)

Stockfish is an external UCI dependency: the executable is never bundled
into the repository, and production code always receives its path through
configuration/construction (`StockfishEvaluator(executable_path, config)`),
never a hard-coded location. The process is not started until an explicit
`start()` call (or entering the evaluator as a context manager) -- never at
import time.

At startup the caller-supplied executable reference is resolved once
(`resolve_executable()`) to a single concrete filesystem path: an absolute
path or a relative path with a separator is taken as a filesystem location
and made absolute; a bare command name is looked up on `PATH` via
`shutil.which`; anything that does not resolve to an existing file fails
loudly with `EngineStartupError`. That one resolved path is used for *both*
hashing the executable and launching it, so the hashed bytes and the
launched process can never disagree. The resolved path itself is an
operational detail and is still excluded from semantic cache identity; the
executable SHA-256 remains the content identity.

### Canonical position semantics (`CANONICAL_POSITION_V1`)

Step 4A evaluates **canonical recurring decision positions**, not exact
historical occurrences. `POSITION_SEMANTICS_VERSION = "CANONICAL_POSITION_V1"`
names this contract:

- standard chess; the canonical four-field `PositionKey` (piece placement,
  side to move, valid castling rights, legally relevant en-passant);
- `halfmove_clock` reset to 0 and `fullmove_number` reset to 1;
- an empty pre-root move history;
- evaluation normalized to the root side to move.

It deliberately does **not** reproduce the original halfmove clock, the
original fullmove number, prior repetition history, or the exact historical
50-/75-move or threefold/fivefold state of any single occurrence. The
product question is *"how should this recurring canonical position be
played?"*, not *"what was the exact draw-rule context of one historical
occurrence?"*. A future **exact-historical evaluation mode** would be a
separate mode with its own identifier; the replayable Step 3 ingestion
model already preserves enough source information to add it later.

### Centralized request validation

`validate_request()` is the one authoritative validation path, run before
every cache lookup and every engine analysis (from `EvaluationRequest`
construction and again inside `StockfishEvaluator.evaluate()`), so the two
agree exactly. A valid Step 4A request requires:

- **Representation** -- a bare four-field EPD with no operation suffixes
  (`hmvc`/`fmvn` rejected); reconstructing the board and re-deriving the key
  must round-trip to the supplied `PositionKey`.
- **Board validity** -- the board must pass python-chess validity checks;
  structurally invalid boards are rejected before any cache or engine use.
- **Search mode** -- only `UNRESTRICTED` or `FORCED_MOVE`; an unknown mode
  is rejected, never treated as unrestricted.
- **Terminal canonical positions are not decision points** -- checkmate,
  stalemate, and python-chess-recognized insufficient material are
  rejected. History-dependent draw claims (50-/75-move, threefold/fivefold)
  are *not* checked: that history is intentionally excluded from
  `CANONICAL_POSITION_V1`.
- **Forced move** -- `FORCED_MOVE` requires a legal root move; `UNRESTRICTED`
  forbids one.

An invalid request raises `RequestValidationError` (a `ValueError`) before
any cache lookup.

### Engine identity

`EngineIdentity` carries:

- `name` -- the UCI `id name`, kept for provenance;
- `executable_sha256` -- SHA-256 of the actual Stockfish executable bytes,
  hashed with chunked/streaming reads (the ~100 MB binary is never loaded
  whole). This is the authoritative content identity for the current
  official Stockfish binary / default-network configuration and provides
  accidental-staleness protection.
- `eval_file` -- the engine's reported default `EvalFile`, kept only as
  network provenance.

The executable **path** is never part of identity. Step 4A does not support
caller-selected external NNUE files; the evaluator always uses Stockfish's
default network. **Future boundary:** if a later version allows an
externally supplied NNUE network, the content digest of that network MUST
become part of engine identity before persistent caching is allowed for
that configuration.

### Fixed analysis profile

`ANALYSIS_PROFILE` is the single authoritative definition of Step 4A's
fixed, analysis-affecting engine policy: single-PV analysis, Skill Level 20,
`UCI_LimitStrength=false`, `UCI_ShowWDL=true`, standard chess, no Syzygy
tablebases, node-limited search, fresh independent search state, no
pondering. `Threads` and `Hash` are separate `EngineAnalysisConfig`
dimensions and are not duplicated in the profile.

`ANALYSIS_PROFILE_FINGERPRINT` is a deterministic SHA-256 over the canonical
(sorted-key, no-whitespace) JSON serialization of the profile -- never
`hash()`, object identity, `pickle`, or unordered `repr`. It changes
whenever a fixed analysis-affecting policy changes.
`StockfishEvaluator.start()` configures the engine from this same profile
definition (via `_profile_uci_configuration`). **`MultiPV` is never passed
to `SimpleEngine.configure()`** -- python-chess manages MultiPV itself and
rejects the managed option; Step 4A uses the ordinary single-PV analyse
path, and the profile records `single_pv: true` as the semantic fact.

### Engine-evidence contract (`ENGINE_EVIDENCE_V1`)

`ENGINE_EVIDENCE_VERSION = "ENGINE_EVIDENCE_V1"` identifies how raw
Stockfish output is accepted and interpreted. It participates in persistent
cache identity and is a **separate concept from the SQLite storage schema
version**. A response is accepted only if it is complete and
comparison-ready:

- root-side-to-move POV normalization (via `PovScore.pov()` /
  `PovWdl.pov()`, never raw `.relative`);
- an exact score is present -- `lowerbound` / `upperbound` results are
  rejected. A hard node budget can stop Stockfish mid-aspiration, leaving
  the final emitted line bounded. `_analyse_exact()` streams the analysis
  and examines each individual analysis report as it arrives: it rejects
  any bounded report as comparison-ready evidence and retains the **last**
  suitable unbounded scored report that carries the required
  evidence/PV. It does **not** explicitly select the report with maximum
  depth; when the search terminates during a later bounded aspiration
  re-search, the retained report may therefore be an earlier (shallower)
  one. The evaluator never repairs a bound line, and fails loudly if the
  engine produced no suitable unbounded scored report;
- engine-reported WDL is present, non-negative, and sums to exactly 1000
  (Stockfish scale);
- `depth` and `nodes` are present;
- the principal variation is non-empty (every Step 4A position is a
  validated nonterminal decision position), every PV move is legal when
  replayed sequentially from the canonical root, and a `FORCED_MOVE` PV
  begins with the forced root move;
- `mate=0` at the root is inconsistent with a validated nonterminal
  position and is rejected.

Malformed engine evidence is never silently repaired.

- **CP vs mate.** Exactly one of `centipawn` / `mate` is set; a mate is
  never encoded as a large centipawn number, and integer `mate=0` is
  represented faithfully (never confused with "unset"). Both CP/mate and
  WDL are kept.

- **`nodes` semantics.** `EngineEvaluation.nodes` is the engine node
  counter as reported *on the retained comparison-ready report* (see
  `_analyse_exact()` above). It is **not** guaranteed to equal the total
  number of nodes Stockfish consumed before the overall `analysis()` call
  terminated: when a later bounded aspiration re-search runs after the
  retained report and is then cut off by the node budget, the retained
  report's counter is lower than the true total. Future Step 4B must not
  interpret `EngineEvaluation.nodes` (or the L2 `actual_nodes` column that
  stores it) as total computational expenditure.

- **Engine-model expected score.** `EngineEvaluation.expected_score` is
  `(wins + 0.5 * draws) / 1000` from the root side to move's POV. This is
  the **engine-model expected score**. It is *not* the user's personal win
  probability, a human win probability at a given rating, or a
  time-control-adjusted probability.

- **Controlled fresh search state.** Each independent analysis passes a
  fresh sentinel as python-chess's `game` marker, so python-chess sends
  `ucinewgame` through the supported UCI lifecycle before the search; one
  evaluation never inherits another's transposition-table state without
  restarting the process. `Threads` defaults to 1. No claim is made of
  bit-for-bit reproducibility across machines or builds. A cache hit never
  invokes `analyse()`.

### Semantic cache key

Both L1 and L2 key on the same `SemanticCacheKey`, so optional identity
fields (absent root move, absent eval file) normalize to the same sentinel
(`""`) in both layers -- `None` and `""` never diverge. The key spans:

`position_semantics_version`, canonical `PositionKey` (EPD), `SearchMode`,
root move (or `""` sentinel for unrestricted), engine executable SHA-256,
engine UCI name, reported default `EvalFile` (or `""`), requested `nodes`,
`threads`, `hash_mb`, `analysis_profile_fingerprint`, and
`evidence_contract_version`.

The executable path, and any rating / time-control / source / opening /
user-identity dimension, are deliberately excluded: Stockfish's evaluation
of a position does not depend on where the position came from. The engine
layer (`engine.py`, `engine_cache.py`) does not import from `personal.py`.

### L1 memory + L2 SQLite

- `InMemoryEvaluationCache` -- plain dict keyed on `SemanticCacheKey`, no
  persistence, no global singleton.
- `SQLiteEvaluationCache` -- standard-library `sqlite3`, caller-provided
  path. Every primary-key column is `NOT NULL`; low-complexity `CHECK`
  constraints cover search-mode/root-move consistency, centipawn/mate
  exclusivity, non-negative WDL, WDL total = 1000, and non-negative
  depth/nodes. Python reconstruction re-validates every row regardless of
  the SQL constraints.
- `TieredEvaluationCache` -- L1-then-L2 get with L1 repopulation on an L2
  hit; **L2-then-L1 put**, so if the persistent write raises (a conflict,
  or corrupt existing data) L1 is never populated with a value L2 does not
  hold.
- `CachedEvaluator` -- builds (and therefore validates) the
  `EvaluationRequest` before consulting the cache, checks the cache, and
  invokes Stockfish only on a miss; a failed evaluation is never cached.

### Checked SQLite schema versioning

`SQLITE_SCHEMA_VERSION = 2`, stored in `PRAGMA user_version`. The cache is
disposable computed data, so there are **no migrations**:

- fresh database (`user_version == 0`, no user tables) -> create the
  current schema, set the current version;
- database already at the supported version -> open normally;
- database at any other non-zero version -> fail with
  `CacheSchemaError` explaining that this is disposable computed cache data
  and should be deleted and rebuilt;
- `user_version == 0` but user tables already present -> fail with
  `CacheSchemaError` rather than initializing over unknown data.

The database is never automatically deleted, and incompatible data is never
silently relabelled.

### Immutable persistent entries; loud corruption

Completed engine evidence for one semantic key is immutable derived data.
`SQLiteEvaluationCache.put()` uses a plain `INSERT`:

- key absent -> insert;
- key present with the **same** evaluation -> idempotent no-op;
- key present with a **different** evaluation -> `CacheIntegrityError`; the
  existing row is never overwritten.

On read, `pv_json` must decode to a JSON **list of UCI strings**; the row is
reconstructed into an `EngineEvaluation` and re-validated against the
`EvaluationRequest`. Malformed JSON, the wrong JSON shape, invalid UCI
moves, an invalid CP/mate or WDL state, or any request/evidence mismatch
raises `CacheIntegrityError` -- persistent corruption fails loudly and is
never turned into a silent cache miss or a repaired value.

### Not part of Step 4A

- **Step 4A does not define EngineRegret or a mistake/damage threshold.**
  That, along with MultiPV-based move recommendations and any acceptable-move
  classification, remains future work. `UNRESTRICTED` records the engine's
  selected root candidate as evidence; it is not labelled "the only correct
  move".
- **Opening/variation classification** is history/context metadata about how
  a position was reached, and must remain separate from `PositionKey`,
  because transpositions can reach the same position through different
  openings.
- Invalid custom-PGN initial-position validation is noted as possible
  future ingestion hardening; Step 4A does not reopen Step 3 architecture.

## Out of scope for now

The following are known future directions but are explicitly not part of
the current foundation and have no design yet:

- EngineRegret / mistake-threshold analysis
- Reference-corpus (Lichess) ingestion
- Opening classification
- Parallel processing
- A persistent database beyond the disposable engine evaluation cache
- A web-based visual interface
