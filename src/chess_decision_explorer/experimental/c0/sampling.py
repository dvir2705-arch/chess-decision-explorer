"""Deterministic, streaming, source-agnostic C0 decision sampling.

EXPERIMENTAL. This module never changes production ingestion semantics: it
reuses `chess_decision_explorer.ingestion`'s accepted-record construction so
a C0-eligible game is exactly a game production ingestion would accept, and
it reuses `personal.classify_time_control` so C0's Rapid/Blitz definition is
the one already in the repository.

The only behaviour C0 adds on top is *tolerance*: production
`iter_pgn_records` raises on the first unacceptable record, which cannot scan
a public corpus. The scanner here counts a rejected record and moves on. Per
the recorded architectural rule for future tolerant corpus ingestion, a
skipped record still consumes its source ordinal, so game identity stays
reproducible.

Nothing here retains the corpus: one `chess.pgn.Game` is read, reduced to at
most one sampled decision, and dropped before the next record is read.

Reproducibility. A game's identity is `source_id + ':' + zero_based_source
_ordinal`, and every deterministic draw is keyed on that identity, so the
guarantee C0 actually provides is:

    same `source_id`
    + same source ordering and content
    + same seed
    + same sampling configuration
    -> same sampled roots

The ordinal is a stream position, so this is NOT independent of stream order:
reordering the corpus, or inserting or removing an earlier record, shifts
later ordinals and changes which roots are drawn, and so does changing
`source_id`. What it IS independent of is where the *scan* starts within one
unchanged stream -- `skip_records` and a scan window preserve every record's
ordinal, so the identity and the draw of any record they reach are unchanged.
C0 does not hash the corpus and does not verify the stream; the recorded
`source_id` is the declaration the run is reproducible against.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import TextIO

import chess
import chess.pgn

from ...domain import DecisionKey, MoveKey, PositionKey
from ...engine import RequestValidationError, reconstruct_board
from ...ingestion import GameRecord, _build_record
from ...personal import TimeControlCategory, classify_time_control

GAME_IDENTITY_SCHEME = "source_id + ':' + zero_based_source_ordinal"
"""How a scanned record's `game_id` is formed. Recorded in the run manifest,
because every deterministic selection digest is keyed on that `game_id`."""

SAMPLING_REPRODUCIBILITY_GUARANTEE = (
    "Same source_id + same source ordering/content + same seed + same "
    "sampling configuration -> same sampled roots. Game identity is "
    f"{GAME_IDENTITY_SCHEME}, so the draw depends on each record's ordinal "
    "in the stream: reordering the corpus, inserting or removing an earlier "
    "record, or changing source_id changes which roots are drawn. Starting "
    "the scan later in the SAME stream (skip_records, a scan window) "
    "preserves ordinals and therefore preserves identities and draws. C0 "
    "does not hash or verify the corpus."
)

# --- rejection accounting ---------------------------------------------------


class GameRejection(Enum):
    """Why a scanned PGN record never produced a sampled decision."""

    PARSE_REJECTED = "parse_rejected"
    """Production ingestion would reject the record (PGN errors, unsupported
    variant / Chess960, non-completed result, malformed rating header)."""

    NOT_RATED = "not_rated"
    TERMINATION_EXCLUDED = "termination_excluded"
    TIME_CONTROL_NOT_ELIGIBLE = "time_control_not_eligible"
    RATING_MISSING = "rating_missing"
    RATING_OUT_OF_BAND = "rating_out_of_band"
    TOO_FEW_PLIES = "too_few_plies"
    REPLAY_FAILED = "replay_failed"
    NO_ELIGIBLE_DECISION = "no_eligible_decision"
    DESELECTED_BY_ACCEPT_RATE = "deselected_by_accept_rate"
    SKIPPED_BY_OFFSET = "skipped_by_offset"


class RootRejection(Enum):
    """Why one candidate root (one ply of an otherwise eligible game) is not
    usable for the primary measurement sample."""

    TERMINAL_ROOT = "terminal_root"
    """Rule-terminal canonical root (insufficient material); Step 4A refuses
    terminal positions as decision points."""

    ONE_LEGAL_MOVE = "one_legal_move"
    """Only one legal move: there is no decision and no alternative."""

    ROOT_NOT_CANONICAL = "root_not_canonical"
    """The position key does not round-trip through canonical
    reconstruction, so it is not a `CANONICAL_POSITION_V1` root."""


@dataclass(slots=True)
class SamplingCounters:
    """Mutable accounting for one scan. Counts only; no corpus retention."""

    records_scanned: int = 0
    games_accepted_by_filters: int = 0
    decisions_sampled: int = 0
    candidate_roots_examined: int = 0
    candidate_roots_eligible: int = 0
    game_rejections: Counter = field(default_factory=Counter)
    root_rejections: Counter = field(default_factory=Counter)

    def reject_game(self, reason: GameRejection) -> None:
        self.game_rejections[reason.value] += 1

    def reject_root(self, reason: RootRejection) -> None:
        self.root_rejections[reason.value] += 1

    def as_dict(self) -> dict:
        return {
            "records_scanned": self.records_scanned,
            "games_accepted_by_filters": self.games_accepted_by_filters,
            "decisions_sampled": self.decisions_sampled,
            "candidate_roots_examined": self.candidate_roots_examined,
            "candidate_roots_eligible": self.candidate_roots_eligible,
            "game_rejections": {
                reason.value: self.game_rejections.get(reason.value, 0)
                for reason in GameRejection
            },
            "root_rejections": {
                reason.value: self.root_rejections.get(reason.value, 0)
                for reason in RootRejection
            },
        }

    @property
    def rejected_games(self) -> int:
        return sum(self.game_rejections.values())


class ScanTermination(Enum):
    """How a scan ended -- decisive for whether the sample is a declared
    prefix/window of the stream rather than a pass over all of it."""

    STREAM_EXHAUSTED = "stream_exhausted"
    SAMPLE_TARGET_REACHED = "sample_target_reached"
    SCAN_LIMIT_REACHED = "scan_limit_reached"
    NOT_FINISHED = "not_finished"

    @property
    def is_declared_prefix(self) -> bool:
        """True when the scan stopped before the stream was exhausted, so the
        sample is a declared prefix/window and NEVER representative of the
        complete source population."""
        return self is not ScanTermination.STREAM_EXHAUSTED


# --- eligibility ------------------------------------------------------------

DEFAULT_EXCLUDED_TERMINATIONS = frozenset({"abandoned", "rules infraction"})
DEFAULT_ELIGIBLE_CATEGORIES = (
    TimeControlCategory.RAPID,
    TimeControlCategory.BLITZ,
)


@dataclass(frozen=True, slots=True)
class EligibilityFilters:
    """C0 pilot eligibility. Every dimension is configuration, recorded in
    the run manifest; none of it is a production rule."""

    min_plies: int
    min_rating: int | None = None
    max_rating: int | None = None
    require_rated: bool = True
    eligible_categories: tuple[TimeControlCategory, ...] = DEFAULT_ELIGIBLE_CATEGORIES
    excluded_terminations: frozenset[str] = DEFAULT_EXCLUDED_TERMINATIONS

    def __post_init__(self) -> None:
        if type(self.min_plies) is not int or self.min_plies < 1:
            raise ValueError(f"min_plies must be a positive int, got {self.min_plies!r}")
        for name in ("min_rating", "max_rating"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a non-negative int or None, got {value!r}")
        if (
            self.min_rating is not None
            and self.max_rating is not None
            and self.min_rating > self.max_rating
        ):
            raise ValueError(
                f"min_rating {self.min_rating} exceeds max_rating {self.max_rating}"
            )
        if not self.eligible_categories:
            raise ValueError("at least one eligible time-control category is required")
        for category in self.eligible_categories:
            if not isinstance(category, TimeControlCategory):
                raise ValueError("eligible_categories must hold TimeControlCategory values")

    def as_manifest_dict(self) -> dict:
        return {
            "min_plies": self.min_plies,
            "min_rating": self.min_rating,
            "max_rating": self.max_rating,
            "rating_band_scope": "both_players",
            "require_rated": self.require_rated,
            "eligible_time_control_categories": [
                category.value for category in self.eligible_categories
            ],
            "excluded_terminations": sorted(self.excluded_terminations),
            "time_control_classifier": (
                "chess_decision_explorer.personal.classify_time_control "
                "(Chess.com estimated-duration model: base + 40*increment)"
            ),
        }


_RATED_TOKEN = "rated"


def is_rated_event(event: str | None) -> bool:
    """Whether a PGN ``Event`` names a rated game.

    Word-level, so ``"Casual Blitz game"`` is not rated and
    ``"Rated Blitz tournament https://..."`` is. An absent ``Event`` is not
    treated as rated: C0 never guesses.
    """
    if not event:
        return False
    return _RATED_TOKEN in event.casefold().replace("-", " ").split()


# --- deterministic selection ------------------------------------------------


def _digest_fraction(seed: int, domain: str, game_id: str) -> float:
    """A deterministic value in ``[0, 1)`` from ``(seed, domain, game_id)``.

    Keyed on the game's identity, which is
    ``source_id + ':' + zero_based_source_ordinal``. A record therefore draws
    the same value wherever a scan over the SAME stream starts, because
    `skip_records` and a scan window preserve ordinals -- but the draw does
    depend on that ordinal, so it changes if the corpus is reordered, if an
    earlier record is inserted or removed, or if `source_id` changes.
    """
    digest = hashlib.blake2b(
        f"{seed}\x00{domain}\x00{game_id}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") / 2**64


def _select_index(seed: int, game_id: str, count: int) -> int:
    """Deterministically choose one of ``count`` eligible roots."""
    if count <= 0:
        raise ValueError("cannot select from an empty candidate set")
    return int(_digest_fraction(seed, "ply", game_id) * count)


# --- sampled decision -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SampledDecision:
    """One sampled canonical decision plus its source provenance."""

    source_id: str
    source_ordinal: int
    game_id: str
    decision: DecisionKey
    ply_index: int
    actor_color: str
    actor_rating: int | None
    opponent_rating: int | None
    white_rating: int | None
    black_rating: int | None
    result: str
    outcome_for_actor: str
    time_control: str | None
    time_control_category: str
    event: str | None
    termination: str | None
    date: str | None
    game_ply_count: int
    eligible_root_count: int

    @property
    def root_id(self) -> str:
        return f"{self.game_id}@{self.ply_index}"

    def as_dict(self) -> dict:
        return {
            "root_id": self.root_id,
            "source_id": self.source_id,
            "source_ordinal": self.source_ordinal,
            "game_id": self.game_id,
            "position_epd": self.decision.position_before.epd,
            "observed_move": self.decision.move.uci,
            "ply_index": self.ply_index,
            "actor_color": self.actor_color,
            "actor_rating": self.actor_rating,
            "opponent_rating": self.opponent_rating,
            "white_rating": self.white_rating,
            "black_rating": self.black_rating,
            "result": self.result,
            "outcome_for_actor": self.outcome_for_actor,
            "time_control": self.time_control,
            "time_control_category": self.time_control_category,
            "event": self.event,
            "termination": self.termination,
            "date": self.date,
            "game_ply_count": self.game_ply_count,
            "eligible_root_count": self.eligible_root_count,
        }


# --- streaming scan ---------------------------------------------------------


def iter_raw_games(handle: TextIO, source_id: str) -> Iterator[tuple[int, str, chess.pgn.Game]]:
    """Yield ``(ordinal, game_id, game)`` one PGN record at a time.

    ``game_id`` is ``source_id + ':' + zero_based_source_ordinal``
    (:data:`GAME_IDENTITY_SCHEME`). The ordinal advances for EVERY logical
    record, including records a caller later rejects, so a tolerant scan
    assigns the same identity to the same record as a strict one would. Only
    one game is ever held.
    """
    if not source_id or not source_id.strip():
        raise ValueError("source_id must not be empty or whitespace-only")
    ordinal = 0
    while True:
        game = chess.pgn.read_game(handle)
        if game is None:
            return
        yield ordinal, f"{source_id}:{ordinal}", game
        ordinal += 1


def eligible_roots(record: GameRecord, counters: SamplingCounters) -> list[tuple[int, DecisionKey, bool]]:
    """Every ply of ``record`` usable as a primary-measurement canonical root.

    Returns ``(ply_index, decision, actor_is_white)`` triples and counts the
    reason each rejected root was rejected. Replaying one game is bounded by
    that game; the corpus is never materialised.
    """
    board = chess.Board(record.initial_fen)
    roots: list[tuple[int, DecisionKey, bool]] = []
    for ply_index, move in enumerate(record.moves):
        if not board.is_legal(move):
            raise ValueError(
                f"illegal move {move.uci()} at ply {ply_index} in game {record.game_id}"
            )
        counters.candidate_roots_examined += 1
        rejection = _root_rejection(board)
        if rejection is None:
            counters.candidate_roots_eligible += 1
            roots.append(
                (
                    ply_index,
                    DecisionKey(PositionKey.from_board(board), MoveKey.from_move(move)),
                    board.turn == chess.WHITE,
                )
            )
        else:
            counters.reject_root(rejection)
        board.push(move)
    return roots


def _root_rejection(board: chess.Board) -> RootRejection | None:
    if board.legal_moves.count() == 1:
        return RootRejection.ONE_LEGAL_MOVE
    if board.is_insufficient_material():
        return RootRejection.TERMINAL_ROOT
    key = PositionKey.from_board(board)
    try:
        canonical = reconstruct_board(key)
    except RequestValidationError:
        return RootRejection.ROOT_NOT_CANONICAL
    if not canonical.is_valid() or PositionKey.from_board(canonical) != key:
        return RootRejection.ROOT_NOT_CANONICAL
    return None


class PilotSampler:
    """Deterministic streaming sampler: at most one decision per game.

    Determinism comes from ``(seed, game_id)`` digests, and ``game_id`` is
    ``source_id + ':' + zero_based_source_ordinal``. The guarantee is
    therefore :data:`SAMPLING_REPRODUCIBILITY_GUARANTEE`: same ``source_id``,
    same source ordering/content, same seed, same sampling configuration ->
    same sampled roots. It is stable against starting the scan later in the
    same stream (ordinals are preserved), not against a reordered or edited
    corpus or a different ``source_id``.

    Acceptance is evaluated in stream order and the scan stops as soon as the
    sample target or the scan window is reached -- which makes the sample a
    DECLARED PREFIX of the stream, recorded as such in the manifest.
    """

    def __init__(
        self,
        *,
        source_id: str,
        filters: EligibilityFilters,
        seed: int,
        sample_size: int,
        accept_rate: float = 1.0,
        max_records_scanned: int | None = None,
        skip_records: int = 0,
    ) -> None:
        if not source_id or not source_id.strip():
            raise ValueError("source_id must not be empty or whitespace-only")
        if type(seed) is not int:
            raise ValueError(f"seed must be an int, got {seed!r}")
        if type(sample_size) is not int or sample_size < 1:
            raise ValueError(f"sample_size must be a positive int, got {sample_size!r}")
        if not 0.0 < accept_rate <= 1.0:
            raise ValueError(f"accept_rate must be in (0, 1], got {accept_rate!r}")
        if max_records_scanned is not None and (
            type(max_records_scanned) is not int or max_records_scanned < 1
        ):
            raise ValueError(
                f"max_records_scanned must be a positive int or None, got "
                f"{max_records_scanned!r}"
            )
        if type(skip_records) is not int or skip_records < 0:
            raise ValueError(f"skip_records must be a non-negative int, got {skip_records!r}")

        self.source_id = source_id
        self.filters = filters
        self.seed = seed
        self.sample_size = sample_size
        self.accept_rate = accept_rate
        self.max_records_scanned = max_records_scanned
        self.skip_records = skip_records
        self.counters = SamplingCounters()
        self.termination = ScanTermination.NOT_FINISHED

    def as_manifest_dict(self) -> dict:
        return {
            "sampler": "C0_DETERMINISTIC_PREFIX_SAMPLER_V1",
            "source_id": self.source_id,
            "game_identity_scheme": GAME_IDENTITY_SCHEME,
            "reproducibility_guarantee": SAMPLING_REPRODUCIBILITY_GUARANTEE,
            "seed": self.seed,
            "sample_target": self.sample_size,
            "accept_rate": self.accept_rate,
            "max_records_scanned": self.max_records_scanned,
            "skip_records": self.skip_records,
            "one_decision_per_game": True,
            "selection_key": "blake2b(seed, domain, game_id)",
        }

    def iter_sample(self, handle: TextIO) -> Iterator[SampledDecision]:
        """Yield at most ``sample_size`` sampled decisions from ``handle``."""
        self.termination = ScanTermination.NOT_FINISHED
        counters = self.counters
        records = iter_raw_games(handle, self.source_id)
        while True:
            # Checked BEFORE pulling the next record, so a scan window reads
            # exactly `max_records_scanned` records off the stream and leaves
            # the handle positioned at the first record outside the window.
            if (
                self.max_records_scanned is not None
                and counters.records_scanned >= self.max_records_scanned
            ):
                self.termination = ScanTermination.SCAN_LIMIT_REACHED
                return
            try:
                ordinal, game_id, game = next(records)
            except StopIteration:
                break
            counters.records_scanned += 1

            if ordinal < self.skip_records:
                counters.reject_game(GameRejection.SKIPPED_BY_OFFSET)
                continue

            sampled = self._sample_one(ordinal, game_id, game)
            if sampled is not None:
                counters.decisions_sampled += 1
                yield sampled
                if counters.decisions_sampled >= self.sample_size:
                    self.termination = ScanTermination.SAMPLE_TARGET_REACHED
                    return
        self.termination = ScanTermination.STREAM_EXHAUSTED

    def _sample_one(
        self, ordinal: int, game_id: str, game: chess.pgn.Game
    ) -> SampledDecision | None:
        counters = self.counters
        filters = self.filters
        headers = game.headers
        event = headers.get("Event") or None
        termination = headers.get("Termination") or None

        try:
            record = _build_record(game, game_id)
        except ValueError:
            counters.reject_game(GameRejection.PARSE_REJECTED)
            return None

        if filters.require_rated and not is_rated_event(event):
            counters.reject_game(GameRejection.NOT_RATED)
            return None
        if termination and termination.strip().casefold() in filters.excluded_terminations:
            counters.reject_game(GameRejection.TERMINATION_EXCLUDED)
            return None

        category = classify_time_control(record.time_control)
        if category not in filters.eligible_categories:
            counters.reject_game(GameRejection.TIME_CONTROL_NOT_ELIGIBLE)
            return None

        if record.white_rating is None or record.black_rating is None:
            counters.reject_game(GameRejection.RATING_MISSING)
            return None
        for rating in (record.white_rating, record.black_rating):
            if filters.min_rating is not None and rating < filters.min_rating:
                counters.reject_game(GameRejection.RATING_OUT_OF_BAND)
                return None
            if filters.max_rating is not None and rating > filters.max_rating:
                counters.reject_game(GameRejection.RATING_OUT_OF_BAND)
                return None

        if len(record.moves) < filters.min_plies:
            counters.reject_game(GameRejection.TOO_FEW_PLIES)
            return None

        if self.accept_rate < 1.0 and _digest_fraction(
            self.seed, "accept", game_id
        ) >= self.accept_rate:
            counters.reject_game(GameRejection.DESELECTED_BY_ACCEPT_RATE)
            return None

        counters.games_accepted_by_filters += 1

        try:
            roots = eligible_roots(record, counters)
        except ValueError:
            counters.reject_game(GameRejection.REPLAY_FAILED)
            return None
        if not roots:
            counters.reject_game(GameRejection.NO_ELIGIBLE_DECISION)
            return None

        ply_index, decision, actor_is_white = roots[
            _select_index(self.seed, game_id, len(roots))
        ]
        actor_color = chess.WHITE if actor_is_white else chess.BLACK
        return SampledDecision(
            source_id=self.source_id,
            source_ordinal=ordinal,
            game_id=game_id,
            decision=decision,
            ply_index=ply_index,
            actor_color="white" if actor_is_white else "black",
            actor_rating=record.white_rating if actor_is_white else record.black_rating,
            opponent_rating=record.black_rating if actor_is_white else record.white_rating,
            white_rating=record.white_rating,
            black_rating=record.black_rating,
            result=record.result.value,
            outcome_for_actor=record.result.to_outcome(actor_color).name.lower(),
            time_control=record.time_control,
            time_control_category=category.value,
            event=event,
            termination=termination,
            date=record.date,
            game_ply_count=len(record.moves),
            eligible_root_count=len(roots),
        )
