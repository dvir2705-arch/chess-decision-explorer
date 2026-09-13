"""Step 4B: canonical-position damaging-move assessment.

Every test here is synthetic: engine evidence is scripted, so the ordinary
pytest suite never needs a real Stockfish binary. Real-engine coverage lives
in ``tests/test_assessment_stockfish.py`` (skipped unless a binary is
supplied) and in ``scripts/stockfish_assessment_smoke.py``.
"""

from __future__ import annotations

import chess
import pytest

from chess_decision_explorer.domain import DecisionKey, MoveKey, PositionKey
from chess_decision_explorer.engine import (
    EngineAnalysisConfig,
    EngineEvaluation,
    EngineIdentity,
    EvaluationRequest,
    RequestValidationError,
    SearchMode,
    SemanticCacheKey,
)
from chess_decision_explorer.engine_cache import InMemoryEvaluationCache
from chess_decision_explorer.assessment import (
    ASSESSMENT_SEMANTICS_VERSION,
    CANONICAL_DAMAGE_SCOPE_NOTE,
    EXPECTED_SCORE_UNIT_SCALE,
    AssessmentEvidenceError,
    AssessmentReason,
    AssessmentStatus,
    STATUS_SEMANTICS,
    CachedEvaluatorPool,
    CalibrationStatus,
    CandidateDiscovery,
    ComparisonPolicy,
    ComparisonRound,
    DecisionAssessor,
    EngineRegret,
    ImmediateTerminal,
    MoveAssessment,
    MoveEvidence,
    SearchLevel,
    _compare_levels,
    assess_decision,
    expected_score_from_units,
    expected_score_units,
    immediate_terminal_after,
)

_FAKE_SHA = "a" * 64

B1 = SearchLevel("B1", 1000)
B2 = SearchLevel("B2", 4000)
B3 = SearchLevel("B3", 16000)


# --- helpers ----------------------------------------------------------------


def identity(name="Stockfish 19", sha=_FAKE_SHA, eval_file=None):
    return EngineIdentity(name=name, executable_sha256=sha, eval_file=eval_file)


def units_wdl(units: int) -> tuple[int, int, int]:
    """Exact WDL triple (summing to 1000) whose ``2*W + D`` equals ``units``."""
    draws = units % 2
    wins = (units - draws) // 2
    return wins, draws, 1000 - wins - draws


def make_evaluation(units=1000, *, cp=None, mate=None, pv=("e2e4",), depth=20, nodes=999):
    wins, draws, losses = units_wdl(units)
    if mate is None and cp is None:
        cp = 0
    return EngineEvaluation(
        centipawn=cp,
        mate=mate,
        wdl_wins=wins,
        wdl_draws=draws,
        wdl_losses=losses,
        depth=depth,
        seldepth=depth + 5,
        nodes=nodes,
        pv=tuple(MoveKey(uci) for uci in pv),
    )


class ScriptedProvider:
    """A :class:`LeveledEvidenceProvider` driven by a lookup table.

    ``script`` maps ``(level nodes, key)`` to an evaluation spec, where
    ``key`` is ``"*"`` for the unrestricted discovery search and the move's
    UCI for a forced search. A spec is either an integer (expected-score
    units) or a dict with ``units`` / ``cp`` / ``mate`` / ``pv`` / ``move``.
    """

    def __init__(self, script, *, threads=1, hash_mb=64, engine_identity=None):
        self.script = dict(script)
        self.threads = threads
        self.hash_mb = hash_mb
        self.engine_identity = engine_identity or identity()
        self.calls: list[EvaluationRequest] = []
        self.unique_requests: set[EvaluationRequest] = set()

    def request_for(self, level, position, search_mode, root_move):
        return EvaluationRequest(
            position=position,
            search_mode=search_mode,
            root_move=root_move,
            engine_identity=self.engine_identity,
            config=EngineAnalysisConfig(
                nodes=level.nodes, threads=self.threads, hash_mb=self.hash_mb
            ),
        )

    def evaluate(self, request):
        self.calls.append(request)
        self.unique_requests.add(request)
        unrestricted = request.search_mode is SearchMode.UNRESTRICTED
        key = "*" if unrestricted else request.root_move.uci
        lookup = (request.config.nodes, key)
        if lookup not in self.script:
            raise AssertionError(f"unscripted engine request: {lookup}")
        spec = self.script[lookup]
        if isinstance(spec, int):
            spec = {"units": spec}
        if unrestricted:
            default_pv = (spec["move"],)
        else:
            default_pv = (request.root_move.uci,)
        return make_evaluation(
            units=spec.get("units", 1000),
            cp=spec.get("cp"),
            mate=spec.get("mate"),
            pv=spec.get("pv", default_pv),
            nodes=request.config.nodes,
        )

    def forced_calls(self, nodes=None):
        return [
            call
            for call in self.calls
            if call.search_mode is SearchMode.FORCED_MOVE
            and (nodes is None or call.config.nodes == nodes)
        ]


def make_policy(
    *,
    b1=B1,
    b2=B2,
    b3=None,
    epsilon_units=10,
    tau_units=40,
    max_gap_drift_units=200,
    max_alternative_drift_units=200,
    max_user_drift_units=200,
    material_negative_gap_units=150,
    max_active_alternatives=2,
    max_requests_per_decision=None,
    calibrated=True,
    policy_id="test-policy",
    policy_version="v1",
):
    return ComparisonPolicy(
        policy_id=policy_id,
        policy_version=policy_version,
        b1=b1,
        b2=b2,
        b3=b3,
        epsilon_units=epsilon_units,
        tau_units=tau_units,
        max_gap_drift_units=max_gap_drift_units,
        max_alternative_drift_units=max_alternative_drift_units,
        max_user_drift_units=max_user_drift_units,
        material_negative_gap_units=material_negative_gap_units,
        max_active_alternatives=max_active_alternatives,
        max_requests_per_decision=max_requests_per_decision,
        calibration_status=(
            CalibrationStatus.CALIBRATED if calibrated else CalibrationStatus.UNCALIBRATED
        ),
        calibration_id="calibration-2026-01" if calibrated else None,
    )


def position_of(fen=None, moves=()):
    board = chess.Board() if fen is None else chess.Board(fen)
    for uci in moves:
        board.push_uci(uci)
    return PositionKey.from_board(board), board


START_POSITION, START_BOARD = position_of()
BLACK_POSITION, BLACK_BOARD = position_of(moves=("e2e4",))

# Exactly one legal move (a8a7); black is not in check, so the root is a
# valid nonterminal decision position.
ONE_MOVE_FEN = "k7/8/8/8/8/8/1R6/K6R b - - 0 1"
CHECKMATE_ROOT_FEN = "7k/6Q1/6K1/8/8/8/8/8 b - - 0 1"
STALEMATE_ROOT_FEN = "7k/5Q2/6K1/8/8/8/8/8 b - - 0 1"
# 1.Ra8# is mate; 1.Kg1h1 is a quiet alternative.
BACK_RANK_FEN = "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1"
# 1.Qf7 is stalemate; 1.Qf6 is not.
STALEMATE_TRAP_FEN = "7k/8/8/8/8/8/5Q2/6K1 w - - 0 1"


def decision(position, move_uci):
    return DecisionKey(position_before=position, move=MoveKey(move_uci))


def evidence(
    level,
    move_uci,
    position=START_POSITION,
    board=START_BOARD,
    engine_identity=None,
    **spec,
):
    request = EvaluationRequest(
        position=position,
        search_mode=SearchMode.FORCED_MOVE,
        root_move=MoveKey(move_uci),
        engine_identity=engine_identity or identity(),
        config=EngineAnalysisConfig(nodes=level.nodes),
    )
    evaluation = make_evaluation(pv=(move_uci,), **spec)
    return MoveEvidence(
        level=level,
        move=MoveKey(move_uci),
        request=request,
        evaluation=evaluation,
        immediate_terminal=immediate_terminal_after(board, MoveKey(move_uci)),
    )


def make_round(level, alternative_uci, user_uci, alt_units, user_units, **kwargs):
    alt_spec = kwargs.pop("alt_spec", {})
    user_spec = kwargs.pop("user_spec", {})
    engine_identity = kwargs.pop("engine_identity", None)
    return ComparisonRound(
        level=level,
        alternative=MoveKey(alternative_uci),
        user_evidence=evidence(
            level,
            user_uci,
            units=user_units,
            engine_identity=engine_identity,
            **user_spec,
        ),
        alternative_evidence=evidence(
            level,
            alternative_uci,
            units=alt_units,
            engine_identity=engine_identity,
            **alt_spec,
        ),
    )


