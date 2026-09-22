"""Human-reference run artefacts: summary, CSV tables, and the report.

Everything here is descriptive. No threshold is proposed, no move is called
good or bad, and no significance claim is made.

The report has one non-negotiable job beyond presentation: it must never let
"games scanned" be read as "eligible reference games accepted", and it must
never present a target as an achievement. If a run asked for 500,000
eligible games and accepted 412,318, the report says 412,318, says the input
was exhausted, and says the target was NOT met -- in the headline, not a
footnote.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from . import PHASE_ID, SCOPE_NOTE
from .compare import REFERENCE_COVERAGE_NOTE, PositionComparison
from .scan import ReferenceScanResult

POSITION_COLUMNS: tuple[str, ...] = (
    "position_epd",
    "side_to_move",
    "personal_occurrence_count",
    "personal_distinct_game_count",
    "personal_distinct_moves",
    "reference_occurrence_count",
    "reference_distinct_game_count",
    "reference_distinct_moves",
    "reference_covered",
    "reference_move_coverage",
    "reference_wins",
    "reference_draws",
    "reference_losses",
    "reference_score_rate",
    "personal_primary_move",
    "reference_top_move",
    "personal_primary_move_reference_rank",
    "personal_primary_move_reference_rate",
)

MOVE_COLUMNS: tuple[str, ...] = (
    "position_epd",
    "side_to_move",
    "move",
    "personal_occurrence_count",
    "personal_distinct_game_count",
    "personal_move_rate",
    "reference_occurrence_count",
    "reference_distinct_game_count",
    "reference_move_rate",
    "reference_wins",
    "reference_draws",
    "reference_losses",
    "reference_outcome_games",
    "reference_win_rate",
    "reference_draw_rate",
    "reference_loss_rate",
    "reference_score_rate",
    "reference_rank",
    "is_personal_move",
    "is_personal_primary_move",
    "is_reference_top_move",
)

_RATE_DIGITS = 6


def _round(value: Any) -> Any:
    """Round a float for stable artefacts; pass everything else through.

    `None` stays `None` -- an undefined rate is never rendered as a number.
    """
    if isinstance(value, float):
        return round(value, _RATE_DIGITS)
    return value


def position_rows(comparisons: Sequence[PositionComparison]) -> list[dict[str, Any]]:
    return [
        {column: _round(comparison.as_dict()[column]) for column in POSITION_COLUMNS}
        for comparison in comparisons
    ]


def move_rows(comparisons: Sequence[PositionComparison]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for comparison in comparisons:
        for move in comparison.moves:
            payload = move.as_dict()
            payload["position_epd"] = comparison.position_epd
            payload["side_to_move"] = comparison.side_to_move
            rows.append({column: _round(payload[column]) for column in MOVE_COLUMNS})
    return rows


def write_csv(path: Path, columns: Iterable[str], rows: Iterable[dict]) -> int:
    columns = list(columns)
    written = 0
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            written += 1
    return written


def _distribution(values: Sequence[int]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": ordered[0],
        "p25": ordered[(len(ordered) - 1) // 4],
        "median": ordered[(len(ordered) - 1) // 2],
        "p75": ordered[(3 * (len(ordered) - 1)) // 4],
        "max": ordered[-1],
        "mean": round(sum(ordered) / len(ordered), 3),
    }


def _rank_bucket(rank: int | None) -> str:
    if rank is None:
        return "not_played_in_reference"
    if rank <= 3:
        return f"rank_{rank}"
    if rank <= 5:
        return "rank_4_5"
    return "rank_6_plus"


def summarize(
    comparisons: Sequence[PositionComparison], scan_result: ReferenceScanResult
) -> dict[str, Any]:
    """Accumulate the run summary over the derived comparisons.

    Every count is a count of something observed. The one comparison-flavoured
    quantity, `primary_move_agreement`, is the number of positions where the
    player's most-played move is also the reference population's most-played
    move -- a description of agreement between two populations, NOT a
    correctness rate.
    """
    covered = [c for c in comparisons if c.reference_covered]
    rank_buckets: Counter = Counter()
    agreement = 0
    personal_only_moves = 0
    reference_only_moves = 0
    shared_moves = 0

    for comparison in comparisons:
        for move in comparison.moves:
            in_reference = move.reference_occurrence_count > 0
            if move.is_personal_move and in_reference:
                shared_moves += 1
            elif move.is_personal_move:
                personal_only_moves += 1
            elif in_reference:
                reference_only_moves += 1

    for comparison in covered:
        rank_buckets[_rank_bucket(comparison.personal_primary_move_reference_rank)] += 1
        if comparison.personal_primary_move == comparison.reference_top_move:
            agreement += 1

    coverage_rate = scan_result.position_coverage_rate
    return {
        "phase": PHASE_ID,
        "scope_note": SCOPE_NOTE,
        "reference_coverage_note": REFERENCE_COVERAGE_NOTE,
        "scan": scan_result.as_dict(),
        "positions": {
            "target_positions": len(comparisons),
            "reference_covered": len(covered),
            "reference_uncovered": len(comparisons) - len(covered),
            "position_coverage_rate": (
                round(coverage_rate, 6) if coverage_rate is not None else None
            ),
            "personal_distinct_game_count": _distribution(
                [c.personal_distinct_game_count for c in comparisons]
            ),
            "reference_distinct_game_count_covered": _distribution(
                [c.reference_distinct_game_count for c in covered]
            ),
            "reference_distinct_moves_covered": _distribution(
                [c.reference_distinct_moves for c in covered]
            ),
        },
        "moves": {
            "comparison_rows": sum(len(c.moves) for c in comparisons),
            "personal_and_reference": shared_moves,
            "personal_only": personal_only_moves,
            "reference_only": reference_only_moves,
        },
        "personal_primary_move_vs_reference": {
            "covered_positions": len(covered),
            "primary_move_agreement": agreement,
            "primary_move_agreement_rate": (
                round(agreement / len(covered), 6) if covered else None
            ),
            "reference_rank_distribution": dict(sorted(rank_buckets.items())),
        },
    }


def _table(rows: Iterable[tuple[str, object]]) -> str:
    lines = ["| key | value |", "| --- | --- |"]
    for key, value in rows:
        lines.append(f"| {key} | {value} |")
    return "\n".join(lines) + "\n"


def _counter_table(title: str, mapping: dict | None) -> str:
    if not mapping:
        return f"**{title}:** none recorded.\n"
    lines = [f"**{title}**\n", "| key | count |", "| --- | --- |"]
    for key, value in sorted(mapping.items()):
        lines.append(f"| {key} | {value} |")
    return "\n".join(lines) + "\n"


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_markdown(
    manifest: dict[str, Any], summary: dict[str, Any]
) -> str:
    """The human-readable run report.

    The accepted-vs-target headline is rendered first, before any comparison
    table, because every number below it is conditional on it.
    """
    scan = summary["scan"]
    counters = scan["counters"]
    positions = summary["positions"]
    moves = summary["moves"]
    primary = summary["personal_primary_move_vs_reference"]

    accepted = scan["eligible_games_accepted"]
    target = scan["target_eligible_games"]
    met = scan["target_met"]

    parts: list[str] = []
    parts.append("# Step 5C-lite -- Personal vs External Human Reference\n")

    parts.append("## Dataset actually obtained\n")
    if met:
        headline = (
            f"**{accepted:,} eligible reference games accepted** "
            f"(target {target:,}: MET)."
        )
    else:
        headline = (
            f"**TARGET NOT MET.** {accepted:,} eligible reference games were "
            f"accepted out of a target of {target:,} "
            f"(shortfall {scan['shortfall']:,}). This run is "
            f"**not** a {target:,}-game reference dataset and must not be "
            f"described as one."
        )
    parts.append(headline + "\n")
    parts.append(
        f"\n{counters['records_scanned']:,} PGN records were **scanned**; "
        f"{accepted:,} of them were **accepted as eligible reference games**. "
        f"Those two numbers are different and only the second one is the "
        f"reference dataset size.\n"
    )
    parts.append(f"\nScan ended: `{scan['scan_termination']}`.")
    if scan["scanned_prefix_declared"]:
        parts.append(
            " The scan stopped before the stream was exhausted, so the "
            "accepted games are a **declared prefix** of the source and are "
            "not a random or representative sample of it.\n"
        )
    else:
        parts.append(" The input stream was read to exhaustion.\n")

    parts.append("\n## What this report does NOT claim\n")
    parts.append(
        "- **No engine evaluation** was used. This is human evidence only.\n"
        "- **Not** the C0/C1 strong-engine reference: no `G_ref`, no `R`, no "
        "`epsilon`, no search-noise calibration.\n"
        "- **Not a rating-matched cohort.** The reference population is "
        "whatever the recorded filters admitted.\n"
        "- **No statistical-significance claim.** Counts, rates, and ranks "
        "only.\n"
        "- A reference move being more popular, or scoring better, is **not** "
        "evidence that a personal move is a mistake.\n"
    )
    parts.append(f"\n> {SCOPE_NOTE}\n")

    parts.append("\n## Run identity\n")
    parts.append(
        _table(
            [
                ("run_id", manifest["run_id"]),
                ("code_commit", manifest["code_commit"]),
                ("code_tree_dirty", manifest["code_tree_dirty"]),
                ("started_at", manifest["started_at"]),
                ("finished_at", manifest["finished_at"]),
                ("source_id", manifest["source_id"]),
                ("source_stream", manifest["source_declaration"].get("stream")),
                ("game_identity_scheme", manifest["scanner"]["game_identity_scheme"]),
                (
                    "position_key_semantics_version",
                    manifest["position_key_semantics_version"],
                ),
                ("personal_cohort", manifest["personal_cohort"]),
                ("player_label", manifest["personal_target_set"].get("player_label")),
                (
                    "recurring_position_threshold",
                    manifest["recurring_position_threshold"],
                ),
                ("target_positions", positions["target_positions"]),
            ]
        )
    )

    parts.append("\n## Eligibility filters\n")
    parts.append(
        _table(
            (key, value) for key, value in sorted(manifest["eligibility_filters"].items())
        )
    )

    parts.append("\n## Scan accounting\n")
    parts.append(
        _table(
            [
                ("records_scanned", f"{counters['records_scanned']:,}"),
                ("eligible_games_accepted", f"{accepted:,}"),
                ("target_eligible_games", f"{target:,}"),
                ("target_met", met),
                ("games_with_at_least_one_match", f"{counters['games_with_match']:,}"),
                ("decisions_examined", f"{counters['decisions_examined']:,}"),
                ("decisions_matched", f"{counters['decisions_matched']:,}"),
                ("match_rate", _fmt(counters["match_rate"])),
            ]
        )
    )
    parts.append("\n" + _counter_table("Rejections by reason", counters["rejections"]))

    parts.append("\n## Coverage of the personal recurring positions\n")
    parts.append(
        _table(
            [
                ("target_positions", positions["target_positions"]),
                ("reference_covered", positions["reference_covered"]),
                ("reference_uncovered", positions["reference_uncovered"]),
                ("position_coverage_rate", _fmt(positions["position_coverage_rate"])),
                ("move_comparison_rows", moves["comparison_rows"]),
                ("moves_played_by_both", moves["personal_and_reference"]),
                ("moves_personal_only", moves["personal_only"]),
                ("moves_reference_only", moves["reference_only"]),
            ]
        )
    )
    parts.append(f"\n{REFERENCE_COVERAGE_NOTE}\n")

    parts.append("\n## Where the player's most-played move sits in the reference\n")
    parts.append(
        "Agreement means the player's most-played move is also the reference "
        "population's most-played move. It is a description of two "
        "populations, **not** a correctness rate.\n\n"
    )
    parts.append(
        _table(
            [
                ("covered_positions", primary["covered_positions"]),
                ("primary_move_agreement", primary["primary_move_agreement"]),
                (
                    "primary_move_agreement_rate",
                    _fmt(primary["primary_move_agreement_rate"]),
                ),
            ]
        )
    )
    parts.append(
        "\n"
        + _counter_table(
            "Reference rank of the personal most-played move",
            primary["reference_rank_distribution"],
        )
    )

    runtime = manifest.get("runtime") or {}
    if runtime:
        parts.append("\n## Runtime\n")
        parts.append(_table((key, _fmt(value)) for key, value in sorted(runtime.items())))

    parts.append("\n## Artefacts\n")
    parts.append(
        "- `reference_manifest.json` -- full provenance for this run\n"
        "- `reference_summary.json` -- the machine-readable summary above\n"
        "- `positions.csv` -- one row per recurring personal position\n"
        "- `moves.csv` -- one row per move at each position\n"
        "- `target_set.json` -- the personal recurring positions this run "
        "matched against (personal data: gitignored output, never committed)\n"
    )
    return "".join(parts)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
