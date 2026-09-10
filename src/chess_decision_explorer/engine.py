from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType

import chess
import chess.engine

from .domain import MoveKey, PositionKey

# --- semantic version identifiers -------------------------------------------

POSITION_SEMANTICS_VERSION = "CANONICAL_POSITION_V1"
"""Identity of the canonical recurring-decision position contract Step 4A
evaluates.

``CANONICAL_POSITION_V1`` means: standard chess; the canonical four-field
``PositionKey`` (piece placement, side to move, valid castling rights,
legally relevant en-passant); ``halfmove_clock`` reset to 0;
``fullmove_number`` reset to 1; an empty pre-root move history; and an
evaluation normalized to the root side to move.

It deliberately does NOT reproduce the original halfmove clock, the original
fullmove number, prior repetition history, or the exact historical
50-/75-move or threefold/fivefold state of any single occurrence. The
product question is "how should this recurring canonical position be
played?", not "what was the exact draw-rule context of one historical
occurrence?". A future exact-historical evaluation mode would be introduced
separately, with its own identifier."""

ENGINE_EVIDENCE_VERSION = "ENGINE_EVIDENCE_V1"
"""Identity of how raw Stockfish output is accepted and interpreted.

``ENGINE_EVIDENCE_V1`` covers: root-side-to-move POV normalization; a
required exact (non-bound) score; a required engine-reported WDL triple that
sums to exactly 1000; required search metadata (depth, nodes); rejection of
``lowerbound`` / ``upperbound`` results; principal-variation validation
(non-empty for a nonterminal decision position, every move legal when
replayed sequentially from the canonical root); forced-root PV consistency;
and linkage to :data:`POSITION_SEMANTICS_VERSION`. This is a semantic
contract, independent of the SQLite storage schema version."""

_HASH_CHUNK_BYTES = 1024 * 1024
_SKILL_LEVEL = 20
_REQUIRED_CAPABILITY_OPTION = "UCI_ShowWDL"
_HEX_DIGITS = frozenset("0123456789abcdef")


# --- fixed Step 4A analysis profile ---------------------------------------

# The single authoritative definition of Step 4A's fixed, analysis-affecting
# engine policy. Threads and Hash are separate EngineAnalysisConfig
# dimensions and are intentionally NOT duplicated here. MultiPV is recorded
# as a semantic fact (single-PV analysis) but is never sent to
# SimpleEngine.configure(): python-chess manages MultiPV itself and rejects
# the managed option, so Step 4A uses the ordinary single-PV analyse path.
_ANALYSIS_PROFILE: dict[str, object] = {
    "profile": "STEP_4A_ANALYSIS_PROFILE",
    "single_pv": True,
    "multipv": 1,
    "skill_level": _SKILL_LEVEL,
    "uci_limit_strength": False,
    "uci_show_wdl": True,
    "variant": "standard",
    "syzygy_tablebases": False,
    "search_budget": "nodes",
    "fresh_search_state_per_search": True,
    "ponder": False,
}

ANALYSIS_PROFILE: Mapping[str, object] = MappingProxyType(_ANALYSIS_PROFILE)


def canonical_analysis_profile_json(profile: Mapping[str, object]) -> str:
    """Stable canonical serialization of an analysis profile: sorted keys,
    no insignificant whitespace. Never uses ``repr``/``pickle``/``hash()``."""
    return json.dumps(dict(profile), sort_keys=True, separators=(",", ":"))


def compute_analysis_profile_fingerprint(profile: Mapping[str, object]) -> str:
    """Deterministic SHA-256 over the canonical serialization of ``profile``."""
    return hashlib.sha256(
        canonical_analysis_profile_json(profile).encode("utf-8")
    ).hexdigest()


ANALYSIS_PROFILE_FINGERPRINT = compute_analysis_profile_fingerprint(ANALYSIS_PROFILE)
"""Fingerprint of the fixed Step 4A analysis profile. Changes whenever a
fixed analysis-affecting policy above changes."""


# --- errors --------------------------------------------------------------


class EngineStartupError(RuntimeError):
    """Raised when the Stockfish process cannot be started or initialized."""


class EngineCapabilityError(RuntimeError):
    """Raised when the connected engine lacks a capability this project
    requires, or fails to report information this project depends on."""


class EngineEvidenceError(EngineCapabilityError):
    """Raised when a raw engine response does not satisfy the
    ``ENGINE_EVIDENCE_V1`` contract (missing/partial fields, a bound score,
    or a principal variation inconsistent with the request)."""


