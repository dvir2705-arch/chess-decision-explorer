# Human Reference V1 — Results

The durable record of the first completed external human-reference corpus.
It states what was measured, under which cohort, at which commit, and — just
as importantly — what the measurement may not be used to claim.

Generated run artefacts are disposable computed data and are excluded from
Git. This document records their **names and meanings**, never their
contents.

---

## 1. Human Reference vs Engine Reference

These are two different architectures that share exactly one thing: the
`PositionKey` identity contract. They must never be mixed, combined into a
single score, or substituted for one another.

| | **Human Reference (this document)** | **Engine Reference (Step 4A/4B/4C.0)** |
| --- | --- | --- |
| Question answered | What did a population of human players actually play here, and how did those games actually end? | How does a move evaluate under controlled Stockfish search from the canonical root? |
| Evidence | Observed move frequencies and actor-relative game outcomes | WDL / centipawn / mate evidence at prescribed node budgets |
| Quantities | `occurrence_count`, `distinct_game_count`, move rates, actor-relative W/D/L, popularity rank | `U(m)`, `G`, `S(A)`, `L(A)`, `epsilon`, `tau`, `G_ref`, `R` |
| Status | **Descriptive.** Facts about two populations. | Measurement subject to calibration; Step 4B remains UNCALIBRATED. |
| What it can support | "The player chooses X here; N % of this cohort chose Y" | "This move loses at least S units against a fixed witness, at these budgets" |
| What it can **never** support | That a move is good, bad, better, or a mistake | Anything about human behaviour, popularity, or rating-specific difficulty |

A move being more popular in the reference cohort — or scoring better in it —
is **not** evidence that another move is a mistake. Popularity is not
quality. The human-reference modules import no engine, assessment, or
Step 4C.0 code, and produce and consume no `G_ref`, `R`, `epsilon`, node
budget, or search-noise quantity.

---

## 2. Cohort definition

| | |
| --- | --- |
| Source | Lichess standard rated monthly database |
| Month | 2026-08 |
| Source id | `lichess_db_standard_rated_2026-08` |
| Variant | Standard only |
| Rated | yes (word-level `rated` token in the PGN `Event` header) |
| Speed | **Rapid only** (`classify_time_control`: `base + 40 × increment ≥ 600 s`) |
| White rating | 1200–1400 inclusive |
| Black rating | 1200–1400 inclusive |
| Both ratings required | yes |
| Result | completed (`1-0`, `0-1`, `1/2-1/2`) |
| Players | **human only** — `WhiteTitle`/`BlackTitle` equal to `BOT` rejected |
| Target | **500,000 ELIGIBLE ACCEPTED games** (not records scanned) |

Titled human players (GM, IM, FM, CM, NM, WGM, WIM, WFM, LM) are accepted
normally; only the `BOT` title is excluded, matched exactly and
case-insensitively.

The corpus was streamed with external on-the-fly decompression; nothing was
downloaded to disk, and the corpus was declared rather than hashed.

## 3. Provenance

| | |
| --- | --- |
| Production commit | `17d534a250086e2e9b0b75265befda0468ac4847` |
| Code tree dirty | `false` |
| `PositionKey` semantics | `POSITION_KEY_V1` |
| Scanner | `HUMAN_REFERENCE_HEADER_FIRST_SCANNER_V2` |
| Personal cohort | Rapid |
| Recurrence threshold | ≥ 2 distinct personal games containing the position |
| Target-set SHA-256 | `fa7c6960b7d09f725b9c578838f1cba7fd7af4def5871ae6f4a004366243fb0e` |

The same target set, byte-identical by SHA-256, was used for the 1,000-game
validation, the 10,000-game benchmark, and the final 500,000-game run. It was
not rebuilt between them.

### Personal side (exact counts, unrounded)

| | |
| --- | --- |
| Personal games seen | 5,128 across 60 PGN files |
| Rapid cohort games seen | 3,773 |
| Rapid CORE eligible / indexed | 2,961 / 2,960 |
| Personal decisions indexed | 85,576 |
| Distinct Rapid positions | 73,882 |
| **Recurring positions (target set)** | **1,341** (653 White-to-move, 688 Black-to-move) |
| Target-set move entries | 2,721 |
| Personal occurrences in the target set | 12,862 |

The player name is a command-line run parameter. It is not recorded in
tracked files.

---

## 4. Final run metrics

