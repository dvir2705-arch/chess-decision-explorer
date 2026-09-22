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

Reference datasets are expected to be large. They are processed as streams
rather than loaded entirely into memory. Step 5C-lite implements this for
human reference evidence (see *Human reference comparison* below); the same
principle applies to any future reference source.

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

## Engine damage assessment (Step 4B)

Step 4B (`assessment.py`) turns Step 4A engine evidence into a bounded,
auditable judgement about one canonical decision. It adds no second engine
abstraction and no second cache: it composes the existing
`EvaluationRequest` / `EngineEvaluation` value objects, `validate_request()`,
and the existing L1/L2 evaluation cache.

### The product rule: damaging-move detector, not best-move detector

A move is never damaging merely because Stockfish prefers a different move.
Several moves in one position may all be perfectly acceptable. The only
supported positive conclusion is:

> there exists a credible alternative A, A is materially better than the
> observed move, that advantage survives the required higher-budget
> confirmation, and no relevant contradiction remains.

This is enforced structurally, not by convention:

- `UNRESTRICTED` search is used **only to discover** a candidate. Its score
  can never reach the arithmetic, because a signed gap exists only on
  `ComparisonRound`, and a round refuses any evidence that is not a
  `FORCED_MOVE` evaluation (`MoveEvidence` rejects a non-forced request).
- Discovery selecting the observed move ends the assessment as
  `NO_DAMAGE_DEMONSTRATED`, explicitly *not* as proof of optimality.
- Precision is prioritized over recall throughout: candidate discovery is
  top-1 only, so missing another strong move costs recall, never precision.

### Expected-score arithmetic

Comparisons are integer-only. For one evaluation,
`U = 2*W + D` over the Stockfish WDL triple (`0 <= U <= 2000`); the display
form is `U / 2000`. For an alternative A and the observed move,
`SignedGapUnits = U(A) - U(observed)`. Signs are preserved exactly: never
clamped, never absolute-valued, never floored at zero. A positive number is a
measurement, not a verdict.

Four things are kept deliberately distinct and are never collapsed into one
float:

1. the positive measured gap (`ComparisonRound.signed_gap_units`),
2. a meaningful measured gap (compared against `tau_units`),
3. a search-robust pairwise advantage (the same witness across two levels,
   passing the drift/consistency gate),
4. the `DAMAGE_SUPPORTED` decision.

### `ComparisonPolicy`

The comparison procedure is one immutable, validated object: policy identity
and version, the B1 / B2 / optional B3 `SearchLevel`s (strictly increasing
node budgets), `epsilon_units`, `tau_units`, three separate drift limits
(gap, alternative component, observed-move component),
`material_negative_gap_units`, `max_active_alternatives`, an optional
`max_requests_per_decision` work bound, and a `CalibrationStatus` with its
`calibration_id`.

**No production thresholds exist anywhere in the module.** Every threshold is
a required constructor argument, so no caller can inherit an invented
epsilon, tau, drift limit, or budget, and there is no module-level default
policy (a test asserts this). A policy whose status is `UNCALIBRATED` runs
the full measurement — which is exactly what a calibration experiment needs —
but can never emit `DAMAGE_SUPPORTED`: the positive label is withheld and the
result is `INCONCLUSIVE` with reason `UNCALIBRATED_POLICY`.

Policy identity is deliberately **outside** primitive engine cache identity.
`SemanticCacheKey` gains no policy dimension, so changing `tau_units` or
`epsilon_units` re-derives every decision from identical cached Stockfish
evidence without a single new engine request.

### Search levels and the evidence provider

Step 4A binds one `EngineAnalysisConfig` — including `nodes` — per
`StockfishEvaluator` for its lifetime, and that interface is unchanged. Step
4B therefore introduces a two-method seam, `LeveledEvidenceProvider`:
`request_for(level, position, mode, root_move)` builds the validated Step 4A
request (so budget, engine identity and forced root move are recorded as
provenance and can be checked for compatibility), and `evaluate(request)`
returns the evidence answering exactly that request.

