import io

import chess
import pytest

from chess_decision_explorer.aggregation import PositionIndex
from chess_decision_explorer.domain import (
    GameResult,
    MoveKey,
    Outcome,
    PositionKey,
)
from chess_decision_explorer.ingestion import (
    GameRecord,
    extract_player_observations,
    iter_pgn_records,
)


def make_pgn(moves: str, **headers: str) -> str:
    base = {
        "Event": "Test",
        "Site": "Testville",
        "Date": "2024.01.01",
        "Round": "1",
        "White": "Alice",
        "Black": "Bob",
        "Result": "1-0",
    }
    base.update(headers)
    header_block = "\n".join(f'[{k} "{v}"]' for k, v in base.items())
    return f"{header_block}\n\n{moves}\n"


def read_one(pgn_text: str, source_id: str = "src") -> GameRecord:
    return next(iter_pgn_records(io.StringIO(pgn_text), source_id))


SCHOLARS_MATE = "1. e4 e5 2. Bc4 Nc6 3. Qh5 Nf6 4. Qxf7# 1-0"


# --- 1. Basic metadata parsing ------------------------------------------------


def test_parses_standard_pgn_metadata():
    record = read_one(
        make_pgn(
            SCHOLARS_MATE,
            White="Alice",
            Black="Bob",
            WhiteElo="1500",
            BlackElo="1490",
            TimeControl="600+0",
            Date="2024.03.04",
            Site="https://example.org/1",
        )
    )
    assert record.game_id == "src:0"
    assert record.white_player == "Alice"
    assert record.black_player == "Bob"
    assert record.white_rating == 1500
    assert record.black_rating == 1490
    assert record.result is GameResult.WHITE_WIN
    assert record.time_control == "600+0"
    assert record.date == "2024.03.04"
    assert record.site == "https://example.org/1"
    assert record.initial_fen == chess.Board().fen()
    assert record.moves[0] == chess.Move.from_uci("e2e4")
    assert len(record.moves) == 7


# --- 2. Player name spelling/case preserved ---------------------------------


def test_player_names_preserved_verbatim():
    record = read_one(make_pgn(SCHOLARS_MATE, White="AaRoN NiMzO", Black="di GIORGIO"))
    assert record.white_player == "AaRoN NiMzO"
    assert record.black_player == "di GIORGIO"


# --- 3. Numeric ratings become ints ---------------------------------------


def test_numeric_ratings_become_ints():
    record = read_one(make_pgn(SCHOLARS_MATE, WhiteElo="2210", BlackElo="1875"))
    assert record.white_rating == 2210
    assert record.black_rating == 1875
    assert isinstance(record.white_rating, int)


# --- 4. Missing / unknown ratings become None ----------------------------


def test_missing_and_unknown_ratings_become_none():
    missing = read_one(make_pgn(SCHOLARS_MATE))
    assert missing.white_rating is None
    assert missing.black_rating is None

    unknown = read_one(make_pgn(SCHOLARS_MATE, WhiteElo="?", BlackElo="-"))
    assert unknown.white_rating is None
    assert unknown.black_rating is None

    blank = read_one(make_pgn(SCHOLARS_MATE, WhiteElo="  "))
    assert blank.white_rating is None


# --- 5. Malformed rating rejected ---------------------------------------


def test_malformed_rating_is_rejected():
    with pytest.raises(ValueError):
        read_one(make_pgn(SCHOLARS_MATE, WhiteElo="high"))


# --- 6/7. Result mapping ------------------------------------------------


def test_white_win_result():
    assert read_one(make_pgn(SCHOLARS_MATE, Result="1-0")).result is GameResult.WHITE_WIN


def test_black_win_and_draw_results():
    black_win = read_one(make_pgn("1. f3 e5 2. g4 Qh4# 0-1", Result="0-1"))
    assert black_win.result is GameResult.BLACK_WIN

    draw = read_one(make_pgn("1. e4 e5 1/2-1/2", Result="1/2-1/2"))
    assert draw.result is GameResult.DRAW


# --- 8. Unfinished game rejected --------------------------------------


def test_unfinished_game_is_rejected():
    with pytest.raises(ValueError):
        read_one(make_pgn("1. e4 e5 2. Nf3 *", Result="*"))


# --- 9. Deterministic game_ids for a multi-game stream -------------------


def test_source_id_and_ordinal_produce_deterministic_game_ids():
    stream = io.StringIO(
        make_pgn("1. e4 e5 1/2-1/2", Result="1/2-1/2")
        + "\n"
        + make_pgn(SCHOLARS_MATE)
        + "\n"
        + make_pgn("1. d4 d5 1/2-1/2", Result="1/2-1/2")
    )
    records = list(iter_pgn_records(stream, "chesscom-export"))
    assert [r.game_id for r in records] == [
        "chesscom-export:0",
        "chesscom-export:1",
        "chesscom-export:2",
    ]


