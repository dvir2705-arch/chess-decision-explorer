# Project State

This file records the current implementation and verification state of Chess Decision Explorer. Durable semantics and architectural contracts live in `docs/architecture.md`, and `docs/statistical_variables.md` is the durable reference for every named quantity and its status (IMPLEMENTED / RESEARCH / FUTURE). The README is the public entry point.

## Current phase

Step 4B is **complete and architecturally reviewed and accepted**, at:

`c83db1e75453a51b24dabb3658b31bf1f6b10452` — `feat: add robust engine damage assessment`

That is the approved baseline. Acceptance covers the architecture and implementation, not any threshold: Step 4B remains **UNCALIBRATED**. No production epsilon, no tau, no drift thresholds, and no canonical B1/B2/B3 search budgets have been calibrated, so the automatic damaging-move label is not production-approved. An `UNCALIBRATED` policy cannot emit `DAMAGE_SUPPORTED`.

Step 4C.0 — the **calibration-evidence pilot** — is implemented and tested on top of that baseline. It is EXPERIMENTAL infrastructure for generating the evidence a later calibration decision will need, and it calibrates nothing itself. Its runtime/reference benchmark has **not** yet been run, so it has produced no pilot evidence.

Step 4C — personalized ranking that combines recurrence with admitted engine-damage evidence — is **not implemented**. The Step 4B admission contract exists specifically so a future ranking layer cannot promote inconclusive or non-damaging evidence into a weakness claim.

Step 5C-lite — the **external human reference comparison** — is implemented, tested, and **validated against real Lichess data at 1,000 eligible games**. It compares the player's recurring positions against a streamed corpus of external human games. It is HUMAN evidence and is architecturally separate from the C0/C1 strong-engine reference: it produces and consumes no `G_ref`, `R`, `epsilon`, node budget, or search-noise quantity, and its modules import no engine, assessment, or C0 code.

**Only the 1,000-game validation has been run.** No 10,000-game run and no 500,000-game run has been performed, so the repository makes no claim at those scales.

Opening/repertoire context, targeted training, longitudinal improvement tracking, and rating-matched human cohorts are future work.

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

### Step 5C-lite — external human reference comparison

Implemented a streaming, source-agnostic human-reference pipeline under `src/chess_decision_explorer/human_reference/`, as production architecture rather than an experimental harness.

The pipeline:

1. builds (or loads) the player's recurring `PositionKey`s from **one** personal cohort;
2. streams an external PGN corpus, accepting exactly a configured number of **eligible** games;
3. examines the position before every move of an accepted game and keeps statistics **only** for positions already in the target set;
4. aggregates human move frequencies and actor-relative outcomes into a second, separate `PositionIndex`;
5. derives a Personal-vs-Reference comparison without mutating either aggregate.

The architectural point is what it does not build. No database of corpus positions exists: a non-target position is discarded the moment it is tested. The invariant is asserted by a test — the reference aggregate never holds more positions than the target set — and measured memory is flat across run sizes.

Supporting properties:

- recurrence is a **distinct personal game** property (v1 default: at least 2), and repeats inside one personal game are never sufficient on their own; occurrence counts and distinct-game counts are both preserved throughout;
- one cohort per run — the three personal cohort indexes are never merged — and cohort, player, rating, source, and time control are recorded beside the keys, never inside a `PositionKey`;
- reference outcomes are **actor-relative** to the side to move at the matched position, which `PositionKey` fixes; no White-centric outcome count exists anywhere;
- the reference aggregate reuses `PositionIndex`, which already gates distinct-game counting with per-call temporary sets, so no external game id is retained in any aggregate;
- the run target counts **eligible accepted games**, never records scanned; a run that exhausts its input first records `target_met: false` with a shortfall, and says so in the first line of its report;
- eligibility is configurable and fully recorded — the v1 defaults are exactly standard chess, completed result, rated, Rapid or Blitz, parseable PGN, and nothing more;
- a run manifest records code commit, source identity and declaration, filters, target/scanned/accepted counts, per-reason rejections, timestamps, the recurrence threshold, the personal cohort, and the `PositionKey` semantics version;
- `--source-id` is required for a stdin corpus, following the Step 4C.0 precedent;
- run directories are immutable — a directory already holding artefacts is refused, and there is no `--force`;
- derived comparisons mutate nothing, ordering is total and deterministic (artefacts are byte-identical across repeated runs), and a rate with a zero denominator is `None`, never `0.0`.