def damage_trace(pairs, *, witness="d2d4", user_uci="e2e4"):
    """Build a realistic (discoveries, rounds) trace for a forged assessment.

    ``pairs`` is a sequence of ``(level, alternative_units, user_units)``.
    """
    discoveries = tuple(
        CandidateDiscovery(
            level=level,
            position=START_POSITION,
            discovered_move=MoveKey(witness),
            request=EvaluationRequest(
                position=START_POSITION,
                search_mode=SearchMode.UNRESTRICTED,
                root_move=None,
                engine_identity=identity(),
                config=EngineAnalysisConfig(nodes=level.nodes),
            ),
            evaluation=make_evaluation(units=1500, pv=(witness,)),
        )
        for level, _, _ in pairs
    )
    rounds = tuple(
        make_round(level, witness, user_uci, alt_units, user_units)
        for level, alt_units, user_units in pairs
    )
    return discoveries, rounds


def forge(
    pairs,
    *,
    policy=None,
    witness="d2d4",
    qualification_levels=None,
    regret_units=None,
    regret_overrides=None,
    conservative_margin_units=None,
    final_gap_units=None,
    unresolved_triggers=(),
    resolved_triggers=(),
    rounds=None,
    discoveries=None,
):
    """Hand-build a DAMAGE_SUPPORTED MoveAssessment.

    Used to attack the Step 4C admission contract directly: every summary
    field can be set to whatever a forger would like it to say, while the
    stored rounds say something else.
    """
    policy = policy or make_policy()
    built_discoveries, built_rounds = damage_trace(pairs, witness=witness)
    if discoveries is None:
        discoveries = built_discoveries
    if rounds is None:
        rounds = built_rounds
    if qualification_levels is None:
        qualification_levels = tuple(level for level, _, _ in pairs)
    gaps = [alt - user for _, alt, user in pairs]
    conservative = min(gaps)
    if regret_units is None:
        regret_units = conservative
    if conservative_margin_units is None:
        conservative_margin_units = conservative - policy.epsilon_units
    if final_gap_units is None:
        final_gap_units = gaps[-1]
    regret_fields = dict(
        units=regret_units,
        witness=MoveKey(witness),
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_fingerprint=policy.fingerprint,
        qualification_levels=tuple(qualification_levels),
    )
    regret_fields.update(regret_overrides or {})
    return MoveAssessment(
        decision=decision(START_POSITION, "e2e4"),
        policy=policy,
        status=AssessmentStatus.DAMAGE_SUPPORTED,
        reasons=(AssessmentReason.CONFIRMED_BY_FIXED_WITNESS,),
        unresolved_triggers=tuple(unresolved_triggers),
        resolved_triggers=tuple(resolved_triggers),
        witness=MoveKey(witness),
        regret=EngineRegret(**regret_fields),
        conservative_margin_units=conservative_margin_units,
        final_gap_units=final_gap_units,
        qualification_levels=tuple(qualification_levels),
        discoveries=discoveries,
        rounds=rounds,
        requests_issued=6,
    )


# The baseline damaging-move script: d2d4 is discovered at both levels and
# stays ~300 units ahead of the observed e2e4 under equal forced budgets.
BASELINE_SCRIPT = {
    (1000, "*"): {"move": "d2d4", "units": 1500},
    (1000, "d2d4"): 1300,
    (1000, "e2e4"): 1000,
    (4000, "*"): {"move": "d2d4", "units": 1500},
    (4000, "d2d4"): 1320,
    (4000, "e2e4"): 1010,
}


def run(script, policy=None, dec=None, **provider_kwargs):
    provider = ScriptedProvider(script, **provider_kwargs)
    policy = policy or make_policy()
    dec = dec or decision(START_POSITION, "e2e4")
    return provider, assess_decision(dec, provider, policy)


# --- 1. exact integer WDL arithmetic ---------------------------------------


@pytest.mark.parametrize(
    "wins, draws, losses, expected",
    [(1000, 0, 0, 2000), (0, 1000, 0, 1000), (0, 0, 1000, 0), (500, 300, 200, 1300)],
)
def test_expected_score_units_is_exact_integer_arithmetic(wins, draws, losses, expected):
    evaluation = EngineEvaluation(
        centipawn=12,
        mate=None,
        wdl_wins=wins,
        wdl_draws=draws,
        wdl_losses=losses,
        depth=20,
        seldepth=25,
        nodes=1000,
        pv=(MoveKey("e2e4"),),
    )
    units = expected_score_units(evaluation)
    assert units == expected
    assert type(units) is int
    assert 0 <= units <= EXPECTED_SCORE_UNIT_SCALE


def test_units_agree_exactly_with_step_4a_expected_score():
    for units in (0, 1, 999, 1000, 1301, 1999, 2000):
        wins, draws, losses = units_wdl(units)
        evaluation = make_evaluation(units=units)
        assert (evaluation.wdl_wins, evaluation.wdl_draws, evaluation.wdl_losses) == (
            wins,
            draws,
            losses,
        )
        assert expected_score_units(evaluation) == units
        assert expected_score_from_units(units) == evaluation.expected_score


def test_expected_score_display_conversion_is_display_only():
    assert expected_score_from_units(1300) == 0.65
    assert expected_score_from_units(0) == 0.0
    assert expected_score_from_units(2000) == 1.0


# --- 2 / 10 / 11. signed gaps, preservation, no clamping --------------------


@pytest.mark.parametrize(
    "alt_units, user_units, expected_gap",
    [(1300, 1000, 300), (1000, 1000, 0), (1220, 1280, -60), (0, 2000, -2000)],
)
def test_signed_gap_is_preserved_exactly(alt_units, user_units, expected_gap):
    comparison = make_round(B1, "d2d4", "e2e4", alt_units, user_units)
    assert comparison.signed_gap_units == expected_gap
    assert comparison.alternative_units == alt_units
    assert comparison.user_units == user_units


def test_negative_gap_is_never_clamped_or_absolute_valued():
    # Alternative 0.61 vs observed move 0.64 -- the challenger simply lost.
    comparison = make_round(B1, "d2d4", "e2e4", 1220, 1280)
    assert comparison.signed_gap_units == -60
    assert comparison.signed_gap_units != abs(comparison.signed_gap_units)
    assert max(0, comparison.signed_gap_units) != comparison.signed_gap_units


def test_negative_near_tie_stops_without_a_damage_label_or_a_regret():
    script = {
        (1000, "*"): {"move": "d2d4", "units": 1400},
        (1000, "d2d4"): 1220,
        (1000, "e2e4"): 1280,
    }
    _, assessment = run(script)
    assert assessment.status is AssessmentStatus.NO_DAMAGE_DEMONSTRATED
    assert assessment.reasons == (AssessmentReason.NEGATIVE_OR_ZERO_GAP_NEAR_TIE,)
    assert assessment.final_gap_units == -60
    assert assessment.regret is None


def test_no_damage_demonstrated_does_not_claim_optimality():
    wording = STATUS_SEMANTICS[AssessmentStatus.NO_DAMAGE_DEMONSTRATED]
    assert "does NOT assert that the observed move is" in wording
    assert "optimal" in wording
    assert "proven equivalent" in wording
    assert set(STATUS_SEMANTICS) == set(AssessmentStatus)


# --- 3. White and Black POV -------------------------------------------------


def test_white_to_move_damage_is_supported():
    _, assessment = run(BASELINE_SCRIPT)
    assert assessment.status is AssessmentStatus.DAMAGE_SUPPORTED
    assert assessment.witness == MoveKey("d2d4")
    assert assessment.regret.units == 300


def test_black_to_move_uses_the_same_root_pov_arithmetic():
    assert BLACK_BOARD.turn == chess.BLACK
    script = {
        (1000, "*"): {"move": "c7c5", "units": 1500},
        (1000, "c7c5"): 1300,
        (1000, "e7e5"): 1000,
        (4000, "*"): {"move": "c7c5", "units": 1500},
        (4000, "c7c5"): 1320,
        (4000, "e7e5"): 1010,
    }
    _, assessment = run(script, dec=decision(BLACK_POSITION, "e7e5"))
    assert assessment.status is AssessmentStatus.DAMAGE_SUPPORTED
    assert assessment.witness == MoveKey("c7c5")
    assert assessment.regret.units == 300
    assert assessment.final_gap_units == 310


# --- 4. the unrestricted discovery score never enters the arithmetic --------