`CachedEvaluatorPool` is the concrete implementation: one started evaluator
per level, all sharing one Step 4A cache and required to report the same
`EngineIdentity`. Two levels may not share an identical analysis config,
which would make them indistinguishable to the cache.

### The bounded procedure

**B1 — initial screen.** Building the Step 4A request validates the root and
the observed move (terminal root, non-canonical key, or illegal move raise
`RequestValidationError` before any engine work). A single legal move ends as
`NO_DAMAGE_DEMONSTRATED` / `ONLY_LEGAL_MOVE` with zero engine calls. An
observed move that delivers immediate checkmate ends the same way
(`U = 2000` is the exact rule-derived maximum, so nothing can beat it), also
with zero engine calls. Otherwise B1 discovers a candidate, force-evaluates
the candidate and the observed move at the same budget, and computes the
signed gap. Because `S(A) = min(G_prev, G_cur) <= G_1`, a B1 gap that cannot
reach tau (`G_1 - epsilon <= tau`) stops the procedure cheaply as
`NO_DAMAGE_DEMONSTRATED` — a screened, non-actionable difference, not a claim
of optimality or equivalence.

**B2 — mandatory confirmation.** A B1 positive can never become
`DAMAGE_SUPPORTED` directly; every potentially actionable positive is
re-measured at the higher budget. B2 rediscovers top-1, keeps at most two
active distinct alternatives, force-evaluates the observed move and every
active alternative at B2, and **backfills** any newly discovered candidate's
B1 forced evaluation before it may act as a two-level witness.

**Fixed witness.** Qualification is per candidate, never over a moving
maximum. `_compare_levels` refuses two rounds whose alternatives differ, and
refuses the same level twice, so "A1 led at B1, A2 leads at B2, therefore
*an* alternative was consistently better" cannot be assembled. For one
candidate A across two prescribed levels:
`S(A) = min(G_prev(A), G_cur(A))` and `L(A) = S(A) - epsilon_units`; damage
requires `L(A) > tau_units` **and** a clean consistency gate. `L` is a
conservative empirical margin, not a mathematical lower confidence bound on
true chess value. The final measured gap, the conservative margin `L`, and
the accepted regret `S` are stored as three separate values.

**Drift / consistency.** Sign agreement alone is not checked; a stable gap
can hide large movement in both components. The gate checks the change in the
pairwise gap, in the alternative's evaluation, and in the observed move's
evaluation, each against its own policy limit, plus a mate-direction
reversal. A mate *appearing* at a higher budget is normal and is not a
contradiction; a mate *direction reversal* (for the root side ↔ against it)
is. Instability only counts as unresolved when some level actually alleges
material damage — an unstable measurement that never approaches tau still
ends conservatively as `NO_DAMAGE_DEMONSTRATED`.

**B3 — one bounded final escalation.** Triggers are: sign reversal, gap
drift, either component drift, mate-direction reversal, a material negative
benchmark discrepancy (the level's own discovered top-1 scoring materially
worse than the observed move under equal forced budgets), and higher-budget
discovery selecting the observed move while forced evidence alleges material
damage. At B3 previous evidence is preserved, the bounded alternative set is
maintained (the weakest known candidate is evicted, so a newly discovered
move never displaces an already stable stronger witness), required backfills
are performed, and qualification uses the **last two prescribed levels**
(B2, B3) — never the two most favourable earlier ones. After B3 the procedure
stops unconditionally; engine work is never increased until a positive result
appears. Unresolved evidence ends as `INCONCLUSIVE`.

An optional `max_requests_per_decision` bounds total distinct engine requests
per decision; hitting it yields `INCONCLUSIVE` / `WORK_BUDGET_EXHAUSTED`
rather than a guess. Repeating an identical request inside one assessment is
memoized and, below that, served by the Step 4A cache: such reuse is evidence
reuse, never an additional independent confirmation. A B1 and a B2 evaluation
of the same move are different requests with different semantic cache keys,
so a cache hit at one level can never satisfy the other.

