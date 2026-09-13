"""Manual smoke helper for the Step 4B damaging-move assessment layer.

Not part of the mandatory pytest suite -- pytest never imports or collects
this file. Run it by hand, against a caller-supplied Stockfish executable:

    .venv/bin/python scripts/stockfish_assessment_smoke.py /path/to/stockfish

The executable path is always a command-line argument; no filesystem
location is hard-coded here or in production code.

IMPORTANT: the node budgets and thresholds used below are demonstration
values chosen to keep this script quick. They are NOT calibrated production
values for epsilon, tau, drift limits, or B1/B2/B3 budgets, and the policy is
labelled accordingly. Real values come from the separate calibration phase.

It exercises:
  A. a White-to-move obvious tactical blunder
  B. a Black-to-move obvious tactical blunder
  C. a quiet, near-equivalent opening choice (must NOT be labelled damaging)
  D. an observed immediate checkmate (no engine work at all)
  E. an immediate stalemate transition
  F. one bounded escalation level (B3) wired end to end
  G. cache reuse: a repeated decision costs no further engine analyses
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import chess

from chess_decision_explorer.domain import DecisionKey, MoveKey, PositionKey
from chess_decision_explorer.engine import (
    EngineAnalysisConfig,
    StockfishEvaluator,
)
from chess_decision_explorer.engine_cache import (
    InMemoryEvaluationCache,
    SQLiteEvaluationCache,
    TieredEvaluationCache,
)
from chess_decision_explorer.assessment import (
    AssessmentStatus,
    CachedEvaluatorPool,
    CalibrationStatus,
    ComparisonPolicy,
    DecisionAssessor,
    SearchLevel,
    expected_score_from_units,
)

B1 = SearchLevel("B1", 40_000)
B2 = SearchLevel("B2", 120_000)
B3 = SearchLevel("B3", 360_000)

DEMO_POLICY = ComparisonPolicy(
    policy_id="smoke-demo-only",
    policy_version="v0",
    b1=B1,
    b2=B2,
    b3=B3,
    epsilon_units=20,
    tau_units=100,
    max_gap_drift_units=600,
    max_alternative_drift_units=600,
    max_user_drift_units=600,
    material_negative_gap_units=400,
    calibration_status=CalibrationStatus.CALIBRATED,
    calibration_id="NOT-A-PRODUCTION-CALIBRATION-smoke-demo",
)


class CountingEvaluator:
    """Counts real analyses so the cache-reuse path is observable."""

    def __init__(self, inner: StockfishEvaluator) -> None:
        self._inner = inner
        self.analyses = 0

    @property
    def config(self):
        return self._inner.config

    @property
    def identity(self):
        return self._inner.identity

    def evaluate(self, position, search_mode, root_move=None):
        self.analyses += 1
        return self._inner.evaluate(position, search_mode, root_move)


def _describe(assessment) -> None:
    print(f"  status:   {assessment.status.value}")
    print(f"  reasons:  {[reason.value for reason in assessment.reasons]}")
    if assessment.unresolved_triggers:
        print(
            "  unresolved: "
            f"{[reason.value for reason in assessment.unresolved_triggers]}"
        )
    if assessment.final_gap_units is not None:
        print(
            f"  final signed gap: {assessment.final_gap_units} units "
            f"({expected_score_from_units(assessment.final_gap_units):+.4f} "
            "expected score)"
        )
    if assessment.status is AssessmentStatus.DAMAGE_SUPPORTED:
        print(f"  witness:  {assessment.witness}")
        print(
            f"  accepted regret: {assessment.regret.units} units "
            f"({assessment.regret.expected_score_loss:.4f} expected score)"
        )
        print(f"  conservative margin L: {assessment.conservative_margin_units}")
        print(
            "  qualification levels: "
            f"{[level.label for level in assessment.qualification_levels]}"
        )
    print(f"  admissible for Step 4C: {assessment.is_engine_damage_admissible}")
    if not assessment.is_engine_damage_admissible:
        for failure in assessment.admission_failures:
            print(f"    - {failure}")
    print(f"  engine requests issued: {assessment.requests_issued}")


def _run_case(assessor, label, fen, move_uci) -> None:
    board = chess.Board(fen)
    position = PositionKey.from_board(board)
    mover = "White" if board.turn == chess.WHITE else "Black"
    print(f"\n=== {label} ({mover} to move, played {move_uci}) ===")
    print(f"  position: {position}")
    assessment = assessor.assess(
        DecisionKey(position_before=position, move=MoveKey(move_uci))
    )
    _describe(assessment)
    return assessment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stockfish", help="path to a Stockfish executable")
    args = parser.parse_args()

    print("Step 4B assessment smoke test")
    print(f"  engine:  {args.stockfish}")
    print(f"  policy:  {DEMO_POLICY.policy_id} {DEMO_POLICY.policy_version}")
    print(f"  policy fingerprint: {DEMO_POLICY.fingerprint}")
    print(
        "  NOTE: demonstration thresholds only -- NOT a production calibration."
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        persistent = SQLiteEvaluationCache(Path(tmpdir) / "evaluations.sqlite")
        cache = TieredEvaluationCache(InMemoryEvaluationCache(), persistent)
        started: list[StockfishEvaluator] = []
        counters: dict[SearchLevel, CountingEvaluator] = {}
        try:
            for level in DEMO_POLICY.levels:
                evaluator = StockfishEvaluator(
                    args.stockfish, EngineAnalysisConfig(nodes=level.nodes)
                )
                evaluator.start()
                started.append(evaluator)
                counters[level] = CountingEvaluator(evaluator)
            print(f"  engine identity: {started[0].identity}")

            pool = CachedEvaluatorPool(counters, cache)
            assessor = DecisionAssessor(pool, DEMO_POLICY)

            _run_case(
                assessor,
                "A. White hangs a queen capture",
                "4k3/8/8/3q4/4P3/8/8/4K3 w - - 0 1",
                "e1f1",
            )
            _run_case(
                assessor,
                "B. Black hangs a queen capture",
                "4k3/8/8/4p3/3Q4/8/8/4K3 b - - 0 1",
                "e8f8",
            )
            _run_case(
                assessor,
                "C. Quiet near-equivalent opening choice",
                "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
                "e7e5",
            )
            _run_case(
                assessor,
                "D. Observed move is immediate checkmate",
                "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1",
                "a1a8",
            )
            _run_case(
                assessor,
                "E. Immediate stalemate transition",
                "7k/8/8/8/8/8/5Q2/6K1 w - - 0 1",
                "f2f7",
            )

            print("\n=== G. Cache reuse ===")
            before = {level: counter.analyses for level, counter in counters.items()}
            _run_case(
                assessor,
                "G. Repeat of case A",
                "4k3/8/8/3q4/4P3/8/8/4K3 w - - 0 1",
                "e1f1",
            )
            reused = all(
                counters[level].analyses == before[level] for level in counters
            )
            print(f"  no new engine analyses on the repeat: {reused}")
            if not reused:
                print("  FAILED: the repeat consumed fresh engine work")
                return 1

            print("\n  analyses per level:")
            for level, counter in counters.items():
                print(f"    {level.label} ({level.nodes} nodes): {counter.analyses}")
        finally:
            for evaluator in started:
                evaluator.close()
            persistent.close()

    print("\nSmoke test finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