class RequestValidationError(ValueError):
    """Raised when an evaluation request is not a valid Step 4A canonical
    decision request. Subclasses ``ValueError`` so callers that already
    guard against invalid construction keep working."""


class SearchMode(Enum):
    """The root-move constraint for one evaluation."""

    UNRESTRICTED = "unrestricted"
    FORCED_MOVE = "forced_move"


def _validate_positive_int(name: str, value: object) -> None:
    # type(...) is int deliberately rejects bool and any other int subclass;
    # values are validated, never coerced.
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive int, got {value!r}")


@dataclass(frozen=True, slots=True)
class EngineAnalysisConfig:
    """Immutable node-budgeted analysis configuration.

    ``nodes`` has no default: a caller must explicitly supply the analysis
    budget rather than inherit a silently chosen production value.
    """

    nodes: int
    threads: int = 1
    hash_mb: int = 64

    def __post_init__(self) -> None:
        _validate_positive_int("nodes", self.nodes)
        _validate_positive_int("threads", self.threads)
        _validate_positive_int("hash_mb", self.hash_mb)


def resolve_executable(executable_reference: str | Path) -> str:
    """Resolve a caller-supplied executable reference to one concrete
    filesystem path.

    Startup uses the returned path for *both* hashing the executable and
    launching it through python-chess, so the two can never disagree (a bare
    command name could otherwise be launched via ``PATH`` by the OS while
    being hashed relative to the current directory).

    - an absolute path, or a relative path that contains a path separator, is
      taken as a filesystem location (``~`` expanded) and made absolute;
    - a bare command name (no separator) is looked up on ``PATH`` via
      :func:`shutil.which`;
    - anything that does not resolve to an existing file raises
      :class:`EngineStartupError`.

    The resolved path is only an operational detail: it is deliberately not
    part of semantic cache identity. The executable's SHA-256 remains the
    authoritative content identity.
    """
    reference_text = os.fspath(executable_reference)
    if not reference_text:
        raise EngineStartupError("no Stockfish executable reference was provided")

    has_separator = os.sep in reference_text or (
        os.altsep is not None and os.altsep in reference_text
    )
    candidate = Path(reference_text).expanduser()

    if candidate.is_absolute() or has_separator:
        resolved = Path(os.path.abspath(candidate))
    else:
        found = shutil.which(reference_text)
        if found is None:
            raise EngineStartupError(
                f"Stockfish executable {reference_text!r} was not found on PATH"
            )
        resolved = Path(os.path.abspath(found))

    if not resolved.is_file():
        raise EngineStartupError(
            f"Stockfish executable reference {reference_text!r} does not resolve "
            f"to an existing file (resolved to {resolved!s})"
        )
    return str(resolved)


