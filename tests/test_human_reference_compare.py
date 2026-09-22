"""Step 5C-lite: derived Personal-vs-Reference comparison rows.

Synthetic only. Exercises the arithmetic, the zero-denominator convention,
ordering determinism, and the rule that derivation never mutates the
primitive aggregates.
"""

from __future__ import annotations

import io

import chess
import chess.pgn
import pytest

from chess_decision_explorer.aggregation import PositionIndex
from chess_decision_explorer.domain import PositionKey
from chess_decision_explorer.human_reference.compare import (
    build_comparison,
    build_position_comparison,
)
from chess_decision_explorer.human_reference.scan import ReferenceScanner
from chess_decision_explorer.human_reference.target import build_target_set
from chess_decision_explorer.ingestion import (
    _build_record,
    extract_player_observations,
)


def pgn(moves, *, white="alpha", black="beta", result="1-0") -> str:
    return "\n".join(
        [
            '[Event "Rated Blitz game"]',
            '[Site "https://example.invalid/x"]',
            '[Date "2026.01.01"]',
            f'[White "{white}"]',
            f'[Black "{black}"]',
            f'[Result "{result}"]',
            '[WhiteElo "1600"]',
            '[BlackElo "1610"]',
            '[TimeControl "300+0"]',
            "",
            f"{moves} {result}",
            "",
        ]
    )


START = PositionKey.from_board(chess.Board())


def hero_target_set():
    """One recurring position -- the start position -- with three personal
    moves in known proportions: e2e4 twice, d2d4 once, g1f3 once."""
    games = [
        "1. e4 e5 2. Nf3 Nc6",
        "1. e4 c5 2. Nf3 d6",
        "1. d4 d5 2. c4 e6",
        "1. Nf3 d5 2. g3 Nf6",
    ]
    index = PositionIndex()
    for ordinal, moves in enumerate(games):
        game = chess.pgn.read_game(io.StringIO(pgn(moves, white="hero")))
        record = _build_record(game, f"personal:{ordinal}")
        index.add_game(extract_player_observations(record, "hero"))
    return build_target_set(
        index, cohort="blitz", player_label="hero", min_distinct_games=2
    )


REFERENCE_CORPUS = (
    [pgn("1. e4 e5 2. Nf3 Nc6", result="1-0") for _ in range(5)]
    + [pgn("1. d4 d5 2. c4 e6", result="0-1") for _ in range(3)]
    + [pgn("1. c4 e5 2. Nc3 Nf6", result="1/2-1/2") for _ in range(2)]
)


def scanned(texts=REFERENCE_CORPUS, target=None):
    target = target or hero_target_set()
    scanner = ReferenceScanner(
        target_set=target, source_id="synthetic", target_games=1000
    )
    return target, scanner.scan(io.StringIO("\n".join(texts)))


def comparison_of(texts=REFERENCE_CORPUS):
    target, result = scanned(texts)
    rows = build_comparison(target, result.index)
    assert len(rows) == 1
    return rows[0]


def test_the_fixture_is_what_the_tests_assume():
    target = hero_target_set()
    assert set(target.positions) == {START}
    profile = target.get(START)
    assert profile.occurrence_count == 4
    assert [m.move.uci for m in profile.moves] == ["e2e4", "d2d4", "g1f3"]


# --- move-frequency arithmetic ---------------------------------------------


def test_personal_and_reference_move_rates():
    comparison = comparison_of()
    rows = {row.move: row for row in comparison.moves}

    assert comparison.personal_occurrence_count == 4
    assert comparison.reference_occurrence_count == 10

    assert rows["e2e4"].personal_occurrence_count == 2
    assert rows["e2e4"].personal_move_rate == pytest.approx(0.5)
    assert rows["e2e4"].reference_occurrence_count == 5
    assert rows["e2e4"].reference_move_rate == pytest.approx(0.5)

    assert rows["d2d4"].personal_move_rate == pytest.approx(0.25)
    assert rows["d2d4"].reference_move_rate == pytest.approx(0.3)


def test_reference_move_rates_sum_to_one_over_reference_moves():
    comparison = comparison_of()
    total = sum(
        row.reference_move_rate
        for row in comparison.moves
        if row.reference_move_rate is not None
    )
    assert total == pytest.approx(1.0)


