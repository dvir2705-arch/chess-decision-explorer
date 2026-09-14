"""Step 4C.0 pilot: strong fixed-witness reference trajectories.

Synthetic only: engine evidence is scripted, so the ordinary pytest suite
never needs a real Stockfish binary.
"""

from __future__ import annotations

import chess
import pytest

from chess_decision_explorer.domain import MoveKey, PositionKey
from chess_decision_explorer.engine import (
    EngineAnalysisConfig,
    EngineEvaluation,
    EngineIdentity,
    EvaluationRequest,
    SearchMode,
)
from chess_decision_explorer.assessment import (
    AssessmentEvidenceError,
    ComparisonRound,
    MoveEvidence,
    SearchLevel,
    immediate_terminal_after,
)
from chess_decision_explorer.experimental.c0.reference import (
    ReferenceLadderError,
    ReferenceLevelMeasurement,
    ReferenceTrajectory,
    Saturation,
    measure_reference_trajectory,
    saturation_of,
    unrestricted_discovery_audit,
    validate_reference_ladder,
)

_FAKE_SHA = "b" * 64

R1 = SearchLevel("R1", 100_000)
R2 = SearchLevel("R2", 400_000)
R3 = SearchLevel("R3", 1_600_000)

START = PositionKey.from_board(chess.Board())
OBSERVED = MoveKey("a2a3")
WITNESS = MoveKey("e2e4")
OTHER = MoveKey("d2d4")


def identity():
    return EngineIdentity(name="FakeFish 1", executable_sha256=_FAKE_SHA)


def units_wdl(units: int) -> tuple[int, int, int]:
    draws = units % 2
    wins = (units - draws) // 2
    return wins, draws, 1000 - wins - draws


def make_evaluation(units, *, cp=0, mate=None, pv=("e2e4",), depth=30, nodes=1):
    wins, draws, losses = units_wdl(units)
    return EngineEvaluation(
        centipawn=None if mate is not None else cp,
        mate=mate,
        wdl_wins=wins,
        wdl_draws=draws,
        wdl_losses=losses,
        depth=depth,
        seldepth=depth + 4,
        nodes=nodes,
        pv=tuple(MoveKey(uci) for uci in pv),
    )


class ScriptedProvider:
    """Fake `LeveledEvidenceProvider` driven by ``{(nodes, key): spec}``."""

    def __init__(self, script, *, threads=1, hash_mb=64):
        self.script = dict(script)
        self.threads = threads
        self.hash_mb = hash_mb
        self.requests: list[EvaluationRequest] = []

    def request_for(self, level, position, search_mode, root_move):
        return EvaluationRequest(
            position=position,
            search_mode=search_mode,
            root_move=root_move,
            engine_identity=identity(),
            config=EngineAnalysisConfig(
                nodes=level.nodes, threads=self.threads, hash_mb=self.hash_mb
            ),
        )

    def evaluate(self, request):
        self.requests.append(request)
        unrestricted = request.search_mode is SearchMode.UNRESTRICTED
        key = "*" if unrestricted else request.root_move.uci
        spec = self.script[(request.config.nodes, key)]
        if isinstance(spec, int):
            spec = {"units": spec}
        default_pv = (spec["move"],) if unrestricted else (key,)
        return make_evaluation(
            spec["units"],
            cp=spec.get("cp", 0),
            mate=spec.get("mate"),
            pv=spec.get("pv", default_pv),
            depth=spec.get("depth", 30),
            nodes=request.config.nodes,
        )


def evidence(level, move, units, *, cp=0, mate=None, position=START, board=None):
    board = board or chess.Board()
    request = EvaluationRequest(
        position=position,
        search_mode=SearchMode.FORCED_MOVE,
        root_move=move,
        engine_identity=identity(),
        config=EngineAnalysisConfig(nodes=level.nodes),
    )
    return MoveEvidence(
        level=level,
        move=move,
        request=request,
        evaluation=make_evaluation(units, cp=cp, mate=mate, pv=(move.uci,)),
        immediate_terminal=immediate_terminal_after(board, move),
    )


def measurement(level, *, witness_units, observed_units, witness=WITNESS,
                observed=OBSERVED, witness_mate=None, observed_mate=None,
                witness_cp=0, observed_cp=0):
    return ReferenceLevelMeasurement(
        level=level,
        round=ComparisonRound(
            level=level,
            alternative=witness,
            user_evidence=evidence(level, observed, observed_units, cp=observed_cp, mate=observed_mate),
            alternative_evidence=evidence(level, witness, witness_units, cp=witness_cp, mate=witness_mate),
        ),
        witness_elapsed_seconds=0.5,
        observed_elapsed_seconds=0.4,
    )


