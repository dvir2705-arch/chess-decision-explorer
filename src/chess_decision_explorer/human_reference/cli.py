"""Command-line entry point for the Step 5C-lite human-reference comparison.

Source-agnostic: the reference corpus is a standard PGN text stream, read
from stdin (the default) or from a path. No decompression exists anywhere in
this project; a compressed corpus is decompressed by an external pipe, which
also keeps it out of memory::

    zstd -dc lichess_db_standard_rated_2024-01.pgn.zst \\
      | .venv/bin/python -m chess_decision_explorer.human_reference \\
          --personal-pgn data/personal --player <name> --cohort rapid \\
          --source - --source-id lichess_db_standard_rated_2024-01 \\
          --target-games 10000

Reproducibility rests on the declared `--source-id`: it names the corpus the
run is reproducible against and it forms each record's transient game id
(`source_id:ordinal`). A stdin stream has no name of its own, so
`--source-id` is REQUIRED with `--source -`, following the Step 4C.0
precedent; a file source defaults to its unambiguous file stem. The corpus is
declared, never hashed.

`--target-games` counts **eligible accepted** games, not records read off the
stream. A run that exhausts its input first accepts fewer, and says so
everywhere: in the manifest (`target_met: false`), in the summary, and in the
first line of the report.

A run never overwrites another run: a directory already holding run
artefacts is refused before anything is written, and there is no `--force`.
"""

from __future__ import annotations

import argparse
import resource
import sys
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..ingestion import iter_pgn_records
from ..personal import (
    PersonalAnalysisPolicy,
    TimeControlCategory,
    build_personal_analysis,
)
from .compare import build_comparison
from .manifest import ReferenceRunManifest, git_commit, utc_now
from .report import (
    MOVE_COLUMNS,
    POSITION_COLUMNS,
    move_rows,
    position_rows,
    render_markdown,
    summarize,
    write_csv,
    write_json,
)
from .scan import ReferenceEligibilityFilters, ReferenceScanner
from .target import DEFAULT_MIN_DISTINCT_GAMES, RecurringTargetSet, build_target_set

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_ROOT = _REPO_ROOT / "reference_runs"

RUN_ARTEFACTS: tuple[str, ...] = (
    "reference_manifest.json",
    "reference_summary.json",
    "positions.csv",
    "moves.csv",
    "report.md",
    "target_set.json",
)
"""Everything a run writes into its run directory. A directory holding any of
these is an existing run -- completed or interrupted -- and is immutable."""