| metric | value |
| --- | --- |
| eligible games accepted | **500,000** |
| `target_met` / shortfall | **true** / 0 |
| scan termination | `target_reached` |
| records scanned | 22,666,625 |
| records header-rejected | 22,166,350 (97.79 %) |
| records fully parsed | 500,275 |
| decision positions examined | 31,101,977 |
| matched decisions | 2,495,954 |
| match rate | 8.03 % |
| runtime | **65.1 minutes** (3,908 s) |
| records / sec | 5,800 |
| eligible games / sec | 127.9 |
| decisions / sec | 7,958 |
| peak RSS | **54.6 MB** |

Runtime split: header scan 34.2 %, full parse of survivors 23.2 %, replay +
`PositionKey` + aggregation 42.6 %.

### Rejections by reason

| reason | count |
| --- | --- |
| time control not eligible | 18,613,752 |
| rating out of band | 3,337,973 |
| not rated | 111,930 |
| bot player | 98,243 |
| result not completed | 4,452 |
| too few plies | 275 |
| parse rejected · rating missing · replay failed · skipped by offset · termination excluded · variant not standard | 0 each |

`22,166,625` rejected + `500,000` accepted = `22,666,625` scanned.
`22,166,350` header-rejected + `500,275` fully parsed = `22,666,625` scanned.
The 275-record gap between fully parsed and accepted is exactly the
`too_few_plies` rejections, which require the reconstructed game.

### Reference aggregate

| | |
| --- | --- |
| reference occurrences | 2,495,954 |
| reference distinct games | 2,495,923 |
| within-game repeats | 31 |
| actor-relative W / D / L | 1,199,984 / 97,006 / 1,198,933 |
| draw share | 3.89 % |
| distinct reference moves | 13,289 |

### Integrity

Zero parse failures and zero replay failures across 22,666,625 real records.
All aggregate invariants held with zero violations: `W + D + L ==
distinct_game_count` at position and move level; `sum(move occurrences) ==
position occurrences`; `occurrences ≥ distinct games`; move games ≤ own
position games; outcome denominator == distinct games; position games ≤
accepted games.

Actor-relative outcomes were verified on both White-to-move and
Black-to-move roots at full scale. Wins and losses mirror exactly across a
move and its successor position, with residual game-count gaps explained
entirely by games that ended on that move — a game's final position is never
a decision position. The aggregate score rate across all positions is
**0.5002**, which is what summing both sides of the same games must produce,
and is an independent global check that no White-centric counting exists.

---

## 5. Coverage

**1,287 of 1,341 recurring positions covered (95.97 %).** 54 positions
(4.03 %) were never reached even by 500,000 games.

### Reference distinct games per covered position

| min | p25 | median | p75 | p90 | p95 | p99 | max | mean |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 20 | 104 | 441 | 1,676 | 3,677 | 18,332 | 500,000 | 1,939.3 |

### Positions with at least N reference games

| N | positions | % of 1,341 | % of covered |
| --- | --- | --- | --- |
| 5 | 1,169 | 87.2 % | 90.8 % |
| 10 | 1,088 | 81.1 % | 84.5 % |
| 20 | 977 | 72.9 % | 75.9 % |
| 50 | 804 | 60.0 % | 62.5 % |
| 100 | 658 | 49.1 % | 51.1 % |
| 250 | 446 | 33.3 % | 34.7 % |
| 500 | 302 | 22.5 % | 23.5 % |
| 1,000 | 187 | 13.9 % | 14.5 % |

The distribution is strongly long-tailed: the median covered position holds
104 reference games while the most common holds 500,000.

### Coverage growth across run sizes

| accepted games | positions covered | share |
| --- | --- | --- |
| 1,000 | 418 | 31.2 % |
| 10,000 | 874 | 65.2 % |
| 500,000 | 1,287 | 95.97 % |

Each covered set is a strict superset of the previous one, and every
per-position aggregate grew monotonically — a scaling-consistency check that
held with zero violations.

---

## 6. Stability findings

Comparing the 10,000-game and 500,000-game outputs over the **874 positions
covered in both**:

- `reference_top_move` changed for **292 positions (33.4 %)**
- the personal primary move's popularity rank changed for **445 (50.9 %)**

Instability falls monotonically with the sample size available at 10k:

