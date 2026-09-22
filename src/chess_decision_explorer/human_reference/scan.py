"""Streaming, source-agnostic external human-reference scan.

The whole point of this module is what it does **not** build. An external
corpus may hold tens of millions of decision positions; none of them is
stored. One PGN record is read, reduced to at most the decisions whose
`PositionKey` is already in the personal target set, folded into the
reference aggregate, and dropped before the next record is read::

    personal recurring positions -> small target PositionKey set
        -> stream external games
            -> for each decision:
                 PositionKey not in target set -> discard
                 PositionKey in target set     -> aggregate

The invariant that follows is checkable and is asserted by a test:
`len(scan_result.index) <= len(target_set)`. Memory scales with the personal
target set and the moves matched inside it, never with corpus size.

Tolerance. Production `iter_pgn_records` raises on the first unacceptable
record, which cannot scan a public corpus. This scanner counts the rejection
by reason and moves on, and a rejected record still consumes its source
ordinal so game identity stays reproducible -- the same rule the Step 4C.0
scanner follows.

The accepted-game target counts **eligible** games, not raw records. A run
stops when `games_accepted == target_games`, when the stream is exhausted,
or when an explicit scan limit is hit; which of those happened is recorded,
and a run that ran out of input before its target says so.

Actor-relative outcomes. `PositionKey` includes the side to move, so a
reference position that matches a personal position necessarily has the same
side to move. Every outcome recorded here is relative to **that** side:
White to move -> `1-0` is a win and `0-1` a loss; Black to move -> `0-1` is a
win and `1-0` a loss; `1/2-1/2` is a draw either way. Nothing here stores a
White-centric win/loss count.
"""

from __future__ import annotations

import io
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TextIO

import chess
import chess.pgn

from ..aggregation import PositionIndex
from ..domain import DecisionKey, DecisionObservation, MoveKey, PositionKey
from ..ingestion import GameRecord, _build_record, _optional_text, _parse_rating
from ..personal import TimeControlCategory, classify_time_control
from . import POSITION_KEY_SEMANTICS_VERSION
from .target import RecurringTargetSet

GAME_IDENTITY_SCHEME = "source_id + ':' + zero_based_source_ordinal"
"""How a scanned record's transient `game_id` is formed.

The ordinal advances for EVERY logical record, including rejected ones, so a
tolerant scan gives a record the same identity a strict one would. The id is
transient: it gates per-game distinct counting inside one `add_game` call and
is then discarded. No reference game id is retained in any aggregate.
"""

COMPLETED_RESULTS = frozenset({"1-0", "0-1", "1/2-1/2"})

DEFAULT_ELIGIBLE_CATEGORIES = (
    TimeControlCategory.RAPID,
    TimeControlCategory.BLITZ,
)

_RATED_TOKEN = "rated"


def is_rated_event(event: str | None) -> bool:
    """Whether a PGN ``Event`` names a rated game.

    Word-level, so ``"Casual Blitz game"`` is not rated and ``"Rated Blitz
    tournament https://..."`` is. An absent ``Event`` is not treated as
    rated: the scan never guesses a game into the reference population.

    Deliberately the same rule the Step 4C.0 scanner uses. It is restated
    here rather than imported because a production module must not depend on
    the experimental package; a test asserts the two stay in agreement.
    """
    if not event:
        return False
    return _RATED_TOKEN in event.casefold().replace("-", " ").split()


def _is_standard(headers: chess.pgn.Headers) -> bool:
    """Whether a record is ordinary standard chess.

    Mirrors production ingestion's variant checks. `Headers.variant()` raises
    on a variant name python-chess does not know, and an unrecognised variant
    is exactly a non-standard game, so the exception is a rejection rather
    than a crash -- a public corpus must not be able to abort a scan with one
    unusual header.
    """
    try:
        return not (
            headers.is_chess960()
            or headers.is_wild()
            or headers.variant() is not chess.Board
        )
    except ValueError:
        return False


BOT_TITLE = "BOT"
"""The Lichess title marking a Bot API account.

Lichess sets `WhiteTitle`/`BlackTitle` to `BOT` for accounts playing through
the Bot API. Every other title value (GM, IM, FM, CM, NM, WGM, WIM, LM, ...)
marks a titled HUMAN player and must not be affected by this rule.
"""