def test_discovery_score_cannot_reach_regret_arithmetic():
    # Discovery reports a wildly optimistic 2000 units; the forced evidence
    # says the two moves are 20 units apart. Only the forced evidence counts.
    script = {
        (1000, "*"): {"move": "d2d4", "units": 2000},
        (1000, "d2d4"): 1020,
        (1000, "e2e4"): 1000,
    }
    _, assessment = run(script)
    assert assessment.status is AssessmentStatus.NO_DAMAGE_DEMONSTRATED
    assert assessment.final_gap_units == 20
    assert assessment.regret is None
    # The discovery evidence is still retained, purely as provenance.
    assert len(assessment.discoveries) == 1
    assert expected_score_units(assessment.discoveries[0].evaluation) == 2000


def test_comparison_round_structurally_refuses_unrestricted_evidence():
    request = EvaluationRequest(
        position=START_POSITION,
        search_mode=SearchMode.UNRESTRICTED,
        root_move=None,
        engine_identity=identity(),
        config=EngineAnalysisConfig(nodes=B1.nodes),
    )
    with pytest.raises(AssessmentEvidenceError) as excinfo:
        MoveEvidence(
            level=B1,
            move=MoveKey("d2d4"),
            request=request,
            evaluation=make_evaluation(pv=("d2d4",)),
            immediate_terminal=ImmediateTerminal.NONE,
        )
    assert excinfo.value.reason is AssessmentReason.INCOMPATIBLE_EVIDENCE


def test_candidate_discovery_requires_an_unrestricted_request():
    request = EvaluationRequest(
        position=START_POSITION,
        search_mode=SearchMode.FORCED_MOVE,
        root_move=MoveKey("d2d4"),
        engine_identity=identity(),
        config=EngineAnalysisConfig(nodes=B1.nodes),
    )
    with pytest.raises(AssessmentEvidenceError):
        CandidateDiscovery(
            level=B1,
            position=START_POSITION,
            discovered_move=MoveKey("d2d4"),
            request=request,
            evaluation=make_evaluation(pv=("d2d4",)),
        )


# --- 5 / 6. a B1 positive alone is never damage; B2 is mandatory ------------


def test_b1_positive_alone_cannot_produce_damage_supported():
    # A large B1 gap that completely evaporates at B2.
    script = {
        (1000, "*"): {"move": "d2d4", "units": 1900},
        (1000, "d2d4"): 1900,
        (1000, "e2e4"): 1000,
        (4000, "*"): {"move": "d2d4", "units": 1900},
        (4000, "d2d4"): 1000,
        (4000, "e2e4"): 1000,
    }
    _, assessment = run(script)
    assert assessment.status is not AssessmentStatus.DAMAGE_SUPPORTED
    assert assessment.regret is None


def test_damage_always_rests_on_two_independent_forced_levels():
    provider, assessment = run(BASELINE_SCRIPT)
    assert assessment.status is AssessmentStatus.DAMAGE_SUPPORTED
    assert assessment.qualification_levels == (B1, B2)
    assert assessment.round_for(B1, MoveKey("d2d4")) is not None
    assert assessment.round_for(B2, MoveKey("d2d4")) is not None
    forced_b2 = {call.root_move.uci for call in provider.forced_calls(nodes=4000)}
    assert forced_b2 == {"d2d4", "e2e4"}


def test_a_screened_small_positive_gap_stops_at_b1():
    script = {
        (1000, "*"): {"move": "d2d4", "units": 1500},
        (1000, "d2d4"): 1030,
        (1000, "e2e4"): 1000,
    }
    provider, assessment = run(script)
    assert assessment.status is AssessmentStatus.NO_DAMAGE_DEMONSTRATED
    assert assessment.reasons == (AssessmentReason.MARGIN_BELOW_TAU,)
    assert {call.config.nodes for call in provider.calls} == {1000}


# --- 7 / 8. fixed-witness confirmation --------------------------------------


def test_compare_levels_refuses_a_candidate_switch():
    previous = make_round(B1, "d2d4", "e2e4", 1400, 1000)
    current = make_round(B2, "g1f3", "e2e4", 1400, 1000)
    with pytest.raises(AssessmentEvidenceError) as excinfo:
        _compare_levels(previous, current, make_policy(), discovered_at_current=True)
    assert excinfo.value.reason is AssessmentReason.INCOMPATIBLE_EVIDENCE


def test_compare_levels_refuses_the_same_level_twice():
    previous = make_round(B1, "d2d4", "e2e4", 1400, 1000)
    current = make_round(B1, "d2d4", "e2e4", 1400, 1000)
    with pytest.raises(AssessmentEvidenceError):
        _compare_levels(previous, current, make_policy(), discovered_at_current=True)


def test_a_moving_maximum_cannot_masquerade_as_confirmation():
    # d2d4 leads at B1, g1f3 leads at B2. "Some alternative was always
    # better" is not evidence: neither candidate survives both levels.
    script = {
        (1000, "*"): {"move": "d2d4", "units": 1500},
        (1000, "d2d4"): 1400,
        (1000, "g1f3"): 1005,
        (1000, "e2e4"): 1000,
        (4000, "*"): {"move": "g1f3", "units": 1500},
        (4000, "d2d4"): 1005,
        (4000, "g1f3"): 1400,
        (4000, "e2e4"): 1000,
    }
    _, assessment = run(
        script,
        policy=make_policy(
            max_gap_drift_units=1000, max_alternative_drift_units=1000
        ),
    )
    assert assessment.status is AssessmentStatus.NO_DAMAGE_DEMONSTRATED
    assert assessment.reasons == (AssessmentReason.NO_WITNESS_SURVIVED_CONFIRMATION,)
    assert assessment.witness is None
    assert assessment.regret is None


# --- 9. a newly discovered witness needs lower-level backfill ---------------


def test_new_b2_candidate_is_backfilled_at_b1_before_it_can_witness():
    script = {
        (1000, "*"): {"move": "d2d4", "units": 1500},
        (1000, "d2d4"): 1300,
        (1000, "g1f3"): 1310,
        (1000, "e2e4"): 1000,
        (4000, "*"): {"move": "g1f3", "units": 1500},
        (4000, "d2d4"): 1320,
        (4000, "g1f3"): 1330,
        (4000, "e2e4"): 1010,
    }
    provider, assessment = run(script)
    backfilled = [
        call
        for call in provider.forced_calls(nodes=1000)
        if call.root_move.uci == "g1f3"
    ]
    assert len(backfilled) == 1, "the B2-discovered candidate must be backfilled at B1"
    assert assessment.round_for(B1, MoveKey("g1f3")) is not None
    assert assessment.status is AssessmentStatus.DAMAGE_SUPPORTED
    # Both candidates qualify; the stronger conservative loss wins.
    assert assessment.witness == MoveKey("g1f3")
    assert assessment.regret.units == 310


# --- 12 / 13 / 14 / 15. consistency and drift gates -------------------------


def test_sign_reversal_is_an_unresolved_contradiction():
    script = {
        (1000, "*"): {"move": "d2d4", "units": 1500},
        (1000, "d2d4"): 1300,
        (1000, "e2e4"): 1000,
        (4000, "*"): {"move": "d2d4", "units": 1500},
        (4000, "d2d4"): 1000,
        (4000, "e2e4"): 1300,
    }
    _, assessment = run(script, policy=make_policy(max_gap_drift_units=1000,
                                                   max_user_drift_units=1000,
                                                   max_alternative_drift_units=1000))
    assert assessment.status is AssessmentStatus.INCONCLUSIVE
    assert AssessmentReason.SIGN_REVERSAL in assessment.unresolved_triggers
    assert assessment.regret is None


def test_stable_sign_but_excessive_gap_drift_is_unresolved():
    script = {
        (1000, "*"): {"move": "d2d4", "units": 1500},
        (1000, "d2d4"): 1300,
        (1000, "e2e4"): 1000,
        (4000, "*"): {"move": "d2d4", "units": 1500},
        (4000, "d2d4"): 1900,
        (4000, "e2e4"): 1000,
    }
    _, assessment = run(
        script,
        policy=make_policy(max_gap_drift_units=200, max_alternative_drift_units=1000),
    )
    assert assessment.status is AssessmentStatus.INCONCLUSIVE
    assert AssessmentReason.GAP_DRIFT_EXCEEDED in assessment.unresolved_triggers


def test_stable_gap_but_large_component_drift_is_unresolved():
    # Both component evaluations move 600 units; the gap is +300 at both
    # levels, so a sign-only check would have accepted this.
    script = {
        (1000, "*"): {"move": "d2d4", "units": 1500},
        (1000, "d2d4"): 1300,
        (1000, "e2e4"): 1000,
        (4000, "*"): {"move": "d2d4", "units": 1500},
        (4000, "d2d4"): 1900,
        (4000, "e2e4"): 1600,
    }
    _, assessment = run(script, policy=make_policy(max_gap_drift_units=1000))
    assert assessment.status is AssessmentStatus.INCONCLUSIVE
    assert AssessmentReason.ALTERNATIVE_DRIFT_EXCEEDED in assessment.unresolved_triggers
    assert AssessmentReason.USER_DRIFT_EXCEEDED in assessment.unresolved_triggers