### Final states

`DAMAGE_SUPPORTED`, `NO_DAMAGE_DEMONSTRATED`, `INCONCLUSIVE`,
`INVALID_EVIDENCE`. `RECHECK_REQUIRED` and `VALID_COMPARISON` deliberately do
not exist: neither is a final verdict about move quality. The exact,
quotable wording of each state lives in `STATUS_SEMANTICS` so downstream
reporting does not paraphrase and overclaim. In particular
`NO_DAMAGE_DEMONSTRATED` never asserts optimality and never asserts that the
compared moves are proven equivalent.

Step 4A's fail-loud style is preserved for malformed *questions*: an invalid
root or observed move raises `RequestValidationError`. Malformed or
incompatible *evidence* raises `AssessmentEvidenceError` at the value-object
boundary (so a bad `MoveEvidence` or `ComparisonRound` can never exist) and
the orchestrator reports it as `INVALID_EVIDENCE` — never as a harmless zero
gap.

### `EngineRegret`

An `EngineRegret` exists **only** for `DAMAGE_SUPPORTED`, enforced in
`MoveAssessment.__post_init__` (regret, witness, and conservative margin are
present if and only if the status is `DAMAGE_SUPPORTED`). It is never
`max(0, gap)` and never derived from a negative gap. Its `units` value is the
conservative accepted loss `S(A)`, which is strictly positive whenever the
contract passed, and it carries the policy id/version/fingerprint and the two
qualification levels as provenance.

Negative gaps are preserved as measurements. A small negative near-tie stops
as `NO_DAMAGE_DEMONSTRATED` — it does not mean the observed move is globally
best, that the evidence is corrupt, or that regret should become zero. A
material negative discrepancy escalates, and a persistent unresolved one ends
as `INCONCLUSIVE`.

### WDL / CP / mate / PV roles

WDL expected score is the single damage metric. CP is a supporting,
explanatory diagnostic (`ComparisonRound.centipawn_gap`) and never enters a
threshold test. Mate is categorical evidence with its distance preserved
(`_mate_direction`); it is never converted into a large centipawn number.
There is no compound "WDL regret + CP bonus + mate penalty" formula: WDL and
CP are not independent confirmation signals. Depth and nodes remain
provenance.

### Terminal semantics

`immediate_terminal_after()` applies the root move locally and reports the
exact rule-derived state (checkmate / stalemate / insufficient material). It
is used for the observed-immediate-checkmate shortcut and as an integrity
check: evidence for a mating move must report `mate == 1`, and evidence for
an exact draw must report no mate; a contradiction is
`TERMINAL_EVIDENCE_CONTRADICTION` / `INVALID_EVIDENCE`. Rule-derived facts
never replace or fabricate engine evidence, and engine provenance is always
preserved. Mate-distance differences alone create no regret: two winning
moves with different mate distances both measure 2000 units (gap 0), and two
losing moves at different distances receive no "losing sooner" penalty.

### Historical semantic limit

Step 4B produces **canonical-position engine-damage evidence**, quotable via
`CANONICAL_DAMAGE_SCOPE_NOTE`. Because `PositionKey` deliberately excludes
the halfmove clock and prior repetition history, an assessment does not claim
that every historical occurrence of the decision lost exactly the same
amount. `MoveAssessment` carries no game, occurrence, ply, rating, or
time-control dimension, and Step 4B does not touch occurrence storage. A
later occurrence-level eligibility check can handle draw-history-sensitive
claims.

### Escalation provenance

`MoveAssessment` records two disjoint trigger sets. `unresolved_triggers`
holds only the contradictions still unresolved at the final decision.
`resolved_triggers` holds escalation triggers an earlier prescribed pair
raised that the later prescribed pair no longer raises — computed as
`B1/B2 triggers - B2/B3 triggers`, so a B1/B2 instability that B3 settled
stays visible on the immutable result instead of silently disappearing. The
two halves can both be non-empty when B3 resolves part of the history and
introduces or retains the rest. `resolved_triggers` is provenance and
calibration information, never a confidence score, and it deliberately does
not block admission once the prescribed later pair genuinely passes.