def is_bot_player(headers: chess.pgn.Headers) -> bool:
    """Whether either side of a record is a Lichess Bot API account.

    Compares the title headers case-insensitively against `BOT` exactly --
    never a substring test, which would also catch a hypothetical title
    containing those letters. An absent title header means an untitled
    player, which is a human.
    """
    for key in ("WhiteTitle", "BlackTitle"):
        title = headers.get(key)
        if title and title.strip().casefold() == BOT_TITLE.casefold():
            return True
    return False


class ReferenceRejection(Enum):
    """Why a scanned record never became an eligible reference game."""

    RESULT_NOT_COMPLETED = "result_not_completed"
    """``*`` or a missing/invalid Result: the game has no outcome to record."""

    VARIANT_NOT_STANDARD = "variant_not_standard"
    """A declared variant, a wild game, or Chess960 castling rights."""

    NOT_RATED = "not_rated"
    PARSE_REJECTED = "parse_rejected"
    """Production record construction refused the record (PGN errors,
    malformed rating header, invalid initial position)."""

    BOT_PLAYER = "bot_player"
    """A side is a Lichess Bot API account (`WhiteTitle`/`BlackTitle` == BOT).

    This is a HUMAN reference cohort, so engine-driven accounts are not part
    of the population. It rejects the BOT title ONLY: GM, IM, FM, NM, WGM and
    every other title marks a titled HUMAN and is accepted normally."""

    TIME_CONTROL_NOT_ELIGIBLE = "time_control_not_eligible"
    TERMINATION_EXCLUDED = "termination_excluded"
    """The PGN ``Termination`` header matched a configured exclusion. Empty by
    default: the scan filters only on what was asked for."""

    RATING_MISSING = "rating_missing"
    RATING_OUT_OF_BAND = "rating_out_of_band"
    TOO_FEW_PLIES = "too_few_plies"
    REPLAY_FAILED = "replay_failed"
    """The main line did not replay legally. Detected before the game is
    counted as accepted, so an unreplayable game contributes nothing -- not
    an acceptance, not a decision, not a statistic."""

    SKIPPED_BY_OFFSET = "skipped_by_offset"


class ScanTermination(Enum):
    """How a scan ended. Decisive for what the run may claim."""

    TARGET_REACHED = "target_reached"
    STREAM_EXHAUSTED = "stream_exhausted"
    SCAN_LIMIT_REACHED = "scan_limit_reached"
    NOT_FINISHED = "not_finished"

    @property
    def reached_target(self) -> bool:
        return self is ScanTermination.TARGET_REACHED

    @property
    def is_declared_prefix(self) -> bool:
        """True when the scan stopped before the stream was exhausted, so the
        accepted games are a declared prefix of the source and are NEVER
        representative of the complete source population."""
        return self is not ScanTermination.STREAM_EXHAUSTED


