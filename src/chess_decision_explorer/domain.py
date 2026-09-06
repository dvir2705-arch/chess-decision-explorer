from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import chess


@dataclass(frozen=True, slots=True)
class PositionKey:
    """Analytical identity of a position: piece placement, side to move,
    castling rights, and only legally relevant en-passant state."""

    epd: str

    @classmethod
    def from_board(cls, board: chess.Board) -> "PositionKey":
        # en_passant="legal" drops en-passant squares that are not actually
        # capturable, so a FEN artifact doesn't create a spurious identity.
        return cls(board.epd(en_passant="legal"))

    def __str__(self) -> str:
        return self.epd


@dataclass(frozen=True, slots=True)
class MoveKey:
    """A move in canonical UCI notation, e.g. 'e2e4', 'e7e8q'."""

    uci: str

    def __post_init__(self) -> None:
        try:
            move = chess.Move.from_uci(self.uci)
        except ValueError as exc:
            raise ValueError(f"Invalid UCI move: {self.uci!r}") from exc
        if move == chess.Move.null():
            raise ValueError(f"Null move is not a valid decision: {self.uci!r}")

    @classmethod
    def from_move(cls, move: chess.Move) -> "MoveKey":
        return cls(move.uci())

    def __str__(self) -> str:
        return self.uci


@dataclass(frozen=True, slots=True)
class DecisionKey:
    """The position a decision was made in, paired with the move played."""

    position_before: PositionKey
    move: MoveKey


class GameResult(Enum):
    WHITE_WIN = "1-0"
    BLACK_WIN = "0-1"
    DRAW = "1/2-1/2"

    def to_outcome(self, actor_color: chess.Color) -> "Outcome":
        if self is GameResult.DRAW:
            return Outcome.DRAW
        winner = chess.WHITE if self is GameResult.WHITE_WIN else chess.BLACK
        return Outcome.WIN if actor_color == winner else Outcome.LOSS


class Outcome(Enum):
    WIN = 1.0
    DRAW = 0.5
    LOSS = 0.0

    @property
    def points(self) -> float:
        return self.value


@dataclass(frozen=True, slots=True)
class DecisionObservation:
    """One actual occurrence of a decision in one game."""

    game_id: str
    ply_index: int
    decision: DecisionKey
    outcome: Outcome

    def __post_init__(self) -> None:
        if not self.game_id.strip():
            raise ValueError("game_id must not be empty or whitespace-only")
        if self.ply_index < 0:
            raise ValueError("ply_index must not be negative")