def hash_executable(path: str | Path) -> str:
    """SHA-256 of the executable's bytes, read in fixed-size chunks so a
    ~100 MB binary is never loaded into memory just to hash it."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class EngineIdentity:
    """Immutable engine identity relevant to cache semantics.

    - ``name`` is the UCI ``id name`` string, kept for provenance.
    - ``executable_sha256`` is the SHA-256 of the actual Stockfish
      executable bytes -- the authoritative content identity for the current
      official Stockfish binary / default-network configuration.
    - ``eval_file`` is the connected engine's default ``EvalFile`` option
      value when it reports one, kept only as network provenance.

    The executable *path* is never part of identity. Step 4A does not support
    caller-selected external NNUE files; the evaluator always uses
    Stockfish's default network. If a future version allows an externally
    supplied NNUE network, the content digest of that network MUST become
    part of this identity before persistent caching is allowed for that
    configuration.
    """

    name: str
    executable_sha256: str
    eval_file: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("engine identity name must not be empty")
        sha = self.executable_sha256
        if (
            not isinstance(sha, str)
            or len(sha) != 64
            or any(character not in _HEX_DIGITS for character in sha)
        ):
            raise ValueError(
                "executable_sha256 must be a 64-character lowercase hex SHA-256 "
                f"digest, got {sha!r}"
            )

    @classmethod
    def from_engine(
        cls, engine: chess.engine.SimpleEngine, executable_path: str | Path
    ) -> "EngineIdentity":
        name = engine.id.get("name")
        if not name:
            raise EngineCapabilityError("engine did not report a UCI id name")

        eval_file: str | None = None
        eval_file_option = engine.options.get("EvalFile")
        if eval_file_option is not None:
            default = eval_file_option.default
            if isinstance(default, str) and default.strip():
                eval_file = default

        return cls(
            name=name,
            executable_sha256=hash_executable(executable_path),
            eval_file=eval_file,
        )


def reconstruct_board(position: PositionKey) -> chess.Board:
    """Reconstruct the ``CANONICAL_POSITION_V1`` :class:`chess.Board` for a
    :class:`PositionKey`.

    ``PositionKey.epd`` carries piece placement, side to move, castling
    rights, and only legally relevant en-passant state. EPD operation
    suffixes (such as ``hmvc`` / ``fmvn``) are rejected: they are not part of
    canonical position identity. The halfmove clock is reset to 0 and the
    fullmove number to 1 -- the deliberate canonical reset.
    """
    try:
        board, operations = chess.Board.from_epd(position.epd)
    except ValueError as exc:
        raise RequestValidationError(
            f"position EPD is not parseable: {position.epd!r}"
        ) from exc
    if operations:
        raise RequestValidationError(
            "position EPD must be a bare four-field key with no EPD operations "
            f"(e.g. hmvc/fmvn); got operations {sorted(operations)!r}"
        )
    board.halfmove_clock = 0
    board.fullmove_number = 1
    return board


@dataclass(frozen=True, slots=True)
class EvaluationRequest:
    """Validated, immutable evaluation request and engine-cache key.

    Construction runs the full Step 4A canonical-request validation (see
    :func:`validate_request`), so an invalid request can never reach a cache
    lookup or the engine. Deliberately excludes player rating, time control,
    personal cohort, and source website: Stockfish's evaluation of a
    position does not depend on where that position came from.
    """

    position: PositionKey
    search_mode: SearchMode
    root_move: MoveKey | None
    engine_identity: EngineIdentity
    config: EngineAnalysisConfig

    def __post_init__(self) -> None:
        validate_request(self)


def validate_request(request: EvaluationRequest) -> chess.Board:
    """The one authoritative Step 4A request/position validation path, used
    before every cache lookup and every engine analysis.

    Returns the canonical root :class:`chess.Board` on success; raises
    :class:`RequestValidationError` otherwise.
    """
    position = request.position
    board = reconstruct_board(position)

    # B. Board validity -- structurally invalid boards are rejected before
    # any cache or engine use.
    if not board.is_valid():
        raise RequestValidationError(
            f"position is not a valid standard chess board: status {board.status()!r}"
        )

    # A. Position representation -- reconstructing and re-deriving the key
    # must agree exactly with the supplied PositionKey.
    if PositionKey.from_board(board) != position:
        raise RequestValidationError(
            f"position key {position.epd!r} is not canonical: it does not "
            "round-trip through board reconstruction and re-derivation"
        )

    # D. Terminal canonical positions are not decision positions. History
    # -dependent draw claims (50-/75-move, threefold/fivefold) are NOT
    # checked here: CANONICAL_POSITION_V1 intentionally excludes that
    # history.
    if board.is_checkmate():
        raise RequestValidationError("checkmate is a terminal position, not a decision")
    if board.is_stalemate():
        raise RequestValidationError("stalemate is a terminal position, not a decision")
    if board.is_insufficient_material():
        raise RequestValidationError(
            "insufficient material is a terminal position, not a decision"
        )

    # C + E. Search mode and root move.
    mode = request.search_mode
    if mode is SearchMode.UNRESTRICTED:
        if request.root_move is not None:
            raise RequestValidationError(
                "UNRESTRICTED requests must not specify a root move"
            )
    elif mode is SearchMode.FORCED_MOVE:
        if request.root_move is None:
            raise RequestValidationError("FORCED_MOVE requests require a root move")
        move = chess.Move.from_uci(request.root_move.uci)
        if move not in board.legal_moves:
            raise RequestValidationError(
                f"forced root move {request.root_move.uci!r} is not legal in "
                f"position {position.epd!r}"
            )
    else:
        raise RequestValidationError(f"unknown search mode: {mode!r}")

    return board


@dataclass(frozen=True, slots=True)
class EngineEvaluation:
    """Immutable engine evidence for one position, normalized to the POV of
    the root side to move.

    Exactly one of ``centipawn`` / ``mate`` is set: a mate is never encoded
    as a large centipawn number, and integer ``mate=0`` is represented
    faithfully (never confused with "unset"). WDL counts are the
    engine-reported Stockfish triple and must sum to exactly 1000.
    ``expected_score`` is the engine-model expected score derived from WDL;
    it is NOT a human win probability, a rating-specific probability, or a
    time-control-adjusted probability.

    ``nodes`` is the engine node counter reported on the retained
    comparison-ready report (see
    :meth:`StockfishEvaluator._analyse_exact`). It is NOT guaranteed to
    equal the total number of nodes Stockfish consumed before the overall
    analysis call terminated: a later bounded aspiration re-search that is
    then cut off by the node budget leaves this counter below the true
    total. It must not be interpreted as total computational expenditure.
    """

    centipawn: int | None
    mate: int | None
    wdl_wins: int
    wdl_draws: int
    wdl_losses: int
    depth: int
    seldepth: int | None
    nodes: int
    pv: tuple[MoveKey, ...]

    def __post_init__(self) -> None:
        has_cp = self.centipawn is not None
        has_mate = self.mate is not None
        if has_cp and type(self.centipawn) is not int:
            raise ValueError(
                f"centipawn must be an int or None, got {self.centipawn!r}"
            )
        if has_mate and type(self.mate) is not int:
            raise ValueError(f"mate must be an int or None, got {self.mate!r}")
        if has_cp == has_mate:
            raise ValueError(
                "exactly one of centipawn/mate must be set, got "
                f"centipawn={self.centipawn!r} mate={self.mate!r}"
            )

        for name in ("wdl_wins", "wdl_draws", "wdl_losses"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(
                    f"{name} must be a non-negative int, got {value!r}"
                )
        total = self.wdl_wins + self.wdl_draws + self.wdl_losses
        if total != 1000:
            raise ValueError(
                f"WDL counts must sum to exactly 1000 (Stockfish scale), got {total}"
            )

        if type(self.depth) is not int or self.depth < 0:
            raise ValueError(
                f"depth must be a non-negative int, got {self.depth!r}"
            )
        if self.seldepth is not None and (
            type(self.seldepth) is not int or self.seldepth < 0
        ):
            raise ValueError(
                f"seldepth must be a non-negative int or None, got {self.seldepth!r}"
            )
        if type(self.nodes) is not int or self.nodes < 0:
            raise ValueError(
                f"nodes must be a non-negative int, got {self.nodes!r}"
            )

        if not isinstance(self.pv, tuple) or not all(
            isinstance(move, MoveKey) for move in self.pv
        ):
            raise ValueError("pv must be a tuple of MoveKey")

    @property
    def expected_score(self) -> float:
        """Engine-model expected score ``(wins + 0.5 * draws) / 1000`` from
        the root side to move's point of view."""
        return (self.wdl_wins + 0.5 * self.wdl_draws) / 1000


