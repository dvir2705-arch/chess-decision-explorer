"""Step 4B: canonical-position damaging-move measurement and orchestration.

This layer answers exactly one question about one canonical recurring
decision:

    In this canonical position, is there robust engine evidence that the
    observed move loses a materially meaningful amount of engine-model
    expected score relative to at least one credible alternative?

It is a **damaging-move detector, not a best-move detector**. A move is
never labelled damaging merely because Stockfish would prefer a different
move: several moves in one position may all be perfectly acceptable. The
only supported positive conclusion is

    there exists a credible alternative A, A is materially better than the
    observed move, that advantage survives the required higher-budget
    confirmation, and no relevant contradiction remains.

Precision is deliberately favoured over recall. Missing a marginal mistake
is acceptable; confidently reporting search noise as a recurring weakness is
not.

Step 4B builds on the Step 4A engine boundary and reuses it unchanged: the
same ``EvaluationRequest`` / ``EngineEvaluation`` value objects, the same
``CANONICAL_POSITION_V1`` root semantics, the same ``ENGINE_EVIDENCE_V1``
evidence contract, and the same L1/L2 semantic evaluation cache. It adds no
second engine abstraction and no second engine cache, and it adds nothing to
primitive engine cache identity -- comparison-policy identity (epsilon, tau,
drift limits, calibration status) is deliberately kept *outside* the engine
cache key, so changing a threshold never invalidates identical Stockfish
work.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Protocol

import chess

from .domain import DecisionKey, MoveKey, PositionKey
from .engine import (
    EngineAnalysisConfig,
    EngineEvaluation,
    EngineEvidenceError,
    EngineIdentity,
    EvaluationRequest,
    SearchMode,
    SemanticCacheKey,
    StockfishEvaluator,
    validate_request,
)
from .engine_cache import CachedEvaluator, EvaluationCache

# --- semantic identity ------------------------------------------------------

ASSESSMENT_SEMANTICS_VERSION = "CANONICAL_DECISION_DAMAGE_V1"
"""Identity of the Step 4B damage-assessment contract.