def test_move_rows_are_the_union_of_personal_and_reference_moves():
    comparison = comparison_of()
    rows = {row.move: row for row in comparison.moves}
    assert set(rows) == {"e2e4", "d2d4", "c2c4", "g1f3"}

    # Played by the reference population, never by the player.
    assert rows["c2c4"].is_personal_move is False
    assert rows["c2c4"].personal_occurrence_count == 0
    assert rows["c2c4"].reference_occurrence_count == 2

    # Played by the player, never by the reference population.
    assert rows["g1f3"].is_personal_move is True
    assert rows["g1f3"].personal_occurrence_count == 1
    assert rows["g1f3"].reference_occurrence_count == 0


# --- actor-relative outcome arithmetic --------------------------------------


def test_reference_outcome_counts_and_rates():
    comparison = comparison_of()
    rows = {row.move: row for row in comparison.moves}

    e2e4 = rows["e2e4"]
    assert (e2e4.reference_wins, e2e4.reference_draws, e2e4.reference_losses) == (5, 0, 0)
    assert e2e4.reference_outcome_games == 5
    assert e2e4.reference_win_rate == pytest.approx(1.0)
    assert e2e4.reference_score_rate == pytest.approx(1.0)

    d2d4 = rows["d2d4"]
    assert (d2d4.reference_wins, d2d4.reference_draws, d2d4.reference_losses) == (0, 0, 3)
    assert d2d4.reference_loss_rate == pytest.approx(1.0)
    assert d2d4.reference_score_rate == pytest.approx(0.0)

    c2c4 = rows["c2c4"]
    assert (c2c4.reference_wins, c2c4.reference_draws, c2c4.reference_losses) == (0, 2, 0)
    assert c2c4.reference_draw_rate == pytest.approx(1.0)
    assert c2c4.reference_score_rate == pytest.approx(0.5)


def test_outcome_denominator_equals_distinct_game_count():
    comparison = comparison_of()
    for row in comparison.moves:
        assert row.reference_outcome_games == row.reference_distinct_game_count


def test_position_level_reference_outcomes_aggregate_the_moves():
    comparison = comparison_of()
    assert (
        comparison.reference_wins,
        comparison.reference_draws,
        comparison.reference_losses,
    ) == (5, 2, 3)
    assert comparison.reference_score_rate == pytest.approx((5 + 1.0) / 10)


# --- zero denominators ------------------------------------------------------


def test_an_uncovered_position_yields_none_rates_never_zero():
    """No reference evidence and "seen but never chosen" must not print the
    same number."""
    comparison = comparison_of(texts=[])
    assert comparison.reference_covered is False
    assert comparison.reference_occurrence_count == 0
    assert comparison.reference_top_move is None
    assert comparison.reference_move_coverage is None
    assert comparison.reference_score_rate is None
    assert comparison.personal_primary_move_reference_rank is None
    assert comparison.personal_primary_move_reference_rate is None

    for row in comparison.moves:
        assert row.reference_move_rate is None
        assert row.reference_win_rate is None
        assert row.reference_draw_rate is None
        assert row.reference_loss_rate is None
        assert row.reference_score_rate is None
        assert row.reference_rank is None


def test_a_personal_move_absent_from_the_reference_has_none_rates():
    comparison = comparison_of()
    row = next(row for row in comparison.moves if row.move == "g1f3")
    assert row.reference_occurrence_count == 0
    assert row.reference_move_rate is None
    assert row.reference_win_rate is None
    assert row.reference_score_rate is None
    assert row.reference_rank is None


def test_a_reference_move_never_played_personally_has_a_none_personal_rate():
    comparison = comparison_of()
    row = next(row for row in comparison.moves if row.move == "c2c4")
    assert row.personal_move_rate is None
    assert row.personal_occurrence_count == 0


# --- identifying the user's move and the reference favourites ---------------


def test_personal_primary_and_reference_top_moves_are_identified():
    comparison = comparison_of()
    assert comparison.personal_primary_move == "e2e4"
    assert comparison.reference_top_move == "e2e4"
    flags = {row.move: (row.is_personal_primary_move, row.is_reference_top_move)
             for row in comparison.moves}
    assert flags["e2e4"] == (True, True)
    assert flags["d2d4"] == (False, False)


def test_reference_rank_of_each_move():
    comparison = comparison_of()
    ranks = {row.move: row.reference_rank for row in comparison.moves}
    assert ranks == {"e2e4": 1, "d2d4": 2, "c2c4": 3, "g1f3": None}
    assert comparison.personal_primary_move_reference_rank == 1
    assert comparison.personal_primary_move_reference_rate == pytest.approx(0.5)


