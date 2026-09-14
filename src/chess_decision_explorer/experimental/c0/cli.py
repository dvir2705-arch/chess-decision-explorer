"""Command-line entry point for the Step 4C.0 calibration-evidence pilot.

EXPERIMENTAL. Source-agnostic: the pilot reads a standard PGN text stream
from stdin (the default) or from a path. No Zstandard handling exists
anywhere in this project; a compressed corpus is decompressed by an external
pipe, which also keeps the corpus out of memory::

    zstd -dc lichess_db_standard_rated_2024-01.pgn.zst \\
      | .venv/bin/python -m chess_decision_explorer.experimental.c0 ...

Every threshold and every node budget is a REQUIRED argument. Step 4B defines
no production defaults, so this pilot invents none: a run is only ever
reproducible against the exact values recorded in its manifest.

Reproducibility rests on the declared `--source-id`: it is an INPUT to every
deterministic sampling draw, not just provenance, so the same roots come back
only from the same `source_id` over the same source ordering and content, with
the same seed and the same sampling configuration. A stdin stream has no name
of its own, so `--source-id` is REQUIRED when `--source -` is used; a file
source defaults to its unambiguous file stem. C0 does not hash the corpus.

A run never overwrites another run: if the target run directory already holds
C0 run artefacts the run is refused before anything is written, and there is
no `--force`. A replay therefore uses a DIFFERENT `--run-id` from the
measurement it replays, with the same source, cache database, policy and
ladder -- both runs may, and should, share one `--cache-db`.

For a first real benchmark on an ordinary development machine, keep
`--threads 1 --hash-mb 64` (the defaults): `build_engine_pool` starts ONE
Stockfish process per bound level -- B1, B2, any B3, every reference level and
the optional discovery audit -- and each process holds its own hash table, so
the configured hash is paid per level, not once. Raise it only after the
actual memory footprint of a run has been measured.

The Step 4B policy a pilot run builds is **always UNCALIBRATED**, and there
is no switch to change that. C0 measures; it does not calibrate. Step 4B is
left free to answer ``INCONCLUSIVE`` / ``UNCALIBRATED_POLICY``, so a pilot
run can never emit ``DAMAGE_SUPPORTED``, can never produce an admissible
``EngineRegret``, and can never present experimental epsilon/tau inputs as a
production conclusion. The research quantity `S` comes only from the narrow
trace re-derivation, tagged ``rederived_from_trace``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from ...assessment import (
    CalibrationStatus,
    ComparisonPolicy,
    SearchLevel,
)
from ...engine_cache import (
    InMemoryEvaluationCache,
    SQLiteEvaluationCache,
    TieredEvaluationCache,
)
from ...personal import TimeControlCategory
from . import C0_SCOPE_NOTE
from .manifest import RunManifest, git_commit, utc_now
from .pilot import build_engine_pool, run_pilot
from .reference import validate_reference_ladder
from .report import (
    REFERENCE_LEVEL_COLUMNS,
    ROOT_SUMMARY_COLUMNS,
    read_records,
    reference_level_rows,
    render_markdown,
    root_summary_row,
    summarize,
    write_csv,
)
from .sampling import EligibilityFilters, PilotSampler

_REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_OUTPUT_ROOT = _REPO_ROOT / "experiments" / "runs"
DEFAULT_CACHE_DB = _REPO_ROOT / "experiments" / "cache" / "c0_evaluations.sqlite"

C0_RUN_ARTEFACTS: tuple[str, ...] = (
    "manifest.json",
    "roots.jsonl",
    "summary.json",
    "roots_summary.csv",
    "reference_levels.csv",
    "report.md",
)
"""Everything a C0 run writes into its run directory.

