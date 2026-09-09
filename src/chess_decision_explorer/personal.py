from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

import chess

from .aggregation import PositionIndex
from .ingestion import GameRecord, extract_player_observations


class TimeControlCategory(Enum):
    """Time-control cohort a personal game belongs to.

    Classification uses Chess.com's estimated-duration model::

        estimated_seconds = base_seconds + 40 * increment_seconds

    with boundaries ``< 180`` -> BULLET, ``180 <= t < 600`` -> BLITZ,
    ``t >= 600`` -> RAPID. Anything that is not a plain ``integer`` or
    ``integer+integer`` string (including ``None``, blank, and
    correspondence/daily formats) is ``UNKNOWN``.
    """

    RAPID = "rapid"
    BLITZ = "blitz"
    BULLET = "bullet"
    UNKNOWN = "unknown"


_TIME_CONTROL_RE = re.compile(r"^(\d+)(?:\+(\d+))?$")


def classify_time_control(time_control: str | None) -> TimeControlCategory:
    """Map a raw PGN ``TimeControl`` value to a :class:`TimeControlCategory`.

    Only ``"<int>"`` and ``"<int>+<int>"`` forms are recognised. Absent,
    blank, or structurally different values (``"1/86400"``, ``"-"``, ...) are
    reported as ``UNKNOWN`` rather than guessed at or raised on.
    """
    if time_control is None:
        return TimeControlCategory.UNKNOWN
    match = _TIME_CONTROL_RE.match(time_control.strip())
    if match is None:
        return TimeControlCategory.UNKNOWN

    base_seconds = int(match.group(1))
    increment_seconds = int(match.group(2)) if match.group(2) is not None else 0
    estimated_seconds = base_seconds + 40 * increment_seconds

    if estimated_seconds < 180:
        return TimeControlCategory.BULLET
    if estimated_seconds < 600:
        return TimeControlCategory.BLITZ
    return TimeControlCategory.RAPID


@dataclass(frozen=True, slots=True)
class PersonalGameContext:
    """The personal facts of one game: which side the actor played, that
    side's own rating in the game, and the game's time-control cohort."""

    player_color: chess.Color
    personal_rating: int | None
    time_control_category: TimeControlCategory


