import chess
import pytest

from chess_decision_explorer.aggregation import (
    OutcomeCounts,
    PositionIndex,
)
from chess_decision_explorer.domain import (
    DecisionKey,
    DecisionObservation,
    MoveKey,
    Outcome,
    PositionKey,
)


def position_after(ucis: list[str]) -> PositionKey:
    board = chess.Board()
    for uci in ucis:
        board.push_uci(uci)
    return PositionKey.from_board(board)


START = PositionKey.from_board(chess.Board())
AFTER_E4 = position_after(["e2e4"])


def obs(
    game_id: str,
    ply_index: int,
    position: PositionKey,
    move: str,
    outcome: Outcome,
) -> DecisionObservation:
    return DecisionObservation(
        game_id=game_id,
        ply_index=ply_index,
        decision=DecisionKey(position, MoveKey(move)),
        outcome=outcome,
    )


# 1. One observation produces correct position and decision counts.
def test_single_observation_counts():
    index = PositionIndex()
    index.add_game([obs("g1", 0, START, "e2e4", Outcome.WIN)])

    stats = index[START]
    assert stats.occurrence_count == 1
    assert stats.distinct_game_count == 1
    assert stats.outcomes.wins == 1

    decision = stats.decisions[MoveKey("e2e4")]
    assert decision.occurrence_count == 1
    assert decision.distinct_game_count == 1
    assert decision.outcomes.wins == 1


# 2. Same position + same move repeated within one game.
def test_repeated_same_position_same_move_in_one_game():
    index = PositionIndex()
    index.add_game(
        [
            obs("g1", 0, START, "e2e4", Outcome.LOSS),
            obs("g1", 10, START, "e2e4", Outcome.LOSS),
            obs("g1", 20, START, "e2e4", Outcome.LOSS),
        ]
    )

    stats = index[START]
    assert stats.occurrence_count == 3
    assert stats.distinct_game_count == 1
    assert stats.outcomes.game_count == 1
    assert stats.outcomes.losses == 1

    decision = stats.decisions[MoveKey("e2e4")]
    assert decision.occurrence_count == 3
    assert decision.distinct_game_count == 1
    assert decision.outcomes.losses == 1


# 3. Same position + different moves in one game.
def test_repeated_position_different_moves_in_one_game():
    index = PositionIndex()
    index.add_game(
        [
            obs("g1", 0, START, "e2e4", Outcome.DRAW),
            obs("g1", 12, START, "d2d4", Outcome.DRAW),
        ]
    )

    stats = index[START]
    assert stats.occurrence_count == 2
    assert stats.distinct_game_count == 1
    assert stats.outcomes.draws == 1
    assert stats.outcomes.game_count == 1

    e4 = stats.decisions[MoveKey("e2e4")]
    d4 = stats.decisions[MoveKey("d2d4")]
    assert e4.distinct_game_count == 1
    assert d4.distinct_game_count == 1
    assert e4.outcomes.draws == 1
    assert d4.outcomes.draws == 1


# 4. Same decision across two different games.
def test_same_decision_across_two_games():
    index = PositionIndex()
    index.add_game([obs("g1", 0, START, "e2e4", Outcome.WIN)])
    index.add_game([obs("g2", 0, START, "e2e4", Outcome.LOSS)])

    decision = index[START].decisions[MoveKey("e2e4")]
    assert decision.occurrence_count == 2
    assert decision.distinct_game_count == 2
    assert decision.outcomes.wins == 1
    assert decision.outcomes.losses == 1


# 5. Win/draw/loss aggregation and score_rate.
def test_outcome_aggregation_and_score_rate():
    counts = OutcomeCounts()
    counts.record(Outcome.WIN)
    counts.record(Outcome.WIN)
    counts.record(Outcome.DRAW)
    counts.record(Outcome.LOSS)

    assert (counts.wins, counts.draws, counts.losses) == (2, 1, 1)
    assert counts.game_count == 4
    assert counts.score_rate == pytest.approx((2 + 0.5) / 4)


# 6. score_rate is None for zero games.
def test_score_rate_none_for_zero_games():
    assert OutcomeCounts().score_rate is None


