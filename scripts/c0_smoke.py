"""Real-Stockfish smoke command for the Step 4C.0 calibration-evidence pilot.

EXPERIMENTAL, manual, and not part of the mandatory pytest suite. It runs the
complete pilot -- deterministic sampling, the real Step 4B path, and a
stronger same-root same-witness reference ladder -- end to end over a tiny
built-in PGN stream, so the wiring can be checked against a real engine
without a corpus::

    .venv/bin/python scripts/c0_smoke.py --stockfish /path/to/stockfish

Budgets below are deliberately tiny so the smoke run is quick. They are NOT
calibrated values and NOT a production configuration. The smoke run
calibrates nothing, trains nothing, and claims nothing.

It also exercises the reproducibility requirement: the same run is repeated
in `--replay-only` mode against the same evaluation cache, which must reach
the identical derived measurements without running a single engine analysis.
The replay keeps the same source, source id, cache database, policy and
reference ladder but takes its OWN run id, because C0 run directories are
immutable and are never overwritten.

Threads and hash stay at 1 and 64 MB: one Stockfish process is started per
bound level, so the configured hash is paid per level, not once.

The pilot path runs under an UNCALIBRATED policy, exactly as the CLI builds
one, so the smoke asserts what a real run must satisfy: no `DAMAGE_SUPPORTED`,
nothing engine-damage admissible, and `S` tagged `rederived_from_trace`. A
separate, clearly labelled parity check builds a CALIBRATED twin of the same
measurement to confirm the re-derived `S` equals the genuine `EngineRegret`.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import chess

from chess_decision_explorer.domain import DecisionKey, MoveKey, PositionKey
from chess_decision_explorer.assessment import (
    AssessmentStatus,
    CalibrationStatus,
    ComparisonPolicy,
    SearchLevel,
)
from chess_decision_explorer.engine_cache import (
    InMemoryEvaluationCache,
    SQLiteEvaluationCache,
    TieredEvaluationCache,
)
from chess_decision_explorer.experimental.c0 import C0_EXPERIMENTAL_CALIBRATION_ID
from chess_decision_explorer.experimental.c0.cli import main as pilot_main
from chess_decision_explorer.experimental.c0.pilot import build_engine_pool, measure_root
from chess_decision_explorer.experimental.c0.sampling import SampledDecision

# Tiny demonstration budgets throughout. NOT calibrated production values.
SMOKE_B1 = SearchLevel("B1", 20_000)
SMOKE_B2 = SearchLevel("B2", 60_000)
SMOKE_R1 = SearchLevel("R1", 120_000)
SMOKE_R2 = SearchLevel("R2", 240_000)

def smoke_policy(*, calibrated: bool = False) -> ComparisonPolicy:
    """The smoke policy. UNCALIBRATED by default, exactly like a pilot run.

    `calibrated=True` is used ONLY by the parity check below, which confirms
    that the `S` re-derived from an UNCALIBRATED trace equals the genuine
    `EngineRegret.units` of the identical measurement. It is never what the
    pilot path runs.
    """
    return ComparisonPolicy(
        policy_id="c0-smoke-experimental",
        policy_version="v0",
        b1=SMOKE_B1,
        b2=SMOKE_B2,
        b3=None,
        epsilon_units=20,
        tau_units=100,
        max_gap_drift_units=600,
        max_alternative_drift_units=600,
        max_user_drift_units=600,
        material_negative_gap_units=400,
        calibration_status=(
            CalibrationStatus.CALIBRATED
            if calibrated
            else CalibrationStatus.UNCALIBRATED
        ),
        calibration_id=C0_EXPERIMENTAL_CALIBRATION_ID if calibrated else None,
    )

# White ignores a hanging queen. A known-damaging decision, used so the smoke
# run always exercises the fixed-witness reference trajectory end to end even
# when the deterministic corpus sample happens to draw only quiet roots.
BLUNDER_FEN = "4k3/8/8/3q4/4P3/8/8/4K3 w - - 0 1"
BLUNDER_MOVE = "e1f1"

# Two short, complete, rated Blitz games. The second contains a well-known
# early blunder, so the sample has something for Step 4B to measure.
SMOKE_PGN = """[Event "Rated Blitz game"]
[Site "https://example.invalid/1"]
[Date "2026.01.01"]
[White "alpha"]
[Black "beta"]
[Result "1-0"]
[WhiteElo "1600"]
[BlackElo "1605"]
[TimeControl "300+0"]
[Termination "Normal"]

1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 6. Re1 b5 7. Bb3 d6 8. c3
O-O 9. h3 Nb8 10. d4 Nbd7 11. Nbd2 Bb7 12. Bc2 Re8 13. Nf1 Bf8 14. Ng3 g6 15.
b3 Bg7 16. d5 c6 17. c4 c5 18. a4 Nb6 19. Qe2 Bc8 20. Bd2 1-0