def trajectory(*measurements, witness=WITNESS, observed=OBSERVED):
    return ReferenceTrajectory(
        position=START,
        observed_move=observed,
        witness=witness,
        measurements=tuple(measurements),
    )


# --- ladder validation ------------------------------------------------------


def test_a_valid_ladder_is_returned_unchanged():
    assert validate_reference_ladder([R1, R2, R3], 60_000) == (R1, R2, R3)


def test_an_empty_ladder_is_rejected():
    with pytest.raises(ReferenceLadderError):
        validate_reference_ladder([], 60_000)


def test_an_unordered_ladder_is_rejected():
    with pytest.raises(ReferenceLadderError, match="strictly increasing"):
        validate_reference_ladder([R2, R1], 60_000)


def test_a_repeated_budget_is_rejected():
    with pytest.raises(ReferenceLadderError, match="strictly increasing"):
        validate_reference_ladder([R1, SearchLevel("R1b", R1.nodes)], 60_000)


def test_duplicate_labels_are_rejected():
    with pytest.raises(ReferenceLadderError, match="distinct"):
        validate_reference_ladder([R1, SearchLevel("R1", R2.nodes)], 60_000)


@pytest.mark.parametrize("audited", [100_000, 200_000, 5_000_000])
def test_a_reference_level_must_be_stronger_than_the_level_it_audits(audited):
    with pytest.raises(ReferenceLadderError, match="stronger"):
        validate_reference_ladder([R1, R2, R3], audited)


def test_a_ladder_only_just_stronger_than_the_audited_level_is_accepted():
    assert validate_reference_ladder([SearchLevel("R1", 60_001)], 60_000)


@pytest.mark.parametrize("audited", [0, -5, "60000", 1.5])
def test_the_audited_production_budget_must_be_a_positive_int(audited):
    with pytest.raises(ReferenceLadderError):
        validate_reference_ladder([R1], audited)


# --- signed arithmetic ------------------------------------------------------


def test_g_ref_is_the_signed_difference_of_utilities():
    step = measurement(R1, witness_units=1400, observed_units=900)
    assert step.witness_units == 1400
    assert step.observed_units == 900
    assert step.g_ref_units == 500


def test_negative_gaps_are_preserved_exactly():
    step = measurement(R1, witness_units=700, observed_units=1250)
    assert step.g_ref_units == -550
    traj = trajectory(step)
    assert traj.as_dict()["levels"][0]["g_ref_units"] == -550


def test_a_zero_gap_is_recorded_as_zero_not_as_missing():
    step = measurement(R1, witness_units=1000, observed_units=1000)
    assert step.g_ref_units == 0


def test_utility_is_two_wins_plus_draws():
    step = measurement(R1, witness_units=1234, observed_units=0)
    witness_evidence = step.round.alternative_evidence.evaluation
    assert 2 * witness_evidence.wdl_wins + witness_evidence.wdl_draws == 1234


def test_centipawn_diagnostics_are_recorded_and_kept_separate():
    step = measurement(R1, witness_units=1400, observed_units=900, witness_cp=120, observed_cp=-30)
    payload = step.as_dict()
    assert payload["centipawn_gap"] == 150
    assert payload["witness"]["centipawn"] == 120
    assert payload["observed"]["centipawn"] == -30
    assert payload["g_ref_units"] == 500


def test_a_mate_score_suppresses_the_centipawn_gap_but_keeps_mate_diagnostics():
    step = measurement(R1, witness_units=2000, observed_units=900, witness_mate=3)
    payload = step.as_dict()
    assert payload["centipawn_gap"] is None
    assert payload["witness"]["mate"] == 3
    assert payload["witness"]["mate_direction"] == 1


# --- same-root / same-witness construction ----------------------------------


def test_a_trajectory_refuses_measurements_with_different_witnesses():
    first = measurement(R1, witness_units=1400, observed_units=900)
    second = measurement(R2, witness_units=1300, observed_units=900, witness=OTHER)
    with pytest.raises(ValueError, match="same fixed witness"):
        trajectory(first, second)


def test_a_trajectory_refuses_measurements_of_a_different_observed_move():
    first = measurement(R1, witness_units=1400, observed_units=900)
    second = measurement(R2, witness_units=1300, observed_units=900, observed=OTHER)
    with pytest.raises(ValueError, match="same observed move"):
        trajectory(first, second)


def test_a_trajectory_refuses_out_of_order_levels():
    with pytest.raises(ValueError, match="strictly increasing"):
        trajectory(
            measurement(R2, witness_units=1300, observed_units=900),
            measurement(R1, witness_units=1400, observed_units=900),
        )


def test_a_trajectory_refuses_the_observed_move_as_its_own_witness():
    with pytest.raises(ValueError, match="must differ"):
        ReferenceTrajectory(
            position=START,
            observed_move=OBSERVED,
            witness=OBSERVED,
            measurements=(measurement(R1, witness_units=1, observed_units=1),),
        )


