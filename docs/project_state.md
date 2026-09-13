# Project State

## Current Phase

Step 4B is IMPLEMENTED and AWAITING REVIEW — the canonical-position
damaging-move measurement and orchestration foundation (`assessment.py`).
It is **not** approved, **not** calibrated, and **not** cleared for
production damaging-move claims. No commit has been made for it.

Step 4A is COMPLETE and architecturally APPROVED — Stockfish engine
foundation and a persistent two-level evaluation cache (`engine.py`,
`engine_cache.py`). Two independent architecture audits reviewed it: the
first drove the audit-correction pass, and the second approved the revised
Step 4A for its documented scope with no remaining P0 blockers. This
checkpoint commit records that approved state.

Step 3C remains complete and approved at commit `447fdb1` — personal
analysis cohorts (`personal.py`): time-control classification, validated
immutable analysis policy, disposition classification, and separate Rapid /
Blitz / Bullet CORE position indexes with per-game transactional
accounting.

Step 3B complete — real-data smoke test passed successfully.

Step 3A complete — local PGN parsing and personal decision extraction.

Step 2 complete — aggregation layer.

Step 1 complete — chess domain value objects.

## Last Approved Code Commit

`feat: add stockfish evaluation foundation` — Step 4A, approved after two
independent architecture audits (this checkpoint commit; supersedes
`447fdb1 feat: add personal analysis cohorts`, Step 3C).

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
- Step 4A (COMPLETE, architecturally APPROVED): Stockfish engine foundation
  and a persistent two-level evaluation cache (`engine.py`,
  `engine_cache.py`).
  A first independent architecture/correctness audit found real
  engine-integration and cache-correctness issues (an invalid `MultiPV`
  passed to `configure()`; leniently accepted engine output; `PRAGMA
  user_version` rewritten on every open; `INSERT OR REPLACE` cache writes;
  no request-aware evidence validation); those corrections were applied. A
  second independent audit then approved the revised Step 4A for its
  documented scope with no remaining P0 blockers, and a final pre-commit
  polish pass corrected the decision-3 canonical-position wording,
  documented `_analyse_exact()` / `EngineEvaluation.nodes` precisely, and
  added one-time executable-path resolution (`resolve_executable()`) so the
  hashed bytes and the launched process cannot disagree.
  - `CANONICAL_POSITION_V1` — Step 4A evaluates canonical recurring decision
    positions: canonical four-field `PositionKey`, `halfmove_clock`/
    `fullmove_number` reset to 0/1, empty pre-root history, evaluation
    normalized to the root side to move. Exact-historical draw/repetition
    context is intentionally excluded and left to a future separate mode.
  - `validate_request()` — one authoritative validation path used before
    every cache lookup and every analysis (rejects EPD operation suffixes,
    non-round-tripping keys, structurally invalid boards, checkmate/
    stalemate/insufficient-material, unknown search modes, and illegal or
    missing/forbidden root moves); `EvaluationRequest` and
    `StockfishEvaluator` route through it and agree exactly
  - `EngineIdentity` — UCI `id name` (provenance) + **SHA-256 of the
    executable bytes** (chunked/streaming hash) + reported default
    `EvalFile` (provenance). Executable path is never identity; external
    NNUE networks are unsupported in Step 4A and would require network
    content identity before persistent caching
  - `ANALYSIS_PROFILE` + `ANALYSIS_PROFILE_FINGERPRINT` — one authoritative
    fixed profile (single-PV, Skill Level 20, `UCI_LimitStrength` off,
    `UCI_ShowWDL` on, standard chess, no Syzygy, node-limited, fresh search
    state, no ponder); deterministic SHA-256 over canonical JSON;
    `start()` configures from the same definition; **`MultiPV` is never
    sent to `configure()`** (python-chess manages it)
  - `ENGINE_EVIDENCE_V1` — request-aware evidence contract, separate from
    the SQLite schema version: exact (non-bound) score required, WDL
    required and summing to exactly 1000, `depth`/`nodes` required,
    non-empty PV that replays legally from the canonical root, forced-root
    PV consistency, `mate=0` at a nonterminal root rejected; malformed
    evidence is never silently repaired. A hard node budget can leave the
    final engine line bounded, so `_analyse_exact()` streams the analysis
    and retains the LAST unbounded scored report carrying the required
    evidence/PV (not explicitly the maximum-depth report); when the search
    ends during a later bounded aspiration re-search the retained report
    may be an earlier, shallower one. `EngineEvaluation.nodes` is that
    retained report's counter, NOT total node expenditure
  - `EngineEvaluation` — exactly one of centipawn/mate (integer `mate=0`
    represented faithfully); WDL non-negative, total exactly 1000; derived
    `expected_score` documented as the engine-model expected score (not a
    human / rating / time-control probability)
  - POV normalization to the root side to move via `PovScore.pov()` /
    `PovWdl.pov()`, with White- and Black-to-move tests
  - `StockfishEvaluator` — not started at import/construction;
    context-manager lifecycle; `UCI_ShowWDL` required at `start()`; a fresh
    `game` sentinel per independent search so python-chess sends
    `ucinewgame`; cache hits never call the engine
  - `SemanticCacheKey` — L1 and L2 key on the same tuple
    (position-semantics version, EPD, search mode, root-move sentinel,
    executable SHA-256, engine name, eval-file sentinel, nodes, threads,
    hash_mb, analysis-profile fingerprint, evidence-contract version);
    `None`/`""` optional fields normalize identically in both layers; no
    rating/time-control/cohort/source/opening/user dimension
  - `SQLiteEvaluationCache` — schema version 2 in `PRAGMA user_version`,
    checked on open (fresh init / supported reopen / incompatible version
    rejected / unversioned-non-empty rejected); no migrations; every
    primary-key column `NOT NULL` plus low-complexity `CHECK` constraints;
    Python re-validates every row; immutable writes (plain `INSERT`;
    same-value put is idempotent, different-value put raises
    `CacheIntegrityError`); corrupt persistent content (bad JSON, wrong
    shape, illegal PV, request/evidence mismatch) raises `CacheIntegrityError`
  - `TieredEvaluationCache` (L2-then-L1 put, so L1 never diverges from L2
    after a persistent conflict) and `CachedEvaluator` (validates the
    request before any cache lookup; failed evaluations never cached)
  - `resolve_executable()` — at `start()` the caller-supplied executable
    reference is resolved once (absolute / relative-with-separator taken as
    a filesystem path; a bare command name resolved through `PATH` via
    `shutil.which`; unresolvable → `EngineStartupError`) and that one path
    is used for both hashing and process launch; still excluded from cache
    identity (executable SHA-256 remains the content identity); stdlib
    only, no new dependency
  - manual, non-pytest smoke helper (`scripts/stockfish_smoke.py`) takes a
    caller-supplied executable path and exercises White/Black canonical
    positions, UNRESTRICTED + FORCED_MOVE, L1/L2 cache hits, and a
    SQLite close/reopen re-read
  - EngineRegret, mistake/damage thresholds, MultiPV-based recommendations,
    and any dependency on `personal.py` are explicitly out of scope