### Step 4C admission contract

`MoveAssessment.is_engine_damage_admissible` (with `admission_failures`
listing every reason it is not) is the one gate Step 4C must consult. It
re-derives the qualification from the recorded trace rather than trusting
`status` or `unresolved_triggers`: the two stored qualification rounds are
fed back through **the same `_compare_levels` gate the orchestrator used**,
so a hand-built or malformed assessment whose rounds actually violate a
fixed-witness, sign, drift, mate-direction, or materiality check can never be
admitted, however its summary fields were filled in. It requires:

- final status `DAMAGE_SUPPORTED`, no unresolved triggers, and a policy whose
  calibration scope permits the conclusion;
- a witness distinct from the observed move, with candidate-discovery
  provenance;
- **accepted-regret provenance** — the regret must name the confirmed
  witness and carry this policy's `policy_id`, `policy_version`,
  `fingerprint`, and the assessment's own qualification levels;
- **the prescribed qualification pair**, not an arbitrary two-level tuple.
  Whether B3 was entered is read off the trace (a B3 discovery *or* a B3
  round); if it was, the pair must be `(B2, B3)`, otherwise `(B1, B2)`. A
  skipped pair such as `(B1, B3)`, and a B3-bearing trace claiming to qualify
  on favourable earlier B1/B2 evidence, are both rejected;
- per-round integrity — each round rooted at the observed position,
  evaluating the observed move, `FORCED_MOVE` on both sides, equal requested
  budgets, compatible engine identity;
- **cross-level compatibility** — the two rounds must agree on every
  dimension of the Step 4A `SemanticCacheKey` except the requested node
  budget (`_CROSS_LEVEL_INVARIANT_FIELDS` is derived from that key, so a
  future key dimension is covered automatically and no new cache-key scheme
  is introduced);
- the re-derived verdict must carry no triggers and must exceed tau, with
  `regret.units == min(G_prev, G_cur)`, `conservative_margin_units == S -
  epsilon`, and `final_gap_units` equal to the signed gap of the later
  qualification round.

Recurrence can never promote `NO_DAMAGE_DEMONSTRATED` or `INCONCLUSIVE` into
damage: fifty occurrences of one uncertain engine decision remain one
uncertain engine decision.

### Not calibrated

Step 4B implements the architecture calibration needs; it does not pretend
calibration has happened. No production epsilon, tau, drift limit, or
B1/B2/B3 budget has been chosen, and no false-positive rate, accepted-label
reliability, abstention rate, or computational cost has been measured. The
budgets and thresholds in `tests/test_assessment_stockfish.py` and
`scripts/stockfish_assessment_smoke.py` are demonstration fixtures labelled
as such, never production configuration.

## Human reference comparison (Step 5C-lite)

Human reference evidence answers a different question from engine evidence:
not "how good is this move" but "what did other humans play here, and how did
those games end". The two are separate architectures that share only
`PositionKey`. Nothing in the human-reference pipeline produces or consumes
`G_ref`, `R`, `epsilon`, a node budget, or any search-noise quantity, and it
never imports the engine, assessment, or Step 4C.0 modules.

### The target set bounds the work

The external corpus may hold tens of millions of decision positions. None of
them is stored. The player's recurring positions are selected first, and the
scan keeps statistics only for `PositionKey`s already in that set:

```
personal recurring positions -> small target PositionKey set
    -> stream external games
        -> for each decision:
             PositionKey not in target set -> discard
             PositionKey in target set     -> aggregate
```

Memory therefore scales with the personal target set and the moves matched
inside it, never with corpus size. The invariant is checkable: the reference
aggregate never holds more positions than the target set.

### Recurrence is a distinct-game property

A position is recurring when at least N **distinct personal games** contain
it (v1 default: 2). Repeats of the same position inside one game are not
sufficient recurrence by themselves. Occurrence counts and distinct-game
counts are both preserved everywhere and are never substituted for each
other.