[Event "Rated Blitz game"]
[Site "https://example.invalid/2"]
[Date "2026.01.02"]
[White "gamma"]
[Black "delta"]
[Result "0-1"]
[WhiteElo "1580"]
[BlackElo "1620"]
[TimeControl "180+2"]
[Termination "Normal"]

1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. Ng5 d5 5. exd5 Nxd5 6. Nxf7 Kxf7 7. Qf3+ Ke6
8. Nc3 Ncb4 9. a3 Nxc2+ 10. Kd1 Nxa1 11. Nxd5 Kd6 12. d4 Bd7 13. Bf4 exf4 14.
Qxf4+ Kc6 15. Qe4 Kd6 16. Qf4+ Kc6 0-1

[Event "Rated Rapid game"]
[Site "https://example.invalid/3"]
[Date "2026.01.03"]
[White "epsilon"]
[Black "zeta"]
[Result "1/2-1/2"]
[WhiteElo "1700"]
[BlackElo "1690"]
[TimeControl "600+0"]
[Termination "Normal"]

1. d4 d5 2. c4 e6 3. Nc3 Nf6 4. Bg5 Be7 5. e3 O-O 6. Nf3 h6 7. Bh4 b6 8. cxd5
Nxd5 9. Bxe7 Qxe7 10. Nxd5 exd5 11. Rc1 Be6 12. Qa4 c5 13. Qa3 Rc8 14. Bb5 a6
15. dxc5 bxc5 16. O-O Ra7 17. Be2 Nd7 18. Nd4 Qf8 19. Nxe6 fxe6 20. e4 d4
1/2-1/2
"""


def build_args(pgn_path: Path, out_dir: Path, cache_db: Path, stockfish: str, replay: bool):
    args = [
        "--source", str(pgn_path),
        "--source-id", "c0-smoke",
        "--seed", "20260914",
        "--sample-size", "3",
        "--min-plies", "20",
        "--min-rating", "1000",
        "--max-rating", "2200",
        # Tiny demonstration budgets. NOT calibrated production values.
        "--b1-nodes", "20000",
        "--b2-nodes", "60000",
        "--epsilon-units", "20",
        "--tau-units", "100",
        "--max-gap-drift-units", "600",
        "--max-alternative-drift-units", "600",
        "--max-user-drift-units", "600",
        "--material-negative-gap-units", "400",
        "--reference-nodes", "120000", "240000",
        "--stockfish", stockfish,
        "--threads", "1",
        "--hash-mb", "64",
        "--cache-db", str(cache_db),
        "--out-dir", str(out_dir),
        # A replay NEVER reuses the measurement's run id: run artefacts are
        # immutable. Same source, source id, cache db, policy and ladder;
        # different run id.
        "--run-id", "c0-smoke-replay" if replay else "c0-smoke-measure",
    ]
    if replay:
        args.append("--replay-only")
    return args


def derived_measurements(run_dir: Path) -> list:
    """The derived pilot measurements, with wall-clock cost stripped out."""
    records = []
    for line in (run_dir / "roots.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        record.pop("cost", None)
        _strip_timings(record)
        records.append(record)
    return records


def _strip_timings(node):
    if isinstance(node, dict):
        node.pop("elapsed_seconds", None)
        node.pop("total_elapsed_seconds", None)
        for value in node.values():
            _strip_timings(value)
    elif isinstance(node, list):
        for value in node:
            _strip_timings(value)


def known_blunder_reference_case(stockfish: str, cache_db: Path) -> int:
    """Measure one known-damaging decision and its stronger reference ladder."""
    board = chess.Board(BLUNDER_FEN)
    position = PositionKey.from_board(board)
    sample = SampledDecision(
        source_id="c0-smoke-fixed",
        source_ordinal=0,
        game_id="c0-smoke-fixed:0",
        decision=DecisionKey(position, MoveKey(BLUNDER_MOVE)),
        ply_index=0,
        actor_color="white",
        actor_rating=None,
        opponent_rating=None,
        white_rating=None,
        black_rating=None,
        result="0-1",
        outcome_for_actor="loss",
        time_control=None,
        time_control_category="unknown",
        event=None,
        termination=None,
        date=None,
        game_ply_count=1,
        eligible_root_count=1,
    )

    uncalibrated = smoke_policy()
    persistent = SQLiteEvaluationCache(cache_db)
    cache = TieredEvaluationCache(InMemoryEvaluationCache(), persistent)
    levels = list(uncalibrated.levels) + [SMOKE_R1, SMOKE_R2]
    pool, counters, evaluators = build_engine_pool(stockfish, levels, cache)
    try:
        # The pilot path: an UNCALIBRATED policy, exactly as the CLI builds.
        record = measure_root(sample, pool, uncalibrated, (SMOKE_R1, SMOKE_R2))
        # Parity check only. Never a pilot result.
        parity = measure_root(
            sample, pool, smoke_policy(calibrated=True), (SMOKE_R1, SMOKE_R2)
        )
    finally:
        for evaluator in evaluators:
            evaluator.close()
        persistent.close()

    print(f"  position: {position}")
    print(f"  observed move: {BLUNDER_MOVE}")
    print(f"  Step 4B status: {record.assessment.status.value}")
    print(
        "  Step 4B reasons: "
        + ", ".join(reason.value for reason in record.assessment.reasons)
    )
    print(
        "  engine-damage admissible: "
        f"{record.assessment.is_engine_damage_admissible}"
    )
    print(f"  witness: {record.witness.witness} (source {record.witness.source})")
    print(f"  S (RESEARCH measurement, not an accepted EngineRegret): "
          f"{record.witness.s_units} units")

    if record.assessment.status is AssessmentStatus.DAMAGE_SUPPORTED:
        print("  FAIL: an UNCALIBRATED pilot run reached DAMAGE_SUPPORTED")
        return 1
    if record.assessment.is_engine_damage_admissible:
        print("  FAIL: an UNCALIBRATED pilot run produced an admissible conclusion")
        return 1
    if record.witness.source != "rederived_from_trace":
        print(f"  FAIL: S was tagged {record.witness.source!r}, "
              "expected 'rederived_from_trace'")
        return 1
    if (parity.witness.s_units, parity.witness.witness) != (
        record.witness.s_units,
        record.witness.witness,
    ):
        print("  FAIL: re-derived S does not match the genuine EngineRegret")
        return 1
    print(
        "  parity (test-only CALIBRATED twin): genuine EngineRegret "
        f"{parity.witness.s_units} units, witness {parity.witness.witness} -- matches"
    )

    if record.trajectory is None:
        print("  FAIL: no reference trajectory was produced for a known blunder")
        return 1
    for measurement in record.trajectory.measurements:
        print(
            f"  {measurement.level.label} ({measurement.level.nodes} nodes): "
            f"U(witness)={measurement.witness_units} "
            f"U(observed)={measurement.observed_units} "
            f"G_ref={measurement.g_ref_units:+d} "
            f"saturation={measurement.saturation_status} "
            f"({measurement.elapsed_seconds:.2f}s)"
        )
    diagnostics = record.trajectory.diagnostics()
    print(f"  monotonicity: {diagnostics.monotonicity}")
    print(f"  max |delta G| between levels: {diagnostics.max_abs_delta_g_units}")
    print(f"  sign changes: {diagnostics.sign_change_count}")
    print("  R = S - G_reference is NOT computed: C0 freezes no reference protocol.")
    print(
        "  engine analyses per level: "
        + ", ".join(
            f"{level.label}={counters[level].analyses}" for level in levels
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stockfish", required=True)
    parser.add_argument(
        "--keep",
        default=None,
        help="Keep the smoke output in this directory instead of a temp dir.",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(args.keep) if args.keep else Path(scratch)
        root.mkdir(parents=True, exist_ok=True)
        pgn_path = root / "smoke.pgn"
        pgn_path.write_text(SMOKE_PGN, encoding="utf-8")
        cache_db = root / "c0_smoke_cache.sqlite"
        out_dir = root / "runs"

        print("=== C0 smoke: known-blunder fixed-witness reference trajectory ===")
        failed = known_blunder_reference_case(
            args.stockfish, root / "c0_blunder_cache.sqlite"
        )
        if failed:
            return failed

        print()
        print("=== C0 smoke: measurement pass (real Stockfish) ===")
        pilot_main(build_args(pgn_path, out_dir, cache_db, args.stockfish, replay=False))

        print()
        print("=== C0 smoke: replay-only pass (must run zero engine analyses) ===")
        pilot_main(build_args(pgn_path, out_dir, cache_db, args.stockfish, replay=True))

        measure = derived_measurements(out_dir / "c0-smoke-measure")
        replay = derived_measurements(out_dir / "c0-smoke-replay")
        if measure != replay:
            print("FAIL: replayed evidence produced different derived measurements")
            return 1
        replay_manifest = json.loads(
            (out_dir / "c0-smoke-replay" / "manifest.json").read_text(encoding="utf-8")
        )
        analyses = replay_manifest["runtime"]["engine_analyses"]
        if analyses != 0:
            print(f"FAIL: replay-only pass ran {analyses} engine analyses")
            return 1

        print()
        print("OK: replayed stored evidence reproduced identical derived measurements")
        print(f"    roots: {len(measure)}, replay engine analyses: {analyses}")
        print((out_dir / "c0-smoke-measure" / "report.md").read_text(encoding="utf-8")[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