@dataclass(frozen=True, slots=True)
class ReferenceEligibilityFilters:
    """External-game eligibility. Every dimension is configuration and every
    value is recorded in the run manifest.

    The v1 defaults are exactly the stated requirement -- standard chess,
    completed result, rated, human players only, Rapid or Blitz, a PGN this
    project can parse -- and nothing more. `excluded_terminations` defaults to empty on purpose:
    the scan filters only on what was asked for, and an unrequested filter
    would silently change which games are in the reference population.
    """

    eligible_categories: tuple[TimeControlCategory, ...] = DEFAULT_ELIGIBLE_CATEGORIES
    require_rated: bool = True
    require_standard: bool = True
    require_completed_result: bool = True
    require_human_players: bool = True
    min_plies: int = 1
    min_rating: int | None = None
    max_rating: int | None = None
    require_both_ratings: bool = False
    excluded_terminations: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if type(self.min_plies) is not int or self.min_plies < 0:
            raise ValueError(
                f"min_plies must be a non-negative int, got {self.min_plies!r}"
            )
        for name in ("min_rating", "max_rating"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(
                    f"{name} must be a non-negative int or None, got {value!r}"
                )
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
                raise ValueError(
                    "eligible_categories must hold TimeControlCategory values"
                )

    @property
    def applies_rating_band(self) -> bool:
        return self.min_rating is not None or self.max_rating is not None

    def as_manifest_dict(self) -> dict[str, Any]:
        return {
            "eligible_time_control_categories": [
                category.value for category in self.eligible_categories
            ],
            "require_rated": self.require_rated,
            "require_standard": self.require_standard,
            "require_completed_result": self.require_completed_result,
            "require_human_players": self.require_human_players,
            "bot_rule": (
                "reject when WhiteTitle or BlackTitle equals 'BOT' "
                "(case-insensitive, exact match); every other title marks a "
                "titled HUMAN and is accepted"
            ),
            "min_plies": self.min_plies,
            "min_rating": self.min_rating,
            "max_rating": self.max_rating,
            "rating_band_scope": "both_players" if self.applies_rating_band else None,
            "require_both_ratings": self.require_both_ratings,
            "excluded_terminations": sorted(self.excluded_terminations),
            "rated_rule": (
                "word-level 'rated' token in the PGN Event header; an absent "
                "Event is not rated"
            ),
            "time_control_classifier": (
                "chess_decision_explorer.personal.classify_time_control "
                "(Chess.com estimated-duration model: base + 40*increment)"
            ),
        }


@dataclass(slots=True)
class ScanCounters:
    """Accounting for one scan. Counts only -- no corpus retention.

    `records_scanned` counts logical PGN records read off the stream.
    `games_accepted` counts ELIGIBLE games whose evidence actually entered
    the aggregate. The two are never interchangeable, and the report is
    required to print both.

    `records_header_rejected` counts records dropped on header evidence
    alone, whose movetext was therefore never tokenised, and
    `records_fully_parsed` counts those that survived headers and were built
    into a game. They partition `records_scanned`:
    ``records_header_rejected + records_fully_parsed == records_scanned``.
    """

    records_scanned: int = 0
    records_header_rejected: int = 0
    records_fully_parsed: int = 0
    games_accepted: int = 0
    games_with_match: int = 0
    decisions_examined: int = 0
    decisions_matched: int = 0
    rejections: Counter = field(default_factory=Counter)

    def reject(self, reason: ReferenceRejection) -> None:
        self.rejections[reason.value] += 1

    @property
    def records_rejected(self) -> int:
        return sum(self.rejections.values())

    @property
    def match_rate(self) -> float | None:
        """Matched decisions per examined decision, or `None` when nothing
        was examined."""
        if self.decisions_examined == 0:
            return None
        return self.decisions_matched / self.decisions_examined

    def as_dict(self) -> dict[str, Any]:
        return {
            "records_scanned": self.records_scanned,
            "records_header_rejected": self.records_header_rejected,
            "records_fully_parsed": self.records_fully_parsed,
            "header_rejection_rate": (
                round(self.records_header_rejected / self.records_scanned, 6)
                if self.records_scanned
                else None
            ),
            "games_accepted": self.games_accepted,
            "games_with_match": self.games_with_match,
            "records_rejected": self.records_rejected,
            "decisions_examined": self.decisions_examined,
            "decisions_matched": self.decisions_matched,
            "match_rate": (
                round(self.match_rate, 8) if self.match_rate is not None else None
            ),
            "rejections": {
                reason.value: self.rejections.get(reason.value, 0)
                for reason in ReferenceRejection
            },
        }


@dataclass(frozen=True, slots=True)
class ReferenceScanResult:
    """The aggregate a scan produced, plus what the scan is allowed to claim.

    `index` is a `PositionIndex` holding ONLY target positions, so it is
    bounded by the target set. It is a separate instance from every personal
    index: personal and reference statistics are never merged.
    """

    index: PositionIndex
    counters: ScanCounters
    termination: ScanTermination
    target_games: int
    target_positions: int

    @property
    def games_accepted(self) -> int:
        return self.counters.games_accepted

    @property
    def target_met(self) -> bool:
        """Whether the run actually accepted the eligible games it asked for.

        This is the gate on any headline claim. A run whose target was not
        met has a smaller reference dataset than requested and must say so.
        """
        return self.counters.games_accepted >= self.target_games

    @property
    def shortfall(self) -> int:
        return max(0, self.target_games - self.counters.games_accepted)

    @property
    def matched_positions(self) -> int:
        """Target positions that appeared at least once in the reference
        corpus."""
        return len(self.index)

    @property
    def position_coverage_rate(self) -> float | None:
        if self.target_positions == 0:
            return None
        return self.matched_positions / self.target_positions

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_eligible_games": self.target_games,
            "eligible_games_accepted": self.counters.games_accepted,
            "target_met": self.target_met,
            "shortfall": self.shortfall,
            "scan_termination": self.termination.value,
            "scanned_prefix_declared": self.termination.is_declared_prefix,
            "target_positions": self.target_positions,
            "matched_positions": self.matched_positions,
            "position_coverage_rate": (
                round(self.position_coverage_rate, 6)
                if self.position_coverage_rate is not None
                else None
            ),
            "counters": self.counters.as_dict(),
        }