def test_reference_rank_when_the_player_disagrees_with_the_population():
    """The player's most-played move being unpopular is reported as a rank,
    not as a verdict."""
    corpus = (
        [pgn("1. d4 d5 2. c4 e6", result="1-0") for _ in range(9)]
        + [pgn("1. e4 e5 2. Nf3 Nc6", result="1-0")]
    )
    comparison = comparison_of(texts=corpus)
    assert comparison.personal_primary_move == "e2e4"
    assert comparison.reference_top_move == "d2d4"
    assert comparison.personal_primary_move_reference_rank == 2
    assert comparison.personal_primary_move_reference_rate == pytest.approx(0.1)


def test_reference_move_coverage():
    comparison = comparison_of()
    # e2e4 and d2d4 appear in the reference; g1f3 does not.
    assert comparison.personal_distinct_moves == 3
    assert comparison.reference_move_coverage == pytest.approx(2 / 3)
    assert comparison.reference_distinct_moves == 3


def test_side_to_move_is_read_from_the_position_key():
    comparison = comparison_of()
    assert comparison.side_to_move == "white"


def test_side_to_move_for_a_black_to_move_root():
    board = chess.Board()
    board.push_uci("e2e4")
    black_key = PositionKey.from_board(board)

    index = PositionIndex()
    for ordinal, moves in enumerate(["1. e4 e5 2. Nf3 Nc6", "1. e4 c5 2. Nf3 d6"]):
        game = chess.pgn.read_game(io.StringIO(pgn(moves, black="hero")))
        record = _build_record(game, f"personal:{ordinal}")
        index.add_game(extract_player_observations(record, "hero"))
    target = build_target_set(
        index, cohort="blitz", player_label="hero", min_distinct_games=2
    )
    assert black_key in target

    scanner = ReferenceScanner(
        target_set=target, source_id="synthetic", target_games=10
    )
    result = scanner.scan(io.StringIO(pgn("1. e4 e5 2. Nf3 Nc6", result="1-0")))
    comparison = build_position_comparison(target.get(black_key), result.index)

    assert comparison.side_to_move == "black"
    # White won, so the side to move here lost.
    assert (comparison.reference_wins, comparison.reference_losses) == (0, 1)


# --- determinism and non-mutation -------------------------------------------


def test_move_rows_are_ordered_by_reference_popularity_then_personal_then_uci():
    comparison = comparison_of()
    assert [row.move for row in comparison.moves] == ["e2e4", "d2d4", "c2c4", "g1f3"]


def test_comparison_output_is_deterministic():
    target, result = scanned()
    first = [row.as_dict() for row in build_comparison(target, result.index)]
    second = [row.as_dict() for row in build_comparison(target, result.index)]
    assert first == second

    first_moves = [
        move.as_dict()
        for row in build_comparison(target, result.index)
        for move in row.moves
    ]
    second_moves = [
        move.as_dict()
        for row in build_comparison(target, result.index)
        for move in row.moves
    ]
    assert first_moves == second_moves


def test_building_a_comparison_mutates_neither_aggregate():
    target, result = scanned()
    before_reference = {
        position.epd: (
            stats.occurrence_count,
            stats.distinct_game_count,
            stats.outcomes.wins,
            stats.outcomes.draws,
            stats.outcomes.losses,
            {move.uci: decision.occurrence_count for move, decision in stats.decisions.items()},
        )
        for position, stats in result.index.items()
    }
    before_personal = {
        profile.position.epd: (
            profile.occurrence_count,
            profile.distinct_game_count,
            [m.move.uci for m in profile.moves],
        )
        for profile in target.ordered_profiles()
    }

    build_comparison(target, result.index)

    after_reference = {
        position.epd: (
            stats.occurrence_count,
            stats.distinct_game_count,
            stats.outcomes.wins,
            stats.outcomes.draws,
            stats.outcomes.losses,
            {move.uci: decision.occurrence_count for move, decision in stats.decisions.items()},
        )
        for position, stats in result.index.items()
    }
    after_personal = {
        profile.position.epd: (
            profile.occurrence_count,
            profile.distinct_game_count,
            [m.move.uci for m in profile.moves],
        )
        for profile in target.ordered_profiles()
    }
    assert after_reference == before_reference
    assert after_personal == before_personal


def test_uncovered_positions_are_reported_not_dropped():
    target, result = scanned(texts=[])
    rows = build_comparison(target, result.index)
    assert len(rows) == len(target)
    assert all(row.reference_covered is False for row in rows)