def test_iter_pgn_records_rejects_blank_source_id():
    with pytest.raises(ValueError):
        list(iter_pgn_records(io.StringIO(make_pgn(SCHOLARS_MATE)), "   "))


def test_empty_stream_yields_nothing():
    assert list(iter_pgn_records(io.StringIO(""), "src")) == []


# --- 10. Only main-line moves stored ---------------------------------


def test_side_variations_are_ignored():
    pgn = make_pgn("1. e4 e5 (1... c5 2. Nf3 d6) 2. Nf3 Nc6 1-0")
    record = read_one(pgn)
    assert [m.uci() for m in record.moves] == ["e2e4", "e7e5", "g1f3", "b8c6"]


def test_comments_and_annotations_are_ignored():
    pgn = make_pgn("1. e4 {best by test} e5 $1 2. Nf3 Nc6 1-0")
    record = read_one(pgn)
    assert [m.uci() for m in record.moves] == ["e2e4", "e7e5", "g1f3", "b8c6"]


# --- 11. Custom legal starting FEN preserved and replayable -------------


def test_custom_starting_fen_is_preserved_and_replayable():
    fen = "4k3/8/8/8/8/8/4P3/4K3 w - - 0 1"
    pgn = make_pgn(
        "1. e4 Ke7 2. e5 Ke6 1-0",
        Result="1-0",
        FEN=fen,
        SetUp="1",
        White="Alice",
        Black="Bob",
    )
    record = read_one(pgn)
    assert record.initial_fen == fen

    observations = extract_player_observations(record, "Alice")
    assert [o.ply_index for o in observations] == [0, 2]


# --- 12. Unsupported variant / Chess960 rejected --------------------


def test_named_variant_is_rejected():
    with pytest.raises(ValueError):
        read_one(make_pgn("1. e4 e5 1-0", Variant="Atomic"))


def test_chess960_is_rejected():
    pgn = make_pgn(
        "1. g3 g6 1-0",
        Variant="Chess960",
        FEN="nrbqkrbn/pppppppp/8/8/8/8/PPPPPPPP/NRBQKRBN w KQkq - 0 1",
        SetUp="1",
    )
    with pytest.raises(ValueError):
        read_one(pgn)


# --- 13. PGN parsing errors rejected -------------------------------


def test_pgn_parsing_errors_are_rejected():
    with pytest.raises(ValueError):
        read_one(make_pgn("1. e5 e5 2. Nf3 1-0"))


# --- GameRecord validation ----------------------------------------


def test_game_record_rejects_blank_game_id():
    with pytest.raises(ValueError):
        GameRecord(
            game_id="  ",
            white_player="A",
            black_player="B",
            white_rating=None,
            black_rating=None,
            result=GameResult.DRAW,
            time_control=None,
            date=None,
            site=None,
            initial_fen=chess.Board().fen(),
            moves=(),
        )


def test_game_record_rejects_blank_player_name():
    with pytest.raises(ValueError):
        GameRecord(
            game_id="src:0",
            white_player="",
            black_player="B",
            white_rating=None,
            black_rating=None,
            result=GameResult.DRAW,
            time_control=None,
            date=None,
            site=None,
            initial_fen=chess.Board().fen(),
            moves=(),
        )


def test_game_record_is_frozen_and_slotted():
    record = read_one(make_pgn(SCHOLARS_MATE))
    assert not hasattr(record, "__dict__")
    with pytest.raises(Exception):
        record.white_player = "Someone"  # type: ignore[misc]


# --- Personal decision extraction --------------------------------


def test_extracting_white_emits_only_white_plies():
    record = read_one(make_pgn(SCHOLARS_MATE))
    observations = extract_player_observations(record, "Alice")
    assert [o.ply_index for o in observations] == [0, 2, 4, 6]


def test_extracting_black_emits_only_black_plies():
    record = read_one(make_pgn(SCHOLARS_MATE))
    observations = extract_player_observations(record, "Bob")
    assert [o.ply_index for o in observations] == [1, 3, 5]


def test_player_matching_is_case_insensitive():
    record = read_one(make_pgn(SCHOLARS_MATE, White="Alice", Black="Bob"))
    assert extract_player_observations(record, "ALICE")
    assert extract_player_observations(record, "bOb")


def test_absent_player_is_rejected():
    record = read_one(make_pgn(SCHOLARS_MATE))
    with pytest.raises(ValueError):
        extract_player_observations(record, "Carol")


def test_blank_player_name_is_rejected():
    record = read_one(make_pgn(SCHOLARS_MATE))
    with pytest.raises(ValueError):
        extract_player_observations(record, "   ")