_CATEGORIES = {
    "rapid": TimeControlCategory.RAPID,
    "blitz": TimeControlCategory.BLITZ,
    "bullet": TimeControlCategory.BULLET,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="human-reference",
        description=(
            "Step 5C-lite: compare the player's recurring positions against "
            "external human reference games. Human evidence only -- no "
            "engine evaluation, no calibration, no significance claim."
        ),
    )

    personal = parser.add_argument_group("personal target set")
    personal.add_argument(
        "--personal-pgn",
        nargs="+",
        default=None,
        help="PGN files and/or directories of *.pgn holding the player's own "
        "games. Mutually exclusive with --target-set.",
    )
    personal.add_argument(
        "--player",
        default=None,
        help="The player's name as it appears in the personal PGN headers. "
        "Required with --personal-pgn.",
    )
    personal.add_argument(
        "--cohort",
        choices=sorted(_CATEGORIES),
        default=None,
        help="Which personal time-control cohort defines the recurring "
        "positions. Required with --personal-pgn. The three personal cohort "
        "indexes are never merged, so one run compares exactly one cohort.",
    )
    personal.add_argument(
        "--min-distinct-games",
        type=int,
        default=DEFAULT_MIN_DISTINCT_GAMES,
        help="Recurring-position threshold: minimum number of DISTINCT "
        "personal games containing the position (default: "
        f"{DEFAULT_MIN_DISTINCT_GAMES}). Repeats inside one personal game "
        "are not sufficient recurrence by themselves.",
    )
    personal.add_argument(
        "--target-set",
        default=None,
        help="Load a previously exported target_set.json instead of building "
        "one from personal PGNs. Mutually exclusive with --personal-pgn.",
    )
    personal.add_argument(
        "--build-target-set-only",
        action="store_true",
        help="Build and write target_set.json, then stop without scanning "
        "any reference corpus.",
    )

    source = parser.add_argument_group("reference source")
    source.add_argument(
        "--source",
        default="-",
        help="Reference PGN path, or '-' for stdin (default). Decompress "
        "externally, e.g. `zstd -dc corpus.pgn.zst | ...`.",
    )
    source.add_argument(
        "--source-id",
        default=None,
        help="Identifier for the reference corpus. REQUIRED with "
        "'--source -' (a stdin stream has no name of its own); defaults to "
        "the file stem for a path source.",
    )
    source.add_argument(
        "--target-games",
        type=int,
        default=None,
        help="Number of ELIGIBLE reference games to accept. Not a record "
        "count: the scan keeps reading until this many games pass every "
        "filter, or until the input is exhausted. Required unless "
        "--build-target-set-only.",
    )
    source.add_argument("--max-records-scanned", type=int, default=None)
    source.add_argument("--skip-records", type=int, default=0)

    filters = parser.add_argument_group("reference eligibility filters")
    filters.add_argument(
        "--categories",
        nargs="+",
        choices=sorted(_CATEGORIES),
        default=["rapid", "blitz"],
    )
    filters.add_argument(
        "--no-require-rated",
        dest="require_rated",
        action="store_false",
        help="Accept games whose PGN Event does not say 'rated'. Recorded in "
        "the manifest.",
    )
    filters.add_argument(
        "--allow-bot-players",
        dest="require_human_players",
        action="store_false",
        help="Accept games where a side is a Lichess Bot API account "
        "(WhiteTitle/BlackTitle == BOT). Off by default: this is a HUMAN "
        "reference cohort. Titled human players (GM, IM, FM, ...) are never "
        "affected by this filter. Recorded in the manifest.",
    )
    filters.add_argument("--min-rating", type=int, default=None)
    filters.add_argument("--max-rating", type=int, default=None)
    filters.add_argument(
        "--require-both-ratings",
        action="store_true",
        help="Reject a game unless both players carry a rating header, even "
        "when no rating band is configured.",
    )
    filters.add_argument("--min-plies", type=int, default=1)
    filters.add_argument(
        "--exclude-termination",
        nargs="+",
        default=[],
        metavar="TERMINATION",
        help="PGN Termination values to exclude (case-insensitive). Empty by "
        "default: the scan filters only on what was asked for.",
    )

    output = parser.add_argument_group("output")
    output.add_argument("--out-dir", default=str(DEFAULT_OUTPUT_ROOT))
    output.add_argument(
        "--run-id",
        default=None,
        help="Run directory name under --out-dir. A directory that already "
        "holds run artefacts is never reused or overwritten, and there is no "
        "--force.",
    )
    return parser


def resolve_source_id(source: str, declared: str | None, from_stdin: bool) -> str:
    """The identifier this run is reproducible against.

    A stdin stream has no name of its own, so defaulting it to `"stdin"`
    would bind every unrelated stdin run to the same placeholder. It must be
    declared. A path source has an unambiguous name, so its file stem is the
    default.
    """
    if declared is not None:
        if not declared.strip():
            raise SystemExit("--source-id must not be empty or whitespace-only")
        return declared
    if from_stdin:
        raise SystemExit(
            "--source-id is required when reading from stdin ('--source -'): "
            "a stdin stream has no name of its own, and the corpus must be "
            "declared for the run to be reproducible, e.g. "
            "--source-id lichess_db_standard_rated_2024-01."
        )
    stem = Path(source).stem
    if not stem:
        raise SystemExit(
            f"could not derive a source id from {source!r}; pass --source-id "
            f"explicitly"
        )
    return stem


def refuse_existing_run(run_dir: Path) -> None:
    """Refuse to write into a directory that already holds a run.

    Run artefacts are evidence. A completed run and an interrupted one both
    leave files behind, and overwriting either would silently replace the
    record a manifest claims to describe.
    """
    if not run_dir.exists():
        return
    if not run_dir.is_dir():
        raise SystemExit(f"run output path {run_dir} exists and is not a directory")
    existing = [name for name in RUN_ARTEFACTS if (run_dir / name).exists()]
    if existing:
        raise SystemExit(
            f"run directory {run_dir} already contains run artefacts "
            f"({', '.join(existing)}). Run artefacts are immutable and there "
            f"is no --force. Use a different --run-id."
        )


