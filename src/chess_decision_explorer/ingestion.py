from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, TextIO

import chess
import chess.pgn

from .domain import (
    DecisionKey,
    DecisionObservation,
    GameResult,
    MoveKey,
    PositionKey,
)

# Header values that stand for "unknown" rather than a real value.
_UNKNOWN_RATING_MARKERS = frozenset({"?", "-"})


@dataclass(frozen=True, slots=True)
class GameRecord:
    """Source-neutral representation of one parsed standard-chess game.

    Holds only factual metadata as found in the PGN. Optional metadata is
    only lightly normalised: a present, meaningful value is kept as trimmed
    source text (ratings as ``int``); absent, blank, or conventional unknown
    markers such as ``?``, ``-`` or ``????.??.??`` become ``None``; a clearly
    malformed rating is rejected. Metadata is otherwise not interpreted,
    classified, or filtered -- ratings are never turned into cohorts and
    ``time_control`` is never labelled blitz/rapid. ``initial_fen`` is the
    complete board FEN the game is replayed from; ``moves`` is the PGN main
    line only.
    """

    game_id: str
    white_player: str
    black_player: str
    white_rating: int | None
    black_rating: int | None
    result: GameResult
    time_control: str | None
    date: str | None
    site: str | None
    initial_fen: str
    moves: tuple[chess.Move, ...]

    def __post_init__(self) -> None:
        if not self.game_id.strip():
            raise ValueError("game_id must not be empty or whitespace-only")
        if not self.white_player.strip():
            raise ValueError("white_player must not be empty or whitespace-only")
        if not self.black_player.strip():
            raise ValueError("black_player must not be empty or whitespace-only")


def _parse_rating(raw: str | None) -> int | None:
    """Numeric header -> int; missing/blank/unknown marker -> None;
    clearly malformed non-numeric content -> ValueError."""
    if raw is None:
        return None
    text = raw.strip()
    if not text or text in _UNKNOWN_RATING_MARKERS:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError(f"malformed rating header: {raw!r}") from exc


def _optional_text(raw: str | None) -> str | None:
    """Raw PGN text, or None when the header is absent or a bare unknown
    marker such as ``?``, ``-`` or ``????.??.??``. The value is not otherwise
    interpreted or classified."""
    if raw is None:
        return None
    text = raw.strip()
    if not text or set(text) <= {"?", ".", "-"}:
        return None
    return text


def _build_record(game: chess.pgn.Game, game_id: str) -> GameRecord:
    if game.errors:
        raise ValueError(
            f"game {game_id} has PGN parsing errors: {game.errors!r}"
        )

    headers = game.headers

    if headers.is_chess960() or headers.is_wild() or headers.variant() is not chess.Board:
        raise ValueError(
            f"game {game_id} uses an unsupported chess variant: "
            f"{headers.get('Variant')!r}"
        )

    result_text = headers.get("Result", "*")
    try:
        result = GameResult(result_text)
    except ValueError as exc:
        raise ValueError(
            f"game {game_id} has a non-completed or invalid result: {result_text!r}"
        ) from exc

    try:
        board = game.board()
    except ValueError as exc:
        raise ValueError(
            f"game {game_id} has an invalid initial position"
        ) from exc
    if board.chess960:
        raise ValueError(f"game {game_id} uses Chess960 castling rights")

    return GameRecord(
        game_id=game_id,
        white_player=headers.get("White", "?"),
        black_player=headers.get("Black", "?"),
        white_rating=_parse_rating(headers.get("WhiteElo")),
        black_rating=_parse_rating(headers.get("BlackElo")),
        result=result,
        time_control=_optional_text(headers.get("TimeControl")),
        date=_optional_text(headers.get("Date")),
        site=_optional_text(headers.get("Site")),
        initial_fen=board.fen(),
        moves=tuple(game.mainline_moves()),
    )


def iter_pgn_records(handle: TextIO, source_id: str) -> Iterator[GameRecord]:
    """Yield one :class:`GameRecord` per game in a local PGN stream.

    Reads a single game at a time from ``handle`` rather than loading the
    whole file. ``game_id`` is ``f"{source_id}:{ordinal}"`` with a zero-based
    ordinal that advances once per parsed game; identity is not derived from
    player names or date, and no cross-game duplicate detection is done.

    A game that python-chess could not parse, that has no completed result,
    or that uses an unsupported variant / Chess960 raises ``ValueError``.
    """
    if not source_id or not source_id.strip():
        raise ValueError("source_id must not be empty or whitespace-only")

    ordinal = 0
    while True:
        game = chess.pgn.read_game(handle)
        if game is None:
            return
        yield _build_record(game, f"{source_id}:{ordinal}")
        ordinal += 1


def extract_player_observations(
    game: GameRecord, player_name: str
) -> list[DecisionObservation]:
    """Return the :class:`DecisionObservation` objects for only ``player_name``'s
    moves in ``game``.

    The game is replayed from ``game.initial_fen``. For each main-line ply
    where the side to move is the requested player, the position *before* the
    move becomes a :class:`PositionKey`, the move a :class:`MoveKey`, and the
    pair a :class:`DecisionKey`; ``ply_index`` is the zero-based half-move
    index. Every observation carries the game's final :class:`Outcome`
    normalised to that player's colour.

    Matching is case-insensitive via ``casefold()``. The full game is
    replayed and validated before anything is returned, so a caller never
    receives a partially valid result.
    """
    if not player_name or not player_name.strip():
        raise ValueError("player_name must not be empty or whitespace-only")

    target = player_name.casefold()
    matches_white = game.white_player.casefold() == target
    matches_black = game.black_player.casefold() == target
    if matches_white and matches_black:
        raise ValueError(
            f"player {player_name!r} matches both sides of game {game.game_id}"
        )
    if not matches_white and not matches_black:
        raise ValueError(
            f"player {player_name!r} is neither White nor Black in game {game.game_id}"
        )

    player_color = chess.WHITE if matches_white else chess.BLACK
    outcome = game.result.to_outcome(player_color)

    try:
        board = chess.Board(game.initial_fen)
    except ValueError as exc:
        raise ValueError(
            f"game {game.game_id} has an invalid initial FEN"
        ) from exc

    observations: list[DecisionObservation] = []
    for ply_index, move in enumerate(game.moves):
        if not board.is_legal(move):
            raise ValueError(
                f"illegal move {move.uci()} at ply {ply_index} in game {game.game_id}"
            )
        if board.turn == player_color:
            decision = DecisionKey(
                PositionKey.from_board(board), MoveKey.from_move(move)
            )
            observations.append(
                DecisionObservation(
                    game_id=game.game_id,
                    ply_index=ply_index,
                    decision=decision,
                    outcome=outcome,
                )
            )
        board.push(move)

    return observations
