"""Real-Stockfish integration coverage for the Step 4B assessment layer.

These tests are **opt-in**. They are skipped unless ``CDE_STOCKFISH_PATH``
points at a Stockfish executable, so the ordinary pytest suite stays fast and
never depends on an external binary:

    CDE_STOCKFISH_PATH=/path/to/stockfish .venv/bin/python -m pytest \
        tests/test_assessment_stockfish.py -q

The node budgets and thresholds below are **test fixtures chosen only to keep
the run cheap**. They are not calibrated production values and must never be
copied into production configuration: real budgets, epsilon, tau, and drift
limits come out of the separate calibration phase.
"""

from __future__ import annotations

import os

import chess
import pytest

from chess_decision_explorer.domain import DecisionKey, MoveKey, PositionKey
from chess_decision_explorer.engine import (
    EngineAnalysisConfig,
    SearchMode,
    StockfishEvaluator,
)
from chess_decision_explorer.engine_cache import (
    InMemoryEvaluationCache,
    SQLiteEvaluationCache,
    TieredEvaluationCache,
)
from chess_decision_explorer.assessment import (
    AssessmentReason,
    AssessmentStatus,
    CachedEvaluatorPool,
    CalibrationStatus,
    ComparisonPolicy,
    DecisionAssessor,
    ImmediateTerminal,
    SearchLevel,
    expected_score_units,
)

STOCKFISH_PATH = os.environ.get("CDE_STOCKFISH_PATH")

pytestmark = pytest.mark.skipif(
    not STOCKFISH_PATH,
    reason="set CDE_STOCKFISH_PATH to run the real-engine Step 4B tests",
)

# Deliberately small so the opt-in run stays cheap. NOT production budgets.
TEST_B1 = SearchLevel("B1", 20_000)
TEST_B2 = SearchLevel("B2", 60_000)


def make_test_policy():
    """A synthetic policy for integration testing only.

    It is marked CALIBRATED so the damage path can be exercised end to end;
    the ``calibration_id`` says plainly that this is not a production
    calibration.
    """
    return ComparisonPolicy(
        policy_id="integration-test-only",
        policy_version="v0",
        b1=TEST_B1,
        b2=TEST_B2,
        b3=None,
        epsilon_units=20,
        tau_units=100,
        max_gap_drift_units=600,
        max_alternative_drift_units=600,
        max_user_drift_units=600,
        material_negative_gap_units=400,
        calibration_status=CalibrationStatus.CALIBRATED,
        calibration_id="NOT-A-PRODUCTION-CALIBRATION-integration-test",
    )


class CountingEvaluator:
    """Wraps a started Step 4A evaluator and counts real analyses, so the
    cache-reuse path can be observed without touching Step 4A."""

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


@pytest.fixture(scope="module")
def engine_pool(tmp_path_factory):
    """One started Stockfish process per comparison level, sharing one L1+L2
    Step 4A cache. Step 4A binds the node budget per evaluator for its
    lifetime, so each level needs its own bound evaluator."""
    db_path = tmp_path_factory.mktemp("step4b") / "evaluations.sqlite"
    persistent = SQLiteEvaluationCache(db_path)
    cache = TieredEvaluationCache(InMemoryEvaluationCache(), persistent)

    started: list[StockfishEvaluator] = []
    counters: dict[SearchLevel, CountingEvaluator] = {}
    try:
        for level in (TEST_B1, TEST_B2):
            evaluator = StockfishEvaluator(
                STOCKFISH_PATH, EngineAnalysisConfig(nodes=level.nodes)
            )
            evaluator.start()
            started.append(evaluator)
            counters[level] = CountingEvaluator(evaluator)
        yield CachedEvaluatorPool(counters, cache), counters
    finally:
        for evaluator in started:
            evaluator.close()
        persistent.close()


def position_of(fen):
    board = chess.Board(fen)
    return PositionKey.from_board(board), board


def assess(pool, fen, move_uci, policy=None):
    position, _ = position_of(fen)
    assessor = DecisionAssessor(pool, policy or make_test_policy())
    return assessor.assess(
        DecisionKey(position_before=position, move=MoveKey(move_uci))
    )


# White to move: 1.exd5 wins the queen; 1.Kf1 is a quiet blunder.
WHITE_TACTIC_FEN = "4k3/8/8/3q4/4P3/8/8/4K3 w - - 0 1"
# Black to move: 1...exd4 wins the queen; 1...Kf8 is a quiet blunder.
BLACK_TACTIC_FEN = "4k3/8/8/4p3/3Q4/8/8/4K3 b - - 0 1"
# 1.Ra8 is mate; 1.Kh1 is quiet.
BACK_RANK_FEN = "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1"
# 1.Qf7 throws away a won game with an immediate stalemate.
STALEMATE_TRAP_FEN = "7k/8/8/8/8/8/5Q2/6K1 w - - 0 1"


def test_obvious_tactical_damage_white_to_move(engine_pool):
    pool, _ = engine_pool
    assessment = assess(pool, WHITE_TACTIC_FEN, "e1f1")
    assert assessment.status is AssessmentStatus.DAMAGE_SUPPORTED
    assert assessment.witness == MoveKey("e4d5")
    assert assessment.regret.units > 0
    assert assessment.is_engine_damage_admissible
    assert assessment.qualification_levels == (TEST_B1, TEST_B2)