class _RecordingReader:
    """Tees every line python-chess reads off the source.

    `chess.pgn.read_game` -- and therefore `read_headers`, which is just
    `read_game` with a headers-only visitor -- touches the stream through
    `readline()` and nothing else. Tapping that single method captures the
    exact text one record consumed, without seeking and without any
    look-ahead of our own. Only the current record's lines are held.
    """

    __slots__ = ("_handle", "_lines")

    def __init__(self, handle: TextIO) -> None:
        self._handle = handle
        self._lines: list[str] = []

    def readline(self, *args, **kwargs) -> str:
        line = self._handle.readline(*args, **kwargs)
        if line:
            self._lines.append(line)
        return line

    def start_record(self) -> None:
        self._lines.clear()

    def record_text(self) -> str:
        return "".join(self._lines)


class RecordStream:
    """Single-pass, header-first PGN record reader.

    `chess.pgn.read_headers` consumes a WHOLE record -- its own docstring
    says it "skips the rest of the game", and its documented usage pattern
    requires `seek()` to parse that record afterwards. A corpus arriving as
    `curl | zstd | python` cannot seek, so this class keeps the record
    instead: the reader tees the lines `read_headers` consumes, and an
    accepted record is re-parsed from that bounded in-memory buffer.

    The result is one forward pass over the source in which:

    - every record costs a headers-only visit;
    - only records that survive header eligibility are tokenised into a game
      tree, which is the expensive part;
    - at most one record's raw text is held at a time.

    Record boundaries are python-chess's own, because python-chess does the
    reading in both phases. The corpus is never materialised.
    """

    def __init__(self, handle: TextIO, source_id: str) -> None:
        if not source_id or not source_id.strip():
            raise ValueError("source_id must not be empty or whitespace-only")
        self.source_id = source_id
        self._reader = _RecordingReader(handle)
        self._ordinal = 0
        self.header_seconds = 0.0
        self.parse_seconds = 0.0

    def __iter__(self) -> Iterator[tuple[int, str, chess.pgn.Headers]]:
        """Yield ``(ordinal, game_id, headers)`` one record at a time.

        The ordinal advances for EVERY logical record, including rejected
        ones, so a record's identity does not depend on how many records
        before it were accepted.
        """
        while True:
            self._reader.start_record()
            start = time.perf_counter()
            headers = chess.pgn.read_headers(self._reader)
            self.header_seconds += time.perf_counter() - start
            if headers is None:
                return
            ordinal = self._ordinal
            self._ordinal += 1
            yield ordinal, f"{self.source_id}:{ordinal}", headers

    def parse_current(self) -> chess.pgn.Game:
        """Fully parse the record just yielded, from its buffered text.

        Called only for records that passed header eligibility. Raises
        `ValueError` if the buffered text does not yield a game, which a
        caller treats exactly like any other unparseable record.
        """
        start = time.perf_counter()
        try:
            game = chess.pgn.read_game(io.StringIO(self._reader.record_text()))
        finally:
            self.parse_seconds += time.perf_counter() - start
        if game is None:
            raise ValueError("buffered record did not yield a game")
        return game


