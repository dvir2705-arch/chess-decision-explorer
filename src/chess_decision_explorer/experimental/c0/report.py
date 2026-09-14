"""C0 summary tables and the human-readable pilot report.

EXPERIMENTAL. Everything here is descriptive. No threshold is proposed, no
convergence verdict is issued, and no reliability claim is made.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path

from . import C0_SCOPE_NOTE

ROOT_SUMMARY_COLUMNS = (
    "root_id",
    "game_id",
    "source_ordinal",
    "ply_index",
    "actor_color",
    "actor_rating",
    "time_control_category",
    "status",
    "witness",
    "witness_source",
    "s_units",
    "final_gap_units",
    "is_engine_damage_admissible",
    "reference_levels",
    "g_ref_first",
    "g_ref_last",
    "monotonicity",
    "saturated_level_count",
    "sign_change_count",
    "mate_direction_change_count",
    "max_abs_delta_g_units",
    "assessment_requests",
    "reference_requests",
    "assessment_seconds",
    "reference_seconds",
    "no_r_target_reason",
)

REFERENCE_LEVEL_COLUMNS = (
    "root_id",
    "level_label",
    "requested_nodes",
    "witness",
    "observed_move",
    "witness_units",
    "observed_units",
    "g_ref_units",
    "centipawn_gap",
    "witness_centipawn",
    "observed_centipawn",
    "witness_mate",
    "observed_mate",
    "witness_wdl",
    "observed_wdl",
    "witness_depth",
    "observed_depth",
    "witness_evidence_nodes",
    "observed_evidence_nodes",
    "saturation_status",
    "elapsed_seconds",
)


def read_records(path: Path) -> Iterator[dict]:
    """Stream records back from a JSONL file, one at a time."""
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def root_summary_row(record: dict) -> dict:
    sample = record["sample"]
    assessment = record.get("assessment") or {}
    witness = record["witness_resolution"]
    trajectory = record.get("reference_trajectory")
    diagnostics = (trajectory or {}).get("diagnostics", {})
    levels = (trajectory or {}).get("levels", [])
    cost = record["cost"]
    return {
        "root_id": sample["root_id"],
        "game_id": sample["game_id"],
        "source_ordinal": sample["source_ordinal"],
        "ply_index": sample["ply_index"],
        "actor_color": sample["actor_color"],
        "actor_rating": sample["actor_rating"],
        "time_control_category": sample["time_control_category"],
        "status": assessment.get("status") or "root_rejected",
        "witness": witness["witness"],
        "witness_source": witness["witness_source"],
        "s_units": witness["s_units"],
        "final_gap_units": assessment.get("final_gap_units"),
        "is_engine_damage_admissible": assessment.get("is_engine_damage_admissible"),
        "reference_levels": len(levels),
        "g_ref_first": levels[0]["g_ref_units"] if levels else None,
        "g_ref_last": levels[-1]["g_ref_units"] if levels else None,
        "monotonicity": diagnostics.get("monotonicity"),
        "saturated_level_count": diagnostics.get("saturated_level_count"),
        "sign_change_count": diagnostics.get("sign_change_count"),
        "mate_direction_change_count": diagnostics.get("mate_direction_change_count"),
        "max_abs_delta_g_units": diagnostics.get("max_abs_delta_g_units"),
        "assessment_requests": cost["assessment_requests"],
        "reference_requests": cost["reference_requests"],
        "assessment_seconds": round(cost["assessment_seconds"], 4),
        "reference_seconds": round(cost["reference_seconds"], 4),
        "no_r_target_reason": witness["no_r_target_reason"],
    }


def reference_level_rows(record: dict) -> list[dict]:
    trajectory = record.get("reference_trajectory")
    if not trajectory:
        return []
    root_id = record["sample"]["root_id"]
    rows = []
    for level in trajectory["levels"]:
        witness = level["witness"]
        observed = level["observed"]
        rows.append(
            {
                "root_id": root_id,
                "level_label": level["level_label"],
                "requested_nodes": level["requested_nodes"],
                "witness": trajectory["witness"],
                "observed_move": trajectory["observed_move"],
                "witness_units": level["witness_units"],
                "observed_units": level["observed_units"],
                "g_ref_units": level["g_ref_units"],
                "centipawn_gap": level["centipawn_gap"],
                "witness_centipawn": witness["centipawn"],
                "observed_centipawn": observed["centipawn"],
                "witness_mate": witness["mate"],
                "observed_mate": observed["mate"],
                "witness_wdl": (
                    f"{witness['wdl_wins']}/{witness['wdl_draws']}/{witness['wdl_losses']}"
                ),
                "observed_wdl": (
                    f"{observed['wdl_wins']}/{observed['wdl_draws']}/{observed['wdl_losses']}"
                ),
                "witness_depth": witness["evidence_depth"],
                "observed_depth": observed["evidence_depth"],
                "witness_evidence_nodes": witness["evidence_nodes"],
                "observed_evidence_nodes": observed["evidence_nodes"],
                "saturation_status": level["saturation_status"],
                "elapsed_seconds": round(level["elapsed_seconds"], 4),
            }
        )
    return rows


def summarize(records: Iterable[dict]) -> dict:
    """Accumulate the C0 summary over a stream of root records."""
    status_counts: Counter = Counter()
    no_target_reasons: Counter = Counter()
    monotonicity_counts: Counter = Counter()
    saturation_status_counts: Counter = Counter()
    witness_sources: Counter = Counter()
    delta_g_values: list[int] = []
    witness_drifts: list[int] = []
    observed_drifts: list[int] = []
    s_values: list[int] = []
    g_last_values: list[int] = []
    per_level_seconds: dict[str, list[float]] = {}
    per_level_requests: Counter = Counter()

    roots = 0
    roots_with_target = 0
    roots_with_trajectory = 0
    invalid_evidence = 0
    root_rejected = 0
    admissible = 0
    sign_changes = 0
    mate_direction_changes = 0
    negative_g_levels = 0
    total_reference_levels = 0
    assessment_requests = 0
    reference_requests = 0
    assessment_seconds = 0.0
    reference_seconds = 0.0

    for record in records:
        roots += 1
        assessment = record.get("assessment")
        cost = record["cost"]
        assessment_requests += cost["assessment_requests"]
        reference_requests += cost["reference_requests"]
        assessment_seconds += cost["assessment_seconds"]
        reference_seconds += cost["reference_seconds"]

        if assessment is None:
            root_rejected += 1
            status_counts["root_rejected"] += 1
        else:
            status_counts[assessment["status"]] += 1
            if assessment["status"] == "invalid_evidence":
                invalid_evidence += 1
            if assessment.get("is_engine_damage_admissible"):
                admissible += 1

        witness = record["witness_resolution"]
        witness_sources[witness["witness_source"]] += 1
        if witness["s_units"] is not None:
            roots_with_target += 1
            s_values.append(witness["s_units"])
        if witness["no_r_target_reason"]:
            no_target_reasons[witness["no_r_target_reason"]] += 1

        trajectory = record.get("reference_trajectory")
        if not trajectory:
            continue
        roots_with_trajectory += 1
        diagnostics = trajectory["diagnostics"]
        monotonicity_counts[diagnostics["monotonicity"]] += 1
        sign_changes += diagnostics["sign_change_count"]
        mate_direction_changes += diagnostics["mate_direction_change_count"]
        for step in diagnostics["steps"]:
            delta_g_values.append(step["delta_g_units"])
            witness_drifts.append(step["witness_drift_units"])
            observed_drifts.append(step["observed_drift_units"])
        levels = trajectory["levels"]
        g_last_values.append(levels[-1]["g_ref_units"])
        for level in levels:
            total_reference_levels += 1
            saturation_status_counts[level["saturation_status"]] += 1
            if level["g_ref_units"] < 0:
                negative_g_levels += 1
            per_level_seconds.setdefault(level["level_label"], []).append(
                level["elapsed_seconds"]
            )
            per_level_requests[level["level_label"]] += 2

    return {
        "roots_measured": roots,
        "roots_with_usable_fixed_witness_and_s": roots_with_target,
        "roots_with_reference_trajectory": roots_with_trajectory,
        "roots_lacking_r_target": roots - roots_with_target,
        "roots_rejected_before_assessment": root_rejected,
        "invalid_evidence_count": invalid_evidence,
        "engine_damage_admissible_count": admissible,
        "status_distribution": dict(sorted(status_counts.items())),
        "witness_source_distribution": dict(sorted(witness_sources.items())),
        "no_r_target_reasons": dict(sorted(no_target_reasons.items())),
        "s_units": _distribution(s_values),
        "final_reference_g_units": _distribution(g_last_values),
        "consecutive_reference_gap_drift_units": _distribution(delta_g_values),
        "witness_utility_drift_units": _distribution(witness_drifts),
        "observed_utility_drift_units": _distribution(observed_drifts),
        "reference_monotonicity": dict(sorted(monotonicity_counts.items())),
        "reference_saturation_status": dict(sorted(saturation_status_counts.items())),
        "reference_sign_change_steps": sign_changes,
        "reference_mate_direction_change_steps": mate_direction_changes,
        "reference_levels_with_negative_g": negative_g_levels,
        "reference_levels_measured": total_reference_levels,
        "cost": {
            "assessment_requests": assessment_requests,
            "reference_requests": reference_requests,
            "assessment_seconds": round(assessment_seconds, 3),
            "reference_seconds": round(reference_seconds, 3),
            "reference_seconds_per_root": (
                round(reference_seconds / roots_with_trajectory, 3)
                if roots_with_trajectory
                else None
            ),
            "reference_seconds_per_move_evaluation": (
                round(reference_seconds / (total_reference_levels * 2), 4)
                if total_reference_levels
                else None
            ),
            "per_reference_level": {
                label: {
                    "levels_measured": len(values),
                    "move_evaluations": per_level_requests[label],
                    "total_seconds": round(sum(values), 3),
                    "mean_seconds": round(sum(values) / len(values), 4),
                }
                for label, values in sorted(per_level_seconds.items())
            },
        },
    }


def _distribution(values: list[int]) -> dict:
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
        "mean": round(sum(ordered) / len(ordered), 2),
        "negative_count": sum(1 for value in ordered if value < 0),
        "zero_count": sum(1 for value in ordered if value == 0),
    }


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


def _table(rows: Iterable[tuple[str, object]]) -> str:
    lines = ["| key | value |", "| --- | --- |"]
    for key, value in rows:
        lines.append(f"| {key} | {value} |")
    return "\n".join(lines)


def _counter_table(title: str, mapping: dict) -> str:
    if not mapping:
        return f"**{title}:** none recorded.\n"
    lines = [f"**{title}**\n", "| key | count |", "| --- | --- |"]
    for key, value in mapping.items():
        lines.append(f"| {key} | {value} |")
    return "\n".join(lines) + "\n"


def _distribution_table(title: str, distribution: dict) -> str:
    if distribution.get("count", 0) == 0:
        return f"**{title}:** no values recorded.\n"
    order = ("count", "min", "p25", "median", "p75", "max", "mean", "negative_count", "zero_count")
    lines = [f"**{title}**\n", "| statistic | value |", "| --- | --- |"]
    for key in order:
        lines.append(f"| {key} | {distribution[key]} |")
    return "\n".join(lines) + "\n"


def render_markdown(manifest: dict, summary: dict, sampling: dict) -> str:
    declaration = manifest["source_sampling_declaration"]
    ladder = ", ".join(
        f"{level['label']}={level['nodes']}" for level in manifest["reference_ladder"]
    )
    budgets = manifest["production_budgets"]
    environment = manifest["environment"]
    runtime = manifest.get("runtime") or {}

    parts: list[str] = []
    parts.append("# Step 4C.0 -- Calibration Evidence Pilot Report\n")
    parts.append("## What this report does NOT claim\n")
    parts.append(
        "- **NO epsilon was calibrated.**\n"
        "- **NO tau was calibrated.**\n"
        "- **NO ML model was trained.**\n"
        "- **NO production reliability claim is made.**\n"
        "- **NO personal games were used for fitting.**\n"
        "- `R = S - G_reference` was **not** computed: C0 does not freeze a "
        "reference protocol. Only the stronger-level trajectory is recorded.\n"
        "- The Step 4B policy below is an **experimental** policy. Its "
        "thresholds and budgets are pilot inputs, not production values.\n"
        "- The Step 4B policy is **UNCALIBRATED**, so no root in this run "
        "reached `DAMAGE_SUPPORTED` and no root is engine-damage admissible. "
        "Every `S` below was re-derived from a recorded trace "
        "(`rederived_from_trace`) and is a RESEARCH MEASUREMENT, **not** an "
        "accepted `EngineRegret` conclusion.\n"
    )
    parts.append(f"\n> {C0_SCOPE_NOTE}\n")

    parts.append("\n## Run identity\n")
    parts.append(
        _table(
            [
                ("run_id", manifest["run_id"]),
                ("code_commit", manifest["code_commit"]),
                ("code_tree_dirty", manifest["code_tree_dirty"]),
                ("started_at", manifest["started_at"]),
                ("finished_at", manifest["finished_at"]),
                ("source_identifier", manifest["source_identifier"]),
                ("sampling_source_id", manifest["sampling_source_id"]),
                ("game_identity_scheme", manifest["game_identity_scheme"]),
                ("seed", manifest["seed"]),
                ("sample_target", manifest["sample_target"]),
                ("threads", manifest["threads"]),
                ("hash_mb", manifest["hash_mb"]),
                ("engine_uci_name", manifest["engine_uci_name"]),
                ("engine_executable_sha256", manifest["engine_executable_sha256"]),
                ("engine_eval_file", manifest["engine_eval_file"]),
                ("position_semantics_version", manifest["position_semantics_version"]),
                ("evidence_contract_version", manifest["evidence_contract_version"]),
                (
                    "assessment_semantics_version",
                    manifest["assessment_semantics_version"],
                ),
                ("production_policy_id", manifest["production_policy"]["policy_id"]),
                (
                    "production_policy_fingerprint",
                    manifest["production_policy"]["fingerprint"],
                ),
                (
                    "production_budgets",
                    f"B1={budgets['b1_nodes']}, B2={budgets['b2_nodes']}, "
                    f"B3={budgets['b3_nodes']}",
                ),
                ("audited_production_nodes", manifest["audited_production_nodes"]),
                (
                    "step_4b_calibration_status",
                    manifest["step_4b_calibration_status"],
                ),
                (
                    "damage_conclusions_permitted",
                    manifest["damage_conclusions_permitted"],
                ),
                ("reference_ladder", ladder),
                ("replay_only", manifest["replay_only"]),
            ]
        )
    )

    parts.append("\n\n## Source sampling declaration\n")
    parts.append(
        f"> **Reproducibility.** {manifest['sampling_reproducibility_guarantee']}\n\n"
    )
    if declaration.get("scanned_prefix_declared"):
        parts.append(
            "> **This run sampled only a declared prefix/window of the source "
            "stream.** It is a feasibility sample. It is **NOT** representative "
            "of the complete source population, and must never be described as "
            "representative of the complete Lichess population.\n\n"
        )
    else:
        parts.append(
            "> The scan reached the end of the supplied stream. This is still a "
            "feasibility sample of whatever that stream contained, not a "
            "population-representative calibration claim.\n\n"
        )
    parts.append(
        _table(
            [
                (key, value)
                for key, value in sorted(declaration.items())
            ]
        )
    )

    parts.append("\n\n## Sampling counts and rejection reasons\n")
    parts.append(
        _table(
            [
                ("records_scanned", sampling["records_scanned"]),
                ("games_accepted_by_filters", sampling["games_accepted_by_filters"]),
                ("decisions_sampled", sampling["decisions_sampled"]),
                ("candidate_roots_examined", sampling["candidate_roots_examined"]),
                ("candidate_roots_eligible", sampling["candidate_roots_eligible"]),
            ]
        )
    )
    parts.append("\n\n")
    parts.append(_counter_table("Game-level rejections", sampling["game_rejections"]))
    parts.append("\n")
    parts.append(
        _counter_table(
            "Candidate-root rejections (terminal / one-legal-move / non-canonical)",
            sampling["root_rejections"],
        )
    )

    parts.append("\n## Step 4B (low-cost) results\n")
    parts.append(
        _table(
            [
                ("roots_measured", summary["roots_measured"]),
                (
                    "roots with usable fixed witness / S",
                    summary["roots_with_usable_fixed_witness_and_s"],
                ),
                (
                    "roots with a reference trajectory",
                    summary["roots_with_reference_trajectory"],
                ),
                ("roots lacking an R target", summary["roots_lacking_r_target"]),
                (
                    "roots rejected before assessment",
                    summary["roots_rejected_before_assessment"],
                ),
                ("INVALID_EVIDENCE count", summary["invalid_evidence_count"]),
                (
                    "engine-damage-admissible count",
                    summary["engine_damage_admissible_count"],
                ),
            ]
        )
    )
    parts.append("\n\n")
    parts.append(_counter_table("Step 4B status distribution", summary["status_distribution"]))
    parts.append("\n")
    parts.append(_counter_table("Witness source", summary["witness_source_distribution"]))
    parts.append("\n")
    parts.append(
        _counter_table("Roots lacking an R target, and why", summary["no_r_target_reasons"])
    )

    parts.append("\n## S (qualifying EngineRegret, Step 4B)\n")
    parts.append(_distribution_table("S units", summary["s_units"]))

    parts.append("\n## Reference trajectories (G_ref)\n")
    parts.append(
        _distribution_table(
            "Final-level G_ref units", summary["final_reference_g_units"]
        )
    )
    parts.append("\n")
    parts.append(
        _distribution_table(
            "Consecutive reference-gap drift (delta G between levels)",
            summary["consecutive_reference_gap_drift_units"],
        )
    )
    parts.append("\n")
    parts.append(
        _distribution_table(
            "Witness utility drift", summary["witness_utility_drift_units"]
        )
    )
    parts.append("\n")
    parts.append(
        _distribution_table(
            "Observed-move utility drift", summary["observed_utility_drift_units"]
        )
    )
    parts.append("\n")
    parts.append(_counter_table("Reference monotonicity", summary["reference_monotonicity"]))
    parts.append("\n")
    parts.append(
        _counter_table(
            "WDL/utility saturation per reference level",
            summary["reference_saturation_status"],
        )
    )
    parts.append("\n")
    parts.append(
        _table(
            [
                ("reference levels measured", summary["reference_levels_measured"]),
                (
                    "levels with negative G_ref (preserved, never clamped)",
                    summary["reference_levels_with_negative_g"],
                ),
                ("sign-change steps", summary["reference_sign_change_steps"]),
                (
                    "mate-direction-change steps",
                    summary["reference_mate_direction_change_steps"],
                ),
            ]
        )
    )
    parts.append(
        "\n\nThese are diagnostics. C0 defines no convergence threshold and "
        "issues no convergence verdict.\n"
    )

    parts.append("\n## Cost and cache reuse\n")
    cost = summary["cost"]
    parts.append(
        _table(
            [
                ("assessment engine requests", cost["assessment_requests"]),
                ("reference engine requests", cost["reference_requests"]),
                ("assessment wall seconds", cost["assessment_seconds"]),
                ("reference wall seconds", cost["reference_seconds"]),
                ("reference seconds per root", cost["reference_seconds_per_root"]),
                (
                    "reference seconds per move evaluation",
                    cost["reference_seconds_per_move_evaluation"],
                ),
                ("engine analyses actually run", runtime.get("engine_analyses")),
                ("evaluation requests issued", runtime.get("evaluation_requests")),
                ("cache hits (requests - analyses)", runtime.get("cache_hits")),
                ("cache hit rate", runtime.get("cache_hit_rate")),
                ("cache database", runtime.get("cache_db")),
            ]
        )
    )
    parts.append("\n\n**Per reference level**\n\n")
    parts.append("| level | levels measured | move evaluations | total s | mean s |\n")
    parts.append("| --- | --- | --- | --- | --- |\n")
    for label, stats in cost["per_reference_level"].items():
        parts.append(
            f"| {label} | {stats['levels_measured']} | {stats['move_evaluations']} | "
            f"{stats['total_seconds']} | {stats['mean_seconds']} |\n"
        )
    if runtime.get("engine_analyses_per_level"):
        parts.append("\n**Engine analyses actually run, per bound level**\n\n")
        parts.append("| level | nodes | analyses |\n")
        parts.append("| --- | --- | --- |\n")
        for entry in runtime["engine_analyses_per_level"]:
            parts.append(
                f"| {entry['label']} | {entry['nodes']} | {entry['analyses']} |\n"
            )

    parts.append("\n## Measured hardware / runtime\n")
    parts.append(
        _table(
            [
                ("platform", environment["platform"]),
                ("processor", environment["processor"] or "(not reported)"),
                ("cpu_count", environment["cpu_count"]),
                ("python", environment["python"]),
                ("python-chess", environment["python_chess"]),
                ("threads (configured)", manifest["threads"]),
                ("hash_mb (configured)", manifest["hash_mb"]),
                ("total wall seconds", runtime.get("total_seconds")),
            ]
        )
    )
    parts.append(
        "\n\nThreads is 1 by default for reproducibility; the actual configured "
        "threads and hash are recorded above and in the manifest. Step 4A's "
        "`EngineAnalysisConfig` defaults were not changed.\n"
    )
    return "".join(parts)