def test_a_trajectory_requires_at_least_one_measurement():
    with pytest.raises(ValueError, match="at least one"):
        ReferenceTrajectory(
            position=START, observed_move=OBSERVED, witness=WITNESS, measurements=()
        )


def test_a_round_refuses_two_different_canonical_roots():
    other_board = chess.Board()
    other_board.push_uci("g1f3")
    other_position = PositionKey.from_board(other_board)
    with pytest.raises(AssessmentEvidenceError):
        ComparisonRound(
            level=R1,
            alternative=WITNESS,
            user_evidence=evidence(R1, OBSERVED, 900),
            alternative_evidence=evidence(
                R1, MoveKey("e7e5"), 1400, position=other_position, board=other_board
            ),
        )


def test_measure_reference_trajectory_uses_one_root_and_one_witness():
    provider = ScriptedProvider(
        {
            (R1.nodes, WITNESS.uci): 1400,
            (R1.nodes, OBSERVED.uci): 900,
            (R2.nodes, WITNESS.uci): 1380,
            (R2.nodes, OBSERVED.uci): 950,
        }
    )
    traj = measure_reference_trajectory(
        provider, chess.Board(), START, OBSERVED, WITNESS, (R1, R2)
    )

    assert [m.g_ref_units for m in traj.measurements] == [500, 430]
    assert {m.witness for m in traj.measurements} == {WITNESS}
    assert {m.observed_move for m in traj.measurements} == {OBSERVED}
    assert all(request.position == START for request in provider.requests)
    assert all(
        request.search_mode is SearchMode.FORCED_MOVE for request in provider.requests
    )


def test_every_reference_level_evaluates_both_moves_independently():
    provider = ScriptedProvider(
        {
            (R1.nodes, WITNESS.uci): 1400,
            (R1.nodes, OBSERVED.uci): 900,
            (R2.nodes, WITNESS.uci): 1380,
            (R2.nodes, OBSERVED.uci): 950,
        }
    )
    measure_reference_trajectory(
        provider, chess.Board(), START, OBSERVED, WITNESS, (R1, R2)
    )
    issued = [(r.config.nodes, r.root_move.uci) for r in provider.requests]
    assert sorted(issued) == sorted(
        [
            (R1.nodes, WITNESS.uci),
            (R1.nodes, OBSERVED.uci),
            (R2.nodes, WITNESS.uci),
            (R2.nodes, OBSERVED.uci),
        ]
    )


def test_reference_levels_differ_only_in_node_budget():
    provider = ScriptedProvider(
        {
            (R1.nodes, WITNESS.uci): 1400,
            (R1.nodes, OBSERVED.uci): 900,
            (R2.nodes, WITNESS.uci): 1380,
            (R2.nodes, OBSERVED.uci): 950,
        },
        threads=1,
        hash_mb=64,
    )
    measure_reference_trajectory(
        provider, chess.Board(), START, OBSERVED, WITNESS, (R1, R2)
    )
    assert {r.config.threads for r in provider.requests} == {1}
    assert {r.config.hash_mb for r in provider.requests} == {64}


def test_measure_refuses_the_observed_move_as_the_witness():
    with pytest.raises(ValueError, match="must differ"):
        measure_reference_trajectory(
            ScriptedProvider({}), chess.Board(), START, OBSERVED, OBSERVED, (R1,)
        )


# --- saturation -------------------------------------------------------------


@pytest.mark.parametrize(
    "units,expected",
    [(2000, Saturation.MAX), (0, Saturation.MIN), (1999, Saturation.NONE), (1, Saturation.NONE)],
)
def test_saturation_of_tags_scale_endpoints(units, expected):
    assert saturation_of(units) is expected


@pytest.mark.parametrize(
    "witness_units,observed_units,expected",
    [
        (2000, 900, "witness"),
        (900, 0, "observed"),
        (2000, 0, "both"),
        (1400, 900, "none"),
    ],
)
def test_level_saturation_status(witness_units, observed_units, expected):
    step = measurement(R1, witness_units=witness_units, observed_units=observed_units)
    assert step.saturation_status == expected
    assert step.as_dict()["saturation_status"] == expected


def test_saturated_levels_are_counted_in_diagnostics():
    traj = trajectory(
        measurement(R1, witness_units=1400, observed_units=900),
        measurement(R2, witness_units=2000, observed_units=900),
    )
    assert traj.diagnostics().saturated_level_count == 1


def test_evidence_records_extreme_wdl_components():
    step = measurement(R1, witness_units=2000, observed_units=900)
    assert step.as_dict()["witness"]["wdl_extreme"] is True
    assert step.as_dict()["observed"]["wdl_extreme"] is False


