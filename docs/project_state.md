# Project State

This file records the current implementation and verification state of Chess Decision Explorer. Durable semantics and architectural contracts live in `docs/architecture.md`, and `docs/statistical_variables.md` is the durable reference for every named quantity and its status (IMPLEMENTED / RESEARCH / FUTURE). The README is the public entry point.

## Current phase

Step 4B is **complete and architecturally reviewed and accepted**, at:

`c83db1e75453a51b24dabb3658b31bf1f6b10452` — `feat: add robust engine damage assessment`

That is the approved baseline. Acceptance covers the architecture and implementation, not any threshold: Step 4B remains **UNCALIBRATED**. No production epsilon, no tau, no drift thresholds, and no canonical B1/B2/B3 search budgets have been calibrated, so the automatic damaging-move label is not production-approved. An `UNCALIBRATED` policy cannot emit `DAMAGE_SUPPORTED`.

Step 4C.0 — the **calibration-evidence pilot** — is implemented and tested on top of that baseline. It is EXPERIMENTAL infrastructure for generating the evidence a later calibration decision will need, and it calibrates nothing itself. Its runtime/reference benchmark has **not** yet been run, so it has produced no pilot evidence.

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

### Step 4C.0 — calibration-evidence pilot (experimental)

Implemented an evidence-generation harness under `src/chess_decision_explorer/experimental/c0/`, deliberately outside the production modules. No production module imports it, and it changes no production semantics.

Its single research question is how the low-cost Step 4B measurement `S` differs from much stronger same-root, same-witness analysis. For one sampled canonical decision it records:

- `S` — the conservative accepted loss of the qualifying fixed witness, produced by the existing, unmodified Step 4B procedure;
- `G_ref_j = U_ref_j(witness) − U_ref_j(observed_move)` at each explicitly supplied stronger reference node budget, from the same canonical root with the same fixed witness.

A pilot run **always** builds an `UNCALIBRATED` Step 4B policy, and there is no switch to change that. Step 4B therefore stays free to answer `INCONCLUSIVE`, no run can emit `DAMAGE_SUPPORTED` or produce an admissible `EngineRegret`, and every recorded `S` is a research measurement re-derived from the recorded trace — never a production damage conclusion.

What C0 deliberately does **not** do: it defines no final `G_reference`, computes no `R = S − G_reference`, freezes no reference protocol, chooses no epsilon or tau, sets no convergence threshold, fits no model, and uses no personal games. It reports the reference trajectory and diagnostics only.

Supporting properties:

- deterministic streaming sampling, one decision per game, with the corpus never materialised; sampled-root identity is `source_id + ':' + zero_based_source_ordinal`, so reproducibility requires the same source id, source ordering/content, seed, and sampling configuration;
- a run manifest recording code commit, source identity, seed, eligibility filters, engine identity and evidence-contract versions, policy identity and budgets, and the reference ladder;
- run artefacts are immutable — a run never overwrites an existing run directory, and a replay uses its own run id while sharing one evaluation cache;
- a `--replay-only` mode that reproduces derived measurements from stored evidence and fails rather than running any engine analysis.

## Verification

### Ordinary tests

The checked-in project has a broad synthetic/behavioral pytest suite covering domain semantics, aggregation, ingestion, personal cohorts, engine evidence, cache integrity, assessment behavior, and the Step 4C.0 pilot harness.

The ordinary suite does not require a real Stockfish binary. The C0 tests replace the engine pool with scripted evidence, so sampling, the reference trajectory, the manifest, and the whole CLI output pipeline are exercised without a binary.

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

A separate manual C0 smoke command exercises the pilot end to end against a real engine over a tiny built-in PGN stream, including a `--replay-only` second pass that must reproduce identical derived measurements without running a single engine analysis.

The node budgets and assessment thresholds used by the demonstration scripts are test/demo values, **not production calibration**.

## Current limitations

The repository does not currently provide:

- calibrated production thresholds or search budgets for Step 4B — no epsilon, tau, drift threshold, or canonical B1/B2/B3 budget exists;
- a defined `G_reference` or `R`; C0 collects the reference trajectory needed to study them and freezes neither;
- any measured reliability, false-positive, or abstention rate;
- Step 4C recurrence × damage ranking;
- rating-matched human-reference analysis;
- Chess.com API ingestion beyond local PGN files;
- opening or repertoire classification;
- targeted training generation;
- a public web/visual interface;
- cross-position pattern intelligence or ML similarity analysis.

The Step 4C.0 runtime/reference benchmark has not been run, so the actual cost per root at a realistic reference ladder is unmeasured and no pilot evidence exists yet.

The in-memory position index grows with unique retained positions/decisions, and the current L1 engine cache has no eviction policy. PGN parsing is streaming, but the current system should not be described as globally bounded-memory.

## Data and repository hygiene

- Personal and reference datasets are excluded from Git by `.gitignore`.
- Generated SQLite engine caches are excluded from Git.
- All generated Step 4C.0 experiment output (`experiments/`) is disposable computed data and is excluded from Git; the harness code is tracked, its output is not.
- Smoke scripts take a caller-supplied Stockfish executable path; no local machine path is required by the code.
- The repository should not contain credentials, tokens, personal PGNs, or private session URLs in tracked files or future commit messages.

## Next engineering step

Run the first Step 4C.0 benchmark: a **5–10-root external Rapid/Blitz pilot**, at `threads = 1` and `hash_mb = 64`, to measure the real cost per root at the intended reference ladder. One Stockfish process is started per bound level and each holds its own hash table, so the configured hash is paid per level rather than once; neither value may be raised until a run's actual memory footprint has been measured.

That benchmark is followed by review before any larger run. Its output is evidence for a later, separate decision about a reference protocol, `G_reference`, and `R` — C0 itself decides none of those.

Step 4B calibration on real data remains the prerequisite for Step 4C ranking, including false positives, abstentions, computational cost, and the production search/threshold policy. Only after the admission semantics are trusted should recurrence be used to prioritize damaging decisions for personalized training.