``CANONICAL_DECISION_DAMAGE_V1`` means: damage is measured as a signed
difference in root-side-to-move engine-model expected score between two
independent ``FORCED_MOVE`` searches from the same ``CANONICAL_POSITION_V1``
root at equal requested budgets, confirmed by one fixed alternative across
two prescribed search levels, and gated by an explicit, separately
calibrated :class:`ComparisonPolicy`.
"""

CANONICAL_DAMAGE_SCOPE_NOTE = (
    "A Step 4B assessment is canonical-position engine-damage evidence. It "
    "describes the canonical recurring decision position identified by "
    "PositionKey, evaluated under CANONICAL_POSITION_V1. Because that "
    "identity deliberately excludes the halfmove clock and prior repetition "
    "history, an assessment does NOT claim that every historical occurrence "
    "of this decision lost exactly the same amount, and it is not an "
    "occurrence-level, game-level, or player-level claim. Occurrence-level, "
    "draw-history-sensitive eligibility remains separate future work."
)
"""The exact scope of a Step 4B result. Kept as a durable constant so that
downstream reporting can quote it instead of paraphrasing (and overclaiming)
what the engine evidence proves."""

EXPECTED_SCORE_UNIT_SCALE = 2000
"""Integer scale of the Step 4B expected-score unit: ``U = 2*W + D`` over the
Stockfish WDL triple (which sums to 1000), so ``0 <= U <= 2000``."""


# --- errors -----------------------------------------------------------------


class AssessmentEvidenceError(ValueError):
    """Raised when engine evidence gathered for a comparison is internally
    inconsistent, incompatible across the two sides of a comparison, or
    contradicts an exactly rule-derivable terminal fact.

    Step 4A's fail-loud style is preserved for the value objects: an invalid
    :class:`MoveEvidence` or :class:`ComparisonRound` can never be
    constructed. :class:`DecisionAssessor` catches this and reports
    :attr:`AssessmentStatus.INVALID_EVIDENCE` rather than letting corrupt
    evidence become a harmless zero.
    """

    def __init__(self, message: str, reason: "AssessmentReason") -> None:
        super().__init__(message)
        self.reason = reason


class _WorkBudgetExceeded(Exception):
    """Internal: the bounded per-decision engine work budget was reached."""


# --- expected-score arithmetic ----------------------------------------------


def expected_score_units(evaluation: EngineEvaluation) -> int:
    """The integer engine-model expected score ``2*W + D`` of one evaluation,
    from the root side to move's point of view.

    Integer units are the internal currency of every Step 4B comparison: no
    float rounding ever enters a gap, a conservative margin, or a regret.
    """
    return 2 * evaluation.wdl_wins + evaluation.wdl_draws


def expected_score_from_units(units: int) -> float:
    """Display-only conversion ``units / 2000``. Never used in a comparison,
    a threshold test, or an accepted regret."""
    return units / EXPECTED_SCORE_UNIT_SCALE


def _mate_direction(evaluation: EngineEvaluation) -> int:
    """Categorical mate evidence: ``+1`` mate for the root side to move,
    ``-1`` mate against it, ``0`` no mate.

    Mate is kept categorical on purpose. It is never converted into a large
    centipawn number and never combined with WDL into a compound score.
    """
    if evaluation.mate is None:
        return 0
    return 1 if evaluation.mate > 0 else -1


# --- terminal semantics -----------------------------------------------------


class ImmediateTerminal(Enum):
    """The exactly rule-derivable state of the position reached by applying
    one legal root move."""

    NONE = "none"
    CHECKMATE = "checkmate"
    STALEMATE = "stalemate"
    INSUFFICIENT_MATERIAL = "insufficient_material"


def immediate_terminal_after(board: chess.Board, move: MoveKey) -> ImmediateTerminal:
    """Apply ``move`` to a copy of the canonical root and report the exact
    terminal state of the resulting position.

    This is rule-derived fact, used only as a shortcut and as an integrity
    diagnostic against the engine's own evidence. It never replaces or
    fabricates engine evidence: no ``EngineEvaluation`` is ever synthesised
    from it, and engine provenance is always preserved.
    """
    probe = board.copy()
    probe.push(chess.Move.from_uci(move.uci))
    if probe.is_checkmate():
        return ImmediateTerminal.CHECKMATE
    if probe.is_stalemate():
        return ImmediateTerminal.STALEMATE
    if probe.is_insufficient_material():
        return ImmediateTerminal.INSUFFICIENT_MATERIAL
    return ImmediateTerminal.NONE


# --- policy -----------------------------------------------------------------


def _validate_positive_int(name: str, value: object) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive int, got {value!r}")


def _validate_non_negative_int(name: str, value: object) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative int, got {value!r}")


@dataclass(frozen=True, slots=True)
class SearchLevel:
    """One named comparison level: a label and its requested node budget.

    Only the semantic budget lives here. ``threads`` / ``hash_mb`` stay
    operational Step 4A ``EngineAnalysisConfig`` dimensions and are supplied
    by whichever evaluator serves the level.
    """

    label: str
    nodes: int

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("SearchLevel label must be a non-empty string")
        _validate_positive_int("SearchLevel nodes", self.nodes)


class CalibrationStatus(Enum):
    """Whether a policy's thresholds have been established by a real
    calibration run, or are still placeholders."""

    UNCALIBRATED = "uncalibrated"
    CALIBRATED = "calibrated"


_V1_MAX_ACTIVE_ALTERNATIVES = 2
"""Candidate-discovery v1 keeps at most two active distinct alternatives.
Production MultiPV is deliberately not implemented: one credible verified
alternative is enough to demonstrate damage, and missing a second strong
move costs recall, not precision."""


@dataclass(frozen=True, kw_only=True, slots=True)
class ComparisonPolicy:
    """The immutable comparison procedure: search levels, allowances,
    thresholds, bounds, and calibration provenance.

    **No production values are defined anywhere in this module.** Every
    threshold is a required constructor argument, so a caller can never
    inherit an invented epsilon, tau, drift limit, or node budget. A policy
    whose :attr:`calibration_status` is
    :attr:`CalibrationStatus.UNCALIBRATED` can never yield
    :attr:`AssessmentStatus.DAMAGE_SUPPORTED`: the measurement still runs
    (that is exactly what a calibration experiment needs) but the positive
    label is withheld and the result is reported as
    :attr:`AssessmentStatus.INCONCLUSIVE`.

    Policy identity is intentionally *not* part of primitive engine cache
    identity. Changing ``tau_units`` or ``epsilon_units`` re-derives every
    decision from the identical cached Stockfish evidence.
    """

    policy_id: str
    policy_version: str
    b1: SearchLevel
    b2: SearchLevel
    b3: SearchLevel | None = None
    epsilon_units: int
    tau_units: int
    max_gap_drift_units: int
    max_alternative_drift_units: int
    max_user_drift_units: int
    material_negative_gap_units: int
    max_active_alternatives: int = _V1_MAX_ACTIVE_ALTERNATIVES
    max_requests_per_decision: int | None = None
    calibration_status: CalibrationStatus
    calibration_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("policy_id", "policy_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")

        levels = [self.b1, self.b2] + ([self.b3] if self.b3 is not None else [])
        for level in levels:
            if not isinstance(level, SearchLevel):
                raise ValueError("search levels must be SearchLevel instances")
        labels = [level.label for level in levels]
        if len(set(labels)) != len(labels):
            raise ValueError(f"search level labels must be distinct, got {labels!r}")
        budgets = [level.nodes for level in levels]
        if any(later <= earlier for earlier, later in zip(budgets, budgets[1:])):
            raise ValueError(
                "search level node budgets must strictly increase B1 < B2 < B3, "
                f"got {budgets!r}"
            )

        for name in (
            "epsilon_units",
            "tau_units",
            "max_gap_drift_units",
            "max_alternative_drift_units",
            "max_user_drift_units",
            "material_negative_gap_units",
        ):
            _validate_non_negative_int(name, getattr(self, name))

        _validate_positive_int("max_active_alternatives", self.max_active_alternatives)
        if self.max_active_alternatives > _V1_MAX_ACTIVE_ALTERNATIVES:
            raise ValueError(
                "candidate discovery v1 supports at most "
                f"{_V1_MAX_ACTIVE_ALTERNATIVES} active alternatives, got "
                f"{self.max_active_alternatives}"
            )
        if self.max_requests_per_decision is not None:
            _validate_positive_int(
                "max_requests_per_decision", self.max_requests_per_decision
            )

        if not isinstance(self.calibration_status, CalibrationStatus):
            raise ValueError("calibration_status must be a CalibrationStatus")
        if self.calibration_status is CalibrationStatus.CALIBRATED:
            if not isinstance(self.calibration_id, str) or not self.calibration_id.strip():
                raise ValueError(
                    "a CALIBRATED policy must carry a non-empty calibration_id"
                )
        elif self.calibration_id is not None:
            raise ValueError(
                "an UNCALIBRATED policy must not carry a calibration_id"
            )

    @property
    def levels(self) -> tuple[SearchLevel, ...]:
        if self.b3 is None:
            return (self.b1, self.b2)
        return (self.b1, self.b2, self.b3)

    @property
    def permits_damage_conclusion(self) -> bool:
        """Whether this policy's calibration scope licenses a positive
        damaging-move label at all."""
        return self.calibration_status is CalibrationStatus.CALIBRATED

    def canonical_json(self) -> str:
        """Deterministic canonical serialization used for the fingerprint."""
        payload = {
            "assessment_semantics_version": ASSESSMENT_SEMANTICS_VERSION,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "levels": [
                {"label": level.label, "nodes": level.nodes} for level in self.levels
            ],
            "epsilon_units": self.epsilon_units,
            "tau_units": self.tau_units,
            "max_gap_drift_units": self.max_gap_drift_units,
            "max_alternative_drift_units": self.max_alternative_drift_units,
            "max_user_drift_units": self.max_user_drift_units,
            "material_negative_gap_units": self.material_negative_gap_units,
            "max_active_alternatives": self.max_active_alternatives,
            "max_requests_per_decision": self.max_requests_per_decision,
            "calibration_status": self.calibration_status.value,
            "calibration_id": self.calibration_id,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @property
    def fingerprint(self) -> str:
        """Deterministic SHA-256 over :meth:`canonical_json`.

        Provenance only. It is never mixed into the Step 4A
        ``SemanticCacheKey``: identical Stockfish requests stay cache-valid
        across policy changes.
        """
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


# --- evidence value objects -------------------------------------------------


@dataclass(frozen=True, slots=True)
class MoveEvidence:
    """One ``FORCED_MOVE`` evaluation of one root move at one search level.

    Wraps -- and never copies -- the immutable Step 4A request/evaluation
    pair. Construction re-checks that the evidence actually answers the
    question being asked, and that it does not contradict an exactly
    rule-derivable immediate terminal result.
    """

    level: SearchLevel
    move: MoveKey
    request: EvaluationRequest
    evaluation: EngineEvaluation
    immediate_terminal: ImmediateTerminal

    def __post_init__(self) -> None:
        if self.request.search_mode is not SearchMode.FORCED_MOVE:
            raise AssessmentEvidenceError(
                "comparison evidence must come from a FORCED_MOVE search, got "
                f"{self.request.search_mode!r}",
                AssessmentReason.INCOMPATIBLE_EVIDENCE,
            )
        if self.request.root_move != self.move:
            raise AssessmentEvidenceError(
                f"evidence for {self.move.uci!r} was produced by a request "
                f"forcing {self.request.root_move!r}",
                AssessmentReason.INCOMPATIBLE_EVIDENCE,
            )
        if self.request.config.nodes != self.level.nodes:
            raise AssessmentEvidenceError(
                f"evidence at level {self.level.label!r} was requested with "
                f"{self.request.config.nodes} nodes, expected {self.level.nodes}",
                AssessmentReason.INCOMPATIBLE_EVIDENCE,
            )
        if not self.evaluation.pv or self.evaluation.pv[0] != self.move:
            raise AssessmentEvidenceError(
                f"forced-move evidence for {self.move.uci!r} does not begin its "
                "principal variation with that move",
                AssessmentReason.INCOMPATIBLE_EVIDENCE,
            )

        # Exact rule-derived terminal facts must not be contradicted by the
        # engine's categorical mate evidence. The engine evidence is kept as
        # is either way; a contradiction is reported, never repaired.
        if self.immediate_terminal is ImmediateTerminal.CHECKMATE:
            if self.evaluation.mate != 1:
                raise AssessmentEvidenceError(
                    f"{self.move.uci!r} delivers immediate checkmate but the "
                    f"engine reported mate={self.evaluation.mate!r}",
                    AssessmentReason.TERMINAL_EVIDENCE_CONTRADICTION,
                )
        elif self.immediate_terminal is not ImmediateTerminal.NONE:
            if self.evaluation.mate is not None:
                raise AssessmentEvidenceError(
                    f"{self.move.uci!r} reaches an exact draw "
                    f"({self.immediate_terminal.value}) but the engine reported "
                    f"mate={self.evaluation.mate!r}",
                    AssessmentReason.TERMINAL_EVIDENCE_CONTRADICTION,
                )

    @property
    def position(self) -> PositionKey:
        return self.request.position

    @property
    def units(self) -> int:
        """Root-side-to-move engine-model expected score in integer units."""
        return expected_score_units(self.evaluation)

    @property
    def mate_direction(self) -> int:
        return _mate_direction(self.evaluation)


@dataclass(frozen=True, slots=True)
class CandidateDiscovery:
    """One ``UNRESTRICTED`` search used only to *discover* a promising
    alternative root move.

    The discovery evaluation is retained purely as provenance. Its score
    never enters regret arithmetic: only :class:`ComparisonRound` computes
    gaps, and a round structurally refuses anything but two ``FORCED_MOVE``
    evaluations.
    """

    level: SearchLevel
    position: PositionKey
    discovered_move: MoveKey
    request: EvaluationRequest
    evaluation: EngineEvaluation

    def __post_init__(self) -> None:
        if self.request.search_mode is not SearchMode.UNRESTRICTED:
            raise AssessmentEvidenceError(
                "candidate discovery requires an UNRESTRICTED search, got "
                f"{self.request.search_mode!r}",
                AssessmentReason.INCOMPATIBLE_EVIDENCE,
            )
        if self.request.position != self.position:
            raise AssessmentEvidenceError(
                "candidate discovery request is for a different root position",
                AssessmentReason.INCOMPATIBLE_EVIDENCE,
            )
        if self.request.config.nodes != self.level.nodes:
            raise AssessmentEvidenceError(
                f"discovery at level {self.level.label!r} was requested with "
                f"{self.request.config.nodes} nodes, expected {self.level.nodes}",
                AssessmentReason.INCOMPATIBLE_EVIDENCE,
            )
        if not self.evaluation.pv:
            raise AssessmentEvidenceError(
                "candidate discovery evidence has no principal variation",
                AssessmentReason.INCOMPATIBLE_EVIDENCE,
            )
        if self.evaluation.pv[0] != self.discovered_move:
            raise AssessmentEvidenceError(
                f"discovered move {self.discovered_move.uci!r} is not the first "
                f"principal-variation move {self.evaluation.pv[0].uci!r}",
                AssessmentReason.INCOMPATIBLE_EVIDENCE,
            )


@dataclass(frozen=True, slots=True)
class ComparisonRound:
    """One pairwise comparison at one search level: the observed move against
    one fixed alternative, both force-evaluated independently from the same
    canonical root at the same requested budget.

    This is the *only* place a signed gap is produced, and it accepts only
    ``FORCED_MOVE`` evidence -- which is how "never use the unrestricted
    discovery score in regret arithmetic" is enforced structurally rather
    than by convention.
    """

    level: SearchLevel
    alternative: MoveKey
    user_evidence: MoveEvidence
    alternative_evidence: MoveEvidence

    def __post_init__(self) -> None:
        user = self.user_evidence
        alt = self.alternative_evidence
        if alt.move != self.alternative:
            raise AssessmentEvidenceError(
                f"round alternative {self.alternative.uci!r} does not match its "
                f"evidence for {alt.move.uci!r}",
                AssessmentReason.INCOMPATIBLE_EVIDENCE,
            )
        if user.move == alt.move:
            raise AssessmentEvidenceError(
                "a comparison round requires a distinct alternative; the "
                f"observed move {user.move.uci!r} cannot be its own challenger",
                AssessmentReason.INCOMPATIBLE_EVIDENCE,
            )
        if user.level != self.level or alt.level != self.level:
            raise AssessmentEvidenceError(
                f"both sides of a round must be evaluated at level "
                f"{self.level.label!r}",
                AssessmentReason.INCOMPATIBLE_EVIDENCE,
            )
        if user.position != alt.position:
            raise AssessmentEvidenceError(
                "a comparison round requires both moves to be evaluated from "
                "the same canonical root position",
                AssessmentReason.INCOMPATIBLE_EVIDENCE,
            )
        if user.request.engine_identity != alt.request.engine_identity:
            raise AssessmentEvidenceError(
                "a comparison round requires compatible engine identity on "
                "both sides",
                AssessmentReason.INCOMPATIBLE_EVIDENCE,
            )
        if user.request.config != alt.request.config:
            raise AssessmentEvidenceError(
                "a comparison round requires equal requested analysis budgets "
                f"on both sides, got {user.request.config!r} vs "
                f"{alt.request.config!r}",
                AssessmentReason.INCOMPATIBLE_EVIDENCE,
            )

    @property
    def position(self) -> PositionKey:
        return self.user_evidence.position

    @property
    def user_move(self) -> MoveKey:
        return self.user_evidence.move

    @property
    def user_units(self) -> int:
        return self.user_evidence.units

    @property
    def alternative_units(self) -> int:
        return self.alternative_evidence.units

    @property
    def signed_gap_units(self) -> int:
        """``U(alternative) - U(observed move)``, exactly, with its sign.

        Never clamped, never absolute-valued, never floored at zero. A
        positive value is a measurement, not a verdict.
        """
        return self.alternative_units - self.user_units

    @property
    def centipawn_gap(self) -> int | None:
        """Supporting CP diagnostic; ``None`` when either side is a mate
        score. Explanatory only -- it is never combined with the WDL gap."""
        user_cp = self.user_evidence.evaluation.centipawn
        alt_cp = self.alternative_evidence.evaluation.centipawn
        if user_cp is None or alt_cp is None:
            return None
        return alt_cp - user_cp


# --- status / reasons -------------------------------------------------------


class AssessmentStatus(Enum):
    """The final state of one decision assessment.

    ``RECHECK_REQUIRED`` and ``VALID_COMPARISON`` deliberately do not exist:
    neither is a final verdict about move quality.
    """

    DAMAGE_SUPPORTED = "damage_supported"
    """One fixed alternative passed the whole evidence contract. A
    damage-qualified :class:`EngineRegret` exists."""

    NO_DAMAGE_DEMONSTRATED = "no_damage_demonstrated"
    """The procedure completed coherently without meeting the damage
    requirements. This does NOT assert that the observed move is optimal, and
    it does NOT assert that the compared moves are equivalent."""

    INCONCLUSIVE = "inconclusive"
    """A material issue remains unresolved within the bounded policy, or the
    policy's calibration scope does not license a conclusion."""

    INVALID_EVIDENCE = "invalid_evidence"
    """Invalid, corrupt, or incompatible evidence prevented assessment. It is
    never silently reduced to a zero gap."""