def test_obvious_tactical_damage_black_to_move(engine_pool):
    pool, _ = engine_pool
    assessment = assess(pool, BLACK_TACTIC_FEN, "e8f8")
    assert assessment.status is AssessmentStatus.DAMAGE_SUPPORTED
    assert assessment.witness == MoveKey("e5d4")
    assert assessment.regret.units > 0
    assert assessment.is_engine_damage_admissible


def test_taking_the_queen_is_not_reported_as_damage(engine_pool):
    pool, _ = engine_pool
    assessment = assess(pool, WHITE_TACTIC_FEN, "e4d5")
    assert assessment.status is AssessmentStatus.NO_DAMAGE_DEMONSTRATED
    assert assessment.regret is None


def test_quiet_near_equivalent_opening_choice_is_not_damage(engine_pool):
    pool, _ = engine_pool
    board = chess.Board()
    board.push_uci("e2e4")
    position = PositionKey.from_board(board)
    assessor = DecisionAssessor(pool, make_test_policy())
    assessment = assessor.assess(
        DecisionKey(position_before=position, move=MoveKey("e7e5"))
    )
    # A perfectly reasonable move must never be labelled damaging just
    # because the engine might prefer something else.
    assert assessment.status is not AssessmentStatus.DAMAGE_SUPPORTED
    assert assessment.regret is None


def test_observed_immediate_checkmate_needs_no_engine_work(engine_pool):
    pool, counters = engine_pool
    before = {level: counter.analyses for level, counter in counters.items()}
    assessment = assess(pool, BACK_RANK_FEN, "a1a8")
    assert assessment.status is AssessmentStatus.NO_DAMAGE_DEMONSTRATED
    assert assessment.reasons == (
        AssessmentReason.USER_MOVE_FORCES_IMMEDIATE_CHECKMATE,
    )
    assert all(counters[level].analyses == before[level] for level in counters)


def test_real_engine_agrees_with_the_exact_terminal_facts(engine_pool):
    pool, _ = engine_pool
    assessment = assess(pool, BACK_RANK_FEN, "g1h1")
    mate_round = assessment.round_for(TEST_B1, MoveKey("a1a8"))
    assert mate_round is not None, "the engine should discover the mate in one"
    mating = mate_round.alternative_evidence
    assert mating.immediate_terminal is ImmediateTerminal.CHECKMATE
    assert mating.evaluation.mate == 1
    assert mating.units == 2000


def test_immediate_stalemate_transition_is_measured_as_damage(engine_pool):
    pool, _ = engine_pool
    assessment = assess(pool, STALEMATE_TRAP_FEN, "f2f7")
    stalemate_round = assessment.rounds[0]
    stalemating = stalemate_round.user_evidence
    assert stalemating.immediate_terminal is ImmediateTerminal.STALEMATE
    assert stalemating.evaluation.mate is None
    assert stalemating.units == 1000  # exact draw
    assert assessment.status is AssessmentStatus.DAMAGE_SUPPORTED
    assert assessment.regret.units > 0


def test_forced_root_consistency_holds_for_every_round(engine_pool):
    pool, _ = engine_pool
    assessment = assess(pool, WHITE_TACTIC_FEN, "e1f1")
    assert assessment.rounds
    for comparison in assessment.rounds:
        for side in (comparison.user_evidence, comparison.alternative_evidence):
            assert side.request.search_mode is SearchMode.FORCED_MOVE
            assert side.request.root_move == side.move
            assert side.evaluation.pv[0] == side.move
            assert side.request.position == assessment.decision.position_before
            assert expected_score_units(side.evaluation) == side.units
        assert (
            comparison.user_evidence.request.config
            == comparison.alternative_evidence.request.config
        )


def test_repeating_a_decision_reuses_cached_engine_evidence(engine_pool):
    pool, counters = engine_pool
    # Warm the cache.
    first = assess(pool, BLACK_TACTIC_FEN, "e8f8")
    before = {level: counter.analyses for level, counter in counters.items()}
    second = assess(pool, BLACK_TACTIC_FEN, "e8f8")
    assert all(counters[level].analyses == before[level] for level in counters)
    assert second.status is first.status
    assert second.regret.units == first.regret.units
    # Reuse is reuse: the second run still needed the same two independent
    # confirmation levels, it did not gain a third.
    assert second.qualification_levels == (TEST_B1, TEST_B2)


def test_changing_thresholds_reuses_cached_engine_evidence(engine_pool):
    pool, counters = engine_pool
    assess(pool, WHITE_TACTIC_FEN, "e1f1")
    before = {level: counter.analyses for level, counter in counters.items()}
    strict = ComparisonPolicy(
        policy_id="integration-test-only",
        policy_version="v0",
        b1=TEST_B1,
        b2=TEST_B2,
        epsilon_units=20,
        tau_units=1900,
        max_gap_drift_units=600,
        max_alternative_drift_units=600,
        max_user_drift_units=600,
        material_negative_gap_units=400,
        calibration_status=CalibrationStatus.CALIBRATED,
        calibration_id="NOT-A-PRODUCTION-CALIBRATION-integration-test",
    )
    assessment = assess(pool, WHITE_TACTIC_FEN, "e1f1", policy=strict)
    assert assessment.status is not AssessmentStatus.DAMAGE_SUPPORTED
    assert all(counters[level].analyses == before[level] for level in counters)
