"""The personal recurring-position target set.

This is the *only* thing the external scan keeps statistics for. Everything
downstream -- what is matched, what is aggregated, how much memory a run
costs -- is bounded by this set.

Recurrence criterion (v1): a position is recurring when the number of
**distinct personal games** containing it is at least `min_distinct_games`
(default 2). Repeats of the same position inside one personal game are
deliberately not sufficient on their own: a player shuffling in one game has
not faced a recurring decision. Both counts survive into the profile, so the
distinction stays inspectable rather than being collapsed at build time.

`PositionKey` semantics are untouched. No opening name, rating, source, user
id, or time control is folded into the key; the cohort and player identity
this set was built from are recorded *alongside* the keys, as run metadata.

A target set is serialisable. Building it needs personal PGNs; matching
against it does not, so an exported target set lets a reference run be
reproduced, audited, or tested without the personal corpus. An exported file
still contains positions from the player's own games and is therefore
personal data: it belongs in gitignored run output, never in Git.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..aggregation import PositionIndex
from ..domain import MoveKey, PositionKey
from . import POSITION_KEY_SEMANTICS_VERSION, TARGET_SET_FORMAT_VERSION

RECURRENCE_CRITERION_V1 = (
    "distinct_personal_games_containing_position >= min_distinct_games; "
    "repeated occurrences of the same position inside ONE personal game are "
    "not sufficient recurrence by themselves"
)

DEFAULT_MIN_DISTINCT_GAMES = 2


@dataclass(frozen=True, slots=True)
class PersonalMoveProfile:
    """What the player actually played in one recurring position.

    `occurrence_count` counts every time the move was played, including
    genuine repeats inside one game. `distinct_game_count` counts the games
    that contain it at most once each. The two are different questions and
    are never merged.
    """

    move: MoveKey
    occurrence_count: int
    distinct_game_count: int

    def __post_init__(self) -> None:
        if self.occurrence_count < 1:
            raise ValueError(
                f"occurrence_count must be positive, got {self.occurrence_count!r}"
            )
        if not 1 <= self.distinct_game_count <= self.occurrence_count:
            raise ValueError(
                f"distinct_game_count must be in [1, occurrence_count], got "
                f"{self.distinct_game_count!r} with occurrence_count "
                f"{self.occurrence_count!r}"
            )


@dataclass(frozen=True, slots=True)
class PersonalPositionProfile:
    """One recurring personal position and the player's choices in it."""

    position: PositionKey
    occurrence_count: int
    distinct_game_count: int
    moves: tuple[PersonalMoveProfile, ...]

    def __post_init__(self) -> None:
        if self.occurrence_count < 1:
            raise ValueError(
                f"occurrence_count must be positive, got {self.occurrence_count!r}"
            )
        if not 1 <= self.distinct_game_count <= self.occurrence_count:
            raise ValueError(
                f"distinct_game_count must be in [1, occurrence_count], got "
                f"{self.distinct_game_count!r}"
            )
        if not self.moves:
            raise ValueError(
                f"a recurring position must carry at least one personal move: "
                f"{self.position}"
            )
        ucis = [profile.move.uci for profile in self.moves]
        if len(set(ucis)) != len(ucis):
            raise ValueError(f"duplicate personal move in position {self.position}")

    @property
    def primary_move(self) -> MoveKey:
        """The player's most-played move here.

        `moves` is stored in a total, deterministic order (most occurrences
        first, then most distinct games, then UCI ascending), so this is
        stable across runs. It is an anchor for reporting -- "the choice the
        player actually makes most often" -- and NOT a claim about intended
        repertoire, correctness, or preference.
        """
        return self.moves[0].move

    def move_profile(self, move: MoveKey) -> PersonalMoveProfile | None:
        for profile in self.moves:
            if profile.move == move:
                return profile
        return None

    def move_rate(self, move: MoveKey) -> float | None:
        """Occurrence-based share of this position's personal occurrences
        that chose `move`; `None` for a move the player never played here.

        Occurrence based, matching `PositionStats.choice_rate`. `None` rather
        than `0.0` so "never played" is distinguishable from a computed zero.
        """
        profile = self.move_profile(move)
        if profile is None:
            return None
        return profile.occurrence_count / self.occurrence_count

    def as_json_dict(self) -> dict[str, Any]:
        return {
            "epd": self.position.epd,
            "occurrence_count": self.occurrence_count,
            "distinct_game_count": self.distinct_game_count,
            "moves": [
                {
                    "uci": profile.move.uci,
                    "occurrence_count": profile.occurrence_count,
                    "distinct_game_count": profile.distinct_game_count,
                }
                for profile in self.moves
            ],
        }

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> "PersonalPositionProfile":
        return cls(
            position=PositionKey(payload["epd"]),
            occurrence_count=payload["occurrence_count"],
            distinct_game_count=payload["distinct_game_count"],
            moves=tuple(
                PersonalMoveProfile(
                    move=MoveKey(entry["uci"]),
                    occurrence_count=entry["occurrence_count"],
                    distinct_game_count=entry["distinct_game_count"],
                )
                for entry in payload["moves"]
            ),
        )