def header_rejection(
    headers: chess.pgn.Headers, filters: ReferenceEligibilityFilters
) -> ReferenceRejection | None:
    """The eligibility verdict obtainable from headers alone, or `None`.

    Every rule here is decided from header text, so a record failing one can
    be dropped before its movetext is tokenised. Rules that genuinely need
    the reconstructed game -- PGN movetext errors, an invalid or Chess960
    starting position, ply count, and legal replay -- are deliberately NOT
    here and stay on the post-parse path.

    The order mirrors the pre-header-first implementation exactly, including
    the position of the rating-header well-formedness check: production
    record construction parses ratings and rejects a malformed one, and it
    did so *before* the time-control test, so that relative order is kept
    and the same rejection reason (`PARSE_REJECTED`) is reported.
    """
    if filters.require_completed_result:
        if headers.get("Result", "*") not in COMPLETED_RESULTS:
            return ReferenceRejection.RESULT_NOT_COMPLETED
    if filters.require_standard and not _is_standard(headers):
        return ReferenceRejection.VARIANT_NOT_STANDARD
    if filters.require_rated and not is_rated_event(headers.get("Event")):
        return ReferenceRejection.NOT_RATED
    if filters.require_human_players and is_bot_player(headers):
        return ReferenceRejection.BOT_PLAYER
    if filters.excluded_terminations:
        termination = headers.get("Termination")
        if (
            termination
            and termination.strip().casefold() in filters.excluded_terminations
        ):
            return ReferenceRejection.TERMINATION_EXCLUDED

    # Mirrors `_build_record`: a malformed rating header is a rejected
    # record, reported as PARSE_REJECTED, and that verdict precedes the
    # time-control test.
    try:
        ratings = (
            _parse_rating(headers.get("WhiteElo")),
            _parse_rating(headers.get("BlackElo")),
        )
    except ValueError:
        return ReferenceRejection.PARSE_REJECTED

    category = classify_time_control(_optional_text(headers.get("TimeControl")))
    if category not in filters.eligible_categories:
        return ReferenceRejection.TIME_CONTROL_NOT_ELIGIBLE

    if filters.require_both_ratings and None in ratings:
        return ReferenceRejection.RATING_MISSING
    if filters.applies_rating_band:
        if None in ratings:
            return ReferenceRejection.RATING_MISSING
        for rating in ratings:
            if filters.min_rating is not None and rating < filters.min_rating:
                return ReferenceRejection.RATING_OUT_OF_BAND
            if filters.max_rating is not None and rating > filters.max_rating:
                return ReferenceRejection.RATING_OUT_OF_BAND
    return None


def matched_observations(
    record: GameRecord, target_set: RecurringTargetSet
) -> tuple[list[DecisionObservation], int, int]:
    """Replay one external game and keep only target-set decisions.

    Returns ``(observations, decisions_examined, decisions_matched)``. Every
    ply is *examined* -- the position before the move becomes a
    `PositionKey` and is tested for membership -- and a position that is not
    in the target set is dropped immediately, never stored.

    The outcome on each observation is taken relative to the side to move at
    that position (`board.turn`), which is the actor of that decision. A
    game therefore contributes win/draw/loss to White-to-move positions and
    to Black-to-move positions from opposite ends of the same result.

    Raises `ValueError` on an illegal main-line move. The caller commits no
    counters until this returns, so an unreplayable game contributes nothing.
    """
    board = chess.Board(record.initial_fen)
    observations: list[DecisionObservation] = []
    examined = 0
    for ply_index, move in enumerate(record.moves):
        if not board.is_legal(move):
            raise ValueError(
                f"illegal move {move.uci()} at ply {ply_index} in game "
                f"{record.game_id}"
            )
        examined += 1
        position = PositionKey.from_board(board)
        if position in target_set:
            observations.append(
                DecisionObservation(
                    game_id=record.game_id,
                    ply_index=ply_index,
                    decision=DecisionKey(position, MoveKey.from_move(move)),
                    # Actor-relative: the side to move here is the actor.
                    outcome=record.result.to_outcome(board.turn),
                )
            )
        board.push(move)
    return observations, examined, len(observations)


