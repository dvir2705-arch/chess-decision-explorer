from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .domain import DecisionKey, DecisionObservation, MoveKey, Outcome, PositionKey


@dataclass
class OutcomeCounts:
    """Mutable accumulator of actor-relative game outcomes.

    Counts are distinct-game based: each contributing game is recorded once.
    """

    wins: int = 0
    draws: int = 0
    losses: int = 0

    def record(self, outcome: Outcome) -> None:
        """Record exactly one game outcome."""
        if outcome is Outcome.WIN:
            self.wins += 1
        elif outcome is Outcome.DRAW:
            self.draws += 1
        elif outcome is Outcome.LOSS:
            self.losses += 1
        else:  # pragma: no cover - defensive
            raise ValueError(f"Unknown outcome: {outcome!r}")

    @property
    def game_count(self) -> int:
        return self.wins + self.draws + self.losses

    @property
    def score_rate(self) -> float | None:
        """Points-per-game in [0.0, 1.0], or None when no games were recorded."""
        total = self.game_count
        if total == 0:
            return None
        return (self.wins + 0.5 * self.draws) / total


@dataclass
class DecisionStats:
    """Mutable occurrence/outcome statistics for one decision.

    The owning PositionStats already keys this by MoveKey, and the position
    is fixed by the PositionIndex entry, so neither key is stored again.
    """

    occurrence_count: int = 0
    distinct_game_count: int = 0
    outcomes: OutcomeCounts = field(default_factory=OutcomeCounts)


@dataclass
class PositionStats:
    """Mutable occurrence/outcome statistics for one position."""

    occurrence_count: int = 0
    distinct_game_count: int = 0
    outcomes: OutcomeCounts = field(default_factory=OutcomeCounts)
    decisions: dict[MoveKey, DecisionStats] = field(default_factory=dict)

    def choice_rate(self, move: MoveKey) -> float:
        """Occurrence-based share of this position's occurrences that chose
        ``move``. Returns 0.0 for a move never seen in this position."""
        decision = self.decisions.get(move)
        if decision is None or self.occurrence_count == 0:
            return 0.0
        return decision.occurrence_count / self.occurrence_count


class PositionIndex:
    """Aggregates factual occurrence/outcome statistics over observed games.

    One generic index is used for every population; personal and reference
    data live in separate instances of this same class. The index records
    what happened only -- it never judges whether a move was good or bad.

    The index does not retain the game_ids it has already seen. It assumes
    each logical game_id is submitted at most once across ``add_game`` calls;
    preventing duplicate ingestion of the same game across calls is the
    responsibility of the future ingestion/corpus layer, not this index.
    """

    def __init__(self) -> None:
        self._positions: dict[PositionKey, PositionStats] = {}

    def add_game(self, observations: Iterable[DecisionObservation]) -> None:
        """Fold one game's observations into the index.

        All observations must share a single game_id. Every observation
        increments occurrence counts; distinct-game counts and outcome
        counts are incremented at most once per position and once per
        decision for this game.

        The caller is responsible for submitting each logical game_id at
        most once: the index keeps no record of previously added game_ids,
        so a game re-submitted in a later call would be double-counted.
        """
        game = list(observations)
        if not game:
            raise ValueError("add_game requires at least one observation")

        game_ids = {obs.game_id for obs in game}
        if len(game_ids) != 1:
            raise ValueError(
                f"all observations in one add_game call must share one game_id; "
                f"got {sorted(game_ids)!r}"
            )

        # Validate outcome consistency before mutating any state, so a
        # rejected game leaves the index untouched. The decision-level check
        # runs first: a repeated identical DecisionKey necessarily repeats
        # its PositionKey too, so checking the position first would mask the
        # more specific decision conflict and leave that branch unreachable.
        position_outcome: dict[PositionKey, Outcome] = {}
        decision_outcome: dict[DecisionKey, Outcome] = {}
        for obs in game:
            existing = decision_outcome.get(obs.decision)
            if existing is not None and existing is not obs.outcome:
                raise ValueError(
                    f"conflicting outcomes for repeated decision "
                    f"({obs.decision.position_before}, {obs.decision.move}) "
                    f"in game {obs.game_id}: {existing} vs {obs.outcome}"
                )
            decision_outcome.setdefault(obs.decision, obs.outcome)

            position = obs.decision.position_before
            existing = position_outcome.get(position)
            if existing is not None and existing is not obs.outcome:
                raise ValueError(
                    f"conflicting outcomes for repeated position {position} "
                    f"in game {obs.game_id}: {existing} vs {obs.outcome}"
                )
            position_outcome.setdefault(position, obs.outcome)

        # Temporary per-game sets: they gate distinct-game increments for this
        # call only and are discarded when it returns. No game-ID sets are
        # retained in PositionStats or DecisionStats.
        counted_positions: set[PositionKey] = set()
        counted_decisions: set[DecisionKey] = set()

        for obs in game:
            position = obs.decision.position_before
            move = obs.decision.move

            position_stats = self._positions.get(position)
            if position_stats is None:
                position_stats = PositionStats()
                self._positions[position] = position_stats
            position_stats.occurrence_count += 1

            decision_stats = position_stats.decisions.get(move)
            if decision_stats is None:
                decision_stats = DecisionStats()
                position_stats.decisions[move] = decision_stats
            decision_stats.occurrence_count += 1

            if position not in counted_positions:
                counted_positions.add(position)
                position_stats.distinct_game_count += 1
                position_stats.outcomes.record(obs.outcome)

            if obs.decision not in counted_decisions:
                counted_decisions.add(obs.decision)
                decision_stats.distinct_game_count += 1
                decision_stats.outcomes.record(obs.outcome)

    def get(self, position: PositionKey) -> PositionStats | None:
        return self._positions.get(position)

    def __getitem__(self, position: PositionKey) -> PositionStats:
        return self._positions[position]

    def __contains__(self, position: object) -> bool:
        return position in self._positions

    def __len__(self) -> int:
        return len(self._positions)

    def items(self) -> Iterable[tuple[PositionKey, PositionStats]]:
        return self._positions.items()