- Step 4B (IMPLEMENTED, AWAITING REVIEW, UNCALIBRATED): canonical-position
  damaging-move measurement and orchestration (`assessment.py`). See the
  "Engine damage assessment (Step 4B)" section of `docs/architecture.md`.
  `engine.py` and `engine_cache.py` are unchanged.
  - `ASSESSMENT_SEMANTICS_VERSION = "CANONICAL_DECISION_DAMAGE_V1"` and
    `CANONICAL_DAMAGE_SCOPE_NOTE` — the quotable statement of exactly what a
    result does and does not claim
  - integer expected-score units `U = 2*W + D` (`0..2000`);
    `SignedGapUnits = U(alternative) - U(observed)` preserved exactly, never
    clamped, absolute-valued, or floored at zero
  - `ComparisonPolicy` — immutable, validated, keyword-only: policy
    id/version, B1/B2/optional-B3 `SearchLevel`s with strictly increasing
    node budgets, `epsilon_units`, `tau_units`, three separate drift limits,
    `material_negative_gap_units`, bounded `max_active_alternatives`,
    optional `max_requests_per_decision`, `CalibrationStatus` +
    `calibration_id`, and a deterministic SHA-256 `fingerprint`. **No
    production threshold values exist in the module and there is no default
    policy**; an `UNCALIBRATED` policy runs the full measurement but can
    never emit `DAMAGE_SUPPORTED`
  - policy identity is outside primitive engine cache identity: changing tau
    or epsilon reuses identical cached Stockfish evidence (tested)
  - `MoveEvidence` / `CandidateDiscovery` / `ComparisonRound` — the signed
    gap exists only on a round, and a round accepts only `FORCED_MOVE`
    evidence, so an `UNRESTRICTED` discovery score structurally cannot enter
    regret arithmetic
  - candidate discovery v1: top-1 unrestricted only, rediscovery when the
    procedure advances, at most two active distinct alternatives, weakest
    evicted first. No MultiPV
  - B1 screen (only-legal-move and observed-immediate-checkmate short
    circuits cost zero engine calls) → mandatory B2 confirmation → one
    optional bounded B3 → hard stop
  - fixed-witness qualification: `S(A) = min(G_prev, G_cur)`,
    `L(A) = S(A) - epsilon`, damage requires `L(A) > tau` plus a clean
    consistency gate; `_compare_levels` refuses a candidate switch and
    refuses the same level twice, so a moving maximum cannot masquerade as
    confirmation; a candidate discovered at a higher level is backfilled at
    the lower level before it may witness
  - drift/consistency gate over the pairwise gap, the alternative component,
    and the observed-move component separately, plus mate-direction
    reversal; escalation triggers also include a material negative benchmark
    discrepancy and higher-budget discovery selecting the observed move
  - `AssessmentStatus` = `DAMAGE_SUPPORTED` / `NO_DAMAGE_DEMONSTRATED` /
    `INCONCLUSIVE` / `INVALID_EVIDENCE`, with quotable `STATUS_SEMANTICS`
    wording; no `RECHECK_REQUIRED`, no `VALID_COMPARISON`. Step 4A's typed
    exceptions are preserved for malformed questions
    (`RequestValidationError`); malformed evidence raises
    `AssessmentEvidenceError` at the value-object boundary and surfaces as
    `INVALID_EVIDENCE`, never as a harmless zero
  - `EngineRegret` exists if and only if the status is `DAMAGE_SUPPORTED`
    (enforced in `MoveAssessment.__post_init__`); it is never `max(0, gap)`
    and never derived from a negative gap. Final measured gap, conservative
    margin `L`, and accepted loss `S` are three separate recorded values
  - exact terminal semantics via `immediate_terminal_after()`, used as a
    shortcut and as an integrity check against the engine's categorical mate
    evidence; rule-derived facts never replace engine evidence
  - `MoveAssessment.is_engine_damage_admissible` / `admission_failures` —
    the Step 4C admission contract. It re-derives the qualification by
    feeding the two stored rounds back through the same `_compare_levels`
    gate the orchestrator used, so it trusts neither `status` nor
    `unresolved_triggers`; it independently checks accepted-regret policy
    provenance (id / version / fingerprint / levels), that `final_gap_units`
    matches the later qualifying round, that the qualification pair is the
    prescribed one for whether B3 was actually entered (read off the trace),
    and that the two rounds agree on every Step 4A `SemanticCacheKey`
    dimension except the node budget. Recurrence can never promote
    `NO_DAMAGE_DEMONSTRATED` or `INCONCLUSIVE` into damage
  - `MoveAssessment.resolved_triggers` — escalation provenance: triggers an
    earlier prescribed pair raised that the later prescribed pair resolved,
    kept disjoint from `unresolved_triggers` so a B1/B2 instability settled
    by B3 stays visible without blocking admission
  - `LeveledEvidenceProvider` protocol + `CachedEvaluatorPool`: one started
    Step 4A evaluator per level over one shared Step 4A cache (Step 4A binds
    the node budget per evaluator for its lifetime, and that interface was
    deliberately left unchanged). Same engine identity required across
    levels; no second engine abstraction and no second cache
  - 106 synthetic unit tests (`tests/test_assessment.py`), including a
    block of adversarial attacks on the admission contract using
    hand-built DAMAGE_SUPPORTED assessments; 10 opt-in
    real-engine tests (`tests/test_assessment_stockfish.py`, skipped unless
    `CDE_STOCKFISH_PATH` is set), and a manual smoke helper
    (`scripts/stockfish_assessment_smoke.py`)