class AssessmentReason(Enum):
    """Why an assessment ended where it did, and which contradictions (if
    any) remained unresolved."""

    # terminating without engine comparison
    ONLY_LEGAL_MOVE = "only_legal_move"
    USER_MOVE_FORCES_IMMEDIATE_CHECKMATE = "user_move_forces_immediate_checkmate"
    DISCOVERY_SELECTED_USER_MOVE = "discovery_selected_user_move"

    # completed comparisons
    MARGIN_BELOW_TAU = "margin_below_tau"
    NEGATIVE_OR_ZERO_GAP_NEAR_TIE = "negative_or_zero_gap_near_tie"
    NO_WITNESS_SURVIVED_CONFIRMATION = "no_witness_survived_confirmation"
    CONFIRMED_BY_FIXED_WITNESS = "confirmed_by_fixed_witness"

    # contradictions / escalation triggers
    SIGN_REVERSAL = "sign_reversal"
    GAP_DRIFT_EXCEEDED = "gap_drift_exceeded"
    ALTERNATIVE_DRIFT_EXCEEDED = "alternative_drift_exceeded"
    USER_DRIFT_EXCEEDED = "user_drift_exceeded"
    MATE_DIRECTION_REVERSAL = "mate_direction_reversal"
    MATERIAL_NEGATIVE_GAP = "material_negative_gap"
    HIGHER_BUDGET_DISCOVERY_SELECTED_USER = "higher_budget_discovery_selected_user"

    # bounded stopping
    ESCALATION_EXHAUSTED = "escalation_exhausted"
    WORK_BUDGET_EXHAUSTED = "work_budget_exhausted"

    # policy / evidence gates
    UNCALIBRATED_POLICY = "uncalibrated_policy"
    INCOMPATIBLE_EVIDENCE = "incompatible_evidence"
    TERMINAL_EVIDENCE_CONTRADICTION = "terminal_evidence_contradiction"