def test_higher_budget_result_overrides_a_favourable_lower_budget_one():
    script = {
        (1000, "*"): {"move": "d2d4", "units": 1500},
        (1000, "d2d4"): 1900,
        (1000, "e2e4"): 1000,
        (4000, "*"): {"move": "d2d4", "units": 1500},
        (4000, "d2d4"): 1020,
        (4000, "e2e4"): 1000,
    }
    _, assessment = run(script, policy=make_policy(max_gap_drift_units=1000,
                                                   max_alternative_drift_units=1000))
    assert assessment.status is AssessmentStatus.NO_DAMAGE_DEMONSTRATED
    assert assessment.final_gap_units == 20
    assert assessment.regret is None


def test_material_negative_benchmark_discrepancy_escalates_and_abstains():
    script = {
        (1000, "*"): {"move": "d2d4", "units": 1500},
        (1000, "d2d4"): 1000,
        (1000, "e2e4"): 1900,
        (4000, "*"): {"move": "d2d4", "units": 1500},
        (4000, "d2d4"): 1000,
        (4000, "e2e4"): 1900,
    }
    _, assessment = run(script, policy=make_policy(material_negative_gap_units=150))
    assert assessment.status is AssessmentStatus.INCONCLUSIVE
    assert AssessmentReason.MATERIAL_NEGATIVE_GAP in assessment.unresolved_triggers


def test_higher_budget_discovery_selecting_the_user_move_escalates():
    script = {
        (1000, "*"): {"move": "d2d4", "units": 1500},
        (1000, "d2d4"): 1300,
        (1000, "e2e4"): 1000,
        (4000, "*"): {"move": "e2e4", "units": 1500},
        (4000, "d2d4"): 1320,
        (4000, "e2e4"): 1010,
    }
    _, assessment = run(script)
    assert assessment.status is AssessmentStatus.INCONCLUSIVE
    assert (
        AssessmentReason.HIGHER_BUDGET_DISCOVERY_SELECTED_USER
        in assessment.unresolved_triggers
    )


# --- 16 / 17 / 18. discovery identity, single move, terminal root -----------


def test_discovery_selecting_the_user_move_demonstrates_no_damage():
    script = {(1000, "*"): {"move": "e2e4", "units": 1500}}
    provider, assessment = run(script)
    assert assessment.status is AssessmentStatus.NO_DAMAGE_DEMONSTRATED
    assert assessment.reasons == (AssessmentReason.DISCOVERY_SELECTED_USER_MOVE,)
    assert assessment.regret is None
    # No forced comparison was needed, and nothing claims global optimality.
    assert provider.forced_calls() == []


def test_only_legal_move_is_not_a_move_choice_and_costs_no_engine_work():
    position, _ = position_of(ONE_MOVE_FEN)
    provider, assessment = run({}, dec=decision(position, "a8a7"))
    assert assessment.status is AssessmentStatus.NO_DAMAGE_DEMONSTRATED
    assert assessment.reasons == (AssessmentReason.ONLY_LEGAL_MOVE,)
    assert provider.calls == []
    assert assessment.requests_issued == 0


def test_terminal_root_is_an_invalid_decision_request():
    position, _ = position_of(CHECKMATE_ROOT_FEN)
    provider = ScriptedProvider({})
    with pytest.raises(RequestValidationError):
        assess_decision(
            DecisionKey(position_before=position, move=MoveKey("h8h7")),
            provider,
            make_policy(),
        )
    assert provider.calls == []


def test_stalemate_root_is_an_invalid_decision_request():
    position, _ = position_of(STALEMATE_ROOT_FEN)
    with pytest.raises(RequestValidationError):
        assess_decision(
            DecisionKey(position_before=position, move=MoveKey("h8h7")),
            ScriptedProvider({}),
            make_policy(),
        )


def test_illegal_observed_move_is_rejected_by_the_step_4a_path():
    with pytest.raises(RequestValidationError):
        assess_decision(decision(START_POSITION, "e2e5"), ScriptedProvider({}), make_policy())


# --- 19 / 20. immediate terminal transitions --------------------------------


def test_immediate_terminal_detection_is_exact():
    _, board = position_of(BACK_RANK_FEN)
    assert immediate_terminal_after(board, MoveKey("a1a8")) is ImmediateTerminal.CHECKMATE
    assert immediate_terminal_after(board, MoveKey("g1h1")) is ImmediateTerminal.NONE
    _, trap = position_of(STALEMATE_TRAP_FEN)
    assert immediate_terminal_after(trap, MoveKey("f2f7")) is ImmediateTerminal.STALEMATE


def test_observed_move_delivering_immediate_checkmate_is_never_damaging():
    position, _ = position_of(BACK_RANK_FEN)
    provider, assessment = run({}, dec=decision(position, "a1a8"))
    assert assessment.status is AssessmentStatus.NO_DAMAGE_DEMONSTRATED
    assert assessment.reasons == (
        AssessmentReason.USER_MOVE_FORCES_IMMEDIATE_CHECKMATE,
    )
    assert provider.calls == []


def test_missing_an_immediate_checkmate_is_damaging():
    position, _ = position_of(BACK_RANK_FEN)
    script = {
        (1000, "*"): {"move": "a1a8", "units": 2000, "mate": 1},
        (1000, "a1a8"): {"units": 2000, "mate": 1},
        (1000, "g1h1"): 1000,
        (4000, "*"): {"move": "a1a8", "units": 2000, "mate": 1},
        (4000, "a1a8"): {"units": 2000, "mate": 1},
        (4000, "g1h1"): 1000,
    }
    _, assessment = run(
        script,
        policy=make_policy(max_alternative_drift_units=1000, max_gap_drift_units=1000),
        dec=decision(position, "g1h1"),
    )
    assert assessment.status is AssessmentStatus.DAMAGE_SUPPORTED
    assert assessment.witness == MoveKey("a1a8")
    assert assessment.regret.units == 1000


def test_walking_into_an_immediate_stalemate_is_damaging():
    position, _ = position_of(STALEMATE_TRAP_FEN)
    script = {
        (1000, "*"): {"move": "f2f6", "units": 2000},
        (1000, "f2f6"): 2000,
        (1000, "f2f7"): 1000,
        (4000, "*"): {"move": "f2f6", "units": 2000},
        (4000, "f2f6"): 2000,
        (4000, "f2f7"): 1000,
    }
    _, assessment = run(
        script,
        policy=make_policy(max_alternative_drift_units=1000, max_gap_drift_units=1000),
        dec=decision(position, "f2f7"),
    )
    assert assessment.status is AssessmentStatus.DAMAGE_SUPPORTED
    assert assessment.regret.units == 1000
    stalemate_evidence = assessment.round_for(B1, MoveKey("f2f6")).user_evidence
    assert stalemate_evidence.immediate_terminal is ImmediateTerminal.STALEMATE


def test_engine_evidence_contradicting_an_exact_terminal_result_is_invalid():
    position, _ = position_of(STALEMATE_TRAP_FEN)
    script = {
        (1000, "*"): {"move": "f2f6", "units": 2000},
        (1000, "f2f6"): 2000,
        # The engine claims a mate on a move that is an exact stalemate.
        (1000, "f2f7"): {"units": 0, "mate": -3},
    }
    _, assessment = run(script, dec=decision(position, "f2f7"))
    assert assessment.status is AssessmentStatus.INVALID_EVIDENCE
    assert assessment.reasons == (AssessmentReason.TERMINAL_EVIDENCE_CONTRADICTION,)


def test_mate_distance_alone_does_not_create_regret():
    # Both moves mate; only the distance differs, and WDL says 2000 both ways.
    comparison = make_round(
        B1,
        "d2d4",
        "e2e4",
        2000,
        2000,
        alt_spec={"mate": 3},
        user_spec={"mate": 7},
    )
    assert comparison.signed_gap_units == 0
    assert comparison.centipawn_gap is None


def test_both_moves_losing_at_different_mate_distances_gets_no_penalty():
    comparison = make_round(
        B1,
        "d2d4",
        "e2e4",
        0,
        0,
        alt_spec={"mate": -9},
        user_spec={"mate": -2},
    )
    assert comparison.signed_gap_units == 0


# --- 21. mate appearance / disappearance ------------------------------------