def resolve_personal_game_context(
    game: GameRecord, player_name: str
) -> PersonalGameContext:
    """Resolve ``player_name`` against ``game`` and return its
    :class:`PersonalGameContext`.

    Matching is case-insensitive via ``casefold()``. A name that matches
    neither side, or both sides, raises ``ValueError``. ``personal_rating``
    is the matched side's own rating header (``None`` when absent).
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

    if matches_white:
        player_color = chess.WHITE
        personal_rating = game.white_rating
    else:
        player_color = chess.BLACK
        personal_rating = game.black_rating

    return PersonalGameContext(
        player_color=player_color,
        personal_rating=personal_rating,
        time_control_category=classify_time_control(game.time_control),
    )


@dataclass(frozen=True, slots=True)
class PersonalAnalysisPolicy:
    """Immutable v1 eligibility policy.

    A game is CORE when the actor's own rating is ``>= minimum`` for its
    time-control category. The product decision for Blitz is ``> 500``; the
    implementation keeps the uniform ``rating >= minimum`` rule and therefore
    configures the Blitz minimum as ``501``. Thresholds are policy, not
    scattered constants, and may change in a later version.
    """

    rapid_min_rating: int = 800
    blitz_min_rating: int = 501
    bullet_min_rating: int = 600

    def __post_init__(self) -> None:
        for name in ("rapid_min_rating", "blitz_min_rating", "bullet_min_rating"):
            value = getattr(self, name)
            # ``type(...) is int`` deliberately rejects ``bool`` and any other
            # subclass; values are validated, never coerced.
            if type(value) is not int or value < 0:
                raise ValueError(
                    f"{name} must be a non-negative int, got {value!r}"
                )

    def minimum_rating(self, category: TimeControlCategory) -> int | None:
        """Minimum CORE rating for ``category``; ``None`` for ``UNKNOWN``."""
        return {
            TimeControlCategory.RAPID: self.rapid_min_rating,
            TimeControlCategory.BLITZ: self.blitz_min_rating,
            TimeControlCategory.BULLET: self.bullet_min_rating,
        }.get(category)


class GameDisposition(Enum):
    """Initial eligibility outcome for one personal game.

    ``zero_decision`` is deliberately not a member: it is a downstream state
    reached only after a game has already been classified ``CORE``.
    """

    CORE = "core"
    LEGACY = "legacy"
    MISSING_RATING = "missing_rating"
    UNKNOWN_TIME_CONTROL = "unknown_time_control"


def classify_disposition(
    context: PersonalGameContext, policy: PersonalAnalysisPolicy
) -> GameDisposition:
    """Classify one game's :class:`GameDisposition` from its context and the
    active policy.

    Order: unknown time control first, then missing rating, then the
    rating-vs-threshold comparison (``CORE`` if ``>=``, else ``LEGACY``).
    """
    if context.time_control_category is TimeControlCategory.UNKNOWN:
        return GameDisposition.UNKNOWN_TIME_CONTROL
    if context.personal_rating is None:
        return GameDisposition.MISSING_RATING

    minimum = policy.minimum_rating(context.time_control_category)
    if context.personal_rating >= minimum:
        return GameDisposition.CORE
    return GameDisposition.LEGACY


@dataclass
class CohortStats:
    """Per-category accounting accumulator.

    For a known category (RAPID / BLITZ / BULLET)::

        games_seen == core_eligible_games + legacy_games + missing_rating_games
        core_eligible_games == indexed_games + zero_decision_games

    ``unknown_time_control_games`` stays ``0`` for those cohorts. The
    separate UNKNOWN accumulator (``PersonalAnalysisResult.unknown_time_control``)
    is the one exception to the first invariant: for it every game only
    advances ``games_seen`` and ``unknown_time_control_games`` in lockstep,
    and it carries no index.

    ``personal_decisions`` is the count of :class:`DecisionObservation`
    objects actually inserted into the cohort index; a zero-decision CORE
    game contributes nothing to it and is not counted as ``indexed``.
    """

    games_seen: int = 0
    core_eligible_games: int = 0
    legacy_games: int = 0
    missing_rating_games: int = 0
    unknown_time_control_games: int = 0
    zero_decision_games: int = 0
    indexed_games: int = 0
    personal_decisions: int = 0


@dataclass(frozen=True, slots=True)
class PersonalCohortResult:
    """One time-control cohort: its own :class:`PositionIndex` and stats."""

    index: PositionIndex
    stats: CohortStats


@dataclass(frozen=True, slots=True)
class DatasetTotals:
    """Dataset-level totals derived from the per-cohort stats."""

    total_games_seen: int
    total_core_eligible: int
    total_legacy: int
    total_missing_rating: int
    total_unknown_time_control: int
    total_zero_decision: int
    total_indexed: int
    total_personal_decisions: int


@dataclass(frozen=True, slots=True)
class PersonalAnalysisResult:
    """Separate CORE indexes and accounting for Rapid / Blitz / Bullet, plus
    the UNKNOWN-time-control accounting.

    The three cohort indexes are never merged: a position is present in a
    cohort's index only if a game of that cohort contributed it.
    """

    rapid: PersonalCohortResult
    blitz: PersonalCohortResult
    bullet: PersonalCohortResult
    unknown_time_control: CohortStats

    def cohort(self, category: TimeControlCategory) -> PersonalCohortResult:
        """The :class:`PersonalCohortResult` for a known category."""
        mapping = {
            TimeControlCategory.RAPID: self.rapid,
            TimeControlCategory.BLITZ: self.blitz,
            TimeControlCategory.BULLET: self.bullet,
        }
        try:
            return mapping[category]
        except KeyError:
            raise ValueError(f"no cohort result for category {category!r}") from None

    @property
    def totals(self) -> DatasetTotals:
        """Derive dataset totals from the per-cohort stats (nothing stored
        twice, so the totals cannot drift from the cohorts)."""
        known = (self.rapid.stats, self.blitz.stats, self.bullet.stats)
        return DatasetTotals(
            total_games_seen=sum(s.games_seen for s in known)
            + self.unknown_time_control.games_seen,
            total_core_eligible=sum(s.core_eligible_games for s in known),
            total_legacy=sum(s.legacy_games for s in known),
            total_missing_rating=sum(s.missing_rating_games for s in known),
            total_unknown_time_control=self.unknown_time_control.unknown_time_control_games,
            total_zero_decision=sum(s.zero_decision_games for s in known),
            total_indexed=sum(s.indexed_games for s in known),
            total_personal_decisions=sum(s.personal_decisions for s in known),
        )


def build_personal_analysis(
    games: Iterable[GameRecord],
    player_name: str,
    policy: PersonalAnalysisPolicy,
) -> PersonalAnalysisResult:
    """Fold a stream of :class:`GameRecord` into per-time-control CORE
    analysis for one player.

    This function is source-independent: it knows nothing about filesystem
    paths, PGN directories, the Chess.com API, or monthly archives -- it
    consumes only the given iterable of already-parsed games.

    For each game: resolve the personal context, classify the disposition,
    and update the relevant cohort's counters. LEGACY, missing-rating, and
    unknown-time-control games never reach a :class:`PositionIndex` and are
    accounted immediately. CORE accounting is per-game transactional: the
    existing :func:`extract_player_observations` is reused and, for a game
    with observations, :meth:`PositionIndex.add_game` is called first; the
    cohort counters are committed only after that risky work succeeds, so a
    CORE game that raises during extraction or indexing contributes nothing.
    A CORE game with no observations counts as ``zero_decision`` (only after
    a successful extraction) and is not indexed.
    """
    cohorts = {
        category: PersonalCohortResult(PositionIndex(), CohortStats())
        for category in (
            TimeControlCategory.RAPID,
            TimeControlCategory.BLITZ,
            TimeControlCategory.BULLET,
        )
    }
    unknown_time_control = CohortStats()

    for game in games:
        context = resolve_personal_game_context(game, player_name)
        disposition = classify_disposition(context, policy)

        if disposition is GameDisposition.UNKNOWN_TIME_CONTROL:
            unknown_time_control.games_seen += 1
            unknown_time_control.unknown_time_control_games += 1
            continue

        cohort = cohorts[context.time_control_category]
        stats = cohort.stats

        # Non-CORE dispositions do no downstream extraction or index
        # mutation, so their accounting is committed immediately.
        if disposition is GameDisposition.MISSING_RATING:
            stats.games_seen += 1
            stats.missing_rating_games += 1
            continue
        if disposition is GameDisposition.LEGACY:
            stats.games_seen += 1
            stats.legacy_games += 1
            continue

        # CORE: per-game transactional accounting. Run every step that can
        # raise -- observation extraction and, when there are observations,
        # PositionIndex.add_game -- before committing any counter, so a CORE
        # game that fails downstream contributes nothing to CohortStats.
        observations = extract_player_observations(game, player_name)
        if not observations:
            stats.games_seen += 1
            stats.core_eligible_games += 1
            stats.zero_decision_games += 1
            continue

        cohort.index.add_game(observations)
        stats.games_seen += 1
        stats.core_eligible_games += 1
        stats.indexed_games += 1
        stats.personal_decisions += len(observations)

    return PersonalAnalysisResult(
        rapid=cohorts[TimeControlCategory.RAPID],
        blitz=cohorts[TimeControlCategory.BLITZ],
        bullet=cohorts[TimeControlCategory.BULLET],
        unknown_time_control=unknown_time_control,
    )