STATUS_SEMANTICS: Mapping[AssessmentStatus, str] = MappingProxyType(
    {
        AssessmentStatus.DAMAGE_SUPPORTED: (
            "One fixed alternative passed the entire evidence contract across "
            "the two prescribed confirmation levels. A damage-qualified "
            "EngineRegret exists."
        ),
        AssessmentStatus.NO_DAMAGE_DEMONSTRATED: (
            "The procedure completed coherently and did not meet the damage "
            "requirements. This does NOT assert that the observed move is "
            "optimal, and it does NOT assert that the compared moves are "
            "proven equivalent -- only that no credible alternative was shown "
            "to be materially better."
        ),
        AssessmentStatus.INCONCLUSIVE: (
            "A material issue remained unresolved inside the bounded policy, "
            "or the policy's calibration scope does not license a conclusion. "
            "Recurrence never promotes this into damage."
        ),
        AssessmentStatus.INVALID_EVIDENCE: (
            "Invalid, corrupt, or incompatible engine evidence prevented "
            "assessment. It is never reduced to a harmless zero gap."
        ),
    }
)
"""Durable, quotable wording for each final state, so downstream reporting
does not paraphrase (and overclaim) what a status means."""


_INSTABILITY_TRIGGERS = frozenset(
    {
        AssessmentReason.SIGN_REVERSAL,
        AssessmentReason.GAP_DRIFT_EXCEEDED,
        AssessmentReason.ALTERNATIVE_DRIFT_EXCEEDED,
        AssessmentReason.USER_DRIFT_EXCEEDED,
        AssessmentReason.MATE_DIRECTION_REVERSAL,
    }
)


def _ordered(reasons: Iterable[AssessmentReason]) -> tuple[AssessmentReason, ...]:
    return tuple(sorted(set(reasons), key=lambda reason: reason.name))


# --- accepted regret --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EngineRegret:
    """Accepted, damage-qualified engine-model expected-score loss.

    An ``EngineRegret`` exists **only** for
    :attr:`AssessmentStatus.DAMAGE_SUPPORTED`. It is never produced by
    ``max(0, gap)`` and never derived from a negative gap: :attr:`units` is
    the conservative accepted loss ``S(A) = min(G_prev(A), G_cur(A))`` of the
    one fixed witness across the two prescribed confirmation levels, which is
    strictly positive whenever the damage contract passed.

    It is kept separate from the conservative margin
    ``L(A) = S(A) - epsilon_units`` and from the final measured gap; the
    three are distinct quantities and are never merged.
    """

    units: int
    witness: MoveKey
    policy_id: str
    policy_version: str
    policy_fingerprint: str
    qualification_levels: tuple[SearchLevel, SearchLevel]

    def __post_init__(self) -> None:
        _validate_positive_int("EngineRegret units", self.units)
        if self.units > EXPECTED_SCORE_UNIT_SCALE:
            raise ValueError(
                f"EngineRegret units must not exceed {EXPECTED_SCORE_UNIT_SCALE}, "
                f"got {self.units}"
            )
        if not isinstance(self.witness, MoveKey):
            raise ValueError("EngineRegret witness must be a MoveKey")
        if len(self.qualification_levels) != 2:
            raise ValueError(
                "EngineRegret must record exactly the two prescribed "
                "qualification levels"
            )

    @property
    def expected_score_loss(self) -> float:
        """Display-only ``units / 2000``."""
        return expected_score_from_units(self.units)


# --- cross-level semantic compatibility -------------------------------------

_BUDGET_CACHE_KEY_FIELD = "nodes"

_CROSS_LEVEL_INVARIANT_FIELDS: tuple[str, ...] = tuple(
    name
    for name in SemanticCacheKey.__dataclass_fields__
    if name != _BUDGET_CACHE_KEY_FIELD
)
"""Every dimension of the Step 4A semantic cache key except the requested
node budget. Two comparison levels of one assessment must agree on all of
them -- same canonical position semantics, same position, same search mode
and forced root move, same engine executable content, same reported network,
same threads/hash, same analysis-profile fingerprint, same evidence contract
-- and differ in nothing but the prescribed budget. Derived from the Step 4A
key itself so a future key dimension is covered automatically; no new
cache-key scheme is introduced."""


def _semantic_identity_excluding_budget(request: EvaluationRequest) -> tuple:
    key = SemanticCacheKey.from_request(request)
    return tuple(getattr(key, name) for name in _CROSS_LEVEL_INVARIANT_FIELDS)