A run directory holding any of these is an existing run -- completed or
interrupted -- and is immutable: see :func:`refuse_existing_run`.
"""

_CATEGORIES = {
    "rapid": TimeControlCategory.RAPID,
    "blitz": TimeControlCategory.BLITZ,
    "bullet": TimeControlCategory.BULLET,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="c0-pilot",
        description=(
            "Step 4C.0 calibration-evidence pilot (EXPERIMENTAL). "
            "Calibrates nothing; trains nothing; claims nothing."
        ),
        epilog=C0_SCOPE_NOTE,
    )

    source = parser.add_argument_group("source")
    source.add_argument(
        "--source",
        default="-",
        help="PGN path, or '-' for stdin (default). Decompress externally, "
        "e.g. `zstd -dc corpus.pgn.zst | ...`.",
    )
    source.add_argument(
        "--source-id",
        default=None,
        help="Source identifier used for game ids and for every deterministic "
        "sampling draw (game identity is source_id + ':' + zero-based source "
        "ordinal). REQUIRED with '--source -', because a stdin stream has no "
        "unambiguous name; defaults to the file stem for a path source. "
        "Recorded in the manifest as `sampling_source_id`.",
    )

    sampling = parser.add_argument_group("sampling")
    sampling.add_argument("--seed", type=int, required=True)
    sampling.add_argument("--sample-size", type=int, required=True)
    sampling.add_argument("--min-plies", type=int, required=True)
    sampling.add_argument("--min-rating", type=int, default=None)
    sampling.add_argument("--max-rating", type=int, default=None)
    sampling.add_argument(
        "--categories",
        nargs="+",
        choices=sorted(_CATEGORIES),
        default=["rapid", "blitz"],
    )
    sampling.add_argument(
        "--no-require-rated",
        dest="require_rated",
        action="store_false",
        help="Accept games whose PGN Event does not say 'rated' (for "
        "non-Lichess sources). Recorded in the manifest.",
    )
    sampling.add_argument("--accept-rate", type=float, default=1.0)
    sampling.add_argument("--max-records-scanned", type=int, default=None)
    sampling.add_argument("--skip-records", type=int, default=0)

    policy = parser.add_argument_group("experimental Step 4B policy (no defaults)")
    policy.add_argument("--b1-nodes", type=int, required=True)
    policy.add_argument("--b2-nodes", type=int, required=True)
    policy.add_argument("--b3-nodes", type=int, default=None)
    policy.add_argument("--epsilon-units", type=int, required=True)
    policy.add_argument("--tau-units", type=int, required=True)
    policy.add_argument("--max-gap-drift-units", type=int, required=True)
    policy.add_argument("--max-alternative-drift-units", type=int, required=True)
    policy.add_argument("--max-user-drift-units", type=int, required=True)
    policy.add_argument("--material-negative-gap-units", type=int, required=True)
    policy.add_argument("--max-active-alternatives", type=int, default=2)
    policy.add_argument("--max-requests-per-decision", type=int, default=None)
    policy.add_argument("--policy-id", default="c0-pilot-experimental")
    policy.add_argument("--policy-version", default="v0")

    reference = parser.add_argument_group("reference ladder")
    reference.add_argument(
        "--reference-nodes",
        nargs="+",
        type=int,
        required=True,
        help="Ordered, strictly increasing reference node budgets, each "
        "stronger than the production qualification level being audited.",
    )
    reference.add_argument(
        "--discovery-audit-nodes",
        type=int,
        default=None,
        help="Optional C0-only experimental single-PV UNRESTRICTED discovery "
        "diagnostic at this budget. Isolated from all fixed-witness "
        "measurement; never enters a gap. No MultiPV.",
    )

    engine = parser.add_argument_group("engine")
    engine.add_argument("--stockfish", required=True)
    engine.add_argument(
        "--threads",
        type=int,
        default=1,
        help="Threads per bound level (default: 1). Keep 1 for a first "
        "benchmark; one Stockfish process is started per bound level.",
    )
    engine.add_argument(
        "--hash-mb",
        type=int,
        default=64,
        help="Hash table per bound level in MB (default: 64). This is paid "
        "PER LEVEL -- B1/B2/B3, every reference level and the discovery "
        "audit each run their own Stockfish process with their own hash -- "
        "so keep 64 for a first benchmark and raise it only after the actual "
        "memory footprint of a run has been measured.",
    )
    engine.add_argument("--cache-db", default=str(DEFAULT_CACHE_DB))
    engine.add_argument(
        "--replay-only",
        action="store_true",
        help="Fail rather than run any engine analysis. Proves that replaying "
        "stored primitive evidence through the same analysis code reproduces "
        "the same derived measurements.",
    )

    output = parser.add_argument_group("output")
    output.add_argument("--out-dir", default=str(DEFAULT_OUTPUT_ROOT))
    output.add_argument(
        "--run-id",
        default=None,
        help="Run directory name under --out-dir. A directory that already "
        "holds C0 run artefacts is never reused or overwritten, and there is "
        "no --force: a replay uses a DIFFERENT run id from the measurement "
        "it replays (e.g. c0-pilot-8-measure / c0-pilot-8-replay) while "
        "sharing the same --cache-db.",
    )
    return parser


def resolve_source_id(args: argparse.Namespace, from_stdin: bool) -> str:
    """The identifier sampling will actually key every draw on.

    `source_id` is not decoration: game identity is
    ``source_id + ':' + zero_based_source_ordinal`` and every deterministic
    selection digest is keyed on it, so two runs only agree on their sampled
    roots if they agree on this value. A stdin stream carries no name of its
    own, so defaulting it to ``"stdin"`` would silently bind a run's
    reproducibility to a placeholder shared by every unrelated stdin run.
    It must therefore be declared. A path source has an unambiguous name of
    its own, so its file stem remains the default.
    """
    if args.source_id is not None:
        if not args.source_id.strip():
            raise SystemExit("--source-id must not be empty or whitespace-only")
        return args.source_id
    if from_stdin:
        raise SystemExit(
            "--source-id is required when reading from stdin ('--source -'): "
            "it is an input to every deterministic sampling draw (game "
            "identity is source_id + ':' + zero-based source ordinal), and a "
            "stdin stream has no name of its own to derive it from. Declare "
            "the corpus explicitly, e.g. "
            "--source-id lichess_db_standard_rated_2024-01."
        )
    stem = Path(args.source).stem
    if not stem:
        raise SystemExit(
            f"could not derive a source id from {args.source!r}; pass "
            f"--source-id explicitly"
        )
    return stem


def refuse_existing_run(run_dir: Path) -> None:
    """Refuse to write into a run directory that already holds a C0 run.

    Run artefacts are immutable evidence. A completed run and an interrupted
    one both leave files behind, and overwriting either would silently
    replace the record a manifest claims to describe, so a repeated `--run-id`
    is an error rather than a reuse. There is deliberately no `--force`: a
    replay is a SEPARATE run id over the same source, cache database, policy
    and ladder (for example `c0-pilot-8-measure` then `c0-pilot-8-replay`),
    and the two share one `--cache-db`.

    Called before the directory is created and before anything is written, so
    a refused run leaves every existing artefact untouched.
    """
    if not run_dir.exists():
        return
    if not run_dir.is_dir():
        raise SystemExit(f"run output path {run_dir} exists and is not a directory")
    existing = [name for name in C0_RUN_ARTEFACTS if (run_dir / name).exists()]
    if existing:
        raise SystemExit(
            f"run directory {run_dir} already contains C0 run artefacts "
            f"({', '.join(existing)}). Run artefacts are immutable and there "
            f"is no --force. Use a different --run-id; a replay of this run "
            f"keeps the same --source, --cache-db, policy and reference "
            f"ladder but takes its own run id "
            f"(e.g. '{run_dir.name}-replay')."
        )


def build_policy(args: argparse.Namespace) -> ComparisonPolicy:
    """Build the pilot's Step 4B policy. ALWAYS UNCALIBRATED.

    C0 is a measurement experiment, not a calibration. Marking an
    experimental policy CALIBRATED would make Step 4B emit
    ``DAMAGE_SUPPORTED`` and an admissible ``EngineRegret`` off nothing but
    pilot epsilon/tau inputs, which is exactly the false production
    conclusion this pilot must never produce. The policy therefore stays
    UNCALIBRATED and carries no ``calibration_id``: Step 4B is left free to
    return ``INCONCLUSIVE`` / ``UNCALIBRATED_POLICY``, and the research
    quantity `S` is obtained only through the narrow trace re-derivation in
    :func:`~.lowcost.resolve_fixed_witness`, tagged ``rederived_from_trace``.

    There is no CLI switch for this. A CALIBRATED policy can still be built
    directly in a test to check parity between a genuine ``EngineRegret`` and
    the trace re-derivation, but it is never reachable from a pilot run.
    """
    return ComparisonPolicy(
        policy_id=args.policy_id,
        policy_version=args.policy_version,
        b1=SearchLevel("B1", args.b1_nodes),
        b2=SearchLevel("B2", args.b2_nodes),
        b3=SearchLevel("B3", args.b3_nodes) if args.b3_nodes else None,
        epsilon_units=args.epsilon_units,
        tau_units=args.tau_units,
        max_gap_drift_units=args.max_gap_drift_units,
        max_alternative_drift_units=args.max_alternative_drift_units,
        max_user_drift_units=args.max_user_drift_units,
        material_negative_gap_units=args.material_negative_gap_units,
        max_active_alternatives=args.max_active_alternatives,
        max_requests_per_decision=args.max_requests_per_decision,
        calibration_status=CalibrationStatus.UNCALIBRATED,
        calibration_id=None,
    )


def build_ladder(args: argparse.Namespace, policy: ComparisonPolicy) -> tuple:
    audited = (policy.b3 or policy.b2).nodes
    ladder = tuple(
        SearchLevel(f"R{index + 1}", nodes)
        for index, nodes in enumerate(args.reference_nodes)
    )
    return validate_reference_ladder(ladder, audited), audited


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    policy = build_policy(args)
    ladder, audited_nodes = build_ladder(args, policy)
    audit_level = (
        SearchLevel("DISCOVERY_AUDIT", args.discovery_audit_nodes)
        if args.discovery_audit_nodes
        else None
    )
    if audit_level is not None and audit_level.nodes <= audited_nodes:
        raise SystemExit(
            f"--discovery-audit-nodes must exceed the audited production budget "
            f"({audited_nodes})"
        )

    all_levels = list(policy.levels) + list(ladder) + (
        [audit_level] if audit_level is not None else []
    )
    # Every bound level needs its own Step 4A evaluator, and two levels with
    # the same node budget would be indistinguishable to the evaluation cache.
    # Refuse that here, with the offending budget named, rather than letting
    # the pool raise after a run directory has already been created.
    budgets = [level.nodes for level in all_levels]
    duplicates = sorted({nodes for nodes in budgets if budgets.count(nodes) > 1})
    if duplicates:
        raise SystemExit(
            "every bound search level needs a distinct node budget "
            "(B1/B2/B3, the reference ladder, and the discovery audit); "
            f"repeated: {duplicates}"
        )

    filters = EligibilityFilters(
        min_plies=args.min_plies,
        min_rating=args.min_rating,
        max_rating=args.max_rating,
        require_rated=args.require_rated,
        eligible_categories=tuple(_CATEGORIES[name] for name in args.categories),
    )

    from_stdin = args.source == "-"
    source_id = resolve_source_id(args, from_stdin)
    sampler = PilotSampler(
        source_id=source_id,
        filters=filters,
        seed=args.seed,
        sample_size=args.sample_size,
        accept_rate=args.accept_rate,
        max_records_scanned=args.max_records_scanned,
        skip_records=args.skip_records,
    )

    run_id = args.run_id or (
        "c0-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + f"-seed{args.seed}"
    )
    run_dir = Path(args.out_dir) / run_id
    # Before anything is created or written: an existing run is never reused
    # and never overwritten.
    refuse_existing_run(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    cache_path = Path(args.cache_db)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    commit, dirty = git_commit(_REPO_ROOT)
    started = utc_now()
    start_perf = time.perf_counter()
    with ExitStack() as stack:
        persistent = stack.enter_context(SQLiteEvaluationCache(cache_path))
        cache = TieredEvaluationCache(InMemoryEvaluationCache(), persistent)
        pool, counters, evaluators = build_engine_pool(
            args.stockfish,
            all_levels,
            cache,
            threads=args.threads,
            hash_mb=args.hash_mb,
            replay_only=args.replay_only,
        )
        stack.callback(lambda: [evaluator.close() for evaluator in evaluators])

        manifest = RunManifest(
            run_id=run_id,
            code_commit=commit,
            code_tree_dirty=dirty,
            source_identifier=(
                "stdin" if from_stdin else str(Path(args.source).resolve())
            ),
            sampling_source_id=source_id,
            source_sampling_declaration=_declaration(args, sampler, from_stdin),
            seed=args.seed,
            eligibility_filters=filters.as_manifest_dict(),
            sample_target=args.sample_size,
            engine_identity=pool.engine_identity,
            threads=args.threads,
            hash_mb=args.hash_mb,
            policy=policy,
            reference_ladder=ladder,
            audited_production_nodes=audited_nodes,
            discovery_audit_level=audit_level,
            replay_only=args.replay_only,
            started_at=started,
        )
        manifest_path = run_dir / "manifest.json"
        manifest.write(manifest_path)

        handle = (
            sys.stdin
            if from_stdin
            else stack.enter_context(open(args.source, "r", encoding="utf-8", errors="replace"))
        )
        records_path = run_dir / "roots.jsonl"
        written = 0
        requests = 0
        with open(records_path, "w", encoding="utf-8") as out:
            for record in run_pilot(
                sampler.iter_sample(handle),
                pool,
                policy,
                ladder,
                discovery_audit_level=audit_level,
            ):
                payload = record.as_dict()
                out.write(json.dumps(payload, sort_keys=True) + "\n")
                out.flush()
                written += 1
                requests += (
                    payload["cost"]["assessment_requests"]
                    + payload["cost"]["reference_requests"]
                )

        analyses = sum(counter.analyses for counter in counters.values())
        total_seconds = time.perf_counter() - start_perf
        manifest.finished_at = utc_now()
        manifest.scan_termination = sampler.termination.value
        manifest.sampling_counters = sampler.counters.as_dict()
        manifest.source_sampling_declaration = _declaration(args, sampler, from_stdin)
        manifest.runtime = {
            "roots_recorded": written,
            "evaluation_requests": requests,
            "engine_analyses": analyses,
            "cache_hits": requests - analyses,
            "cache_hit_rate": (
                round((requests - analyses) / requests, 4) if requests else None
            ),
            "cache_db": str(cache_path),
            "total_seconds": round(total_seconds, 3),
            "engine_analyses_per_level": [
                {
                    "label": level.label,
                    "nodes": level.nodes,
                    "analyses": counters[level].analyses,
                }
                for level in all_levels
            ],
        }
        manifest.write(manifest_path)

    summary = summarize(read_records(records_path))
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    write_csv(
        run_dir / "roots_summary.csv",
        ROOT_SUMMARY_COLUMNS,
        (root_summary_row(record) for record in read_records(records_path)),
    )
    write_csv(
        run_dir / "reference_levels.csv",
        REFERENCE_LEVEL_COLUMNS,
        (
            row
            for record in read_records(records_path)
            for row in reference_level_rows(record)
        ),
    )
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    (run_dir / "report.md").write_text(
        render_markdown(manifest_data, summary, manifest_data["sampling_counters"]),
        encoding="utf-8",
    )

    print(f"C0 pilot run written to {run_dir}")
    print(f"  roots recorded: {summary['roots_measured']}")
    print(
        "  with usable fixed witness / S: "
        f"{summary['roots_with_usable_fixed_witness_and_s']}"
    )
    print(f"  scan termination: {sampler.termination.value}")
    return 0


def _declaration(args, sampler: PilotSampler, from_stdin: bool) -> dict:
    declaration = {
        "stream": "stdin" if from_stdin else str(Path(args.source).resolve()),
        "decompression": "external pipe only; no Zstandard handling in this project",
        "scan_termination": sampler.termination.value,
        "scanned_prefix_declared": sampler.termination.is_declared_prefix,
        "records_scanned": sampler.counters.records_scanned,
        "representativeness": (
            "FEASIBILITY SAMPLE ONLY. Not representative of the complete source "
            "population, and never to be described as representative of the "
            "complete Lichess population."
        ),
    }
    declaration.update(sampler.as_manifest_dict())
    return declaration


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