def _sorted_move_profiles(
    decisions: dict[MoveKey, Any]
) -> tuple[PersonalMoveProfile, ...]:
    """Move profiles in a total, deterministic order.

    Most occurrences first, then most distinct games, then UCI ascending.
    The final UCI key makes the order total, so two runs over the same data
    always produce byte-identical artefacts.
    """
    profiles = [
        PersonalMoveProfile(
            move=move,
            occurrence_count=stats.occurrence_count,
            distinct_game_count=stats.distinct_game_count,
        )
        for move, stats in decisions.items()
    ]
    profiles.sort(
        key=lambda profile: (
            -profile.occurrence_count,
            -profile.distinct_game_count,
            profile.move.uci,
        )
    )
    return tuple(profiles)


@dataclass(frozen=True)
class RecurringTargetSet:
    """The player's recurring positions, plus the identity of the personal
    population they came from.

    Membership testing is the hot path of the external scan -- it runs once
    per ply of every accepted reference game -- so positions are held in a
    dict keyed by `PositionKey`, and `__contains__` is a plain dict lookup.

    The cohort and player identity are recorded here as *metadata about the
    set*, never inside a `PositionKey`.
    """

    positions: dict[PositionKey, PersonalPositionProfile]
    min_distinct_games: int
    cohort: str
    player_label: str
    personal_source_declaration: dict[str, Any]

    def __post_init__(self) -> None:
        if type(self.min_distinct_games) is not int or self.min_distinct_games < 1:
            raise ValueError(
                f"min_distinct_games must be a positive int, got "
                f"{self.min_distinct_games!r}"
            )
        if not self.cohort or not self.cohort.strip():
            raise ValueError("cohort must not be empty or whitespace-only")
        if not self.player_label or not self.player_label.strip():
            raise ValueError("player_label must not be empty or whitespace-only")

    def __contains__(self, position: object) -> bool:
        return position in self.positions

    def __len__(self) -> int:
        return len(self.positions)

    def get(self, position: PositionKey) -> PersonalPositionProfile | None:
        return self.positions.get(position)

    def ordered_profiles(self) -> tuple[PersonalPositionProfile, ...]:
        """Every profile in a total, deterministic order: most distinct
        personal games first, then most occurrences, then EPD ascending."""
        profiles = list(self.positions.values())
        profiles.sort(
            key=lambda profile: (
                -profile.distinct_game_count,
                -profile.occurrence_count,
                profile.position.epd,
            )
        )
        return tuple(profiles)

    def __iter__(self) -> Iterator[PersonalPositionProfile]:
        return iter(self.ordered_profiles())

    @property
    def total_personal_occurrences(self) -> int:
        return sum(profile.occurrence_count for profile in self.positions.values())

    @property
    def total_personal_distinct_games(self) -> int:
        """Summed per-position distinct-game counts.

        NOT the number of personal games: one game contributes to every
        recurring position it contains, so this sum double-counts games
        across positions by design. It is a workload measure, never a
        population size.
        """
        return sum(profile.distinct_game_count for profile in self.positions.values())

    def as_manifest_dict(self) -> dict[str, Any]:
        return {
            "target_set_format_version": TARGET_SET_FORMAT_VERSION,
            "position_key_semantics_version": POSITION_KEY_SEMANTICS_VERSION,
            "recurrence_criterion": RECURRENCE_CRITERION_V1,
            "min_distinct_games": self.min_distinct_games,
            "personal_cohort": self.cohort,
            "player_label": self.player_label,
            "target_positions": len(self.positions),
            "total_personal_occurrences": self.total_personal_occurrences,
            "total_personal_distinct_game_counts": self.total_personal_distinct_games,
            "personal_source_declaration": self.personal_source_declaration,
        }

    def as_json_dict(self) -> dict[str, Any]:
        payload = self.as_manifest_dict()
        payload["positions"] = [
            profile.as_json_dict() for profile in self.ordered_profiles()
        ]
        return payload

    def write_json(self, path: Path) -> None:
        path.write_text(
            json.dumps(self.as_json_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> "RecurringTargetSet":
        """Rebuild a target set, refusing one written under different
        `PositionKey` semantics.

        A target set whose keys mean something else cannot be matched against
        a corpus scanned under these semantics, so the mismatch is an error
        rather than a silently wrong comparison.
        """
        recorded = payload.get("position_key_semantics_version")
        if recorded != POSITION_KEY_SEMANTICS_VERSION:
            raise ValueError(
                f"target set was written under position key semantics "
                f"{recorded!r}, but this build matches under "
                f"{POSITION_KEY_SEMANTICS_VERSION!r}; the two are not "
                f"comparable"
            )
        recorded_format = payload.get("target_set_format_version")
        if recorded_format != TARGET_SET_FORMAT_VERSION:
            raise ValueError(
                f"unsupported target set format {recorded_format!r}; expected "
                f"{TARGET_SET_FORMAT_VERSION!r}"
            )
        profiles = [
            PersonalPositionProfile.from_json_dict(entry)
            for entry in payload["positions"]
        ]
        positions = {profile.position: profile for profile in profiles}
        if len(positions) != len(profiles):
            raise ValueError("target set file contains duplicate positions")
        return cls(
            positions=positions,
            min_distinct_games=payload["min_distinct_games"],
            cohort=payload["personal_cohort"],
            player_label=payload["player_label"],
            personal_source_declaration=payload.get(
                "personal_source_declaration", {}
            ),
        )

    @classmethod
    def read_json(cls, path: Path) -> "RecurringTargetSet":
        return cls.from_json_dict(json.loads(path.read_text(encoding="utf-8")))


def build_target_set(
    index: PositionIndex,
    *,
    cohort: str,
    player_label: str,
    min_distinct_games: int = DEFAULT_MIN_DISTINCT_GAMES,
    personal_source_declaration: dict[str, Any] | None = None,
) -> RecurringTargetSet:
    """Select the recurring positions of one personal cohort index.

    `index` is a single cohort's `PositionIndex` -- the three personal
    cohort indexes are never merged, so a run compares exactly one cohort.
    A position is kept when its personal `distinct_game_count` is at least
    `min_distinct_games`; occurrence counts are carried through untouched
    and never substituted for the recurrence test.

    The source index is only read. Nothing here mutates personal statistics.
    """
    if type(min_distinct_games) is not int or min_distinct_games < 1:
        raise ValueError(
            f"min_distinct_games must be a positive int, got {min_distinct_games!r}"
        )

    positions: dict[PositionKey, PersonalPositionProfile] = {}
    for position, stats in index.items():
        if stats.distinct_game_count < min_distinct_games:
            continue
        positions[position] = PersonalPositionProfile(
            position=position,
            occurrence_count=stats.occurrence_count,
            distinct_game_count=stats.distinct_game_count,
            moves=_sorted_move_profiles(stats.decisions),
        )

    return RecurringTargetSet(
        positions=positions,
        min_distinct_games=min_distinct_games,
        cohort=cohort,
        player_label=player_label,
        personal_source_declaration=personal_source_declaration or {},
    )