def discover_personal_pgns(entries: list[str]) -> list[Path]:
    """Every PGN path named, with directories expanded to sorted `*.pgn`.

    Sorted, so the personal ingestion order -- and therefore the personal
    game ids -- is the same on every run over the same files.
    """
    paths: list[Path] = []
    for entry in entries:
        path = Path(entry)
        if path.is_dir():
            paths.extend(sorted(path.glob("*.pgn")))
        elif path.exists():
            paths.append(path)
        else:
            raise SystemExit(f"personal PGN path does not exist: {entry}")
    if not paths:
        raise SystemExit(f"no PGN files found under {entries!r}")
    return paths


def build_personal_target_set(args: argparse.Namespace) -> RecurringTargetSet:
    """Ingest the personal PGNs and select the recurring positions.

    Reuses the existing personal pipeline unchanged: strict
    `iter_pgn_records`, then `build_personal_analysis`, then the selected
    cohort's `PositionIndex`. Nothing about personal semantics is redefined
    here.
    """
    if not args.player:
        raise SystemExit("--player is required with --personal-pgn")
    if not args.cohort:
        raise SystemExit("--cohort is required with --personal-pgn")

    paths = discover_personal_pgns(args.personal_pgn)
    category = _CATEGORIES[args.cohort]

    def personal_games():
        for path in paths:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                yield from iter_pgn_records(handle, path.stem)

    analysis = build_personal_analysis(
        personal_games(), args.player, PersonalAnalysisPolicy()
    )
    cohort = analysis.cohort(category)
    totals = analysis.totals

    declaration = {
        "personal_pgn_files": [str(path.resolve()) for path in paths],
        "personal_pgn_file_count": len(paths),
        "personal_games_seen": totals.total_games_seen,
        "cohort_games_seen": cohort.stats.games_seen,
        "cohort_core_eligible_games": cohort.stats.core_eligible_games,
        "cohort_indexed_games": cohort.stats.indexed_games,
        "cohort_personal_decisions": cohort.stats.personal_decisions,
        "cohort_distinct_positions": len(cohort.index),
        "eligibility_policy": "PersonalAnalysisPolicy() v1 defaults",
    }

    return build_target_set(
        cohort.index,
        cohort=args.cohort,
        player_label=args.player,
        min_distinct_games=args.min_distinct_games,
        personal_source_declaration=declaration,
    )


def resolve_target_set(args: argparse.Namespace) -> RecurringTargetSet:
    if bool(args.personal_pgn) == bool(args.target_set):
        raise SystemExit(
            "pass exactly one of --personal-pgn (build the target set from "
            "personal PGNs) or --target-set (load a previously exported one)"
        )
    if args.target_set:
        return RecurringTargetSet.read_json(Path(args.target_set))
    return build_personal_target_set(args)