def test_a_mate_appearing_at_a_higher_budget_is_not_a_contradiction():
    script = {
        (1000, "*"): {"move": "d2d4", "units": 1500},
        (1000, "d2d4"): 1300,
        (1000, "e2e4"): 1000,
        (4000, "*"): {"move": "d2d4", "units": 1500},
        (4000, "d2d4"): {"units": 1400, "mate": 12},
        (4000, "e2e4"): 1010,
    }
    _, assessment = run(script)
    assert assessment.status is AssessmentStatus.DAMAGE_SUPPORTED
    assert AssessmentReason.MATE_DIRECTION_REVERSAL not in assessment.unresolved_triggers


def test_a_mate_direction_reversal_is_a_contradiction():
    script = {
        (1000, "*"): {"move": "d2d4", "units": 1500},
        (1000, "d2d4"): {"units": 1300, "mate": 9},
        (1000, "e2e4"): 1000,
        (4000, "*"): {"move": "d2d4", "units": 1500},
        (4000, "d2d4"): {"units": 1320, "mate": -9},
        (4000, "e2e4"): 1010,
    }
    _, assessment = run(script)
    assert assessment.status is AssessmentStatus.INCONCLUSIVE
    assert AssessmentReason.MATE_DIRECTION_REVERSAL in assessment.unresolved_triggers


# --- 22 / 23. bounded work and deterministic stopping -----------------------


def test_work_budget_exhaustion_abstains_rather_than_guessing():
    _, assessment = run(
        BASELINE_SCRIPT, policy=make_policy(max_requests_per_decision=3)
    )
    assert assessment.status is AssessmentStatus.INCONCLUSIVE
    assert assessment.reasons == (AssessmentReason.WORK_BUDGET_EXHAUSTED,)
    assert assessment.unresolved_triggers == (AssessmentReason.WORK_BUDGET_EXHAUSTED,)
    assert assessment.requests_issued == 3
    assert assessment.regret is None


def test_escalation_stops_after_b3_and_never_searches_further():
    # Every level contradicts the previous one.
    script = {
        (1000, "*"): {"move": "d2d4", "units": 1500},
        (1000, "d2d4"): 1900,
        (1000, "e2e4"): 1000,
        (4000, "*"): {"move": "d2d4", "units": 1500},
        (4000, "d2d4"): 1000,
        (4000, "e2e4"): 1900,
        (16000, "*"): {"move": "d2d4", "units": 1500},
        (16000, "d2d4"): 1900,
        (16000, "e2e4"): 1000,
    }
    provider, assessment = run(script, policy=make_policy(b3=B3))
    assert assessment.status is AssessmentStatus.INCONCLUSIVE
    assert AssessmentReason.ESCALATION_EXHAUSTED in assessment.reasons
    assert {call.config.nodes for call in provider.calls} == {1000, 4000, 16000}
    assert assessment.qualification_levels == ()


def test_b3_can_resolve_a_b2_contradiction():
    script = {
        (1000, "*"): {"move": "d2d4", "units": 1500},
        (1000, "d2d4"): 1900,
        (1000, "e2e4"): 1000,
        (4000, "*"): {"move": "d2d4", "units": 1500},
        (4000, "d2d4"): 1300,
        (4000, "e2e4"): 1000,
        (16000, "*"): {"move": "d2d4", "units": 1500},
        (16000, "d2d4"): 1320,
        (16000, "e2e4"): 1010,
    }
    _, assessment = run(script, policy=make_policy(b3=B3))
    assert assessment.status is AssessmentStatus.DAMAGE_SUPPORTED
    # Qualification uses the last two prescribed levels, not the two most
    # favourable ones.
    assert assessment.qualification_levels == (B2, B3)
    assert assessment.regret.units == 300
    # The B1/B2 instability that forced the escalation is preserved as
    # provenance rather than vanishing from the final result.
    assert assessment.unresolved_triggers == ()
    assert AssessmentReason.GAP_DRIFT_EXCEEDED in assessment.resolved_triggers
    assert AssessmentReason.ALTERNATIVE_DRIFT_EXCEEDED in assessment.resolved_triggers
    # Resolved history must not block Step 4C admission.
    assert assessment.is_engine_damage_admissible


# --- 24. cache reuse is reuse, not confirmation -----------------------------


def test_repeating_an_identical_request_is_reuse_not_extra_work():
    script = {
        (1000, "*"): {"move": "d2d4", "units": 1500},
        (1000, "d2d4"): 1300,
        (1000, "g1f3"): 1290,
        (1000, "e2e4"): 1000,
        (4000, "*"): {"move": "g1f3", "units": 1500},
        (4000, "d2d4"): 1320,
        (4000, "g1f3"): 1300,
        (4000, "e2e4"): 1010,
    }
    provider, assessment = run(script)
    # The observed move takes part in two rounds per level but is requested
    # exactly once per level.
    for nodes in (1000, 4000):
        user_calls = [
            call for call in provider.forced_calls(nodes) if call.root_move.uci == "e2e4"
        ]
        assert len(user_calls) == 1
    assert assessment.requests_issued == len(provider.unique_requests)
    assert len(provider.calls) == assessment.requests_issued


def test_reused_b1_evidence_cannot_count_as_a_b2_confirmation():
    # A B1 and a B2 evaluation of the same move are different requests, so a
    # cache hit at one level can never satisfy the other.
    b1_request = EvaluationRequest(
        position=START_POSITION,
        search_mode=SearchMode.FORCED_MOVE,
        root_move=MoveKey("d2d4"),
        engine_identity=identity(),
        config=EngineAnalysisConfig(nodes=B1.nodes),
    )
    b2_request = EvaluationRequest(
        position=START_POSITION,
        search_mode=SearchMode.FORCED_MOVE,
        root_move=MoveKey("d2d4"),
        engine_identity=identity(),
        config=EngineAnalysisConfig(nodes=B2.nodes),
    )
    assert b1_request != b2_request
    assert SemanticCacheKey.from_request(b1_request) != SemanticCacheKey.from_request(
        b2_request
    )
    with pytest.raises(AssessmentEvidenceError):
        MoveEvidence(
            level=B2,
            move=MoveKey("d2d4"),
            request=b1_request,
            evaluation=make_evaluation(pv=("d2d4",)),
            immediate_terminal=ImmediateTerminal.NONE,
        )


# --- 25. only DAMAGE_SUPPORTED exposes an accepted regret -------------------


@pytest.mark.parametrize(
    "status",
    [
        AssessmentStatus.NO_DAMAGE_DEMONSTRATED,
        AssessmentStatus.INCONCLUSIVE,
        AssessmentStatus.INVALID_EVIDENCE,
    ],
)
def test_non_damage_statuses_cannot_carry_a_regret(status):
    regret = EngineRegret(
        units=300,
        witness=MoveKey("d2d4"),
        policy_id="p",
        policy_version="v1",
        policy_fingerprint="f",
        qualification_levels=(B1, B2),
    )
    with pytest.raises(ValueError):
        MoveAssessment(
            decision=decision(START_POSITION, "e2e4"),
            policy=make_policy(),
            status=status,
            reasons=(),
            unresolved_triggers=(),
            witness=MoveKey("d2d4"),
            regret=regret,
            conservative_margin_units=290,
            final_gap_units=300,
            qualification_levels=(B1, B2),
            discoveries=(),
            rounds=(),
            requests_issued=0,
        )


def test_damage_supported_requires_a_regret():
    with pytest.raises(ValueError):
        MoveAssessment(
            decision=decision(START_POSITION, "e2e4"),
            policy=make_policy(),
            status=AssessmentStatus.DAMAGE_SUPPORTED,
            reasons=(),
            unresolved_triggers=(),
            witness=MoveKey("d2d4"),
            regret=None,
            conservative_margin_units=290,
            final_gap_units=300,
            qualification_levels=(B1, B2),
            discoveries=(),
            rounds=(),
            requests_issued=0,
        )


def test_engine_regret_rejects_non_positive_and_oversized_values():
    for bad in (0, -1, 2001, True, 3.0):
        with pytest.raises(ValueError):
            EngineRegret(
                units=bad,
                witness=MoveKey("d2d4"),
                policy_id="p",
                policy_version="v1",
                policy_fingerprint="f",
                qualification_levels=(B1, B2),
            )


def test_engine_regret_keeps_gap_margin_and_accepted_loss_separate():
    _, assessment = run(BASELINE_SCRIPT)
    assert assessment.regret.units == 300  # S(A) = min(300, 310)
    assert assessment.conservative_margin_units == 290  # L(A) = S - epsilon
    assert assessment.final_gap_units == 310  # the B2 measured gap
    assert assessment.regret.expected_score_loss == 0.15