What it deliberately does **not** do: no engine evaluation, no calibration, no ML, no opening classification, no rating matching, no statistical-significance or effect-size claim, and no combination of human evidence with engine evidence into a single score.

Artefacts per run (`reference_manifest.json`, `reference_summary.json`, `positions.csv`, `moves.csv`, `report.md`, `target_set.json`) are written under `reference_runs/` and are gitignored — they are disposable computed data, and `target_set.json` additionally contains the player's own positions.

### Step 5C-lite — real-data validation and header-first scanning

**First real external run** — Lichess `lichess_db_standard_rated_2026-08`, streamed with on-the-fly decompression (`curl | zstd | python`, nothing downloaded to disk). Cohort: Standard, rated, **Rapid only**, both players rated **1200–1400** inclusive, completed result, **human players only**. Personal target set: the maintainer's own Chess.com archive, Rapid cohort, recurrence ≥ 2 distinct personal games. (The player name is a run parameter supplied on the command line; it is not recorded in tracked files.)

Personal target set: 5,128 personal games across 60 files → 3,773 Rapid games seen, 2,961 CORE eligible, 2,960 indexed, 85,576 personal decisions over 73,882 distinct positions → **1,341 recurring positions** (653 White-to-move, 688 Black-to-move) with 2,721 move entries.

Result: **56,977 records scanned, 1,000 eligible games accepted** (`target_met: true`), 60,188 decision positions examined, 4,795 matches (7.97 % match rate), **418 of 1,341 recurring positions covered (31.2 %)**. Rejections: 48,713 wrong time control, 6,836 rating out of band, 220 unrated, **202 bot players**, 6 incomplete result, and **zero** parse or replay failures across 56,977 real records.

Actor-relative outcomes were verified on real matched roots: the 1.d4 pair mirrors exactly (W 93 / D 13 / L 113 for White to move against W 113 / D 13 / L 93 for Black to move over the same 219 games). Every aggregate invariant held with zero violations.

**Human-players-only filter.** Lichess Bot API accounts (`WhiteTitle`/`BlackTitle` == `BOT`) are rejected by default with their own reason; titled humans are never affected. 202 real bot games would otherwise have entered a "human" reference population.

**Header-first scanning.** The first real run exposed a bottleneck synthetic tests could not: the narrow cohort accepted 1.76 % of records, yet every record was fully move-parsed, so ~98 % of parsing work was discarded (measured: `PositionKey` construction 2.7 % of runtime, parsing ~97 %, network/zstd ~1.4 %). Eligibility was therefore split into header-decidable rules and rules needing the reconstructed game, with the former applied before any movetext is tokenised. A single-record tee buffer keeps this working on a non-seekable stdin stream.

Re-running the identical cohort produced **byte-identical `positions.csv`, `moves.csv`, and `target_set.json`**, identical rejection counts by reason, and identical aggregates, at **9.1 s instead of 177.9 s — a 19.6× speedup** (320 → 6,264 records/sec; 5.6 → 110 eligible games/sec). Time now splits as ~46 % header scan over all 56,977 records, ~18 % full parse of the 1,000 survivors, ~36 % replay / `PositionKey` / aggregation. Peak RSS 36.9 MB.

## Verification

### Ordinary tests

The checked-in project has a broad synthetic/behavioral pytest suite covering domain semantics, aggregation, ingestion, personal cohorts, engine evidence, cache integrity, assessment behavior, and the Step 4C.0 pilot harness.

The ordinary suite does not require a real Stockfish binary. The C0 tests replace the engine pool with scripted evidence, so sampling, the reference trajectory, the manifest, and the whole CLI output pipeline are exercised without a binary.