# --- assessment result ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MoveAssessment:
    """The complete, immutable result of assessing one canonical decision.

    See :data:`CANONICAL_DAMAGE_SCOPE_NOTE` for exactly what this does and
    does not claim. In particular it carries no game, occurrence, ply,
    rating, or time-control dimension: it is a statement about the canonical
    recurring position only.
    """

    decision: DecisionKey
    policy: ComparisonPolicy
    status: AssessmentStatus
    reasons: tuple[AssessmentReason, ...]
    unresolved_triggers: tuple[AssessmentReason, ...]
    witness: MoveKey | None
    regret: EngineRegret | None
    conservative_margin_units: int | None
    final_gap_units: int | None
    qualification_levels: tuple[SearchLevel, ...]
    discoveries: tuple[CandidateDiscovery, ...]
    rounds: tuple[ComparisonRound, ...]
    requests_issued: int
    resolved_triggers: tuple[AssessmentReason, ...] = ()
    """Escalation triggers raised by an earlier prescribed pair that the
    later prescribed pair subsequently resolved.

    Provenance and calibration information only -- never a confidence score,
    and never an admission gate. Once the prescribed later pair genuinely
    passes, a historical trigger recorded here does not block Step 4C
    admission; what still blocks it lives in
    :attr:`unresolved_triggers`. The two are always disjoint."""
    detail: str | None = None

    def __post_init__(self) -> None:
        supported = self.status is AssessmentStatus.DAMAGE_SUPPORTED
        if (self.regret is not None) != supported:
            raise ValueError(
                "an EngineRegret exists if and only if the status is "
                f"DAMAGE_SUPPORTED; got status={self.status!r} regret="
                f"{self.regret!r}"
            )
        if (self.witness is not None) != supported:
            raise ValueError(
                "a confirmed witness is recorded if and only if the status is "
                f"DAMAGE_SUPPORTED; got status={self.status!r} witness="
                f"{self.witness!r}"
            )
        if (self.conservative_margin_units is not None) != supported:
            raise ValueError(
                "a conservative margin is recorded if and only if the status "
                "is DAMAGE_SUPPORTED"
            )
        if supported and self.regret.witness != self.witness:
            raise ValueError("the accepted regret must name the confirmed witness")
        overlap = set(self.resolved_triggers) & set(self.unresolved_triggers)
        if overlap:
            raise ValueError(
                "a trigger cannot be both resolved and unresolved: "
                f"{sorted(reason.value for reason in overlap)}"
            )
        _validate_non_negative_int("requests_issued", self.requests_issued)

    @property
    def semantics_version(self) -> str:
        return ASSESSMENT_SEMANTICS_VERSION

    @property
    def scope_note(self) -> str:
        return CANONICAL_DAMAGE_SCOPE_NOTE

    def round_for(self, level: SearchLevel, move: MoveKey) -> ComparisonRound | None:
        for comparison in self.rounds:
            if comparison.level == level and comparison.alternative == move:
                return comparison
        return None

    # --- Step 4C admission contract ------------------------------------

    @property
    def admission_failures(self) -> tuple[str, ...]:
        """Every reason this assessment may NOT be admitted downstream as
        engine-damaging evidence.

        This re-derives the qualification contract from the recorded trace
        instead of trusting :attr:`status` or :attr:`unresolved_triggers`:
        the two qualification rounds are fed back through the very same
        :func:`_compare_levels` gate the orchestrator used, so a malformed or
        hand-built assessment whose stored rounds actually violate a
        fixed-witness, sign, drift, mate-direction, or materiality gate can
        never be admitted, however its summary fields were filled in.

        Recurrence can never promote ``NO_DAMAGE_DEMONSTRATED`` or
        ``INCONCLUSIVE`` into damage: fifty occurrences of one uncertain
        engine decision remain one uncertain engine decision.
        :attr:`resolved_triggers` is provenance about an earlier prescribed
        pair and deliberately does NOT block admission once the later
        prescribed pair genuinely passes.
        """
        failures: list[str] = []
        policy = self.policy

        if self.status is not AssessmentStatus.DAMAGE_SUPPORTED:
            failures.append(f"final status is {self.status.value}, not damage_supported")
        if self.unresolved_triggers:
            failures.append(
                "unresolved contradictions remain: "
                + ", ".join(reason.value for reason in self.unresolved_triggers)
            )
        if not policy.permits_damage_conclusion:
            failures.append(
                "policy calibration scope does not permit a damaging-move "
                f"conclusion ({policy.calibration_status.value})"
            )

        witness = self.witness
        if witness is None:
            failures.append("no confirmed witness")
            return tuple(failures)
        if witness == self.decision.move:
            failures.append("witness is the observed move itself")

        # --- accepted-regret provenance ---------------------------------
        regret = self.regret
        if regret is None:
            failures.append("no accepted EngineRegret")
        else:
            if regret.witness != witness:
                failures.append("accepted regret does not name the confirmed witness")
            if regret.policy_id != policy.policy_id:
                failures.append(
                    f"accepted regret was issued under policy id "
                    f"{regret.policy_id!r}, not {policy.policy_id!r}"
                )
            if regret.policy_version != policy.policy_version:
                failures.append(
                    f"accepted regret was issued under policy version "
                    f"{regret.policy_version!r}, not {policy.policy_version!r}"
                )
            if regret.policy_fingerprint != policy.fingerprint:
                failures.append(
                    "accepted regret carries a policy fingerprint that does not "
                    "match this assessment's policy"
                )
            if tuple(regret.qualification_levels) != tuple(self.qualification_levels):
                failures.append(
                    "accepted regret records different qualification levels than "
                    "the assessment"
                )

        # --- the qualifying pair is prescribed, not free -----------------
        if len(self.qualification_levels) != 2:
            failures.append("qualification did not use exactly two prescribed levels")
            return tuple(failures)

        b3_entered = policy.b3 is not None and (
            any(discovery.level == policy.b3 for discovery in self.discoveries)
            or any(comparison.level == policy.b3 for comparison in self.rounds)
        )
        prescribed = (policy.b2, policy.b3) if b3_entered else (policy.b1, policy.b2)
        if tuple(self.qualification_levels) != prescribed:
            failures.append(
                "qualification pair "
                f"{[level.label for level in self.qualification_levels]} is not the "
                f"prescribed pair {[level.label for level in prescribed]} "
                + (
                    "(B3 escalation was entered)"
                    if b3_entered
                    else "(B3 escalation was not entered)"
                )
            )
            return tuple(failures)

        if not any(
            discovery.discovered_move == witness for discovery in self.discoveries
        ):
            failures.append("witness has no candidate-discovery provenance")

        # --- per-round integrity -----------------------------------------
        previous_level, current_level = self.qualification_levels
        previous_round = self.round_for(previous_level, witness)
        current_round = self.round_for(current_level, witness)
        for level, comparison in (
            (previous_level, previous_round),
            (current_level, current_round),
        ):
            if comparison is None:
                failures.append(
                    f"witness has no comparison round at level {level.label}"
                )
                continue
            if comparison.position != self.decision.position_before:
                failures.append(
                    f"round at level {level.label} is not rooted at the observed "
                    "decision position"
                )
            if comparison.user_move != self.decision.move:
                failures.append(
                    f"round at level {level.label} does not evaluate the observed move"
                )
            for side in (comparison.user_evidence, comparison.alternative_evidence):
                if side.request.search_mode is not SearchMode.FORCED_MOVE:
                    failures.append(
                        f"round at level {level.label} used a non-forced search"
                    )
            if (
                comparison.user_evidence.request.config
                != comparison.alternative_evidence.request.config
            ):
                failures.append(
                    f"round at level {level.label} compared unequal requested budgets"
                )
            if (
                comparison.user_evidence.request.engine_identity
                != comparison.alternative_evidence.request.engine_identity
            ):
                failures.append(
                    f"round at level {level.label} compared incompatible engines"
                )

        if previous_round is None or current_round is None:
            return tuple(failures)

        # --- cross-level engine / profile / semantics compatibility ------
        for role, earlier, later in (
            (
                "observed move",
                previous_round.user_evidence,
                current_round.user_evidence,
            ),
            (
                "witness",
                previous_round.alternative_evidence,
                current_round.alternative_evidence,
            ),
        ):
            if _semantic_identity_excluding_budget(
                earlier.request
            ) != _semantic_identity_excluding_budget(later.request):
                failures.append(
                    "the two qualification rounds are not engine/profile/semantics "
                    f"compatible for the {role}: they differ in more than the "
                    "prescribed node budget"
                )

        # --- re-derive the qualification through the orchestrator's gate --
        discovered_at_current = any(
            discovery.level == current_level and discovery.discovered_move == witness
            for discovery in self.discoveries
        )
        try:
            verdict = _compare_levels(
                previous_round,
                current_round,
                policy,
                discovered_at_current=discovered_at_current,
            )
        except AssessmentEvidenceError as exc:
            failures.append(
                f"qualification rounds are not a valid fixed-witness pair: {exc}"
            )
            return tuple(failures)

        if verdict.triggers:
            failures.append(
                "re-derived consistency gate fails: "
                + ", ".join(sorted(trigger.value for trigger in verdict.triggers))
            )
        if not verdict.exceeds_tau:
            failures.append(
                f"re-derived conservative margin {verdict.margin_units} does not "
                f"exceed tau {policy.tau_units}"
            )
        if regret is not None and regret.units != verdict.conservative_units:
            failures.append(
                "accepted regret does not equal the re-derived conservative "
                "accepted loss min(G_prev, G_cur)"
            )
        if self.conservative_margin_units != verdict.margin_units:
            failures.append(
                "recorded conservative margin does not equal the re-derived "
                "S(A) - epsilon"
            )
        if self.final_gap_units != verdict.current_gap_units:
            failures.append(
                "final_gap_units does not equal the signed gap of the later "
                "qualification round"
            )

        return tuple(failures)

    @property
    def is_engine_damage_admissible(self) -> bool:
        """The single gate Step 4C must consult before treating this decision
        as engine-damaging."""
        return not self.admission_failures


# --- evidence provider ------------------------------------------------------