# --- 26 / 27. policy provenance and cache independence ----------------------


def test_policy_fingerprint_is_deterministic_and_threshold_sensitive():
    first = make_policy(tau_units=40)
    second = make_policy(tau_units=40)
    third = make_policy(tau_units=41)
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != third.fingerprint
    assert len(first.fingerprint) == 64


def test_regret_records_policy_provenance():
    policy = make_policy(policy_id="cde-damage", policy_version="v7")
    _, assessment = run(BASELINE_SCRIPT, policy=policy)
    assert assessment.regret.policy_id == "cde-damage"
    assert assessment.regret.policy_version == "v7"
    assert assessment.regret.policy_fingerprint == policy.fingerprint
    assert assessment.regret.qualification_levels == (B1, B2)
    assert assessment.semantics_version == ASSESSMENT_SEMANTICS_VERSION


def test_changing_tau_reuses_identical_primitive_engine_evidence():
    provider = ScriptedProvider(BASELINE_SCRIPT)
    dec = decision(START_POSITION, "e2e4")

    first = assess_decision(dec, provider, make_policy(tau_units=40))
    after_first = set(provider.unique_requests)

    second = assess_decision(dec, provider, make_policy(tau_units=1000))
    third = assess_decision(dec, provider, make_policy(epsilon_units=295))
    assert provider.unique_requests == after_first, (
        "changing tau/epsilon must not require any new primitive Stockfish request"
    )
    assert first.status is AssessmentStatus.DAMAGE_SUPPORTED
    assert second.status is AssessmentStatus.NO_DAMAGE_DEMONSTRATED
    assert third.status is AssessmentStatus.NO_DAMAGE_DEMONSTRATED


def test_policy_identity_is_not_part_of_engine_cache_identity():
    provider = ScriptedProvider(BASELINE_SCRIPT)
    request = provider.request_for(B1, START_POSITION, SearchMode.FORCED_MOVE, MoveKey("e2e4"))
    key = SemanticCacheKey.from_request(request)
    key_fields = set(SemanticCacheKey.__dataclass_fields__)
    for forbidden in (
        "policy_id",
        "policy_version",
        "policy_fingerprint",
        "tau_units",
        "epsilon_units",
        "calibration_id",
        "calibration_status",
    ):
        assert forbidden not in key_fields
    policy = make_policy()
    serialized = repr(key)
    assert policy.policy_id not in serialized
    assert policy.fingerprint not in serialized
    # The same primitive request under a different policy is the same key.
    assert key == SemanticCacheKey.from_request(
        provider.request_for(B1, START_POSITION, SearchMode.FORCED_MOVE, MoveKey("e2e4"))
    )


def test_uncalibrated_policy_cannot_produce_a_damaging_label():
    _, assessment = run(BASELINE_SCRIPT, policy=make_policy(calibrated=False))
    assert assessment.status is AssessmentStatus.INCONCLUSIVE
    assert assessment.reasons == (AssessmentReason.UNCALIBRATED_POLICY,)
    assert assessment.regret is None
    assert not assessment.is_engine_damage_admissible


def test_policy_validation_rejects_incoherent_configuration():
    with pytest.raises(ValueError):
        make_policy(b2=SearchLevel("B2", 1000))  # budgets must strictly increase
    with pytest.raises(ValueError):
        make_policy(epsilon_units=-1)
    with pytest.raises(ValueError):
        make_policy(tau_units=True)
    with pytest.raises(ValueError):
        make_policy(max_active_alternatives=3)
    with pytest.raises(ValueError):
        ComparisonPolicy(
            policy_id="p",
            policy_version="v1",
            b1=B1,
            b2=B2,
            epsilon_units=1,
            tau_units=1,
            max_gap_drift_units=1,
            max_alternative_drift_units=1,
            max_user_drift_units=1,
            material_negative_gap_units=1,
            calibration_status=CalibrationStatus.CALIBRATED,
            calibration_id=None,
        )
    with pytest.raises(ValueError):
        ComparisonPolicy(
            policy_id="p",
            policy_version="v1",
            b1=B1,
            b2=B2,
            epsilon_units=1,
            tau_units=1,
            max_gap_drift_units=1,
            max_alternative_drift_units=1,
            max_user_drift_units=1,
            material_negative_gap_units=1,
            calibration_status=CalibrationStatus.UNCALIBRATED,
            calibration_id="cal",
        )


def test_no_module_level_production_policy_exists():
    import chess_decision_explorer.assessment as module

    policies = [
        name
        for name, value in vars(module).items()
        if isinstance(value, ComparisonPolicy)
    ]
    assert policies == [], f"Step 4B must ship no pre-baked policy, found {policies}"


# --- 28. invalid evidence never becomes a harmless zero ---------------------


def test_incompatible_evidence_is_reported_not_zeroed():
    class MismatchedProvider(ScriptedProvider):
        def evaluate(self, request):
            evaluation = super().evaluate(request)
            if request.search_mode is SearchMode.FORCED_MOVE:
                # Evidence whose PV does not start with the forced root move.
                return make_evaluation(units=1300, pv=("h2h4",))
            return evaluation

    provider = MismatchedProvider(BASELINE_SCRIPT)
    assessment = assess_decision(
        decision(START_POSITION, "e2e4"), provider, make_policy()
    )
    assert assessment.status is AssessmentStatus.INVALID_EVIDENCE
    assert assessment.reasons == (AssessmentReason.INCOMPATIBLE_EVIDENCE,)
    assert assessment.regret is None
    assert assessment.final_gap_units is None
    assert assessment.detail


def test_round_rejects_unequal_requested_budgets():
    user = evidence(B1, "e2e4", units=1000)
    mismatched_request = EvaluationRequest(
        position=START_POSITION,
        search_mode=SearchMode.FORCED_MOVE,
        root_move=MoveKey("d2d4"),
        engine_identity=identity(),
        config=EngineAnalysisConfig(nodes=B1.nodes, threads=2),
    )
    alternative = MoveEvidence(
        level=B1,
        move=MoveKey("d2d4"),
        request=mismatched_request,
        evaluation=make_evaluation(units=1300, pv=("d2d4",)),
        immediate_terminal=ImmediateTerminal.NONE,
    )
    with pytest.raises(AssessmentEvidenceError):
        ComparisonRound(
            level=B1,
            alternative=MoveKey("d2d4"),
            user_evidence=user,
            alternative_evidence=alternative,
        )


def test_round_rejects_a_challenger_identical_to_the_observed_move():
    with pytest.raises(AssessmentEvidenceError):
        ComparisonRound(
            level=B1,
            alternative=MoveKey("e2e4"),
            user_evidence=evidence(B1, "e2e4", units=1000),
            alternative_evidence=evidence(B1, "e2e4", units=1300),
        )


def test_round_rejects_incompatible_engine_identity():
    user = evidence(B1, "e2e4", units=1000)
    other = EvaluationRequest(
        position=START_POSITION,
        search_mode=SearchMode.FORCED_MOVE,
        root_move=MoveKey("d2d4"),
        engine_identity=identity(sha="b" * 64),
        config=EngineAnalysisConfig(nodes=B1.nodes),
    )
    alternative = MoveEvidence(
        level=B1,
        move=MoveKey("d2d4"),
        request=other,
        evaluation=make_evaluation(units=1300, pv=("d2d4",)),
        immediate_terminal=ImmediateTerminal.NONE,
    )
    with pytest.raises(AssessmentEvidenceError):
        ComparisonRound(
            level=B1,
            alternative=MoveKey("d2d4"),
            user_evidence=user,
            alternative_evidence=alternative,
        )


# --- 29. canonical scope language -------------------------------------------


def test_scope_note_does_not_claim_exact_historical_damage():
    note = CANONICAL_DAMAGE_SCOPE_NOTE.lower()
    assert "canonical" in note
    assert "does not claim that every historical occurrence" in note
    assert "occurrence-level" in note
    _, assessment = run(BASELINE_SCRIPT)
    assert assessment.scope_note == CANONICAL_DAMAGE_SCOPE_NOTE


def test_assessment_carries_no_occurrence_or_cohort_dimension():
    fields = set(MoveAssessment.__dataclass_fields__)
    for forbidden in ("game_id", "ply_index", "outcome", "rating", "time_control"):
        assert forbidden not in fields


# --- Step 4C admission contract ---------------------------------------------


def test_admission_contract_accepts_a_fully_confirmed_damage_result():
    _, assessment = run(BASELINE_SCRIPT)
    assert assessment.admission_failures == ()
    assert assessment.is_engine_damage_admissible


