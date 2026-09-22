"""Derived Personal-vs-Reference comparison rows.

Everything here is a **derivation**. The personal target set and the
reference `PositionIndex` are read and never mutated, so the primitive
aggregates stay exactly what the ingestion and scan layers produced and a
comparison can be rebuilt, or rebuilt differently, without rescanning.

What a comparison row is not: it is not a score, not a ranking of move
quality, and not a significance test. It reports what the player played,
what the reference population played, and how the reference population's
games ended -- three descriptions side by side. A gap between a personal
rate and a reference rate says the two populations differ, nothing more.

Rate conventions, applied uniformly:

- a rate whose denominator is zero is ``None``, never ``0.0``. "No reference
  evidence" and "seen, never chosen" are different facts and must not print
  the same;
- personal move rates are occurrence based (matching
  `PositionStats.choice_rate`), reference move rates likewise;
- outcome rates are distinct-game based, because `OutcomeCounts` records one
  outcome per game.

Ordering is total and deterministic at every level, so two runs over the same
aggregates produce byte-identical artefacts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..aggregation import PositionIndex
from ..domain import MoveKey, PositionKey
from .target import PersonalPositionProfile, RecurringTargetSet

REFERENCE_COVERAGE_NOTE = (
    "reference_covered: the personal recurring position occurred at least "
    "once in the accepted reference games. reference_move_coverage: the "
    "share of the player's OWN distinct moves in that position that were "
    "also played at least once by the reference population (None when the "
    "position is uncovered). Neither is a sample-size guarantee."
)


def _rate(numerator: float, denominator: float) -> float | None:
    """`numerator / denominator`, or `None` when the denominator is zero."""
    if denominator == 0:
        return None
    return numerator / denominator


@dataclass(frozen=True, slots=True)
class MoveComparison:
    """One move at one recurring position, personal and reference side by
    side.

    A move appears here if the player played it, the reference population
    played it, or both -- the row set is the union, so a common reference
    move the player never chooses is visible, and so is a personal move
    nobody else plays.
    """

    move: str
    personal_occurrence_count: int
    personal_distinct_game_count: int
    personal_move_rate: float | None
    reference_occurrence_count: int
    reference_distinct_game_count: int
    reference_move_rate: float | None
    reference_wins: int
    reference_draws: int
    reference_losses: int
    reference_rank: int | None
    is_personal_move: bool
    is_personal_primary_move: bool
    is_reference_top_move: bool

    @property
    def reference_outcome_games(self) -> int:
        """Denominator of every reference outcome rate.

        Equals `reference_distinct_game_count` by construction:
        `OutcomeCounts` records exactly one outcome per distinct game.
        """
        return self.reference_wins + self.reference_draws + self.reference_losses

    @property
    def reference_win_rate(self) -> float | None:
        return _rate(self.reference_wins, self.reference_outcome_games)

    @property
    def reference_draw_rate(self) -> float | None:
        return _rate(self.reference_draws, self.reference_outcome_games)

    @property
    def reference_loss_rate(self) -> float | None:
        return _rate(self.reference_losses, self.reference_outcome_games)

    @property
    def reference_score_rate(self) -> float | None:
        """Actor-relative points per game: `(wins + 0.5*draws) / games`.

        A *historical human* rate for the side that had to move. Never an
        engine quantity and never a move-quality verdict.
        """
        return _rate(
            self.reference_wins + 0.5 * self.reference_draws,
            self.reference_outcome_games,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "move": self.move,
            "personal_occurrence_count": self.personal_occurrence_count,
            "personal_distinct_game_count": self.personal_distinct_game_count,
            "personal_move_rate": self.personal_move_rate,
            "reference_occurrence_count": self.reference_occurrence_count,
            "reference_distinct_game_count": self.reference_distinct_game_count,
            "reference_move_rate": self.reference_move_rate,
            "reference_wins": self.reference_wins,
            "reference_draws": self.reference_draws,
            "reference_losses": self.reference_losses,
            "reference_outcome_games": self.reference_outcome_games,
            "reference_win_rate": self.reference_win_rate,
            "reference_draw_rate": self.reference_draw_rate,
            "reference_loss_rate": self.reference_loss_rate,
            "reference_score_rate": self.reference_score_rate,
            "reference_rank": self.reference_rank,
            "is_personal_move": self.is_personal_move,
            "is_personal_primary_move": self.is_personal_primary_move,
            "is_reference_top_move": self.is_reference_top_move,
        }


@dataclass(frozen=True, slots=True)
class PositionComparison:
    """One recurring personal position, with its reference evidence."""

    position_epd: str
    side_to_move: str
    personal_occurrence_count: int
    personal_distinct_game_count: int
    reference_occurrence_count: int
    reference_distinct_game_count: int
    reference_wins: int
    reference_draws: int
    reference_losses: int
    personal_primary_move: str
    reference_top_move: str | None
    personal_primary_move_reference_rank: int | None
    personal_primary_move_reference_rate: float | None
    moves: tuple[MoveComparison, ...]

    @property
    def reference_covered(self) -> bool:
        return self.reference_occurrence_count > 0

    @property
    def reference_move_coverage(self) -> float | None:
        """Share of the player's own distinct moves here that the reference
        population also played at least once. `None` when uncovered."""
        if not self.reference_covered:
            return None
        personal_moves = [move for move in self.moves if move.is_personal_move]
        if not personal_moves:
            return None
        seen = sum(
            1 for move in personal_moves if move.reference_occurrence_count > 0
        )
        return seen / len(personal_moves)

    @property
    def reference_distinct_moves(self) -> int:
        return sum(1 for move in self.moves if move.reference_occurrence_count > 0)

    @property
    def personal_distinct_moves(self) -> int:
        return sum(1 for move in self.moves if move.is_personal_move)

    @property
    def reference_score_rate(self) -> float | None:
        """Actor-relative points per reference game reaching this position."""
        games = self.reference_wins + self.reference_draws + self.reference_losses
        return _rate(self.reference_wins + 0.5 * self.reference_draws, games)

    def as_dict(self) -> dict[str, Any]:
        return {
            "position_epd": self.position_epd,
            "side_to_move": self.side_to_move,
            "personal_occurrence_count": self.personal_occurrence_count,
            "personal_distinct_game_count": self.personal_distinct_game_count,
            "personal_distinct_moves": self.personal_distinct_moves,
            "reference_occurrence_count": self.reference_occurrence_count,
            "reference_distinct_game_count": self.reference_distinct_game_count,
            "reference_distinct_moves": self.reference_distinct_moves,
            "reference_covered": self.reference_covered,
            "reference_move_coverage": self.reference_move_coverage,
            "reference_wins": self.reference_wins,
            "reference_draws": self.reference_draws,
            "reference_losses": self.reference_losses,
            "reference_score_rate": self.reference_score_rate,
            "personal_primary_move": self.personal_primary_move,
            "reference_top_move": self.reference_top_move,
            "personal_primary_move_reference_rank": (
                self.personal_primary_move_reference_rank
            ),
            "personal_primary_move_reference_rate": (
                self.personal_primary_move_reference_rate
            ),
        }


def _side_to_move(epd: str) -> str:
    """'white' or 'black', read off the EPD's side-to-move field.

    `PositionKey` is an EPD, so the actor of every decision at this position
    is fixed by the key itself -- which is exactly why reference outcomes can
    be actor-relative without storing any extra context.
    """
    fields = epd.split()
    if len(fields) < 2 or fields[1] not in ("w", "b"):
        raise ValueError(f"position key is not a usable EPD: {epd!r}")
    return "white" if fields[1] == "w" else "black"


def _reference_ranks(reference_stats: Any) -> dict[MoveKey, int]:
    """1-based reference popularity rank per move.

    Ordered by reference occurrence count descending, ties broken by UCI
    ascending so the rank is total and reproducible. Ties therefore receive
    *distinct* consecutive ranks rather than a shared rank; the underlying
    counts are in the row beside the rank, so an apparent 3-vs-4 ordering of
    equal counts is visible as equal.
    """
    if reference_stats is None:
        return {}
    ordered = sorted(
        reference_stats.decisions.items(),
        key=lambda item: (-item[1].occurrence_count, item[0].uci),
    )
    return {move: rank for rank, (move, _) in enumerate(ordered, start=1)}


def build_position_comparison(
    profile: PersonalPositionProfile, reference_index: PositionIndex
) -> PositionComparison:
    """Derive one position's comparison row set. Mutates nothing."""
    reference_stats = reference_index.get(profile.position)
    ranks = _reference_ranks(reference_stats)
    reference_decisions = reference_stats.decisions if reference_stats else {}
    reference_occurrences = reference_stats.occurrence_count if reference_stats else 0

    primary = profile.primary_move
    move_keys = sorted(
        {p.move for p in profile.moves} | set(reference_decisions),
        key=lambda move: move.uci,
    )
    reference_top = min(
        reference_decisions.items(),
        key=lambda item: (-item[1].occurrence_count, item[0].uci),
        default=None,
    )
    reference_top_move = reference_top[0] if reference_top else None

    rows: list[MoveComparison] = []
    for move in move_keys:
        personal = profile.move_profile(move)
        reference = reference_decisions.get(move)
        rows.append(
            MoveComparison(
                move=move.uci,
                personal_occurrence_count=personal.occurrence_count if personal else 0,
                personal_distinct_game_count=(
                    personal.distinct_game_count if personal else 0
                ),
                personal_move_rate=profile.move_rate(move),
                reference_occurrence_count=(
                    reference.occurrence_count if reference else 0
                ),
                reference_distinct_game_count=(
                    reference.distinct_game_count if reference else 0
                ),
                reference_move_rate=(
                    _rate(reference.occurrence_count, reference_occurrences)
                    if reference
                    else None
                ),
                reference_wins=reference.outcomes.wins if reference else 0,
                reference_draws=reference.outcomes.draws if reference else 0,
                reference_losses=reference.outcomes.losses if reference else 0,
                reference_rank=ranks.get(move),
                is_personal_move=personal is not None,
                is_personal_primary_move=move == primary,
                is_reference_top_move=(
                    reference_top_move is not None and move == reference_top_move
                ),
            )
        )

    # Most-played in the reference population first, then most-played by the
    # player, then UCI: a total order, so the artefact is byte-stable.
    rows.sort(
        key=lambda row: (
            -row.reference_occurrence_count,
            -row.personal_occurrence_count,
            row.move,
        )
    )
    primary_row = next(row for row in rows if row.move == primary.uci)

    return PositionComparison(
        position_epd=profile.position.epd,
        side_to_move=_side_to_move(profile.position.epd),
        personal_occurrence_count=profile.occurrence_count,
        personal_distinct_game_count=profile.distinct_game_count,
        reference_occurrence_count=reference_occurrences,
        reference_distinct_game_count=(
            reference_stats.distinct_game_count if reference_stats else 0
        ),
        reference_wins=reference_stats.outcomes.wins if reference_stats else 0,
        reference_draws=reference_stats.outcomes.draws if reference_stats else 0,
        reference_losses=reference_stats.outcomes.losses if reference_stats else 0,
        personal_primary_move=primary.uci,
        reference_top_move=reference_top_move.uci if reference_top_move else None,
        personal_primary_move_reference_rank=primary_row.reference_rank,
        personal_primary_move_reference_rate=primary_row.reference_move_rate,
        moves=tuple(rows),
    )


def build_comparison(
    target_set: RecurringTargetSet, reference_index: PositionIndex
) -> tuple[PositionComparison, ...]:
    """Derive every recurring position's comparison, in a total order.

    Positions the reference corpus never reached are included with zeroed
    reference evidence: an uncovered recurring position is a finding about
    the corpus, not a row to hide.
    """
    return tuple(
        build_position_comparison(profile, reference_index)
        for profile in target_set.ordered_profiles()
    )