class LeveledEvidenceProvider(Protocol):
    """The seam between Step 4B orchestration and the Step 4A engine layer.

    Two methods on purpose: the orchestrator builds the request itself (so
    the request -- with its budget, engine identity and forced root move --
    is recorded as provenance and can be checked for compatibility), and then
    asks for the evidence answering exactly that request.
    """

    def request_for(
        self,
        level: SearchLevel,
        position: PositionKey,
        search_mode: SearchMode,
        root_move: MoveKey | None,
    ) -> EvaluationRequest:
        """Build the validated Step 4A request for this level. Raises
        ``RequestValidationError`` for an invalid canonical request."""
        ...

    def evaluate(self, request: EvaluationRequest) -> EngineEvaluation:
        """Return the Step 4A evidence answering ``request`` (cache or
        engine)."""
        ...


class CachedEvaluatorPool:
    """A :class:`LeveledEvidenceProvider` backed by one started Step 4A
    evaluator per search level, sharing one Step 4A evaluation cache.

    Step 4A binds one ``EngineAnalysisConfig`` -- including the node budget
    -- per ``StockfishEvaluator`` for its lifetime, so serving several
    comparison levels means several bound evaluators. This adds no second
    engine abstraction and no second cache: each level simply gets its own
    ``CachedEvaluator`` over the same shared cache, and the shared semantic
    cache key already separates levels by their requested ``nodes``.

    All levels must report the same :class:`EngineIdentity`, and no two
    levels may share an identical analysis config (that would make the two
    levels indistinguishable to the cache).
    """

    def __init__(
        self,
        bindings: Mapping[SearchLevel, StockfishEvaluator],
        cache: EvaluationCache,
    ) -> None:
        if not bindings:
            raise ValueError("at least one search level must be bound to an evaluator")

        self._by_level: dict[SearchLevel, CachedEvaluator] = {}
        self._configs: dict[SearchLevel, EngineAnalysisConfig] = {}
        self._by_config: dict[EngineAnalysisConfig, CachedEvaluator] = {}
        identities: set[EngineIdentity] = set()

        for level, evaluator in bindings.items():
            if not isinstance(level, SearchLevel):
                raise ValueError("pool bindings must be keyed by SearchLevel")
            config = evaluator.config
            if config.nodes != level.nodes:
                raise ValueError(
                    f"evaluator bound to level {level.label!r} requests "
                    f"{config.nodes} nodes, expected {level.nodes}"
                )
            if config in self._by_config:
                raise ValueError(
                    "two search levels share an identical analysis config; "
                    "levels must be distinguishable to the evaluation cache"
                )
            cached = CachedEvaluator(evaluator, cache)
            identities.add(evaluator.identity)
            self._by_level[level] = cached
            self._configs[level] = config
            self._by_config[config] = cached

        if len(identities) != 1:
            raise ValueError(
                "every comparison level must use the same engine identity"
            )
        self._identity = identities.pop()

    @property
    def engine_identity(self) -> EngineIdentity:
        return self._identity

    def request_for(
        self,
        level: SearchLevel,
        position: PositionKey,
        search_mode: SearchMode,
        root_move: MoveKey | None,
    ) -> EvaluationRequest:
        try:
            config = self._configs[level]
        except KeyError:
            raise ValueError(f"no evaluator is bound to level {level!r}") from None
        return EvaluationRequest(
            position=position,
            search_mode=search_mode,
            root_move=root_move,
            engine_identity=self._identity,
            config=config,
        )

    def evaluate(self, request: EvaluationRequest) -> EngineEvaluation:
        if request.engine_identity != self._identity:
            raise AssessmentEvidenceError(
                "request engine identity does not match this evaluator pool",
                AssessmentReason.INCOMPATIBLE_EVIDENCE,
            )
        cached = self._by_config.get(request.config)
        if cached is None:
            raise ValueError(
                f"no evaluator is bound to analysis config {request.config!r}"
            )
        return cached.evaluate(
            request.position, request.search_mode, request.root_move
        )


# --- candidate verdicts -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CandidateVerdict:
    """Internal per-candidate result of comparing two prescribed levels."""

    candidate: MoveKey
    previous_gap_units: int
    current_gap_units: int
    conservative_units: int
    """S(A) = min(G_previous(A), G_current(A)) -- the conservative accepted
    loss. Kept separate from the margin and from the final measured gap."""
    margin_units: int
    """L(A) = S(A) - epsilon_units. A conservative empirical margin, NOT a
    mathematical lower confidence bound on true chess value."""
    exceeds_tau: bool
    triggers: frozenset[AssessmentReason]
    alleges_damage: bool

    @property
    def qualifies(self) -> bool:
        return not self.triggers and self.exceeds_tau


def _compare_levels(
    previous: ComparisonRound,
    current: ComparisonRound,
    policy: ComparisonPolicy,
    *,
    discovered_at_current: bool,
) -> _CandidateVerdict:
    """Fixed-witness qualification and consistency checks for one candidate
    across two prescribed levels.

    The same candidate must appear on both sides -- a moving maximum ("A1
    looked best at B1, A2 looks best at B2, so *an* alternative was
    consistently better") is not evidence and cannot reach this function.
    """
    if previous.alternative != current.alternative:
        raise AssessmentEvidenceError(
            "fixed-witness confirmation requires the same candidate at both "
            f"levels, got {previous.alternative.uci!r} and "
            f"{current.alternative.uci!r}",
            AssessmentReason.INCOMPATIBLE_EVIDENCE,
        )
    if previous.user_move != current.user_move:
        raise AssessmentEvidenceError(
            "fixed-witness confirmation requires the same observed move at "
            "both levels",
            AssessmentReason.INCOMPATIBLE_EVIDENCE,
        )
    if previous.level == current.level:
        raise AssessmentEvidenceError(
            "confirmation requires two distinct search levels; a cache hit is "
            "evidence reuse, not an independent confirmation",
            AssessmentReason.INCOMPATIBLE_EVIDENCE,
        )

    g_prev = previous.signed_gap_units
    g_cur = current.signed_gap_units

    triggers: set[AssessmentReason] = set()
    if (g_prev > 0 > g_cur) or (g_prev < 0 < g_cur):
        triggers.add(AssessmentReason.SIGN_REVERSAL)
    if abs(g_cur - g_prev) > policy.max_gap_drift_units:
        triggers.add(AssessmentReason.GAP_DRIFT_EXCEEDED)
    if (
        abs(current.alternative_units - previous.alternative_units)
        > policy.max_alternative_drift_units
    ):
        triggers.add(AssessmentReason.ALTERNATIVE_DRIFT_EXCEEDED)
    if abs(current.user_units - previous.user_units) > policy.max_user_drift_units:
        triggers.add(AssessmentReason.USER_DRIFT_EXCEEDED)

    for earlier, later in (
        (previous.alternative_evidence, current.alternative_evidence),
        (previous.user_evidence, current.user_evidence),
    ):
        if earlier.mate_direction * later.mate_direction == -1:
            triggers.add(AssessmentReason.MATE_DIRECTION_REVERSAL)

    if discovered_at_current and g_cur <= -policy.material_negative_gap_units:
        triggers.add(AssessmentReason.MATERIAL_NEGATIVE_GAP)

    conservative = min(g_prev, g_cur)
    margin = conservative - policy.epsilon_units
    alleges = (max(g_prev, g_cur) - policy.epsilon_units) > policy.tau_units

    return _CandidateVerdict(
        candidate=current.alternative,
        previous_gap_units=g_prev,
        current_gap_units=g_cur,
        conservative_units=conservative,
        margin_units=margin,
        exceeds_tau=margin > policy.tau_units,
        triggers=frozenset(triggers),
        alleges_damage=alleges,
    )


# --- orchestration ----------------------------------------------------------