@pytest.mark.parametrize(
    "script, policy_kwargs",
    [
        (BASELINE_SCRIPT, {"calibrated": False}),
        (
            {
                (1000, "*"): {"move": "d2d4", "units": 1500},
                (1000, "d2d4"): 1030,
                (1000, "e2e4"): 1000,
            },
            {},
        ),
        ({(1000, "*"): {"move": "e2e4", "units": 1500}}, {}),
    ],
)
def test_admission_contract_refuses_everything_but_damage_supported(
    script, policy_kwargs
):
    _, assessment = run(script, policy=make_policy(**policy_kwargs))
    assert not assessment.is_engine_damage_admissible
    assert assessment.admission_failures


def test_admission_contract_recomputes_the_contract_from_the_trace():
    # A hand-built "damage" result with no rounds and no discovery must not
    # be admissible even though its status says DAMAGE_SUPPORTED.
    regret = EngineRegret(
        units=300,
        witness=MoveKey("d2d4"),
        policy_id="p",
        policy_version="v1",
        policy_fingerprint="f",
        qualification_levels=(B1, B2),
    )
    forged = MoveAssessment(
        decision=decision(START_POSITION, "e2e4"),
        policy=make_policy(),
        status=AssessmentStatus.DAMAGE_SUPPORTED,
        reasons=(AssessmentReason.CONFIRMED_BY_FIXED_WITNESS,),
        unresolved_triggers=(),
        witness=MoveKey("d2d4"),
        regret=regret,
        conservative_margin_units=290,
        final_gap_units=300,
        qualification_levels=(B1, B2),
        discoveries=(),
        rounds=(),
        requests_issued=0,
    )
    failures = forged.admission_failures
    assert not forged.is_engine_damage_admissible
    assert any("discovery provenance" in failure for failure in failures)
    assert any("comparison round" in failure for failure in failures)


def test_recurrence_cannot_promote_an_inconclusive_decision():
    script = {
        (1000, "*"): {"move": "d2d4", "units": 1500},
        (1000, "d2d4"): 1900,
        (1000, "e2e4"): 1000,
        (4000, "*"): {"move": "d2d4", "units": 1500},
        (4000, "d2d4"): 1000,
        (4000, "e2e4"): 1900,
    }
    _, assessment = run(script, policy=make_policy(max_gap_drift_units=2000,
                                                   max_user_drift_units=2000,
                                                   max_alternative_drift_units=2000))
    assert assessment.status is AssessmentStatus.INCONCLUSIVE
    repeated = [assessment] * 50
    assert not any(one.is_engine_damage_admissible for one in repeated)


# --- SearchLevel / MoveEvidence basics --------------------------------------


def test_search_level_validation():
    with pytest.raises(ValueError):
        SearchLevel("", 1000)
    with pytest.raises(ValueError):
        SearchLevel("B1", 0)
    with pytest.raises(ValueError):
        SearchLevel("B1", True)


def test_move_evidence_rejects_a_budget_mismatch():
    request = EvaluationRequest(
        position=START_POSITION,
        search_mode=SearchMode.FORCED_MOVE,
        root_move=MoveKey("d2d4"),
        engine_identity=identity(),
        config=EngineAnalysisConfig(nodes=B2.nodes),
    )
    with pytest.raises(AssessmentEvidenceError):
        MoveEvidence(
            level=B1,
            move=MoveKey("d2d4"),
            request=request,
            evaluation=make_evaluation(pv=("d2d4",)),
            immediate_terminal=ImmediateTerminal.NONE,
        )


def test_centipawn_gap_is_a_diagnostic_not_a_metric():
    comparison = make_round(
        B1, "d2d4", "e2e4", 1300, 1000, alt_spec={"cp": 90}, user_spec={"cp": 20}
    )
    assert comparison.centipawn_gap == 70
    assert comparison.signed_gap_units == 300


# --- CachedEvaluatorPool ----------------------------------------------------


class FakeBoundEvaluator:
    def __init__(self, config, engine_identity=None):
        self._config = config
        self._identity = engine_identity or identity()

    @property
    def config(self):
        return self._config

    @property
    def identity(self):
        return self._identity

    def evaluate(self, position, search_mode, root_move=None):
        return make_evaluation(
            pv=(root_move.uci if root_move is not None else "e2e4",)
        )


def test_pool_builds_level_bound_requests():
    pool = CachedEvaluatorPool(
        {
            B1: FakeBoundEvaluator(EngineAnalysisConfig(nodes=B1.nodes)),
            B2: FakeBoundEvaluator(EngineAnalysisConfig(nodes=B2.nodes)),
        },
        InMemoryEvaluationCache(),
    )
    request = pool.request_for(B2, START_POSITION, SearchMode.FORCED_MOVE, MoveKey("d2d4"))
    assert request.config.nodes == B2.nodes
    assert request.engine_identity == pool.engine_identity
    assert pool.evaluate(request).pv[0] == MoveKey("d2d4")


def test_pool_rejects_a_level_budget_mismatch():
    with pytest.raises(ValueError):
        CachedEvaluatorPool(
            {B1: FakeBoundEvaluator(EngineAnalysisConfig(nodes=B2.nodes))},
            InMemoryEvaluationCache(),
        )


def test_pool_rejects_mixed_engine_identities():
    with pytest.raises(ValueError):
        CachedEvaluatorPool(
            {
                B1: FakeBoundEvaluator(EngineAnalysisConfig(nodes=B1.nodes)),
                B2: FakeBoundEvaluator(
                    EngineAnalysisConfig(nodes=B2.nodes),
                    engine_identity=identity(sha="c" * 64),
                ),
            },
            InMemoryEvaluationCache(),
        )


def test_pool_rejects_an_empty_binding_and_unknown_levels():
    with pytest.raises(ValueError):
        CachedEvaluatorPool({}, InMemoryEvaluationCache())
    pool = CachedEvaluatorPool(
        {B1: FakeBoundEvaluator(EngineAnalysisConfig(nodes=B1.nodes))},
        InMemoryEvaluationCache(),
    )
    with pytest.raises(ValueError):
        pool.request_for(B2, START_POSITION, SearchMode.UNRESTRICTED, None)


def test_pool_rejects_a_foreign_engine_identity_on_evaluate():
    pool = CachedEvaluatorPool(
        {B1: FakeBoundEvaluator(EngineAnalysisConfig(nodes=B1.nodes))},
        InMemoryEvaluationCache(),
    )
    foreign = EvaluationRequest(
        position=START_POSITION,
        search_mode=SearchMode.UNRESTRICTED,
        root_move=None,
        engine_identity=identity(sha="d" * 64),
        config=EngineAnalysisConfig(nodes=B1.nodes),
    )
    with pytest.raises(AssessmentEvidenceError):
        pool.evaluate(foreign)


def test_assessor_exposes_its_policy():
    policy = make_policy()
    assessor = DecisionAssessor(ScriptedProvider({}), policy)
    assert assessor.policy is policy


# --- adversarial Step 4C admission attacks ----------------------------------
#
# Every test here hand-builds a DAMAGE_SUPPORTED MoveAssessment whose summary
# fields claim a clean confirmation while the stored qualification rounds,
# provenance, or level pair say otherwise. None of them may be admissible.


def _failure_text(assessment):
    return " | ".join(assessment.admission_failures)


def test_forged_baseline_is_admissible_so_the_attacks_are_meaningful():
    forged = forge([(B1, 1300, 1000), (B2, 1320, 1010)])
    assert forged.admission_failures == ()
    assert forged.is_engine_damage_admissible


def test_forged_empty_unresolved_cannot_hide_excessive_gap_drift():
    forged = forge(
        [(B1, 1300, 1000), (B2, 1900, 1000)],
        regret_units=300,
        conservative_margin_units=290,
        final_gap_units=900,
        unresolved_triggers=(),
    )
    assert forged.unresolved_triggers == ()
    assert not forged.is_engine_damage_admissible
    assert AssessmentReason.GAP_DRIFT_EXCEEDED.value in _failure_text(forged)


def test_forged_empty_unresolved_cannot_hide_component_drift():
    forged = forge(
        [(B1, 1300, 1000), (B2, 1900, 1600)],
        policy=make_policy(max_gap_drift_units=1000),
    )
    assert forged.unresolved_triggers == ()
    assert not forged.is_engine_damage_admissible
    text = _failure_text(forged)
    assert AssessmentReason.ALTERNATIVE_DRIFT_EXCEEDED.value in text
    assert AssessmentReason.USER_DRIFT_EXCEEDED.value in text