# --- convergence diagnostics ------------------------------------------------


def test_consecutive_deltas_and_component_drift():
    traj = trajectory(
        measurement(R1, witness_units=1400, observed_units=900),
        measurement(R2, witness_units=1350, observed_units=1000),
    )
    step = traj.diagnostics().steps[0]
    assert step.from_label == "R1" and step.to_label == "R2"
    assert step.delta_g_units == 350 - 500
    assert step.witness_drift_units == -50
    assert step.observed_drift_units == 100


def test_sign_change_is_detected_across_zero():
    traj = trajectory(
        measurement(R1, witness_units=1400, observed_units=900),
        measurement(R2, witness_units=800, observed_units=1000),
    )
    diagnostics = traj.diagnostics()
    assert diagnostics.steps[0].sign_change is True
    assert diagnostics.sign_change_count == 1


def test_a_gap_touching_zero_is_not_counted_as_a_sign_change():
    traj = trajectory(
        measurement(R1, witness_units=1400, observed_units=900),
        measurement(R2, witness_units=1000, observed_units=1000),
    )
    assert traj.diagnostics().steps[0].sign_change is False


def test_mate_direction_reversal_and_appearance_are_distinguished():
    reversal = trajectory(
        measurement(R1, witness_units=1900, observed_units=900, witness_mate=4),
        measurement(R2, witness_units=100, observed_units=900, witness_mate=-4),
    )
    appearance = trajectory(
        measurement(R1, witness_units=1400, observed_units=900),
        measurement(R2, witness_units=2000, observed_units=900, witness_mate=3),
    )
    assert reversal.diagnostics().steps[0].witness_mate_direction_change is True
    assert reversal.diagnostics().mate_direction_change_count == 1
    assert appearance.diagnostics().steps[0].witness_mate_direction_change is False
    assert appearance.diagnostics().steps[0].witness_mate_appeared is True


@pytest.mark.parametrize(
    "gaps,expected",
    [
        ([500], "single_level"),
        ([300, 400, 500], "non_decreasing"),
        ([500, 400, 300], "non_increasing"),
        ([300, 500, 400], "non_monotonic"),
        ([400, 400, 400], "non_decreasing"),
    ],
)
def test_monotonicity_is_classified_without_a_verdict(gaps, expected):
    levels = (R1, R2, R3)[: len(gaps)]
    traj = trajectory(
        *[
            measurement(level, witness_units=900 + gap, observed_units=900)
            for level, gap in zip(levels, gaps)
        ]
    )
    assert traj.diagnostics().monotonicity == expected


def test_max_absolute_delta_is_reported():
    traj = trajectory(
        measurement(R1, witness_units=1400, observed_units=900),
        measurement(R2, witness_units=1000, observed_units=900),
        measurement(R3, witness_units=1050, observed_units=900),
    )
    assert traj.diagnostics().max_abs_delta_g_units == 400


def test_diagnostics_carry_no_convergence_verdict():
    traj = trajectory(measurement(R1, witness_units=1400, observed_units=900))
    payload = traj.diagnostics().as_dict()
    assert "converged" not in payload
    assert "C0 defines no convergence threshold" in payload["note"]


def test_trajectory_dict_records_every_level_and_no_r_value():
    traj = trajectory(
        measurement(R1, witness_units=1400, observed_units=900),
        measurement(R2, witness_units=1350, observed_units=920),
    )
    payload = traj.as_dict()
    assert len(payload["levels"]) == 2
    assert payload["witness"] == WITNESS.uci
    assert payload["observed_move"] == OBSERVED.uci
    assert "g_reference" not in payload
    assert "r_units" not in payload


def test_semantic_cache_keys_are_recorded_for_both_moves():
    step = measurement(R1, witness_units=1400, observed_units=900)
    payload = step.as_dict()
    for side in ("witness", "observed"):
        key = payload[side]["semantic_cache_key"]
        assert key["position_epd"] == START.epd
        assert key["search_mode"] == "forced_move"
        assert key["nodes"] == R1.nodes
        assert key["evidence_contract_version"] == "ENGINE_EVIDENCE_V1"
        assert key["position_semantics_version"] == "CANONICAL_POSITION_V1"


# --- the isolated experimental discovery audit ------------------------------


def test_discovery_audit_is_unrestricted_single_pv_and_isolated():
    provider = ScriptedProvider({(R3.nodes, "*"): {"units": 1500, "move": "d2d4"}})
    audit = unrestricted_discovery_audit(provider, START, R3)

    assert audit["experimental"] is True
    assert audit["isolated_from_reference_measurement"] is True
    assert audit["multipv"] is False
    assert audit["selected_move"] == "d2d4"
    assert provider.requests[0].search_mode is SearchMode.UNRESTRICTED
    assert "g_ref_units" not in audit