class _Run:
    """Mutable per-decision bookkeeping. Never leaks into a result: the
    published :class:`MoveAssessment` holds only immutable value objects."""

    def __init__(
        self,
        provider: LeveledEvidenceProvider,
        policy: ComparisonPolicy,
        decision: DecisionKey,
        board: chess.Board,
    ) -> None:
        self.provider = provider
        self.policy = policy
        self.decision = decision
        self.board = board
        self.position = decision.position_before
        self.user_move = decision.move
        self.active: list[MoveKey] = []
        self.discoveries: dict[str, CandidateDiscovery] = {}
        self.evidence: dict[tuple[str, str], MoveEvidence] = {}
        self.rounds: dict[tuple[str, str], ComparisonRound] = {}
        self._issued: dict[EvaluationRequest, EngineEvaluation] = {}

    # -- engine access ---------------------------------------------------

    def issue(self, request: EvaluationRequest) -> EngineEvaluation:
        """Issue one request, at most once per assessment.

        A repeated identical request is served from this per-decision memo
        (and, below it, from the Step 4A cache). Such reuse is never counted
        as additional engine work and never as an additional confirmation.
        """
        memoized = self._issued.get(request)
        if memoized is not None:
            return memoized
        cap = self.policy.max_requests_per_decision
        if cap is not None and len(self._issued) >= cap:
            raise _WorkBudgetExceeded()
        evaluation = self.provider.evaluate(request)
        self._issued[request] = evaluation
        return evaluation

    @property
    def requests_issued(self) -> int:
        return len(self._issued)

    # -- discovery -------------------------------------------------------

    def discover(self, level: SearchLevel) -> CandidateDiscovery:
        existing = self.discoveries.get(level.label)
        if existing is not None:
            return existing
        request = self.provider.request_for(
            level, self.position, SearchMode.UNRESTRICTED, None
        )
        evaluation = self.issue(request)
        if not evaluation.pv:
            raise AssessmentEvidenceError(
                "candidate discovery returned no principal variation",
                AssessmentReason.INCOMPATIBLE_EVIDENCE,
            )
        discovery = CandidateDiscovery(
            level=level,
            position=self.position,
            discovered_move=evaluation.pv[0],
            request=request,
            evaluation=evaluation,
        )
        self.discoveries[level.label] = discovery
        return discovery

    def activate(self, candidate: MoveKey) -> None:
        """Add a candidate to the bounded active alternative set.

        When the set is full the weakest currently-known candidate is evicted
        (lowest measured gap at the highest level where it has a round), so a
        newly discovered move never displaces an already stable, stronger
        witness.
        """
        if candidate == self.user_move or candidate in self.active:
            return
        if len(self.active) >= self.policy.max_active_alternatives:
            weakest = min(self.active, key=self._strength)
            self.active.remove(weakest)
        self.active.append(candidate)

    def _strength(self, candidate: MoveKey) -> tuple[int, int, str]:
        best = (-1, 0)
        for (label, move_uci), comparison in self.rounds.items():
            if move_uci != candidate.uci:
                continue
            best = max(best, (comparison.level.nodes, comparison.signed_gap_units))
        # candidate.uci keeps eviction deterministic when everything ties
        return (best[1], best[0], candidate.uci)

    # -- forced evidence and rounds --------------------------------------

    def evidence_for(self, level: SearchLevel, move: MoveKey) -> MoveEvidence:
        key = (level.label, move.uci)
        existing = self.evidence.get(key)
        if existing is not None:
            return existing
        request = self.provider.request_for(
            level, self.position, SearchMode.FORCED_MOVE, move
        )
        evaluation = self.issue(request)
        evidence = MoveEvidence(
            level=level,
            move=move,
            request=request,
            evaluation=evaluation,
            immediate_terminal=immediate_terminal_after(self.board, move),
        )
        self.evidence[key] = evidence
        return evidence

    def round_for(self, level: SearchLevel, candidate: MoveKey) -> ComparisonRound:
        key = (level.label, candidate.uci)
        existing = self.rounds.get(key)
        if existing is not None:
            return existing
        comparison = ComparisonRound(
            level=level,
            alternative=candidate,
            user_evidence=self.evidence_for(level, self.user_move),
            alternative_evidence=self.evidence_for(level, candidate),
        )
        self.rounds[key] = comparison
        return comparison

    # -- result construction ---------------------------------------------

    def _trace(
        self,
    ) -> tuple[tuple[CandidateDiscovery, ...], tuple[ComparisonRound, ...]]:
        discoveries = tuple(
            sorted(self.discoveries.values(), key=lambda d: (d.level.nodes, d.level.label))
        )
        rounds = tuple(
            sorted(
                self.rounds.values(),
                key=lambda r: (r.level.nodes, r.level.label, r.alternative.uci),
            )
        )
        return discoveries, rounds

    def finish(
        self,
        status: AssessmentStatus,
        reasons: Iterable[AssessmentReason],
        *,
        unresolved: Iterable[AssessmentReason] = (),
        resolved: Iterable[AssessmentReason] = (),
        witness: MoveKey | None = None,
        regret: EngineRegret | None = None,
        conservative_margin_units: int | None = None,
        final_gap_units: int | None = None,
        qualification_levels: tuple[SearchLevel, ...] = (),
        detail: str | None = None,
    ) -> MoveAssessment:
        discoveries, rounds = self._trace()
        return MoveAssessment(
            decision=self.decision,
            policy=self.policy,
            status=status,
            reasons=_ordered(reasons),
            unresolved_triggers=_ordered(unresolved),
            resolved_triggers=_ordered(resolved),
            witness=witness,
            regret=regret,
            conservative_margin_units=conservative_margin_units,
            final_gap_units=final_gap_units,
            qualification_levels=qualification_levels,
            discoveries=discoveries,
            rounds=rounds,
            requests_issued=self.requests_issued,
            detail=detail,
        )