@dataclass(frozen=True, slots=True)
class SemanticCacheKey:
    """The full semantic identity of one cached evaluation. Both the L1 and
    L2 caches key on this exact tuple, so optional fields (absent root move,
    absent eval file) normalize to the same sentinel (``""``) in both."""

    position_semantics_version: str
    position_epd: str
    search_mode: str
    root_move: str
    engine_executable_sha256: str
    engine_name: str
    engine_eval_file: str
    nodes: int
    threads: int
    hash_mb: int
    analysis_profile_fingerprint: str
    evidence_contract_version: str

    @classmethod
    def from_request(cls, request: EvaluationRequest) -> "SemanticCacheKey":
        return cls(
            position_semantics_version=POSITION_SEMANTICS_VERSION,
            position_epd=request.position.epd,
            search_mode=request.search_mode.value,
            root_move=request.root_move.uci if request.root_move is not None else "",
            engine_executable_sha256=request.engine_identity.executable_sha256,
            engine_name=request.engine_identity.name,
            engine_eval_file=request.engine_identity.eval_file or "",
            nodes=request.config.nodes,
            threads=request.config.threads,
            hash_mb=request.config.hash_mb,
            analysis_profile_fingerprint=ANALYSIS_PROFILE_FINGERPRINT,
            evidence_contract_version=ENGINE_EVIDENCE_VERSION,
        )