| reference games at 10k | positions | top-move changed | rank changed | median abs. move-rate change |
| --- | --- | --- | --- | --- |
| 1–4 | 421 | 48.0 % | 64.1 % | 0.2233 |
| 5–9 | 154 | 32.5 % | 55.8 % | 0.0862 |
| 10–19 | 106 | 18.9 % | 40.6 % | 0.0721 |
| 20–49 | 95 | 12.6 % | 31.6 % | 0.0438 |
| 50–99 | 43 | 7.0 % | 18.6 % | 0.0419 |
| 100–249 | 30 | 13.3 % | 23.3 % | 0.0219 |
| 250–499 | 14 | 7.1 % | 7.1 % | 0.0141 |
| 500+ | 11 | 0.0 % | 0.0 % | 0.0094 |

**Why this matters.** `reference_top_move` and `reference_rank` are the
fields most likely to drive any future presentation or recommendation, and
they are exactly the fields that are unreliable at small samples. A position
carrying a handful of reference games currently renders its rates with the
same apparent confidence as one carrying 500,000.

**No reportability threshold has been chosen or implemented.** Two caveats
bear on choosing one: the 100–249 band is non-monotonic against the band
below it on only 30 positions, and the clean 0 % result in the 500+ band
rests on only 11 positions. Sample counts remain exposed beside every rate so
a consumer can apply its own rule.

---

## 7. Descriptive comparison

Among the 75 positions holding **both** ≥ 20 personal games and ≥ 500
reference games, the player's most-played move is:

| | |
| --- | --- |
| the cohort's most-played move | 42 / 75 (56.0 %) |
| within the cohort's top 2 | 66 / 75 (88.0 %) |
| within the cohort's top 3 | 69 / 75 (92.0 %) |
| never played by the cohort | 0 / 75 |

Divergences exist and are substantial — in several positions the player
chooses a move played by under 10 % of the cohort, including one held by 855
personal games against 195,617 reference games. In a number of those same
positions the player's own move carries the **higher** cohort score rate,
which is precisely why popularity must not be read as quality.

These are **descriptive comparisons between two populations**. This document
makes no claim that a cohort move is objectively better, performs no
significance testing, reports no effect size, and uses no rating-matched
control group.

---

## 8. Scope and caveats

- **One month, one cohort.** Lichess 2026-08, Rapid only, both players
  1200–1400, human accounts only. No other month, time control, or rating
  band has been run.
- **A declared prefix, not a random sample.** The scan stops as soon as the
  accepted-game target is met, so the 500,000 games are the first 500,000
  eligible games encountered in the stream, not a random draw from the month.
  They must never be described as representative of the complete population.
- **Filtered cohort, not a rating-matched control.** The rating band is an
  eligibility filter, not a matching procedure against the player's own
  rating trajectory.
- **Descriptive only.** No statistical-significance claim, no confidence
  interval, no effect size, no move-quality judgement.
- **No engine evidence.** Nothing here was evaluated by Stockfish, and no
  quantity here may be combined with engine evidence into a single score
  without an explicitly approved design.
- **No minimum-sample rule.** Rates are printed for every covered position
  regardless of sample size.
- **Corpus declared, not hashed.** Reproducibility rests on the recorded
  source id together with the source's ordering and content.
- **The dataset size is `eligible_games_accepted`**, never the target
  requested. A run that exhausts its input first records `target_met: false`
  with an explicit shortfall, and its report says so in its first line.

---

## 9. Generated artefacts

Every run writes the following into its own run directory. All generated run
output is gitignored: it is disposable computed data, and the exported target
set contains positions from the player's own games.

| file | contents |
| --- | --- |
| `reference_manifest.json` | full provenance: commit, dirty flag, source id and declaration, filters, target/scanned/accepted counts, per-reason rejections, header-first counters, timestamps, runtime instrumentation, recurrence threshold, personal cohort, `PositionKey` semantics version |
| `reference_summary.json` | machine-readable run summary: scan accounting, coverage, move totals, primary-move agreement |
| `positions.csv` | one row per recurring personal position (1,341 rows) — personal and reference occurrence/game counts, coverage, actor-relative W/D/L, primary and top moves, rank |
| `moves.csv` | one row per move at each position — personal and reference counts and rates, actor-relative W/D/L and outcome rates, popularity rank, role flags |
| `report.md` | human-readable report, leading with accepted-vs-target and scanned-vs-accepted |
| `target_set.json` | the personal recurring positions the run matched against (personal data) |

Run directories are immutable: a directory already holding artefacts is
refused, and there is no force option.

Runs performed for Human Reference V1, all against the same source id and
target set: a 1,000-game correctness validation, a 10,000-game benchmark, and
the final 500,000-game production corpus.