The Step 5C-lite tests are synthetic throughout: every PGN is built in-process or written into a temporary directory, so they need no network, no external corpus, and no engine. They cover exact `PositionKey` matching, discarding of non-target positions, one-pass streaming, the accepted-eligible-games target, input exhaustion before target, Rapid/Blitz filtering, standard/rated/completed filtering, actor-relative outcomes for both White-to-move and Black-to-move roots, draw handling, occurrence vs distinct-game counting, a position repeated inside one external game, move-frequency arithmetic, zero-denominator rates, deterministic ordering, the manifest contract, stdin source identity, and the no-full-corpus-materialisation invariant.

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
- human-reference evidence beyond the single validated 1,000-game Rapid 1200–1400 run: **no 10,000-game or 500,000-game run has been performed**, and no result may be claimed at those scales;
- a minimum-reference-sample rule: a position matched by 2 reference games prints rates exactly as confidently as one matched by 1,000. Sample counts are exposed beside every rate, and the reportability threshold will be decided after the 10k coverage distribution is seen;
- rating-matched human-reference cohorts, and any significance or effect-size claim over human reference evidence;
- Chess.com API ingestion beyond local PGN files;
- opening or repertoire classification;
- targeted training generation;
- a public web/visual interface;
- cross-position pattern intelligence or ML similarity analysis.

The Step 4C.0 runtime/reference benchmark has not been run, so the actual cost per root at a realistic reference ladder is unmeasured and no pilot evidence exists yet.

The in-memory position index grows with unique retained positions/decisions, and the current L1 engine cache has no eviction policy. PGN parsing is streaming, but the current system should not be described as globally bounded-memory. The Step 5C-lite reference scan is the one component that *is* bounded by construction — it retains only target positions — and that boundedness does not extend to the personal index it is built from.

## Data and repository hygiene

- Personal and reference datasets are excluded from Git by `.gitignore`.
- Generated SQLite engine caches are excluded from Git.
- All generated Step 4C.0 experiment output (`experiments/`) is disposable computed data and is excluded from Git; the harness code is tracked, its output is not.
- All generated Step 5C-lite run output (`reference_runs/`) is likewise excluded from Git. It is disposable computed data, and its `target_set.json` contains positions from the player's own games.
- Smoke scripts take a caller-supplied Stockfish executable path; no local machine path is required by the code.
- The repository should not contain credentials, tokens, personal PGNs, or private session URLs in tracked files or future commit messages.

## Next engineering step

Run the first Step 4C.0 benchmark: a **5–10-root external Rapid/Blitz pilot**, at `threads = 1` and `hash_mb = 64`, to measure the real cost per root at the intended reference ladder. One Stockfish process is started per bound level and each holds its own hash table, so the configured hash is paid per level rather than once; neither value may be raised until a run's actual memory footprint has been measured.

That benchmark is followed by review before any larger run. Its output is evidence for a later, separate decision about a reference protocol, `G_reference`, and `R` — C0 itself decides none of those.

Step 4B calibration on real data remains the prerequisite for Step 4C ranking, including false positives, abstentions, computational cost, and the production search/threshold policy. Only after the admission semantics are trusted should recurrence be used to prioritize damaging decisions for personalized training.

Separately, and independently of the engine track: Step 5C-lite's **10,000-eligible-game run** is the next step, pending review of the validated 1,000-game result above. Linear extrapolation from the measured header-first run puts 10,000 games at roughly 1.5 minutes and 500,000 at roughly 1.3 hours (~28.5 M records scanned), but those are extrapolations from a single 1,000-game measurement and are not yet validated at either scale. Until a run reports `target_met: true` at a target, the dataset size is whatever `eligible_games_accepted` says and nothing larger may be claimed.

Parallel processing remains **not** justified. The header-first change removed the dominant cost without concurrency; the remaining profile is roughly half header scanning and a third replay/`PositionKey` work. Any further optimisation — including the private-API `_transposition_key()` identity shortcut — is an architectural decision to be taken separately, not an implementation detail.