def validate_evaluation_evidence(
    evaluation: EngineEvaluation, request: EvaluationRequest, board: chess.Board
) -> None:
    """Validate an :class:`EngineEvaluation` against the request it answers.

    Raises :class:`EngineEvidenceError` if the principal variation is empty,
    malformed, or not sequentially legal from the canonical root; if a
    ``FORCED_MOVE`` PV does not begin with the forced root move; or if the
    engine reports ``mate=0`` at the root (inconsistent with a validated
    nonterminal decision position).
    """
    if evaluation.mate == 0:
        raise EngineEvidenceError(
            "engine reported mate=0 at the root, which is inconsistent with a "
            "validated nonterminal decision position"
        )
    if not evaluation.pv:
        raise EngineEvidenceError(
            "evaluation has no principal variation for a nonterminal decision "
            "position"
        )

    probe = board.copy()
    for index, move_key in enumerate(evaluation.pv):
        try:
            move = chess.Move.from_uci(move_key.uci)
        except ValueError as exc:
            raise EngineEvidenceError(
                f"principal-variation move {move_key.uci!r} at ply {index} is "
                "malformed"
            ) from exc
        if move not in probe.legal_moves:
            raise EngineEvidenceError(
                f"principal-variation move {move_key.uci!r} at ply {index} is "
                "illegal when replayed from the canonical root"
            )
        probe.push(move)

    if request.search_mode is SearchMode.FORCED_MOVE:
        if evaluation.pv[0] != request.root_move:
            raise EngineEvidenceError(
                f"FORCED_MOVE principal variation begins with {evaluation.pv[0].uci!r}, "
                f"expected the forced root move {request.root_move.uci!r}"
            )


def _evaluation_from_info(
    info: chess.engine.InfoDict, request: EvaluationRequest, board: chess.Board
) -> EngineEvaluation:
    """Build and fully validate an :class:`EngineEvaluation` from one
    engine analysis line. ``info`` must already be an exact (non-bound)
    scored line -- bound handling happens in :meth:`StockfishEvaluator._analyse_exact`.
    """
    score = info.get("score")
    if score is None:
        raise EngineEvidenceError("engine analysis line has no score")
    wdl = info.get("wdl")
    if wdl is None:
        raise EngineEvidenceError(
            "engine analysis line has no WDL statistics (is UCI_ShowWDL enabled?)"
        )
    depth = info.get("depth")
    if depth is None:
        raise EngineEvidenceError("engine analysis line has no depth")
    nodes = info.get("nodes")
    if nodes is None:
        raise EngineEvidenceError("engine analysis line has no node count")

    root_turn = board.turn
    pov_score = score.pov(root_turn)
    pov_wdl = wdl.pov(root_turn)

    pv_source = info.get("pv")
    if not pv_source:
        raise EngineEvidenceError(
            "engine analysis line has no principal variation for a nonterminal "
            "decision position"
        )
    try:
        pv = tuple(MoveKey.from_move(move) for move in pv_source)
    except ValueError as exc:
        raise EngineEvidenceError(
            f"engine principal variation is malformed: {exc}"
        ) from exc

    try:
        evaluation = EngineEvaluation(
            centipawn=pov_score.score(),
            mate=pov_score.mate(),
            wdl_wins=pov_wdl.wins,
            wdl_draws=pov_wdl.draws,
            wdl_losses=pov_wdl.losses,
            depth=depth,
            seldepth=info.get("seldepth"),
            nodes=nodes,
            pv=pv,
        )
    except ValueError as exc:
        raise EngineEvidenceError(
            f"engine evidence is structurally invalid: {exc}"
        ) from exc

    validate_evaluation_evidence(evaluation, request, board)
    return evaluation


def _profile_uci_configuration(config: EngineAnalysisConfig) -> dict[str, object]:
    """The UCI options Step 4A always applies, derived from the same
    authoritative :data:`ANALYSIS_PROFILE` that the fingerprint covers.

    ``Threads`` / ``Hash`` come from :class:`EngineAnalysisConfig`. ``MultiPV``
    is deliberately absent -- python-chess manages it and rejects the managed
    option.
    """
    return {
        "Threads": config.threads,
        "Hash": config.hash_mb,
        "Skill Level": _ANALYSIS_PROFILE["skill_level"],
        "UCI_LimitStrength": _ANALYSIS_PROFILE["uci_limit_strength"],
        "UCI_ShowWDL": _ANALYSIS_PROFILE["uci_show_wdl"],
    }