def test_forged_empty_unresolved_cannot_hide_a_sign_reversal():
    forged = forge(
        [(B1, 1300, 1000), (B2, 1000, 1300)],
        policy=make_policy(
            max_gap_drift_units=1000,
            max_alternative_drift_units=1000,
            max_user_drift_units=1000,
        ),
        regret_units=300,
        conservative_margin_units=290,
        final_gap_units=-300,
    )
    assert not forged.is_engine_damage_admissible
    assert AssessmentReason.SIGN_REVERSAL.value in _failure_text(forged)


def test_forged_empty_unresolved_cannot_hide_a_mate_direction_reversal():
    rounds = (
        make_round(B1, "d2d4", "e2e4", 1300, 1000, alt_spec={"mate": 9}),
        make_round(B2, "d2d4", "e2e4", 1320, 1010, alt_spec={"mate": -9}),
    )
    forged = forge([(B1, 1300, 1000), (B2, 1320, 1010)], rounds=rounds)
    assert not forged.is_engine_damage_admissible
    assert AssessmentReason.MATE_DIRECTION_REVERSAL.value in _failure_text(forged)


@pytest.mark.parametrize(
    "override, expected",
    [
        ({"policy_id": "someone-elses-policy"}, "policy id"),
        ({"policy_version": "v999"}, "policy version"),
        ({"policy_fingerprint": "0" * 64}, "policy fingerprint"),
    ],
)
def test_forged_regret_policy_provenance_must_match(override, expected):
    forged = forge([(B1, 1300, 1000), (B2, 1320, 1010)], regret_overrides=override)
    assert not forged.is_engine_damage_admissible
    assert expected in _failure_text(forged)


def test_forged_regret_qualification_levels_must_match_the_assessment():
    forged = forge(
        [(B1, 1300, 1000), (B2, 1320, 1010)],
        regret_overrides={"qualification_levels": (B2, B3)},
    )
    assert not forged.is_engine_damage_admissible
    assert "different qualification levels" in _failure_text(forged)


def test_forged_final_gap_must_match_the_later_qualification_round():
    forged = forge([(B1, 1300, 1000), (B2, 1320, 1010)], final_gap_units=999)
    assert not forged.is_engine_damage_admissible
    assert "final_gap_units does not equal" in _failure_text(forged)


def test_forged_conservative_margin_must_match_the_re_derived_value():
    forged = forge([(B1, 1300, 1000), (B2, 1320, 1010)], conservative_margin_units=1999)
    assert not forged.is_engine_damage_admissible
    assert "conservative margin does not equal" in _failure_text(forged)


def test_forged_regret_units_must_equal_the_re_derived_conservative_loss():
    forged = forge([(B1, 1300, 1000), (B2, 1320, 1010)], regret_units=310)
    assert not forged.is_engine_damage_admissible
    assert "conservative accepted loss" in _failure_text(forged)


def test_forged_qualification_pair_cannot_skip_b2():
    forged = forge(
        [(B1, 1300, 1000), (B3, 1320, 1010)],
        policy=make_policy(b3=B3),
    )
    assert forged.qualification_levels == (B1, B3)
    assert not forged.is_engine_damage_admissible
    assert "is not the prescribed pair" in _failure_text(forged)


def test_forged_b3_trace_cannot_qualify_on_the_earlier_b1_b2_pair():
    # The trace shows B3 was entered; a DAMAGE_SUPPORTED conclusion must then
    # rest on (B2, B3), never on favourable earlier B1/B2 evidence.
    forged = forge(
        [(B1, 1900, 1000), (B2, 1300, 1000), (B3, 1000, 1300)],
        policy=make_policy(b3=B3, max_gap_drift_units=1000),
        qualification_levels=(B1, B2),
        regret_units=300,
        conservative_margin_units=290,
        final_gap_units=300,
    )
    assert not forged.is_engine_damage_admissible
    text = _failure_text(forged)
    assert "is not the prescribed pair" in text
    assert "B3 escalation was entered" in text


def test_forged_pair_without_a_b3_trace_must_be_b1_b2():
    # The trace shows no B3 escalation at all, so claiming a (B2, B3)
    # qualification is not the prescribed pair.
    forged = forge(
        [(B1, 1300, 1000), (B2, 1320, 1010)],
        policy=make_policy(b3=B3),
        qualification_levels=(B2, B3),
    )
    assert not forged.is_engine_damage_admissible
    text = _failure_text(forged)
    assert "is not the prescribed pair" in text
    assert "B3 escalation was not entered" in text


def test_forged_cross_level_engine_provenance_must_be_compatible():
    rounds = (
        make_round(B1, "d2d4", "e2e4", 1300, 1000),
        # Same position, same budget shape, but a different engine binary.
        make_round(
            B2, "d2d4", "e2e4", 1320, 1010, engine_identity=identity(sha="e" * 64)
        ),
    )
    forged = forge([(B1, 1300, 1000), (B2, 1320, 1010)], rounds=rounds)
    assert not forged.is_engine_damage_admissible
    text = _failure_text(forged)
    assert "engine/profile/semantics" in text
    assert "observed move" in text
    assert "witness" in text


def test_cross_level_compatibility_covers_every_step_4a_key_dimension():
    from chess_decision_explorer.assessment import _CROSS_LEVEL_INVARIANT_FIELDS

    key_fields = set(SemanticCacheKey.__dataclass_fields__)
    assert set(_CROSS_LEVEL_INVARIANT_FIELDS) == key_fields - {"nodes"}


def test_forged_witness_without_discovery_provenance_is_rejected():
    forged = forge([(B1, 1300, 1000), (B2, 1320, 1010)], discoveries=())
    assert not forged.is_engine_damage_admissible
    assert "discovery provenance" in _failure_text(forged)


def test_forged_rounds_for_a_different_witness_are_rejected():
    rounds = (
        make_round(B1, "g1f3", "e2e4", 1300, 1000),
        make_round(B2, "g1f3", "e2e4", 1320, 1010),
    )
    forged = forge([(B1, 1300, 1000), (B2, 1320, 1010)], rounds=rounds)
    assert not forged.is_engine_damage_admissible
    assert "comparison round" in _failure_text(forged)


# --- resolved-trigger provenance --------------------------------------------


def test_resolved_and_unresolved_triggers_must_be_disjoint():
    with pytest.raises(ValueError):
        MoveAssessment(
            decision=decision(START_POSITION, "e2e4"),
            policy=make_policy(),
            status=AssessmentStatus.INCONCLUSIVE,
            reasons=(AssessmentReason.GAP_DRIFT_EXCEEDED,),
            unresolved_triggers=(AssessmentReason.GAP_DRIFT_EXCEEDED,),
            resolved_triggers=(AssessmentReason.GAP_DRIFT_EXCEEDED,),
            witness=None,
            regret=None,
            conservative_margin_units=None,
            final_gap_units=None,
            qualification_levels=(),
            discoveries=(),
            rounds=(),
            requests_issued=0,
        )


def test_a_clean_two_level_confirmation_records_no_resolved_triggers():
    _, assessment = run(BASELINE_SCRIPT)
    assert assessment.status is AssessmentStatus.DAMAGE_SUPPORTED
    assert assessment.resolved_triggers == ()
    assert assessment.unresolved_triggers == ()


def test_partially_resolved_escalation_keeps_both_halves_of_the_history():
    # B1/B2 raises both component-drift contradictions. The B2/B3 pair
    # settles the observed move (no further user drift) but introduces a
    # sign reversal and gap drift, so part of the history is resolved and
    # part is still unresolved. Both halves must be visible.
    script = {
        (1000, "*"): {"move": "d2d4", "units": 1500},
        (1000, "d2d4"): 1300,
        (1000, "e2e4"): 1000,
        (4000, "*"): {"move": "d2d4", "units": 1500},
        (4000, "d2d4"): 1900,
        (4000, "e2e4"): 1600,
        (16000, "*"): {"move": "d2d4", "units": 1500},
        (16000, "d2d4"): 1300,
        (16000, "e2e4"): 1600,
    }
    _, assessment = run(
        script,
        policy=make_policy(b3=B3, max_gap_drift_units=200, max_alternative_drift_units=500,
                           max_user_drift_units=500),
    )
    assert assessment.status is AssessmentStatus.INCONCLUSIVE
    # Still unresolved at the final decision.
    assert AssessmentReason.GAP_DRIFT_EXCEEDED in assessment.unresolved_triggers
    assert AssessmentReason.SIGN_REVERSAL in assessment.unresolved_triggers
    # Raised by B1/B2 and no longer raised by B2/B3.
    assert AssessmentReason.USER_DRIFT_EXCEEDED in assessment.resolved_triggers
    assert not set(assessment.resolved_triggers) & set(assessment.unresolved_triggers)
    assert not assessment.is_engine_damage_admissible