def _peak_memory_mb() -> float | None:
    """Approximate peak RSS of this process, in MB.

    `ru_maxrss` is kilobytes on Linux. Approximate by nature -- it is the
    high-water mark of the whole process, interpreter included -- and
    reported as such.
    """
    try:
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except (OSError, ValueError):  # pragma: no cover - defensive only
        return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.min_distinct_games < 1:
        raise SystemExit("--min-distinct-games must be at least 1")
    if not args.build_target_set_only and args.target_games is None:
        raise SystemExit(
            "--target-games is required (the number of ELIGIBLE reference "
            "games to accept), unless --build-target-set-only is passed"
        )

    from_stdin = args.source == "-"
    run_id = args.run_id or (
        "href-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    run_dir = Path(args.out_dir) / run_id
    refuse_existing_run(run_dir)

    build_start = time.perf_counter()
    target_set = resolve_target_set(args)
    build_seconds = time.perf_counter() - build_start
    if not target_set.positions:
        raise SystemExit(
            f"no recurring positions: no position in cohort "
            f"{target_set.cohort!r} appears in at least "
            f"{target_set.min_distinct_games} distinct personal games"
        )

    run_dir.mkdir(parents=True, exist_ok=True)
    target_set.write_json(run_dir / "target_set.json")

    if args.build_target_set_only:
        print(f"target set written to {run_dir / 'target_set.json'}")
        print(f"  cohort: {target_set.cohort}")
        print(f"  recurring positions: {len(target_set):,}")
        print(f"  build seconds: {build_seconds:.3f}")
        return 0

    source_id = resolve_source_id(args.source, args.source_id, from_stdin)
    filters = ReferenceEligibilityFilters(
        eligible_categories=tuple(_CATEGORIES[name] for name in args.categories),
        require_rated=args.require_rated,
        require_human_players=args.require_human_players,
        min_plies=args.min_plies,
        min_rating=args.min_rating,
        max_rating=args.max_rating,
        require_both_ratings=args.require_both_ratings,
        excluded_terminations=frozenset(
            value.strip().casefold() for value in args.exclude_termination
        ),
    )
    scanner = ReferenceScanner(
        target_set=target_set,
        source_id=source_id,
        target_games=args.target_games,
        filters=filters,
        max_records_scanned=args.max_records_scanned,
        skip_records=args.skip_records,
    )

    commit, dirty = git_commit(_REPO_ROOT)
    manifest = ReferenceRunManifest(
        run_id=run_id,
        code_commit=commit,
        code_tree_dirty=dirty,
        source_id=source_id,
        source_declaration={
            "stream": "stdin" if from_stdin else str(Path(args.source).resolve()),
            "decompression": (
                "external pipe only; no decompression exists in this project"
            ),
            "corpus_hashed": False,
        },
        eligibility_filters=filters.as_manifest_dict(),
        target_eligible_games=args.target_games,
        scanner_declaration=scanner.as_manifest_dict(),
        target_set_declaration=target_set.as_manifest_dict(),
        started_at=utc_now(),
    )
    manifest_path = run_dir / "reference_manifest.json"
    manifest.write(manifest_path)

    scan_start = time.perf_counter()
    with ExitStack() as stack:
        handle = (
            sys.stdin
            if from_stdin
            else stack.enter_context(
                open(args.source, "r", encoding="utf-8", errors="replace")
            )
        )
        scan_result = scanner.scan(handle)
    scan_seconds = time.perf_counter() - scan_start

    comparisons = build_comparison(target_set, scan_result.index)
    summary = summarize(comparisons, scan_result)

    written_positions = write_csv(
        run_dir / "positions.csv", POSITION_COLUMNS, position_rows(comparisons)
    )
    written_moves = write_csv(
        run_dir / "moves.csv", MOVE_COLUMNS, move_rows(comparisons)
    )
    write_json(run_dir / "reference_summary.json", summary)

    counters = scan_result.counters
    manifest.finished_at = utc_now()
    manifest.scan_result = scan_result.as_dict()
    manifest.runtime = {
        "target_set_build_seconds": round(build_seconds, 3),
        "scan_seconds": round(scan_seconds, 3),
        "records_scanned_per_second": _per_second(
            counters.records_scanned, scan_seconds
        ),
        "eligible_games_per_second": _per_second(
            counters.games_accepted, scan_seconds
        ),
        "decisions_examined_per_second": _per_second(
            counters.decisions_examined, scan_seconds
        ),
        "header_scan_seconds": (
            round(scanner.stream.header_seconds, 3) if scanner.stream else None
        ),
        "full_parse_seconds": (
            round(scanner.stream.parse_seconds, 3) if scanner.stream else None
        ),
        "records_header_rejected": counters.records_header_rejected,
        "records_fully_parsed": counters.records_fully_parsed,
        "approx_peak_memory_mb": _peak_memory_mb(),
        "aggregate_positions": len(scan_result.index),
        "aggregate_move_entries": sum(
            len(stats.decisions) for _, stats in scan_result.index.items()
        ),
        "positions_csv_rows": written_positions,
        "moves_csv_rows": written_moves,
    }
    manifest.write(manifest_path)

    manifest_data = manifest.as_dict()
    (run_dir / "report.md").write_text(
        render_markdown(manifest_data, summary), encoding="utf-8"
    )

    print(f"human-reference run written to {run_dir}")
    print(f"  records scanned:          {counters.records_scanned:,}")
    print(
        f"  ELIGIBLE games accepted:  {counters.games_accepted:,} "
        f"of target {args.target_games:,}"
    )
    if not scan_result.target_met:
        print(
            f"  TARGET NOT MET (shortfall {scan_result.shortfall:,}); "
            f"scan ended: {scan_result.termination.value}. This run is NOT a "
            f"{args.target_games:,}-game reference dataset."
        )
    print(f"  recurring positions:      {len(target_set):,}")
    print(f"  positions matched:        {scan_result.matched_positions:,}")
    print(f"  decisions examined:       {counters.decisions_examined:,}")
    print(f"  decisions matched:        {counters.decisions_matched:,}")
    print(f"  scan seconds:             {scan_seconds:.3f}")
    return 0


def _per_second(count: int, seconds: float) -> float | None:
    if seconds <= 0:
        return None
    return round(count / seconds, 2)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