class StockfishEvaluator:
    """Focused adapter around python-chess's UCI support for one Stockfish
    process.

    The executable path is caller-supplied configuration; nothing under
    this class hard-codes a filesystem location. The process is not started
    until :meth:`start` is called (or the context manager is entered) --
    never at import time or at construction time.
    """

    def __init__(self, executable_path: str, config: EngineAnalysisConfig) -> None:
        self._executable_path = executable_path
        self._config = config
        self._engine: chess.engine.SimpleEngine | None = None
        self._identity: EngineIdentity | None = None

    def __enter__(self) -> "StockfishEvaluator":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def config(self) -> EngineAnalysisConfig:
        return self._config

    @property
    def identity(self) -> EngineIdentity:
        if self._identity is None:
            raise RuntimeError("engine has not been started")
        return self._identity

    def start(self) -> None:
        """Start the Stockfish process, validate required capabilities, and
        apply the fixed Step 4A analysis profile. A no-op if already started."""
        if self._engine is not None:
            return

        # Resolve the caller-supplied reference to ONE concrete filesystem
        # path, then use it for both hashing and process launch.
        resolved_path = resolve_executable(self._executable_path)

        try:
            engine = chess.engine.SimpleEngine.popen_uci(resolved_path)
        except (OSError, chess.engine.EngineError) as exc:
            raise EngineStartupError(
                f"failed to start Stockfish executable at "
                f"{resolved_path!r}: {exc}"
            ) from exc

        try:
            if _REQUIRED_CAPABILITY_OPTION not in engine.options:
                raise EngineCapabilityError(
                    f"engine does not support {_REQUIRED_CAPABILITY_OPTION}, "
                    "which this project requires"
                )

            engine.configure(_profile_uci_configuration(self._config))
            identity = EngineIdentity.from_engine(engine, resolved_path)
        except Exception:
            engine.quit()
            raise

        self._engine = engine
        self._identity = identity

    def close(self) -> None:
        """Cleanly close the engine process. A no-op if not started."""
        if self._engine is not None:
            self._engine.quit()
            self._engine = None
            self._identity = None

    def evaluate(
        self,
        position: PositionKey,
        search_mode: SearchMode,
        root_move: MoveKey | None = None,
    ) -> EngineEvaluation:
        """Run one UNRESTRICTED or FORCED_MOVE analysis and return an
        :class:`EngineEvaluation` normalized to the root side to move.

        The request is validated through the shared :func:`validate_request`
        path first (so this and :class:`EvaluationRequest` agree exactly),
        then a fresh sentinel is passed as python-chess's ``game`` marker so
        python-chess sends ``ucinewgame`` before this independent search.
        """
        if self._engine is None:
            raise RuntimeError("engine has not been started")

        request = EvaluationRequest(
            position=position,
            search_mode=search_mode,
            root_move=root_move,
            engine_identity=self._identity,
            config=self._config,
        )
        board = validate_request(request)

        root_moves: list[chess.Move] | None = None
        if search_mode is SearchMode.FORCED_MOVE:
            root_moves = [chess.Move.from_uci(request.root_move.uci)]

        info = self._analyse_exact(board, root_moves)
        return _evaluation_from_info(info, request, board)

    def _analyse_exact(
        self, board: chess.Board, root_moves: list[chess.Move] | None
    ) -> chess.engine.InfoDict:
        """Stream one node-budgeted analysis and return the retained
        comparison-ready analysis report.

        This examines each individual streaming analysis report as it
        arrives. It rejects any report whose score is marked ``lowerbound``
        / ``upperbound`` as comparison-ready evidence, and retains the
        **last** suitable unbounded scored report that also carries a
        principal variation.

        It does **not** explicitly choose the report with maximum depth. A
        hard node budget can stop Stockfish mid-aspiration: the last emitted
        report is then bounded and is skipped, so the retained report is the
        one before it. If the engine terminates *during* a later bounded
        aspiration re-search, the retained report can therefore be an
        earlier, shallower one than a report the engine had already emitted.

        A bound report is never repaired. If the engine produced no suitable
        unbounded scored report, that is reported rather than repaired.
        """
        last_exact: chess.engine.InfoDict | None = None
        saw_scored_line = False
        with self._engine.analysis(
            board,
            chess.engine.Limit(nodes=self._config.nodes),
            game=object(),
            root_moves=root_moves,
        ) as analysis:
            for info in analysis:
                if "score" not in info or not info.get("pv"):
                    continue
                saw_scored_line = True
                if info.get("lowerbound") or info.get("upperbound"):
                    continue
                last_exact = info

        if last_exact is not None:
            return last_exact
        if saw_scored_line:
            raise EngineEvidenceError(
                "engine produced only lower/upper-bound scored lines within the "
                "node budget; no exact evaluation to record"
            )
        raise EngineEvidenceError(
            "engine analysis produced no scored principal-variation line"
        )