### The reference aggregate is the same index, a separate instance

Reference statistics use `PositionIndex`, the same population-agnostic
aggregation as personal statistics, in its own instance. Personal and
reference statistics are never merged. This reuse also supplies the
per-game distinct counting and the guarantee that no permanent game-ID set
is retained: a reference game's id exists only inside the one `add_game`
call that consumes it.

### A human reference cohort excludes bot accounts

Lichess marks Bot API accounts with `WhiteTitle`/`BlackTitle` equal to `BOT`.
Those games are engine-driven and are not human evidence, so they are
rejected by default with their own reason. The rule matches the `BOT` title
exactly and case-insensitively; every other title (GM, IM, FM, NM, WGM, …)
marks a titled HUMAN and is accepted normally.

### Reference outcomes are actor-relative

`PositionKey` includes the side to move, so a matched reference position has
the same actor as the personal position. Every reference win/draw/loss is
recorded relative to that side. No White-centric outcome count exists
anywhere in the pipeline.

### One cohort per run

The three personal cohort indexes are never merged, so one run compares
exactly one cohort and records which one. Cohort, player identity, rating,
source, opening, and time control are recorded as metadata beside the keys
and are never folded into `PositionKey`.

### Header-first scanning

Eligibility splits into rules decidable from PGN header text and rules that
genuinely need the reconstructed game. Header-decidable rules — completed
result, standard variant, rated event, human players (no Lichess Bot API
account), time-control category, rating-header well-formedness, rating
presence and band, configured termination exclusion — are applied FIRST, and
a record failing one is dropped before its movetext is tokenised. Only
movetext validity, starting-position validity, ply count, and legal replay
remain on the post-parse path.

This matters because a narrow reference cohort accepts a small fraction of a
public corpus: the first real Lichess run accepted 1.76 % of records, so
parsing every record in full spent ~98 % of the work on records that were
then discarded.

`chess.pgn.read_headers` consumes a whole record and its documented usage
requires `seek()` afterwards, which a `curl | zstd | python` stream cannot
do. The scanner therefore tees the lines python-chess reads into a
single-record buffer and re-parses an accepted record from it. Record
boundaries stay python-chess's own in both phases, at most one record's raw
text is held, the pass over the source stays single and forward-only, and
file and stdin sources behave identically.

**Semantic boundary.** A record failing both a header rule and a movetext
rule is attributed to the header reason, because its movetext is never read.
The accepted population is unchanged — such a record was rejected either way
— but the recorded reason can move from a parse reason to a header reason.
The relative order of header-decidable rules is preserved exactly, including
the rating-header well-formedness check, which precedes the time-control
test and still reports as a parse rejection.

### Accepted eligible games, not records scanned

A run's target is a count of **eligible accepted** games. The number of
records read off the stream is a different quantity and is reported
separately. A run that exhausts its input before reaching its target records
`target_met: false` and a shortfall, and its report says so in its first
line. A target is never reported as an achievement.

### Comparison is derived, never stored back

Personal-vs-reference rows are derived from the two aggregates and mutate
neither, so a comparison can be rebuilt, or rebuilt differently, without
rescanning the corpus. A rate with a zero denominator is undefined (`None`),
never `0.0`.

### What this milestone does not decide

It defines no statistical-significance test, no effect size, no rating
matching, and no combination of human evidence with engine evidence. A
difference between a personal rate and a reference rate describes two
populations; it is not a claim that a move is good or bad.

## Out of scope for now

The following are known future directions but are explicitly not part of
the current foundation and have no design yet:

- Calibrated production thresholds for Step 4B (epsilon, tau, drift
  limits, B1/B2/B3 budgets) and any measured reliability claim
- Step 4C: recurring-weakness ranking / Top-K over accepted engine damage
- Rating-matched human reference cohorts, and any statistical-significance
  or effect-size claim over human reference evidence
- Combining human reference evidence with engine evidence into one score
- Opening classification
- Parallel processing
- A persistent database beyond the disposable engine evaluation cache
- A web-based visual interface
