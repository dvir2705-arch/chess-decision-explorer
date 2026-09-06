import dataclasses

import chess
import pytest

from chess_decision_explorer.domain import (
    DecisionKey,
    DecisionObservation,
    GameResult,
    MoveKey,
    Outcome,
    PositionKey,
)


def replace_ep_field(fen: str, new_ep: str) -> str:
    fields = fen.split()
    fields[3] = new_ep
    return " ".join(fields)


# --- PositionKey -------------------------------------------------------


def test_position_key_equality_for_same_board_state():
    board_a = chess.Board()
    board_b = chess.Board()
    assert PositionKey.from_board(board_a) == PositionKey.from_board(board_b)


def test_position_key_ignores_halfmove_and_fullmove_counters():
    board_a = chess.Board(
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    )
    board_b = chess.Board(
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 17 42"
    )
    assert PositionKey.from_board(board_a) == PositionKey.from_board(board_b)


def test_position_key_changes_with_side_to_move():
    board_white = chess.Board(
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    )
    board_black = chess.Board(
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1"
    )
    assert PositionKey.from_board(board_white) != PositionKey.from_board(board_black)


def test_position_key_changes_with_castling_rights():
    board_full_rights = chess.Board(
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    )
    board_no_rights = chess.Board(
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w - - 0 1"
    )
    assert PositionKey.from_board(board_full_rights) != PositionKey.from_board(
        board_no_rights
    )


def test_legally_relevant_en_passant_affects_position_key():
    board_with_ep = chess.Board()
    for uci in ["e2e4", "a7a6", "e4e5", "d7d5"]:
        board_with_ep.push_uci(uci)
    assert board_with_ep.has_legal_en_passant()

    fen_without_ep = replace_ep_field(board_with_ep.fen(), "-")
    board_without_ep = chess.Board(fen_without_ep)

    assert PositionKey.from_board(board_with_ep) != PositionKey.from_board(
        board_without_ep
    )


def test_non_legal_en_passant_fen_artifact_does_not_change_identity():
    # After 1. e4 the FEN conventionally lists e3 as the ep square even
    # though no black pawn can actually capture there.
    board_with_artifact = chess.Board(
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
    )
    board_without_artifact = chess.Board(
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    )
    assert not board_with_artifact.has_legal_en_passant()
    assert PositionKey.from_board(board_with_artifact) == PositionKey.from_board(
        board_without_artifact
    )


# --- MoveKey -------------------------------------------------------------


def test_move_key_construction_from_chess_move():
    move = chess.Move.from_uci("g1f3")
    assert MoveKey.from_move(move) == MoveKey("g1f3")


@pytest.mark.parametrize("bad_uci", ["", "zzzz", "e2e9", "e2", "notamove"])
def test_move_key_rejects_invalid_uci(bad_uci):
    with pytest.raises(ValueError):
        MoveKey(bad_uci)


def test_move_key_rejects_null_move():
    with pytest.raises(ValueError):
        MoveKey("0000")


# --- DecisionKey -----------------------------------------------------------


def test_different_moves_from_same_position_give_different_decision_keys():
    position = PositionKey.from_board(chess.Board())
    decision_e4 = DecisionKey(position, MoveKey("e2e4"))
    decision_d4 = DecisionKey(position, MoveKey("d2d4"))
    assert decision_e4 != decision_d4


# --- GameResult / Outcome ---------------------------------------------------


def test_white_win_gives_win_for_white_and_loss_for_black():
    assert GameResult.WHITE_WIN.to_outcome(chess.WHITE) is Outcome.WIN
    assert GameResult.WHITE_WIN.to_outcome(chess.BLACK) is Outcome.LOSS


def test_black_win_gives_win_for_black_and_loss_for_white():
    assert GameResult.BLACK_WIN.to_outcome(chess.BLACK) is Outcome.WIN
    assert GameResult.BLACK_WIN.to_outcome(chess.WHITE) is Outcome.LOSS


def test_draw_gives_draw_for_both_colors():
    assert GameResult.DRAW.to_outcome(chess.WHITE) is Outcome.DRAW
    assert GameResult.DRAW.to_outcome(chess.BLACK) is Outcome.DRAW


def test_outcome_point_values():
    assert Outcome.WIN.points == 1.0
    assert Outcome.DRAW.points == 0.5
    assert Outcome.LOSS.points == 0.0


# --- DecisionObservation -----------------------------------------------------


def _sample_decision() -> DecisionKey:
    return DecisionKey(PositionKey.from_board(chess.Board()), MoveKey("e2e4"))


def test_decision_observation_rejects_empty_game_id():
    with pytest.raises(ValueError):
        DecisionObservation(
            game_id="",
            ply_index=0,
            decision=_sample_decision(),
            outcome=Outcome.WIN,
        )


def test_decision_observation_rejects_whitespace_only_game_id():
    with pytest.raises(ValueError):
        DecisionObservation(
            game_id="   ",
            ply_index=0,
            decision=_sample_decision(),
            outcome=Outcome.WIN,
        )


def test_decision_observation_rejects_negative_ply_index():
    with pytest.raises(ValueError):
        DecisionObservation(
            game_id="game-1",
            ply_index=-1,
            decision=_sample_decision(),
            outcome=Outcome.WIN,
        )


# --- Hashability and immutability -------------------------------------------


def test_key_value_objects_are_hashable():
    position = PositionKey.from_board(chess.Board())
    move = MoveKey("e2e4")
    decision = DecisionKey(position, move)
    {position, move, decision}  # must not raise


def test_value_objects_use_slots():
    position = PositionKey.from_board(chess.Board())
    move = MoveKey("e2e4")
    decision = DecisionKey(position, move)
    observation = DecisionObservation(
        game_id="game-1", ply_index=0, decision=decision, outcome=Outcome.WIN
    )
    for obj in (position, move, decision, observation):
        assert not hasattr(obj, "__dict__")


def test_frozen_value_objects_cannot_be_mutated():
    position = PositionKey.from_board(chess.Board())
    move = MoveKey("e2e4")
    decision = DecisionKey(position, move)
    observation = DecisionObservation(
        game_id="game-1", ply_index=0, decision=decision, outcome=Outcome.WIN
    )

    for obj, field, value in [
        (position, "epd", "x"),
        (move, "uci", "e2e4"),
        (decision, "move", move),
        (observation, "ply_index", 1),
    ]:
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, field, value)