class DecisionAssessor:
    """Orchestrates the bounded Step 4B procedure for one canonical decision.

    B1 screens, B2 confirms (always -- a B1 positive can never become
    ``DAMAGE_SUPPORTED`` on its own), and one optional B3 resolves
    contradictions. After B3 the procedure stops unconditionally: engine work
    is never increased until a positive result appears.
    """

    def __init__(
        self, provider: LeveledEvidenceProvider, policy: ComparisonPolicy
    ) -> None:
        self._provider = provider
        self._policy = policy

    @property
    def policy(self) -> ComparisonPolicy:
        return self._policy

    def assess(self, decision: DecisionKey) -> MoveAssessment:
        """Assess one canonical decision.

        Raises ``RequestValidationError`` (Step 4A's typed input-validation
        error) when the root is not a valid canonical decision position -- a
        terminal root, a non-canonical key, or an illegal observed move. That
        is a malformed question, not an assessment outcome.
        """
        policy = self._policy
        position = decision.position_before
        user_move = decision.move

        # Step 4A owns root and move validation: constructing the request
        # runs validate_request(), so a terminal root, a non-canonical key,
        # or an illegal observed move fails here with the Step 4A typed
        # error before any engine work happens.
        user_request = self._provider.request_for(
            policy.b1, position, SearchMode.FORCED_MOVE, user_move
        )
        board = validate_request(user_request)

        run = _Run(self._provider, policy, decision, board)

        if board.legal_moves.count() == 1:
            return run.finish(
                AssessmentStatus.NO_DAMAGE_DEMONSTRATED,
                [AssessmentReason.ONLY_LEGAL_MOVE],
            )

        if immediate_terminal_after(board, user_move) is ImmediateTerminal.CHECKMATE:
            # Exact rule-derived maximum: no alternative can be better.
            return run.finish(
                AssessmentStatus.NO_DAMAGE_DEMONSTRATED,
                [AssessmentReason.USER_MOVE_FORCES_IMMEDIATE_CHECKMATE],
            )

        try:
            return self._run_procedure(run)
        except _WorkBudgetExceeded:
            return run.finish(
                AssessmentStatus.INCONCLUSIVE,
                [AssessmentReason.WORK_BUDGET_EXHAUSTED],
                unresolved=[AssessmentReason.WORK_BUDGET_EXHAUSTED],
            )
        except AssessmentEvidenceError as exc:
            return run.finish(
                AssessmentStatus.INVALID_EVIDENCE, [exc.reason], detail=str(exc)
            )
        except EngineEvidenceError as exc:
            return run.finish(
                AssessmentStatus.INVALID_EVIDENCE,
                [AssessmentReason.INCOMPATIBLE_EVIDENCE],
                detail=str(exc),
            )

    # -- procedure -------------------------------------------------------

    def _run_procedure(self, run: _Run) -> MoveAssessment:
        policy = self._policy

        # --- B1: initial screen -----------------------------------------
        discovery = run.discover(policy.b1)
        if discovery.discovered_move == run.user_move:
            # The engine's own top-1 at B1 is the move that was played. This
            # is NOT proof that the move is globally optimal; it only means
            # this procedure found no alternative to challenge it with.
            return run.finish(
                AssessmentStatus.NO_DAMAGE_DEMONSTRATED,
                [AssessmentReason.DISCOVERY_SELECTED_USER_MOVE],
            )

        run.activate(discovery.discovered_move)
        screen = run.round_for(policy.b1, discovery.discovered_move)
        gap = screen.signed_gap_units

        # The best conservative margin any confirmation could produce with
        # this candidate is bounded by the B1 gap, because S = min(G1, G2).
        potentially_actionable = (gap - policy.epsilon_units) > policy.tau_units
        materially_negative = gap <= -policy.material_negative_gap_units

        if not potentially_actionable and not materially_negative:
            reason = (
                AssessmentReason.MARGIN_BELOW_TAU
                if gap > 0
                else AssessmentReason.NEGATIVE_OR_ZERO_GAP_NEAR_TIE
            )
            return run.finish(
                AssessmentStatus.NO_DAMAGE_DEMONSTRATED,
                [reason],
                final_gap_units=gap,
            )

        # --- B2: mandatory confirmation ---------------------------------
        outcome = self._assess_level_pair(run, policy.b1, policy.b2)
        if not outcome.unresolved:
            return self._finish_pair(run, outcome)

        # --- B3: one bounded final escalation ---------------------------
        if policy.b3 is None:
            return run.finish(
                AssessmentStatus.INCONCLUSIVE,
                [AssessmentReason.ESCALATION_EXHAUSTED, *outcome.unresolved],
                unresolved=outcome.unresolved,
                final_gap_units=outcome.final_gap_units,
            )

        final = self._assess_level_pair(run, policy.b2, policy.b3)
        # Escalation provenance: whatever the B1/B2 pair raised and the
        # B2/B3 pair no longer raises was genuinely resolved by the higher
        # prescribed pair. It stays on the immutable result rather than
        # silently disappearing.
        resolved = tuple(
            trigger
            for trigger in outcome.unresolved
            if trigger not in set(final.unresolved)
        )
        if not final.unresolved:
            return self._finish_pair(run, final, resolved=resolved)

        # Bounded stop. Never escalate further looking for a positive.
        return run.finish(
            AssessmentStatus.INCONCLUSIVE,
            [AssessmentReason.ESCALATION_EXHAUSTED, *final.unresolved],
            unresolved=final.unresolved,
            resolved=resolved,
            final_gap_units=final.final_gap_units,
        )

    def _assess_level_pair(
        self, run: _Run, previous: SearchLevel, current: SearchLevel
    ) -> "_PairOutcome":
        """Rediscover at ``current``, maintain the bounded alternative set,
        backfill whatever forced evidence the pair needs, and qualify every
        active candidate across exactly these two prescribed levels."""
        policy = self._policy

        discovery = run.discover(current)
        global_triggers: set[AssessmentReason] = set()
        if discovery.discovered_move == run.user_move:
            global_triggers.add(
                AssessmentReason.HIGHER_BUDGET_DISCOVERY_SELECTED_USER
            )
        else:
            run.activate(discovery.discovered_move)

        # A candidate can only act as a two-level witness once it has forced
        # evidence at BOTH prescribed levels -- newly discovered candidates
        # are backfilled at the lower level before they can qualify.
        for candidate in list(run.active):
            run.round_for(previous, candidate)
            run.round_for(current, candidate)

        verdicts: list[_CandidateVerdict] = []
        for candidate in list(run.active):
            verdicts.append(
                _compare_levels(
                    run.round_for(previous, candidate),
                    run.round_for(current, candidate),
                    policy,
                    discovered_at_current=(candidate == discovery.discovered_move),
                )
            )

        unresolved: set[AssessmentReason] = set()
        alleges_any = False
        for verdict in verdicts:
            alleges_any = alleges_any or verdict.alleges_damage
            if verdict.alleges_damage:
                unresolved |= verdict.triggers & _INSTABILITY_TRIGGERS
            if AssessmentReason.MATERIAL_NEGATIVE_GAP in verdict.triggers:
                unresolved.add(AssessmentReason.MATERIAL_NEGATIVE_GAP)
        if alleges_any:
            unresolved |= global_triggers

        qualifying = [verdict for verdict in verdicts if verdict.qualifies]
        best = (
            max(qualifying, key=lambda v: (v.conservative_units, v.candidate.uci))
            if qualifying
            else None
        )
        final_gap = (
            best.current_gap_units
            if best is not None
            else max((v.current_gap_units for v in verdicts), default=None)
        )

        return _PairOutcome(
            levels=(previous, current),
            best=best,
            unresolved=_ordered(unresolved),
            final_gap_units=final_gap,
        )

    def _finish_pair(
        self,
        run: _Run,
        outcome: "_PairOutcome",
        *,
        resolved: Iterable[AssessmentReason] = (),
    ) -> MoveAssessment:
        policy = self._policy
        best = outcome.best
        if best is None:
            return run.finish(
                AssessmentStatus.NO_DAMAGE_DEMONSTRATED,
                [AssessmentReason.NO_WITNESS_SURVIVED_CONFIRMATION],
                resolved=resolved,
                final_gap_units=outcome.final_gap_units,
            )

        if not policy.permits_damage_conclusion:
            # The measurement is complete and coherent, but this policy's
            # thresholds are not calibrated, so the positive label is
            # withheld rather than manufactured.
            return run.finish(
                AssessmentStatus.INCONCLUSIVE,
                [AssessmentReason.UNCALIBRATED_POLICY],
                unresolved=[AssessmentReason.UNCALIBRATED_POLICY],
                resolved=resolved,
                final_gap_units=outcome.final_gap_units,
            )

        regret = EngineRegret(
            units=best.conservative_units,
            witness=best.candidate,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            policy_fingerprint=policy.fingerprint,
            qualification_levels=outcome.levels,
        )
        return run.finish(
            AssessmentStatus.DAMAGE_SUPPORTED,
            [AssessmentReason.CONFIRMED_BY_FIXED_WITNESS],
            resolved=resolved,
            witness=best.candidate,
            regret=regret,
            conservative_margin_units=best.margin_units,
            final_gap_units=best.current_gap_units,
            qualification_levels=outcome.levels,
        )


@dataclass(frozen=True, slots=True)
class _PairOutcome:
    levels: tuple[SearchLevel, SearchLevel]
    best: _CandidateVerdict | None
    unresolved: tuple[AssessmentReason, ...]
    final_gap_units: int | None


def assess_decision(
    decision: DecisionKey,
    provider: LeveledEvidenceProvider,
    policy: ComparisonPolicy,
) -> MoveAssessment:
    """Convenience wrapper around :class:`DecisionAssessor`."""
    return DecisionAssessor(provider, policy).assess(decision)
