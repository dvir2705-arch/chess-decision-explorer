# Statistical Variable Reference

The durable reference for every quantity this project names, and — just as
important — the status of each one.

Status vocabulary, used strictly:

- **IMPLEMENTED** — defined in approved, committed code, with tests.
- **RESEARCH** — being *measured* by an experimental phase. Not a production
  quantity, not calibrated, not a claim.
- **FUTURE** — named so that discussion is unambiguous. No implementation, no
  design decision, and no data behind it yet.

No entry below is a calibrated production value. Where a variable has a
threshold, the threshold is configuration supplied by a caller; this project
defines no default epsilon, tau, drift limit, or node budget anywhere.

---

## 1. Position, decision, and occurrence identity

| Symbol / name | Meaning | Status |
| --- | --- | --- |
| `PositionKey` | Analytical identity of a position before a move: piece placement, side to move, castling rights, legally relevant en-passant. Excludes halfmove clock, fullmove number, and repetition history, so a recurring position stays one position. | IMPLEMENTED |
| `MoveKey` | A move in canonical UCI notation. | IMPLEMENTED |
| `DecisionKey` | `(PositionKey, MoveKey)` — the decision faced and the choice made. | IMPLEMENTED |
| `DecisionObservation` | One occurrence of a `DecisionKey` in one game (`game_id`, `ply_index`, `Outcome`). | IMPLEMENTED |
| `CANONICAL_POSITION_V1` | The canonical recurring-position contract engine evaluation uses: halfmove clock 0, fullmove number 1, empty pre-root history, evaluation normalised to the root side to move. | IMPLEMENTED |

## 2. Historical occurrence statistics

| Symbol / name | Definition | Status |
| --- | --- | --- |
| `occurrence_count` | Every observation, including genuine repeats inside one game. | IMPLEMENTED |
| `distinct_game_count` | Counted at most once per game per position, and at most once per game per decision. | IMPLEMENTED |
| `OutcomeCounts` (`wins`/`draws`/`losses`) | Distinct-game based; `wins + draws + losses == distinct_game_count`. | IMPLEMENTED |
| `score_rate` | `(wins + 0.5 * draws) / distinct_game_count`; `None` with no games. A *historical* rate, never an engine quantity. | IMPLEMENTED |
| `choice_rate(move)` | `decision occurrence_count / position occurrence_count`. Occurrence based. | IMPLEMENTED |

These are facts about what happened. None of them is a move-quality
judgement, and none of them may be combined with engine evidence into a
single score without an explicitly approved design.

## 3. Personal cohort variables

| Symbol / name | Definition | Status |
| --- | --- | --- |
| `TimeControlCategory` | RAPID / BLITZ / BULLET / UNKNOWN, via `base_seconds + 40 * increment_seconds` (`< 180` bullet, `< 600` blitz, else rapid). | IMPLEMENTED |
| `personal_rating` | The actor's own rating in that game — White's rating when the player was White. Not a peak or external rating. | IMPLEMENTED |
| `GameDisposition` | CORE / LEGACY / MISSING_RATING / UNKNOWN_TIME_CONTROL. | IMPLEMENTED |
| v1 rating thresholds | Rapid ≥ 800, Blitz ≥ 501, Bullet ≥ 600. Configuration on `PersonalAnalysisPolicy`, not constants. | IMPLEMENTED |

## 4. Engine evidence primitives (Step 4A)

| Symbol / name | Definition | Status |
| --- | --- | --- |
| WDL triple (`W`, `D`, `L`) | Engine-reported Stockfish win/draw/loss counts, root-side-to-move POV, summing to exactly 1000. | IMPLEMENTED |
| `expected_score` | `(W + 0.5 * D) / 1000`. The **engine-model** expected score. NOT a human win probability, NOT rating-specific, NOT time-control adjusted. | IMPLEMENTED |
| `centipawn` / `mate` | Exactly one is set. A mate is never encoded as a large centipawn number; `mate = 0` is represented faithfully. | IMPLEMENTED |
| `depth`, `seldepth`, `nodes` | Search provenance. `nodes` is the counter on the *retained* comparison-ready report, NOT total node expenditure. | IMPLEMENTED |
| `SemanticCacheKey` | Full cache identity: position semantics version, EPD, search mode, root move, executable SHA-256, engine name, eval file, requested nodes, threads, hash_mb, analysis-profile fingerprint, evidence-contract version. | IMPLEMENTED |

