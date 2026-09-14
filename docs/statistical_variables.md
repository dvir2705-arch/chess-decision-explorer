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

## 7. Explicitly not defined

The following have no definition, no implementation, and no data:

- a calibrated production `epsilon`, `tau`, drift limit, or B1/B2/B3 budget
- any measured reliability, false-positive, or abstention rate
- `G_reference` and `R`
- a weighted combination of engine, reference-population, and personal
  historical evidence
- reference-population (Lichess) statistics
- opening identity as a statistical dimension (opening identity must stay
  separate from `PositionKey`)
- any machine-learned model or fitted parameter
