# Project State

This file records the current implementation and verification state of Chess Decision Explorer. Durable semantics and architectural contracts live in `docs/architecture.md`; the README is the public entry point.

## Current phase

The latest verified code checkpoint is Step 4B, committed as:

`c83db1e` — `feat: add robust engine damage assessment`

Step 4B is **implemented but not production-calibrated**. The assessment procedure, provenance, admission contract, and real-engine checks exist, but production thresholds and search budgets have not been established. An `UNCALIBRATED` policy cannot emit `DAMAGE_SUPPORTED`.

Step 4C — personalized ranking that combines recurrence with admitted engine-damage evidence — is **not implemented**. The Step 4B admission contract exists specifically so a future ranking layer cannot promote inconclusive or non-damaging evidence into a weakness claim.

Human-reference analysis, opening/repertoire context, targeted training, and longitudinal improvement tracking are also future work.

## Implemented milestones

### Step 1 — domain model

Implemented immutable chess-domain value objects:

- `PositionKey`
- `MoveKey`
- `DecisionKey`
- `GameResult`
- `Outcome`
- `DecisionObservation`

`PositionKey` represents recurring-position identity using piece placement, side to move, castling rights, and legally relevant en-passant state. Halfmove/fullmove counters and prior repetition history are intentionally excluded from recurring-position identity.

### Step 2 — aggregation

Implemented `PositionIndex` aggregation with:

- occurrence counts;
- distinct-game counts;
- distinct-game-based outcome statistics;
- occurrence-based move choice rates;
- validation before mutation;
- temporary per-game bookkeeping only, with no permanent game-ID sets.

A repeated position inside one game can therefore contribute multiple occurrences while contributing only one game outcome.

### Step 3A — streaming local PGN ingestion

Implemented source-neutral `GameRecord` parsing and player-decision extraction:

- PGNs are read one game at a time;
- only the main line is consumed;
- malformed, unfinished, unsupported-variant, and Chess960 games are rejected;
- legal custom starting FENs are supported;
- requested-player decisions are extracted from the position before each move;
- replay must complete successfully before observations are returned.

### Step 3B — real personal-data exercise

The personal pipeline was exercised on a local Chess.com history containing **5,128 completed games across 60 monthly PGN files**.

Raw personal PGNs remain outside Git. The purpose of this run is real-input and edge-case validation; it is not presented as a large public corpus.

### Step 3C — personal analysis cohorts

Implemented separate Rapid, Blitz, and Bullet CORE indexes with:

- time-control classification;
- actor-side rating resolution;
- immutable eligibility policy;
- disposition accounting for legacy, missing-rating, and unknown-time-control games;
- transactional per-game indexing.

The current v1 minimum ratings are:

- Rapid: 800
- Blitz: 501
- Bullet: 600

These are policy configuration, not universal chess thresholds.

### Step 4A — Stockfish evaluation and semantic cache

Step 4A is implemented and architecturally approved for its documented scope.

It includes:

- Stockfish integration through UCI using `python-chess`;
- canonical-position evaluation normalized to the root side to move;
- unrestricted and forced-move evaluation modes;
- request-aware validation of scores, WDL, depth/nodes, PV legality, and forced-root consistency;
- engine identity using UCI name plus SHA-256 of the executable bytes;
- a shared semantic cache key for in-memory L1 and persistent SQLite L2;
- immutable/conflict-detecting persistent writes;
- request/evidence revalidation when reading persistent cache entries;
- executable-path resolution without including machine-specific paths in cache identity.

The cache stores reusable engine evidence. Assessment-policy thresholds are deliberately outside primitive engine cache identity so calibration changes can reuse identical Stockfish work.

### Step 4B — canonical move-damage assessment

Implemented a bounded damaging-move assessment procedure over canonical recurring positions.

The key product rule is: **this is a damaging-move detector, not a best-move detector**.

An unrestricted search may discover a candidate alternative, but only equal-budget forced-root evaluations enter the comparison. A positive result requires the same alternative to remain materially better across the prescribed confirmation levels, with consistency checks for gap drift, component drift, sign reversal, mate-direction conflicts, and evidence compatibility.

The procedure can return:

- `DAMAGE_SUPPORTED`
- `NO_DAMAGE_DEMONSTRATED`
- `INCONCLUSIVE`
- `INVALID_EVIDENCE`

`NO_DAMAGE_DEMONSTRATED` does not claim optimality. Ambiguous or contradictory evidence may abstain instead of being converted into a weakness label.

The Step 4B result also exposes an admission contract for the future ranking layer. Admission re-checks qualification from recorded evidence/provenance rather than trusting summary fields alone.

## Verification

### Ordinary tests

The checked-in project has a broad synthetic/behavioral pytest suite covering domain semantics, aggregation, ingestion, personal cohorts, engine evidence, cache integrity, and assessment behavior.

The ordinary suite does not require a real Stockfish binary.

### Real Stockfish checks

Real-engine smoke/integration checks were run against Stockfish 19 during Step 4A and Step 4B development. They exercised:

- White- and Black-to-move normalization;
- unrestricted and forced-root evaluation;
- L1/L2 cache reuse;
- SQLite close/reopen persistence;
- tactical damaging-move examples;
- a quiet near-equivalent move;
- terminal-position shortcuts and integrity checks;
- repeated-decision cache reuse.

The node budgets and assessment thresholds used by the demonstration scripts are test/demo values, **not production calibration**.

## Current limitations

The repository does not currently provide:

- calibrated production thresholds or search budgets for Step 4B;
- Step 4C recurrence × damage ranking;
- rating-matched human-reference analysis;
- Chess.com API ingestion beyond local PGN files;
- opening or repertoire classification;
- targeted training generation;
- a public web/visual interface;
- cross-position pattern intelligence or ML similarity analysis.

The in-memory position index grows with unique retained positions/decisions, and the current L1 engine cache has no eviction policy. PGN parsing is streaming, but the current system should not be described as globally bounded-memory.

## Data and repository hygiene

- Personal and reference datasets are excluded from Git by `.gitignore`.
- Generated SQLite engine caches are excluded from Git.
- Smoke scripts take a caller-supplied Stockfish executable path; no local machine path is required by the code.
- The repository should not contain credentials, tokens, personal PGNs, or private session URLs in tracked files or future commit messages.

## Next engineering step

Before Step 4C ranking, Step 4B requires review/calibration work on real data, including false positives, abstentions, computational cost, and the production search/threshold policy.

Only after the admission semantics are trusted should recurrence be used to prioritize damaging decisions for personalized training.