## 5. Damage measurement (Step 4B)

| Symbol / name | Definition | Status |
| --- | --- | --- |
| `U(m)` | `2W + D` for a `FORCED_MOVE` evaluation of move `m` from the canonical root. Integer, `0 ≤ U ≤ 2000`. Display form `U / 2000`. | IMPLEMENTED |
| `SignedGapUnits` / `G` | `U(alternative) − U(observed)`. Signed exactly: never clamped, never absolute-valued, never floored at zero. A positive value is a measurement, not a verdict. | IMPLEMENTED |
| `G_1`, `G_2`, `G_3` | The signed gap at levels B1 / B2 / B3 for one fixed candidate. | IMPLEMENTED |
| `S(A)` | `min(G_prev(A), G_cur(A))` over the two **prescribed** qualification levels for one fixed witness `A`. The conservative accepted loss. | IMPLEMENTED |
| `L(A)` | `S(A) − epsilon_units`. A conservative *empirical* margin, NOT a mathematical lower confidence bound on true chess value. | IMPLEMENTED |
| `epsilon_units` | Search-error allowance. Required constructor argument; **no value chosen**. | RESEARCH (value uncalibrated) |
| `tau_units` | Materiality threshold; damage requires `L(A) > tau_units`. Required argument; **no value chosen**. | RESEARCH (value uncalibrated) |
| `max_gap_drift_units`, `max_alternative_drift_units`, `max_user_drift_units` | Three separate consistency limits: on the pairwise gap, on the alternative's own evaluation, and on the observed move's own evaluation. Required arguments; **no values chosen**. | RESEARCH (values uncalibrated) |
| `material_negative_gap_units` | The size of a negative benchmark discrepancy that forces escalation. Required argument; **no value chosen**. | RESEARCH (value uncalibrated) |
| B1 / B2 / B3 node budgets | Strictly increasing search levels. Required arguments; **no production budgets exist in this repository** — the only values present are demonstration fixtures in tests and smoke scripts, labelled as such. | RESEARCH (values uncalibrated) |
| `EngineRegret.units` | The accepted `S(A)`. Exists **only** for `DAMAGE_SUPPORTED`. Never `max(0, gap)`, never derived from a negative gap. | IMPLEMENTED |
| `centipawn_gap` | `cp(alternative) − cp(observed)`, `None` when either side is a mate. A supporting diagnostic only; it never enters a threshold test and is never combined with the WDL gap. | IMPLEMENTED |
| `is_engine_damage_admissible` / `admission_failures` | The Step 4C admission contract; re-derives qualification from the recorded trace rather than trusting summary fields. | IMPLEMENTED |
| accepted-label reliability, false-positive rate, abstention rate, cost per decision | **Not measured.** No value exists. | FUTURE |

## 6. Calibration-evidence pilot (Step 4C.0) — RESEARCH ONLY

Step 4C.0 generates evidence about how the low-cost Step 4B measurement
relates to much stronger same-root, same-witness analysis. It calibrates
nothing.

| Symbol / name | Definition | Status |
| --- | --- | --- |
| `S` | The conservative accepted loss `min(G_prev, G_cur)` of the qualifying fixed witness for one sampled root, obtained from the existing Step 4B procedure. A C0 run is **always UNCALIBRATED**, so Step 4B withholds the positive label and `S` is re-derived from the recorded rounds through the production `_compare_levels` gate and tagged `rederived_from_trace`. The re-derivation applies **only** when calibration scope is the *sole* reason the label was withheld; any substantive Step 4B refusal (no qualifying candidate, instability surviving escalation, exhausted work budget, invalid evidence) yields no `S` at all. A C0 `S` is a RESEARCH MEASUREMENT, **never** an admissible `EngineRegret` conclusion. | RESEARCH (measured, not calibrated) |
| `U_ref_j(m)` | `U(m)` measured by a `FORCED_MOVE` analysis at reference node budget `j`, from the same canonical root, with the same engine identity, analysis profile, threads and hash — only the node budget differs. | RESEARCH |
| `G_ref_j` | `U_ref_j(witness) − U_ref_j(observed_move)`. Signed, preserved exactly. | RESEARCH |
| reference ladder | The explicitly supplied, ordered, strictly increasing list of reference budgets, every one of them stronger than the production qualification level it audits. No schedule is hard-coded. | RESEARCH |
| sampled-root identity | `source_id + ':' + zero_based_source_ordinal`. Every deterministic C0 draw is keyed on it, so the reproducibility guarantee is: same `source_id` + same source ordering/content + same seed + same sampling configuration → same sampled roots. It is NOT independent of stream order (the ordinal is a stream position); it IS stable against starting the scan later in the same unchanged stream. C0 does not hash or verify the corpus. | RESEARCH |
| `delta G_j` | `G_ref_j − G_ref_{j-1}` between consecutive reference levels. Diagnostic. | RESEARCH |
| witness drift / observed drift | `U_ref_j(witness) − U_ref_{j-1}(witness)` and the same for the observed move. Diagnostic; a stable gap can hide large movement in both components. | RESEARCH |
| saturation | Whether a level's `U` sits on a scale endpoint (`2000` or `0`), where the value cannot move further in one direction. Diagnostic tag. | RESEARCH |
| monotonicity | Whether the `G_ref` sequence is non-decreasing, non-increasing, or non-monotonic. A description, **not** a convergence verdict. C0 defines no convergence threshold. | RESEARCH |
| `G_reference` | A single frozen reference gap, once a reference protocol is chosen. **Not defined, not frozen, not chosen.** | FUTURE |
| `R` | `S − G_reference`. **Deliberately not computed in C0**, because `G_reference` is not frozen. C0 collects the trajectory needed to study it. | FUTURE |