- Real Stockfish Step 4B checks: **passed** on 2026-09-13 against
  `/home/dvir/tools/stockfish/stockfish/stockfish-linux-x86-64-universal`
  (UCI name `Stockfish 19`, network `nn-1a298aa575a0.nnue`). The 10 opt-in
  integration tests ran in ~3.6 s at 20000/60000-node test budgets; the
  manual smoke script ran at 40000/120000/360000-node demonstration budgets
  and covered a White tactical blunder, a Black tactical blunder, a quiet
  near-equivalent opening choice (correctly NOT damaging, measured gap 5
  units), an observed immediate checkmate (0 engine requests), an immediate
  stalemate transition, forced-root consistency, and a repeated decision
  consuming no further engine analyses. These budgets and thresholds are
  demonstration fixtures, NOT calibrated production values.
- Real Stockfish smoke test: **passed** on 2026-09-10 against the local
  binary `/home/dvir/tools/stockfish/stockfish/stockfish-linux-x86-64-universal`
  (reports UCI name `Stockfish 19`, default network `nn-1a298aa575a0.nnue`).
  Startup through python-chess, executable SHA-256 identity, White-to-move
  and Black-to-move POV results, FORCED_MOVE using the first PV move, cache
  hits, and SQLite close/reopen re-read all succeeded at a 300000-node
  development budget. Re-run and passed again after the final polish pass
  added one-time executable-path resolution.
