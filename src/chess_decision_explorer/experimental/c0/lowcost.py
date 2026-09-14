"""The low-cost Step 4B trajectory, recorded verbatim.

EXPERIMENTAL. This module runs the REAL, UNMODIFIED Step 4B code path
(`DecisionAssessor.assess`) and serialises everything it produced. It
reimplements none of Step 4B's logic: every judgement here is either read off
the returned `MoveAssessment` or re-derived by calling production functions
(`_compare_levels`, `MoveAssessment.admission_failures`).

`S` is the qualifying `EngineRegret.units` of the Step 4B procedure. When the
pilot runs under a genuinely UNCALIBRATED policy the production contract
deliberately withholds the positive label -- and therefore the witness and
the regret -- so the fixed witness and `S` are re-derived by feeding the
recorded rounds back through the production `_compare_levels` gate, exactly
as the Step 4C admission contract does. Which path produced `S` is always
recorded.

That re-derivation is narrow on purpose. It applies ONLY when calibration
scope is the sole thing Step 4B withheld the label for
(:func:`withheld_only_for_calibration_scope`). Any other refusal -- no
qualifying candidate, an instability that survived escalation, an exhausted
work budget, invalid evidence -- is a substantive Step 4B outcome, and C0
records it as a root with no `R` target rather than re-deriving around it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...domain import MoveKey
from ...engine import SemanticCacheKey
from ...assessment import (
    AssessmentReason,
    AssessmentStatus,
    CandidateDiscovery,
    ComparisonPolicy,
    ComparisonRound,
    MoveAssessment,
    MoveEvidence,
    SearchLevel,
    _compare_levels,
)


class WitnessSource:
    """How the fixed witness and `S` were obtained."""

    ENGINE_REGRET = "engine_regret"
    """Straight off the Step 4B `EngineRegret` (the policy permitted the
    positive label)."""

    REDERIVED_FROM_TRACE = "rederived_from_trace"
    """Re-derived from the recorded rounds through the production
    `_compare_levels` gate, because the policy is UNCALIBRATED and calibration
    scope was the ONLY reason the production contract withheld the label."""

    NONE = "none"


def prescribed_qualification_pair(
    assessment: MoveAssessment,
) -> tuple[SearchLevel, SearchLevel]:
    """The two levels this assessment is allowed to qualify on.

    Read off the trace with the same rule the Step 4C admission contract
    uses: whether B3 was entered is decided by a B3 discovery or a B3 round,
    never by a summary field.
    """
    policy = assessment.policy
    b3_entered = policy.b3 is not None and (
        any(discovery.level == policy.b3 for discovery in assessment.discoveries)
        or any(comparison.level == policy.b3 for comparison in assessment.rounds)
    )
    return (policy.b2, policy.b3) if b3_entered else (policy.b1, policy.b2)


@dataclass(frozen=True, slots=True)
class WitnessResolution:
    """The fixed witness and `S` for one root, or why there is no R target."""

    witness: MoveKey | None
    s_units: int | None
    source: str
    conservative_margin_units: int | None
    qualification_levels: tuple[SearchLevel, ...]
    no_r_target_reason: str | None

    @property
    def has_target(self) -> bool:
        return self.witness is not None and self.s_units is not None

    def as_dict(self) -> dict:
        return {
            "witness": self.witness.uci if self.witness is not None else None,
            "s_units": self.s_units,
            "witness_source": self.source,
            "conservative_margin_units": self.conservative_margin_units,
            "qualification_levels": [
                {"label": level.label, "nodes": level.nodes}
                for level in self.qualification_levels
            ],
            "no_r_target_reason": self.no_r_target_reason,
        }


def resolve_fixed_witness(assessment: MoveAssessment) -> WitnessResolution:
    """Resolve the eligible fixed witness and qualifying `S` for one root.

    Never fabricates an `R` target: a root with no qualifying witness keeps
    its recorded reason and no numbers.
    """
    if assessment.regret is not None and assessment.witness is not None:
        return WitnessResolution(
            witness=assessment.witness,
            s_units=assessment.regret.units,
            source=WitnessSource.ENGINE_REGRET,
            conservative_margin_units=assessment.conservative_margin_units,
            qualification_levels=tuple(assessment.qualification_levels),
            no_r_target_reason=None,
        )

    reason = _no_target_reason(assessment)

    if not withheld_only_for_calibration_scope(assessment):
        # Step 4B refused to name a witness for a substantive reason -- no
        # candidate qualified, an instability survived escalation, the work
        # budget ran out, or the evidence was invalid. Re-deriving would
        # manufacture an `S` the production procedure declined to produce.
        return WitnessResolution(
            witness=None,
            s_units=None,
            source=WitnessSource.NONE,
            conservative_margin_units=None,
            qualification_levels=(),
            no_r_target_reason=reason,
        )

    previous_level, current_level = prescribed_qualification_pair(assessment)
    candidates = sorted(
        {
            comparison.alternative
            for comparison in assessment.rounds
            if comparison.level in (previous_level, current_level)
        },
        key=lambda move: move.uci,
    )

    best = None
    for candidate in candidates:
        previous = assessment.round_for(previous_level, candidate)
        current = assessment.round_for(current_level, candidate)
        if previous is None or current is None:
            continue
        # Read off the trace with the same rule the Step 4C admission
        # contract uses, so the re-derived gate is the production gate.
        discovered_at_current = any(
            discovery.level == current_level
            and discovery.discovered_move == candidate
            for discovery in assessment.discoveries
        )
        verdict = _compare_levels(
            previous,
            current,
            assessment.policy,
            discovered_at_current=discovered_at_current,
        )
        if not verdict.qualifies:
            continue
        if best is None or (verdict.conservative_units, verdict.candidate.uci) > (
            best.conservative_units,
            best.candidate.uci,
        ):
            best = verdict

    if best is None:
        return WitnessResolution(
            witness=None,
            s_units=None,
            source=WitnessSource.NONE,
            conservative_margin_units=None,
            qualification_levels=(),
            no_r_target_reason=reason,
        )

    return WitnessResolution(
        witness=best.candidate,
        s_units=best.conservative_units,
        source=WitnessSource.REDERIVED_FROM_TRACE,
        conservative_margin_units=best.margin_units,
        qualification_levels=(previous_level, current_level),
        no_r_target_reason=None,
    )


def withheld_only_for_calibration_scope(assessment: MoveAssessment) -> bool:
    """Whether Step 4B completed a coherent qualification and withheld the
    positive label *only* because the policy's calibration scope forbids it.

    That is the one case a C0 re-derivation is faithful to Step 4B. The
    orchestrator reaches it by finishing a prescribed pair with a qualifying
    verdict and then declining to label it (`UNCALIBRATED_POLICY` as the sole
    unresolved trigger). Every other withholding -- `ESCALATION_EXHAUSTED`
    with surviving instability triggers, an exhausted work budget, invalid
    evidence, or simply no qualifying candidate -- is a substantive refusal
    that C0 must reproduce, not re-derive around.
    """
    return (
        assessment.status is AssessmentStatus.INCONCLUSIVE
        and assessment.unresolved_triggers
        == (AssessmentReason.UNCALIBRATED_POLICY,)
    )


def _no_target_reason(assessment: MoveAssessment) -> str:
    reasons = ",".join(reason.value for reason in assessment.reasons)
    if assessment.status is AssessmentStatus.INVALID_EVIDENCE:
        return f"invalid_evidence:{reasons}"
    if not assessment.rounds:
        return f"no_comparison_rounds:{reasons or assessment.status.value}"
    return f"{assessment.status.value}:{reasons}" if reasons else assessment.status.value


# --- serialisation ----------------------------------------------------------


def _cache_key_dict(request) -> dict:
    key = SemanticCacheKey.from_request(request)
    return {
        name: getattr(key, name) for name in SemanticCacheKey.__dataclass_fields__
    }


def evidence_dict(evidence: MoveEvidence) -> dict:
    evaluation = evidence.evaluation
    return {
        "move": evidence.move.uci,
        "level_label": evidence.level.label,
        "requested_nodes": evidence.request.config.nodes,
        "units": evidence.units,
        "centipawn": evaluation.centipawn,
        "mate": evaluation.mate,
        "mate_direction": evidence.mate_direction,
        "wdl_wins": evaluation.wdl_wins,
        "wdl_draws": evaluation.wdl_draws,
        "wdl_losses": evaluation.wdl_losses,
        "evidence_depth": evaluation.depth,
        "evidence_seldepth": evaluation.seldepth,
        "evidence_nodes": evaluation.nodes,
        "pv": [move.uci for move in evaluation.pv],
        "immediate_terminal": evidence.immediate_terminal.value,
        "semantic_cache_key": _cache_key_dict(evidence.request),
    }


def discovery_dict(discovery: CandidateDiscovery, order_index: int) -> dict:
    evaluation = discovery.evaluation
    return {
        "order_index": order_index,
        "level_label": discovery.level.label,
        "requested_nodes": discovery.level.nodes,
        "discovered_move": discovery.discovered_move.uci,
        "search_mode": discovery.request.search_mode.value,
        "centipawn": evaluation.centipawn,
        "mate": evaluation.mate,
        "wdl_wins": evaluation.wdl_wins,
        "wdl_draws": evaluation.wdl_draws,
        "wdl_losses": evaluation.wdl_losses,
        "evidence_depth": evaluation.depth,
        "evidence_nodes": evaluation.nodes,
        "pv": [move.uci for move in evaluation.pv],
        "semantic_cache_key": _cache_key_dict(discovery.request),
    }


def round_dict(comparison: ComparisonRound, order_index: int) -> dict:
    return {
        "order_index": order_index,
        "level_label": comparison.level.label,
        "requested_nodes": comparison.level.nodes,
        "alternative": comparison.alternative.uci,
        "observed_move": comparison.user_move.uci,
        "alternative_units": comparison.alternative_units,
        "observed_units": comparison.user_units,
        "signed_gap_units": comparison.signed_gap_units,
        "centipawn_gap": comparison.centipawn_gap,
        "observed_evidence": evidence_dict(comparison.user_evidence),
        "alternative_evidence": evidence_dict(comparison.alternative_evidence),
    }


def assessment_dict(assessment: MoveAssessment) -> dict:
    """Everything the Step 4B procedure produced, preserved verbatim.

    Discoveries and rounds are recorded in the assessment's own stored order,
    which is chronological in search strength (ascending level budget), with
    an explicit ``order_index``.
    """
    return {
        "assessment_semantics_version": assessment.semantics_version,
        "scope_note": assessment.scope_note,
        "status": assessment.status.value,
        "reasons": [reason.value for reason in assessment.reasons],
        "unresolved_triggers": [
            reason.value for reason in assessment.unresolved_triggers
        ],
        "resolved_triggers": [reason.value for reason in assessment.resolved_triggers],
        "witness": assessment.witness.uci if assessment.witness is not None else None,
        "engine_regret_units": (
            assessment.regret.units if assessment.regret is not None else None
        ),
        "conservative_margin_units": assessment.conservative_margin_units,
        "final_gap_units": assessment.final_gap_units,
        "qualification_levels": [
            {"label": level.label, "nodes": level.nodes}
            for level in assessment.qualification_levels
        ],
        "requests_issued": assessment.requests_issued,
        "detail": assessment.detail,
        "is_engine_damage_admissible": assessment.is_engine_damage_admissible,
        "admission_failures": list(assessment.admission_failures),
        "discoveries": [
            discovery_dict(discovery, index)
            for index, discovery in enumerate(assessment.discoveries)
        ],
        "rounds": [
            round_dict(comparison, index)
            for index, comparison in enumerate(assessment.rounds)
        ],
    }


def policy_dict(policy: ComparisonPolicy) -> dict:
    return {
        "policy_id": policy.policy_id,
        "policy_version": policy.policy_version,
        "fingerprint": policy.fingerprint,
        "canonical_json": policy.canonical_json(),
        "calibration_status": policy.calibration_status.value,
        "calibration_id": policy.calibration_id,
        "levels": [
            {"label": level.label, "nodes": level.nodes} for level in policy.levels
        ],
        "epsilon_units": policy.epsilon_units,
        "tau_units": policy.tau_units,
        "max_gap_drift_units": policy.max_gap_drift_units,
        "max_alternative_drift_units": policy.max_alternative_drift_units,
        "max_user_drift_units": policy.max_user_drift_units,
        "material_negative_gap_units": policy.material_negative_gap_units,
        "max_active_alternatives": policy.max_active_alternatives,
        "max_requests_per_decision": policy.max_requests_per_decision,
    }