## 7. Human reference variables (Step 5C-lite)

Human reference evidence is what **other human players** did in the player's
own recurring positions. It is a separate architecture from the C0/C1
strong-engine reference and shares nothing with it but `PositionKey`: no
`G_ref`, no `R`, no `epsilon`, no node budget, no search-noise quantity.

All of these are facts about two observed populations. None of them is a
move-quality judgement, and none may be combined with engine evidence into a
single score without an explicitly approved design.

### Target set

| Symbol / name | Definition | Status |
| --- | --- | --- |
| `POSITION_KEY_V1` | The `PositionKey` identity contract matching is performed under: `board.epd(en_passant="legal")`. Recorded in every run manifest; deliberately distinct from `CANONICAL_POSITION_V1`, which is the *engine reconstruction* contract. A target set written under a different value is refused, not silently matched. | IMPLEMENTED |
| recurring position | A personal position whose **distinct personal game count** is at least `min_distinct_games`. Repeats of the same position inside ONE personal game are not sufficient recurrence by themselves. | IMPLEMENTED |
| `min_distinct_games` | The recurrence threshold. Configuration on the run, recorded in the manifest. v1 default `2`. | IMPLEMENTED |
| `personal_cohort` | Which single personal cohort (Rapid / Blitz / Bullet) the target set was built from. One cohort per run; the three cohort indexes are never merged. Metadata beside the keys, never inside a `PositionKey`. | IMPLEMENTED |
| `primary_move` | The player's most-played move in a recurring position, ties broken by UCI ascending. A reporting anchor — **not** a claim about intended repertoire, preference, or correctness. | IMPLEMENTED |

### Scan accounting

| Symbol / name | Definition | Status |
| --- | --- | --- |
| `records_scanned` | Logical PGN records read off the stream, including every rejected one. **Never** the reference dataset size. | IMPLEMENTED |
| `records_header_rejected` | Records dropped on header evidence alone, whose movetext was never tokenised. | IMPLEMENTED |
| `records_fully_parsed` | Records that survived header eligibility and were built into a game. Partitions with the previous row: `records_header_rejected + records_fully_parsed == records_scanned`. | IMPLEMENTED |
| `games_accepted` | ELIGIBLE reference games whose evidence actually entered the aggregate. This — and only this — is the reference dataset size. | IMPLEMENTED |
| `target_eligible_games` | How many eligible games the run asked for. A request, never an achievement. | IMPLEMENTED |
| `target_met` | Whether `games_accepted >= target_eligible_games`. False means the run has a smaller dataset than requested and may not be described by its target. | IMPLEMENTED |
| `shortfall` | `max(0, target_eligible_games - games_accepted)`. | IMPLEMENTED |
| `scan_termination` | `target_reached` / `stream_exhausted` / `scan_limit_reached`. Anything but `stream_exhausted` makes the accepted games a **declared prefix** of the source, never a random or representative sample of it. | IMPLEMENTED |
| rejection counts | Per-reason counts: not completed, non-standard variant, not rated, **bot player**, unparseable, time control not eligible, termination excluded, rating missing, rating out of band, too few plies, replay failed, skipped by offset. | IMPLEMENTED |
| `bot_player` | A side is a Lichess Bot API account (`WhiteTitle`/`BlackTitle` == `BOT`, exact and case-insensitive). Excluded by default: this is a HUMAN cohort. Every other title marks a titled human and is accepted. | IMPLEMENTED |
| `decisions_examined` / `decisions_matched` | Plies examined in accepted games, and those whose `PositionKey` was in the target set. `match_rate` is their ratio, `None` when nothing was examined. | IMPLEMENTED |
| `position_coverage_rate` | `matched_positions / target_positions`: the share of the player's recurring positions the corpus reached at all. Not a sample-size guarantee. | IMPLEMENTED |

