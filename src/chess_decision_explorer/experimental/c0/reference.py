"""Strong fixed-witness reference trajectories and convergence diagnostics.

EXPERIMENTAL. This module measures, at each explicitly supplied stronger
reference node budget `j`:

    G_ref_j = U_ref_j(witness) - U_ref_j(observed_move)

from the SAME canonical root, with the SAME fixed witness, using the SAME
Step 4A / Step 4B value objects the production procedure uses. It computes no
final `G_reference` and freezes no reference protocol: C0 collects the
trajectory, it does not collapse it.

Reuse, not reimplementation:

- `MoveEvidence` re-checks that a reference evaluation is a `FORCED_MOVE`
  search of the intended move at the intended budget, and supplies
  `U = 2W + D` through the production `expected_score_units`;
- `ComparisonRound` is the only thing that ever produces a signed gap, and it
  structurally refuses two evaluations that are not rooted at the same
  position, at the same level, at equal requested budgets, under compatible
  engine identity;
- `ReferenceTrajectory` additionally refuses any measurement whose witness or
  observed move differs from the rest -- gaps from different witnesses can
  never be mixed into one trajectory.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum

import chess

from ...domain import MoveKey, PositionKey
from ...engine import EvaluationRequest, SearchMode, SemanticCacheKey
from ...assessment import (
    EXPECTED_SCORE_UNIT_SCALE,
    ComparisonRound,
    LeveledEvidenceProvider,
    MoveEvidence,
    SearchLevel,
    immediate_terminal_after,
)


class ReferenceLadderError(ValueError):
    """Raised when a reference ladder is not a valid C0 reference schedule."""


def validate_reference_ladder(
    levels: Iterable[SearchLevel], audited_production_nodes: int
) -> tuple[SearchLevel, ...]:
    """Validate an explicitly supplied reference ladder.

    A reference level only audits a production qualification level if it is
    genuinely stronger than it, so every reference budget must exceed
    ``audited_production_nodes`` (the largest production level the policy can
    qualify on). The ladder itself must be ordered and strictly increasing,
    with distinct labels. No schedule is hard-coded anywhere.
    """
    ladder = tuple(levels)
    if not ladder:
        raise ReferenceLadderError("a reference ladder requires at least one level")
    for level in ladder:
        if not isinstance(level, SearchLevel):
            raise ReferenceLadderError("reference ladder entries must be SearchLevel")
    labels = [level.label for level in ladder]
    if len(set(labels)) != len(labels):
        raise ReferenceLadderError(
            f"reference level labels must be distinct, got {labels!r}"
        )
    budgets = [level.nodes for level in ladder]
    if any(later <= earlier for earlier, later in zip(budgets, budgets[1:])):
        raise ReferenceLadderError(
            f"reference node budgets must be ordered and strictly increasing, got {budgets!r}"
        )
    if type(audited_production_nodes) is not int or audited_production_nodes < 1:
        raise ReferenceLadderError(
            f"audited_production_nodes must be a positive int, got "
            f"{audited_production_nodes!r}"
        )
    if budgets[0] <= audited_production_nodes:
        raise ReferenceLadderError(
            f"every reference level must be stronger than the production "
            f"qualification level it audits ({audited_production_nodes} nodes); "
            f"got {budgets!r}"
        )
    return ladder


class Saturation(Enum):
    """Whether an expected-score unit value sits on a scale endpoint.

    Endpoint values cannot move further in one direction, so a gap that
    involves one is not evidence of convergence.
    """

    NONE = "none"
    MAX = "max"
    MIN = "min"


def saturation_of(units: int) -> Saturation:
    if units >= EXPECTED_SCORE_UNIT_SCALE:
        return Saturation.MAX
    if units <= 0:
        return Saturation.MIN
    return Saturation.NONE


def _evidence_dict(evidence: MoveEvidence, elapsed_s: float) -> dict:
    evaluation = evidence.evaluation
    key = SemanticCacheKey.from_request(evidence.request)
    return {
        "move": evidence.move.uci,
        "requested_nodes": evidence.request.config.nodes,
        "units": evidence.units,
        "expected_score": evaluation.expected_score,
        "saturation": saturation_of(evidence.units).value,
        "centipawn": evaluation.centipawn,
        "mate": evaluation.mate,
        "mate_direction": evidence.mate_direction,
        "wdl_wins": evaluation.wdl_wins,
        "wdl_draws": evaluation.wdl_draws,
        "wdl_losses": evaluation.wdl_losses,
        "wdl_extreme": 1000 in (
            evaluation.wdl_wins,
            evaluation.wdl_draws,
            evaluation.wdl_losses,
        ),
        "evidence_depth": evaluation.depth,
        "evidence_seldepth": evaluation.seldepth,
        "evidence_nodes": evaluation.nodes,
        "pv": [move.uci for move in evaluation.pv],
        "immediate_terminal": evidence.immediate_terminal.value,
        "elapsed_seconds": elapsed_s,
        "semantic_cache_key": {
            name: getattr(key, name)
            for name in SemanticCacheKey.__dataclass_fields__
        },
    }


@dataclass(frozen=True, slots=True)
class ReferenceLevelMeasurement:
    """One reference level: two independent forced analyses from one root."""

    level: SearchLevel
    round: ComparisonRound
    witness_elapsed_seconds: float
    observed_elapsed_seconds: float

    def __post_init__(self) -> None:
        if self.round.level != self.level:
            raise ValueError(
                "reference measurement level does not match its comparison round"
            )

    @property
    def witness(self) -> MoveKey:
        return self.round.alternative

    @property
    def observed_move(self) -> MoveKey:
        return self.round.user_move

    @property
    def witness_units(self) -> int:
        return self.round.alternative_units

    @property
    def observed_units(self) -> int:
        return self.round.user_units

    @property
    def g_ref_units(self) -> int:
        """``U_ref(witness) - U_ref(observed_move)``, signed, never clamped."""
        return self.round.signed_gap_units

    @property
    def elapsed_seconds(self) -> float:
        return self.witness_elapsed_seconds + self.observed_elapsed_seconds

    @property
    def saturation_status(self) -> str:
        witness = saturation_of(self.witness_units) is not Saturation.NONE
        observed = saturation_of(self.observed_units) is not Saturation.NONE
        if witness and observed:
            return "both"
        if witness:
            return "witness"
        if observed:
            return "observed"
        return "none"

    def as_dict(self) -> dict:
        return {
            "level_label": self.level.label,
            "requested_nodes": self.level.nodes,
            "witness_units": self.witness_units,
            "observed_units": self.observed_units,
            "g_ref_units": self.g_ref_units,
            "g_ref_expected_score": self.g_ref_units / EXPECTED_SCORE_UNIT_SCALE,
            "centipawn_gap": self.round.centipawn_gap,
            "saturation_status": self.saturation_status,
            "elapsed_seconds": self.elapsed_seconds,
            "witness": _evidence_dict(
                self.round.alternative_evidence, self.witness_elapsed_seconds
            ),
            "observed": _evidence_dict(
                self.round.user_evidence, self.observed_elapsed_seconds
            ),
        }


@dataclass(frozen=True, slots=True)
class ReferenceStepDiagnostic:
    """Diagnostics between two consecutive reference levels. Diagnostics
    only: C0 defines no convergence threshold and emits no convergence
    verdict."""

    from_label: str
    to_label: str
    delta_g_units: int
    witness_drift_units: int
    observed_drift_units: int
    sign_change: bool
    witness_mate_direction_change: bool
    observed_mate_direction_change: bool
    witness_mate_appeared: bool
    observed_mate_appeared: bool

    def as_dict(self) -> dict:
        return {
            "from_level": self.from_label,
            "to_level": self.to_label,
            "delta_g_units": self.delta_g_units,
            "witness_drift_units": self.witness_drift_units,
            "observed_drift_units": self.observed_drift_units,
            "sign_change": self.sign_change,
            "witness_mate_direction_change": self.witness_mate_direction_change,
            "observed_mate_direction_change": self.observed_mate_direction_change,
            "witness_mate_appeared": self.witness_mate_appeared,
            "observed_mate_appeared": self.observed_mate_appeared,
        }


@dataclass(frozen=True, slots=True)
class ReferenceConvergenceDiagnostics:
    steps: tuple[ReferenceStepDiagnostic, ...]
    monotonicity: str
    saturated_level_count: int
    sign_change_count: int
    mate_direction_change_count: int
    max_abs_delta_g_units: int | None

    def as_dict(self) -> dict:
        return {
            "steps": [step.as_dict() for step in self.steps],
            "monotonicity": self.monotonicity,
            "saturated_level_count": self.saturated_level_count,
            "sign_change_count": self.sign_change_count,
            "mate_direction_change_count": self.mate_direction_change_count,
            "max_abs_delta_g_units": self.max_abs_delta_g_units,
            "note": (
                "Diagnostics only. C0 defines no convergence threshold and "
                "makes no convergence claim."
            ),
        }


@dataclass(frozen=True, slots=True)
class ReferenceTrajectory:
    """The ordered reference-level measurements for ONE fixed witness.

    Construction refuses any measurement rooted at a different position, with
    a different observed move, or with a different witness: reference gaps
    from different witnesses can never be mixed into one trajectory.
    """

    position: PositionKey
    observed_move: MoveKey
    witness: MoveKey
    measurements: tuple[ReferenceLevelMeasurement, ...]

    def __post_init__(self) -> None:
        if not self.measurements:
            raise ValueError("a reference trajectory requires at least one measurement")
        if self.witness == self.observed_move:
            raise ValueError("the reference witness must differ from the observed move")
        budgets = [m.level.nodes for m in self.measurements]
        if any(later <= earlier for earlier, later in zip(budgets, budgets[1:])):
            raise ValueError(
                f"reference measurements must be ordered by strictly increasing "
                f"node budget, got {budgets!r}"
            )
        for measurement in self.measurements:
            if measurement.round.position != self.position:
                raise ValueError(
                    "every reference measurement must share the same canonical root"
                )
            if measurement.observed_move != self.observed_move:
                raise ValueError(
                    "every reference measurement must evaluate the same observed move"
                )
            if measurement.witness != self.witness:
                raise ValueError(
                    "every reference measurement must use the same fixed witness; "
                    f"got {measurement.witness.uci!r} and {self.witness.uci!r}"
                )

    @property
    def total_elapsed_seconds(self) -> float:
        return sum(m.elapsed_seconds for m in self.measurements)

    def diagnostics(self) -> ReferenceConvergenceDiagnostics:
        steps: list[ReferenceStepDiagnostic] = []
        for earlier, later in zip(self.measurements, self.measurements[1:]):
            g_prev = earlier.g_ref_units
            g_cur = later.g_ref_units
            steps.append(
                ReferenceStepDiagnostic(
                    from_label=earlier.level.label,
                    to_label=later.level.label,
                    delta_g_units=g_cur - g_prev,
                    witness_drift_units=later.witness_units - earlier.witness_units,
                    observed_drift_units=later.observed_units - earlier.observed_units,
                    # Same strict rule the production drift gate uses.
                    sign_change=(g_prev > 0 > g_cur) or (g_prev < 0 < g_cur),
                    witness_mate_direction_change=_mate_reversal(
                        earlier.round.alternative_evidence,
                        later.round.alternative_evidence,
                    ),
                    observed_mate_direction_change=_mate_reversal(
                        earlier.round.user_evidence, later.round.user_evidence
                    ),
                    witness_mate_appeared=_mate_appeared(
                        earlier.round.alternative_evidence,
                        later.round.alternative_evidence,
                    ),
                    observed_mate_appeared=_mate_appeared(
                        earlier.round.user_evidence, later.round.user_evidence
                    ),
                )
            )

        gaps = [m.g_ref_units for m in self.measurements]
        if len(gaps) < 2:
            monotonicity = "single_level"
        elif all(later >= earlier for earlier, later in zip(gaps, gaps[1:])):
            monotonicity = "non_decreasing"
        elif all(later <= earlier for earlier, later in zip(gaps, gaps[1:])):
            monotonicity = "non_increasing"
        else:
            monotonicity = "non_monotonic"

        return ReferenceConvergenceDiagnostics(
            steps=tuple(steps),
            monotonicity=monotonicity,
            saturated_level_count=sum(
                1 for m in self.measurements if m.saturation_status != "none"
            ),
            sign_change_count=sum(1 for step in steps if step.sign_change),
            mate_direction_change_count=sum(
                1
                for step in steps
                if step.witness_mate_direction_change
                or step.observed_mate_direction_change
            ),
            max_abs_delta_g_units=(
                max(abs(step.delta_g_units) for step in steps) if steps else None
            ),
        )

    def as_dict(self) -> dict:
        return {
            "position_epd": self.position.epd,
            "observed_move": self.observed_move.uci,
            "witness": self.witness.uci,
            "levels": [m.as_dict() for m in self.measurements],
            "diagnostics": self.diagnostics().as_dict(),
            "total_elapsed_seconds": self.total_elapsed_seconds,
        }


def _mate_reversal(earlier: MoveEvidence, later: MoveEvidence) -> bool:
    return earlier.mate_direction * later.mate_direction == -1


def _mate_appeared(earlier: MoveEvidence, later: MoveEvidence) -> bool:
    return earlier.mate_direction == 0 and later.mate_direction != 0


def measure_reference_trajectory(
    provider: LeveledEvidenceProvider,
    board: chess.Board,
    position: PositionKey,
    observed_move: MoveKey,
    witness: MoveKey,
    ladder: Iterable[SearchLevel],
) -> ReferenceTrajectory:
    """Run independent forced analyses of ``witness`` and ``observed_move``
    from the same canonical root at every level of ``ladder``.

    Both sides of every level go through the same provider, so the engine
    identity, analysis profile, threads and hash are identical for the two
    moves and across levels: nothing but the requested node budget changes.
    """
    if witness == observed_move:
        raise ValueError("the reference witness must differ from the observed move")

    measurements: list[ReferenceLevelMeasurement] = []
    for level in ladder:
        witness_evidence, witness_elapsed = _forced_evidence(
            provider, board, position, level, witness
        )
        observed_evidence, observed_elapsed = _forced_evidence(
            provider, board, position, level, observed_move
        )
        measurements.append(
            ReferenceLevelMeasurement(
                level=level,
                round=ComparisonRound(
                    level=level,
                    alternative=witness,
                    user_evidence=observed_evidence,
                    alternative_evidence=witness_evidence,
                ),
                witness_elapsed_seconds=witness_elapsed,
                observed_elapsed_seconds=observed_elapsed,
            )
        )

    return ReferenceTrajectory(
        position=position,
        observed_move=observed_move,
        witness=witness,
        measurements=tuple(measurements),
    )


def _forced_evidence(
    provider: LeveledEvidenceProvider,
    board: chess.Board,
    position: PositionKey,
    level: SearchLevel,
    move: MoveKey,
) -> tuple[MoveEvidence, float]:
    request: EvaluationRequest = provider.request_for(
        level, position, SearchMode.FORCED_MOVE, move
    )
    started = time.perf_counter()
    evaluation = provider.evaluate(request)
    elapsed = time.perf_counter() - started
    evidence = MoveEvidence(
        level=level,
        move=move,
        request=request,
        evaluation=evaluation,
        immediate_terminal=immediate_terminal_after(board, move),
    )
    return evidence, elapsed


def unrestricted_discovery_audit(
    provider: LeveledEvidenceProvider,
    position: PositionKey,
    level: SearchLevel,
) -> Mapping[str, object]:
    """C0-ONLY experimental stronger-discovery diagnostic.

    One ordinary single-PV `UNRESTRICTED` search at a stronger budget, run
    from the same canonical root. It records what a stronger *unrestricted*
    search selects, which is the hook a later stronger-discovery audit needs.

    It changes no production discovery behaviour, adds no MultiPV, and is
    kept strictly OUTSIDE the fixed-witness reference measurement: its score
    never enters any gap. A full stronger-discovery audit (for example
    MultiPV candidate enumeration) remains follow-up work.
    """
    request = provider.request_for(level, position, SearchMode.UNRESTRICTED, None)
    started = time.perf_counter()
    evaluation = provider.evaluate(request)
    elapsed = time.perf_counter() - started
    return {
        "experimental": True,
        "isolated_from_reference_measurement": True,
        "multipv": False,
        "level_label": level.label,
        "requested_nodes": level.nodes,
        "selected_move": evaluation.pv[0].uci if evaluation.pv else None,
        "centipawn": evaluation.centipawn,
        "mate": evaluation.mate,
        "wdl_wins": evaluation.wdl_wins,
        "wdl_draws": evaluation.wdl_draws,
        "wdl_losses": evaluation.wdl_losses,
        "evidence_depth": evaluation.depth,
        "evidence_nodes": evaluation.nodes,
        "pv": [move.uci for move in evaluation.pv],
        "elapsed_seconds": elapsed,
    }