# 7. choice_rate uses occurrence counts.
def test_choice_rate_uses_occurrence_counts():
    index = PositionIndex()
    index.add_game(
        [
            obs("g1", 0, START, "e2e4", Outcome.WIN),
            obs("g1", 2, START, "e2e4", Outcome.WIN),
            obs("g1", 4, START, "d2d4", Outcome.WIN),
        ]
    )

    stats = index[START]
    # distinct_game_count would give 1/1 for both; occurrence gives 2/3 and 1/3.
    assert stats.choice_rate(MoveKey("e2e4")) == pytest.approx(2 / 3)
    assert stats.choice_rate(MoveKey("d2d4")) == pytest.approx(1 / 3)


# 8. choice rates across all moves sum to approximately 1.0.
def test_choice_rates_sum_to_one():
    index = PositionIndex()
    index.add_game([obs("g1", 0, START, "e2e4", Outcome.WIN)])
    index.add_game(
        [
            obs("g2", 0, START, "d2d4", Outcome.DRAW),
            obs("g2", 2, START, "d2d4", Outcome.DRAW),
        ]
    )
    index.add_game([obs("g3", 0, START, "g1f3", Outcome.LOSS)])

    stats = index[START]
    total = sum(stats.choice_rate(move) for move in stats.decisions)
    assert total == pytest.approx(1.0)


# 9. Unknown move choice rate is 0.0.
def test_unknown_move_choice_rate_is_zero():
    index = PositionIndex()
    index.add_game([obs("g1", 0, START, "e2e4", Outcome.WIN)])
    assert index[START].choice_rate(MoveKey("h2h4")) == 0.0


# 10. add_game rejects empty input.
def test_add_game_rejects_empty_input():
    index = PositionIndex()
    with pytest.raises(ValueError):
        index.add_game([])


# 11. add_game rejects mixed game_ids.
def test_add_game_rejects_mixed_game_ids():
    index = PositionIndex()
    with pytest.raises(ValueError):
        index.add_game(
            [
                obs("g1", 0, START, "e2e4", Outcome.WIN),
                obs("g2", 1, AFTER_E4, "e7e5", Outcome.WIN),
            ]
        )


# 12. Conflicting Outcome for a repeated PositionKey in one game.
# Different moves from the same position, so this must hit the position
# conflict (not the decision conflict).
def test_conflicting_position_outcome_rejected():
    index = PositionIndex()
    with pytest.raises(ValueError, match="repeated position"):
        index.add_game(
            [
                obs("g1", 0, START, "e2e4", Outcome.WIN),
                obs("g1", 8, START, "d2d4", Outcome.LOSS),
            ]
        )
    assert START not in index


# 13. Conflicting Outcome for a repeated DecisionKey in one game.
# The same move from the same position, so the decision-specific conflict
# must fire even though the position also repeats.
def test_conflicting_decision_outcome_rejected():
    index = PositionIndex()
    with pytest.raises(ValueError, match=r"repeated decision .*e2e4"):
        index.add_game(
            [
                obs("g1", 0, START, "e2e4", Outcome.WIN),
                obs("g1", 8, START, "e2e4", Outcome.DRAW),
            ]
        )
    assert START not in index


# 14. Sum of decision occurrence_count equals position occurrence_count.
def test_decision_occurrence_sum_equals_position_occurrence():
    index = PositionIndex()
    index.add_game(
        [
            obs("g1", 0, START, "e2e4", Outcome.WIN),
            obs("g1", 2, START, "e2e4", Outcome.WIN),
            obs("g1", 4, START, "d2d4", Outcome.WIN),
        ]
    )
    index.add_game([obs("g2", 0, START, "g1f3", Outcome.LOSS)])

    stats = index[START]
    assert sum(d.occurrence_count for d in stats.decisions.values()) == (
        stats.occurrence_count
    )


# 15. Sum of decision distinct_game_count may exceed position distinct_game_count.
def test_decision_distinct_game_sum_can_exceed_position_distinct_game():
    index = PositionIndex()
    index.add_game(
        [
            obs("g1", 0, START, "e2e4", Outcome.DRAW),
            obs("g1", 14, START, "d2d4", Outcome.DRAW),
        ]
    )

    stats = index[START]
    assert stats.distinct_game_count == 1
    decision_distinct_total = sum(
        d.distinct_game_count for d in stats.decisions.values()
    )
    assert decision_distinct_total == 2
    assert decision_distinct_total > stats.distinct_game_count


def test_add_game_consumes_iterator_input():
    index = PositionIndex()
    index.add_game(
        iter([obs("g1", 0, START, "e2e4", Outcome.WIN)])
    )
    assert index[START].occurrence_count == 1