### Reference evidence

Reference statistics use `PositionIndex` — the same population-agnostic
aggregation as personal statistics — in its **own instance**. So
`occurrence_count`, `distinct_game_count`, and `OutcomeCounts` keep exactly
the definitions in section 2, including the rule that no permanent game-ID
set is retained.

| Symbol / name | Definition | Status |
| --- | --- | --- |
| reference `occurrence_count` | Every matched occurrence, including genuine repeats inside one reference game. | IMPLEMENTED |
| reference `distinct_game_count` | Counted at most once per reference game per position, and once per reference game per move. A game that repeats a position cannot inflate it. | IMPLEMENTED |
| actor-relative outcome | The reference game's result as seen by **the side to move at the matched position**. White to move: `1-0` win, `0-1` loss. Black to move: `0-1` win, `1-0` loss. `1/2-1/2` draw either way. `PositionKey` fixes the side to move, so the actor is determined by the key. No White-centric outcome count exists anywhere in the pipeline. | IMPLEMENTED |
| `reference_move_rate` | Reference move `occurrence_count` / reference position `occurrence_count`. Occurrence based. `None` when the denominator is zero. | IMPLEMENTED |
| `reference_win_rate` / `draw_rate` / `loss_rate` | Distinct-game based, over `reference_outcome_games` (which equals the move's reference `distinct_game_count`). `None` when the denominator is zero. | IMPLEMENTED |
| `reference_score_rate` | `(wins + 0.5 * draws) / games`, actor-relative. A *historical human* rate. Never an engine quantity, never a move-quality verdict. | IMPLEMENTED |
| `reference_rank` | 1-based popularity rank of a move among the reference moves at that position, by occurrence count descending, ties broken by UCI ascending. Equal counts therefore receive distinct consecutive ranks; the counts sit beside the rank so ties stay visible. `None` for a move the reference population never played. | IMPLEMENTED |
| `reference_top_move` | The reference population's most-played move at that position; `None` when uncovered. | IMPLEMENTED |
| `reference_covered` | Whether the recurring position occurred at least once in the accepted reference games. | IMPLEMENTED |
| `reference_move_coverage` | Share of the player's OWN distinct moves at that position that the reference population also played at least once. `None` when uncovered. | IMPLEMENTED |
| `primary_move_agreement` | Count of covered positions where the player's most-played move is also the reference population's most-played move. A description of agreement between two populations — **not** a correctness rate. | IMPLEMENTED |

### Zero-denominator convention

Every rate in this section is `None` when its denominator is zero, never
`0.0`. "No reference evidence" and "seen but never chosen" are different
facts and must not render as the same number.

### Deliberately not defined by this milestone

| Symbol / name | Status |
| --- | --- |
| statistical significance, p-values, confidence intervals, effect sizes over reference evidence | FUTURE — **not defined, not computed** |
| rating-matched reference cohorts | FUTURE |
| minimum reference sample size for a position to be reportable | FUTURE |
| any combination of human reference evidence with engine evidence into one score | FUTURE |

## 8. Explicitly not defined

The following have no definition, no implementation, and no data:

- a calibrated production `epsilon`, `tau`, drift limit, or B1/B2/B3 budget
- any measured reliability, false-positive, or abstention rate
- `G_reference` and `R`
- a weighted combination of engine, reference-population, and personal
  historical evidence
- rating-matched reference-population statistics, and any significance or
  effect-size claim over human reference evidence (section 7 defines the
  observed-frequency and observed-outcome quantities only)
- opening identity as a statistical dimension (opening identity must stay
  separate from `PositionKey`)
- any machine-learned model or fitted parameter