- The complete test suite has 378 passing tests plus 10 skipped opt-in
  real-engine tests, plus one known unrelated python-chess
  `chess.engine` deprecation warning. Ordinary pytest never depends on a
  real Stockfish binary: the Step 4B integration module is skipped unless
  `CDE_STOCKFISH_PATH` is set.

### Tested local environment (Step 4A verification)

Step 4A was implemented and verified in this local environment (versions
read from the project virtual environment, not assumed):

- Python 3.14.4 (`.venv`)
- `chess` (python-chess) 1.11.2
- pytest 9.1.1 (local `.venv`)
- Stockfish 19 — the manually validated external UCI engine
  (`/home/dvir/tools/stockfish/stockfish/stockfish-linux-x86-64-universal`,
  reports UCI name `Stockfish 19`, default network `nn-1a298aa575a0.nnue`);
  not required by, or exercised in, the ordinary pytest suite.

The independent external Astra audit ran in its own disposable environment
and reported python-chess/chess 1.11.2 and pytest 9.1.1; these happen to
match the local versions above. This is not dependency pinning or lockfile
work — no version constraints were added in this pass.

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

Step 4A (engine foundation + evaluation cache) is implemented and
architecturally approved (see the Engine evaluation section of
`docs/architecture.md`): the `CANONICAL_POSITION_V1` canonical-position
contract, `ENGINE_EVIDENCE_V1` request-aware evidence validation, streaming
exact-line selection in `_analyse_exact()`, `SemanticCacheKey` shared by
L1/L2, one-time executable-path resolution, SQLite schema version 2 with
no migrations, and immutable-`INSERT` cache writes. It does not define
EngineRegret.

Step 4B (damaging-move measurement and orchestration) is implemented but
NOT yet approved and NOT yet calibrated (see the Engine damage assessment
section of `docs/architecture.md`). Its architecture — integer
expected-score units, forced-move-only comparison arithmetic, fixed-witness
two-level confirmation, drift/consistency gating, bounded B3 escalation,
`EngineRegret` only for `DAMAGE_SUPPORTED`, and the Step 4C admission
contract — is complete, but **no production epsilon, tau, drift limit, or
B1/B2/B3 node budget has been chosen, and no reliability, false-positive
rate, abstention rate, or computational cost has been measured.**
Automatic damaging-move claims are therefore NOT production-approved.

Not implemented:

- Chess.com network/API ingestion
- reference-corpus ingestion
- rating bands inside CORE
- legacy / combined PositionIndexes
- calibrated Step 4B thresholds / any measured reliability claim
- Step 4C recurring-weakness ranking over accepted engine damage
- opening classification
- training/content loop, retention/progress mechanics
- Lichess reference cohorts
- weakness/priority ranking

Remaining future work: Step 4B calibration, Step 4C Top-K weakness
ranking, reference-population (Lichess) ingestion, opening classification,
visual/training UI.

Preventing cross-source / cross-call duplicate ingestion of the same game is
the responsibility of the future ingestion/corpus layer, not `PositionIndex`
and not the local PGN parser.

Future architectural rule (not implemented now): a source ordinal represents
a logical PGN record's position in its source. If future tolerant corpus
ingestion skips malformed records, each skipped record must still consume its
source ordinal so that game identity stays reproducible.

## Next Step

Step 4B is implemented and awaiting review. Nothing has been committed.

Required before Step 4B can be considered done:

1. **Architecture/correctness review** of `assessment.py`, its tests, and the
   two documentation sections above.
2. **A real-data calibration phase**, run separately after review. Until it
   has run, the automatic damaging-move label is not production-approved and
   no reliability claim may be made. Calibration must determine:
   - the B1 / B2 / B3 node budgets
   - `epsilon_units` (the search-error allowance)
   - `tau_units` (the materiality threshold)
   - the three drift limits and `material_negative_gap_units`
   - the accepted-label reliability / false-positive rate
   - the abstention (`INCONCLUSIVE`) rate
   - the computational cost per decision and per dataset
3. Only then may a `CALIBRATED` `ComparisonPolicy` with real values be
   defined, and only then may Step 4C consume accepted engine damage.

Step 4C (recurring-weakness ranking over accepted engine damage), reference-
corpus (Lichess) ingestion, opening classification, and the visual/training
UI all remain out of scope until explicitly planned and approved. No Step 4C
architecture has been decided yet.