class ReferenceScanner:
    """One streaming pass over an external corpus.

    Accepts exactly `target_games` ELIGIBLE games, or fewer if the input runs
    out first -- in which case `termination` says `stream_exhausted` and
    `ReferenceScanResult.target_met` is False.
    """

    def __init__(
        self,
        *,
        target_set: RecurringTargetSet,
        source_id: str,
        target_games: int,
        filters: ReferenceEligibilityFilters | None = None,
        max_records_scanned: int | None = None,
        skip_records: int = 0,
    ) -> None:
        if not source_id or not source_id.strip():
            raise ValueError("source_id must not be empty or whitespace-only")
        if type(target_games) is not int or target_games < 1:
            raise ValueError(
                f"target_games must be a positive int, got {target_games!r}"
            )
        if max_records_scanned is not None and (
            type(max_records_scanned) is not int or max_records_scanned < 1
        ):
            raise ValueError(
                f"max_records_scanned must be a positive int or None, got "
                f"{max_records_scanned!r}"
            )
        if type(skip_records) is not int or skip_records < 0:
            raise ValueError(
                f"skip_records must be a non-negative int, got {skip_records!r}"
            )

        self.target_set = target_set
        self.source_id = source_id
        self.target_games = target_games
        self.filters = filters or ReferenceEligibilityFilters()
        self.max_records_scanned = max_records_scanned
        self.skip_records = skip_records
        self.counters = ScanCounters()
        self.termination = ScanTermination.NOT_FINISHED
        self.index = PositionIndex()
        self.stream: RecordStream | None = None

    def as_manifest_dict(self) -> dict[str, Any]:
        return {
            "scanner": "HUMAN_REFERENCE_HEADER_FIRST_SCANNER_V2",
            "scan_strategy": (
                "header-first: every record costs a headers-only visit; only "
                "records passing header eligibility are tokenised into a game "
                "tree. One record's raw text is buffered so an accepted "
                "record can be fully parsed without seeking, which keeps a "
                "non-seekable stdin stream (curl | zstd | python) working "
                "identically to a file."
            ),
            "source_id": self.source_id,
            "game_identity_scheme": GAME_IDENTITY_SCHEME,
            "position_key_semantics_version": POSITION_KEY_SEMANTICS_VERSION,
            "target_eligible_games": self.target_games,
            "max_records_scanned": self.max_records_scanned,
            "skip_records": self.skip_records,
            "retains_external_game_ids": False,
            "materialises_corpus": False,
        }

    def scan(self, handle: TextIO) -> ReferenceScanResult:
        """Run one pass and return the aggregate.

        Stops as soon as the accepted-game target is met, leaving the rest of
        the stream unread.
        """
        self.termination = ScanTermination.NOT_FINISHED
        counters = self.counters
        self.stream = RecordStream(handle, self.source_id)
        records = iter(self.stream)

        while True:
            if (
                self.max_records_scanned is not None
                and counters.records_scanned >= self.max_records_scanned
            ):
                self.termination = ScanTermination.SCAN_LIMIT_REACHED
                break
            try:
                ordinal, game_id, headers = next(records)
            except StopIteration:
                self.termination = ScanTermination.STREAM_EXHAUSTED
                break
            counters.records_scanned += 1

            if ordinal < self.skip_records:
                counters.records_header_rejected += 1
                counters.reject(ReferenceRejection.SKIPPED_BY_OFFSET)
                continue

            # Header-first gate: everything decidable from header text is
            # decided here, before any movetext is tokenised.
            reason = header_rejection(headers, self.filters)
            if reason is not None:
                counters.records_header_rejected += 1
                counters.reject(reason)
                continue

            counters.records_fully_parsed += 1
            self._consume(game_id, self.stream)
            # The target counts ACCEPTED ELIGIBLE games, never raw records.
            if counters.games_accepted >= self.target_games:
                self.termination = ScanTermination.TARGET_REACHED
                break

        return ReferenceScanResult(
            index=self.index,
            counters=counters,
            termination=self.termination,
            target_games=self.target_games,
            target_positions=len(self.target_set),
        )

    def _consume(self, game_id: str, stream: "RecordStream") -> None:
        """Fully parse a header-eligible record and fold its matches in.

        Reached only after `header_rejection` returned `None`, so this is the
        expensive path and it runs for header-eligible records only. The
        rules applied here are exactly the ones that need the reconstructed
        game: movetext validity, starting-position validity, ply count, and
        legal replay.
        """
        counters = self.counters
        filters = self.filters

        try:
            game = stream.parse_current()
            record = _build_record(game, game_id)
        except ValueError:
            counters.reject(ReferenceRejection.PARSE_REJECTED)
            return

        if len(record.moves) < filters.min_plies:
            counters.reject(ReferenceRejection.TOO_FEW_PLIES)
            return

        # Everything that can raise runs before a counter is committed, so a
        # game that fails to replay is rejected rather than half-counted.
        try:
            observations, examined, matched = matched_observations(
                record, self.target_set
            )
        except ValueError:
            counters.reject(ReferenceRejection.REPLAY_FAILED)
            return

        if observations:
            self.index.add_game(observations)

        counters.games_accepted += 1
        counters.decisions_examined += examined
        counters.decisions_matched += matched
        if observations:
            counters.games_with_match += 1