def test_ambiguous_player_identity_is_rejected():
    record = read_one(make_pgn(SCHOLARS_MATE, White="Sam", Black="sam"))
    with pytest.raises(ValueError):
        extract_player_observations(record, "Sam")


def test_personal_outcome_normalized_for_white():
    record = read_one(make_pgn(SCHOLARS_MATE, Result="1-0"))
    observations = extract_player_observations(record, "Alice")
    assert observations
    assert all(o.outcome is Outcome.WIN for o in observations)

    loss_record = read_one(make_pgn("1. f3 e5 2. g4 Qh4# 0-1", Result="0-1"))
    white_loss = extract_player_observations(loss_record, "Alice")
    assert all(o.outcome is Outcome.LOSS for o in white_loss)


def test_personal_outcome_normalized_for_black():
    win_record = read_one(make_pgn("1. f3 e5 2. g4 Qh4# 0-1", Result="0-1"))
    black_win = extract_player_observations(win_record, "Bob")
    assert black_win
    assert all(o.outcome is Outcome.WIN for o in black_win)

    draw_record = read_one(make_pgn("1. e4 e5 1/2-1/2", Result="1/2-1/2"))
    black_draw = extract_player_observations(draw_record, "Bob")
    assert all(o.outcome is Outcome.DRAW for o in black_draw)


def test_ply_index_is_zero_based_halfmove_index():
    record = read_one(make_pgn(SCHOLARS_MATE))
    white = extract_player_observations(record, "Alice")
    black = extract_player_observations(record, "Bob")
    assert white[0].ply_index == 0
    assert black[0].ply_index == 1
    assert white[1].ply_index == 2


def test_position_key_is_board_before_the_move():
    record = read_one(make_pgn(SCHOLARS_MATE))
    observations = extract_player_observations(record, "Alice")

    board = chess.Board()
    assert observations[0].decision.position_before == PositionKey.from_board(board)

    board.push_uci("e2e4")
    board.push_uci("e7e5")
    assert observations[1].decision.position_before == PositionKey.from_board(board)
    assert observations[1].decision.move == MoveKey("f1c4")


def test_six_ply_game_gives_three_observations_per_side():
    record = read_one(make_pgn("1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 1-0"))
    white = extract_player_observations(record, "Alice")
    black = extract_player_observations(record, "Bob")
    assert len(white) == 3
    assert len(black) == 3
    assert len(record.moves) == 6


# --- 23. Extracted observations feed PositionIndex.add_game --------


def test_observations_can_be_added_to_position_index():
    record = read_one(make_pgn("1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 1-0"))
    white = extract_player_observations(record, "Alice")

    index = PositionIndex()
    index.add_game(white)

    start_stats = index[PositionKey.from_board(chess.Board())]
    assert start_stats.occurrence_count == 1
    assert start_stats.decisions[MoveKey("e2e4")].outcomes.wins == 1


# --- Extraction is turn-based, not ply-parity based --------------------


def _game_record(initial_fen: str, ucis: list[str], **overrides) -> GameRecord:
    fields = dict(
        game_id="src:0",
        white_player="Alice",
        black_player="Bob",
        white_rating=None,
        black_rating=None,
        result=GameResult.DRAW,
        time_control=None,
        date=None,
        site=None,
        initial_fen=initial_fen,
        moves=tuple(chess.Move.from_uci(u) for u in ucis),
    )
    fields.update(overrides)
    return GameRecord(**fields)


def test_extraction_uses_board_turn_for_black_to_move_start():
    # Legal K+P endgame with Black to move at ply 0.
    fen = "4k3/8/8/8/8/8/4P3/4K3 b - - 0 1"
    record = _game_record(fen, ["e8e7", "e2e4", "e7e6"])

    black = extract_player_observations(record, "Bob")
    white = extract_player_observations(record, "Alice")

    # Parity would put ply 0 with White; board.turn correctly assigns it to Black.
    assert [o.ply_index for o in black] == [0, 2]
    assert [o.ply_index for o in white] == [1]
    assert black[0].decision.position_before == PositionKey.from_board(
        chess.Board(fen)
    )
    assert black[0].decision.move == MoveKey("e8e7")
    assert white[0].ply_index == 1
    assert white[0].decision.move == MoveKey("e2e4")


def test_replay_failure_raises_and_returns_no_partial_list():
    # Two legal moves, then an illegal one (e4 pawn is blocked by the e5 pawn).
    record = _game_record(
        chess.Board().fen(),
        ["e2e4", "e7e5", "e4e5"],
        result=GameResult.WHITE_WIN,
    )

    with pytest.raises(ValueError):
        extract_player_observations(record, "Alice")
