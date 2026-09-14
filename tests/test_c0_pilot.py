"""Step 4C.0 pilot: root measurement, manifest, summary, and isolation.

Synthetic only -- the real Step 4B code path runs against scripted engine
evidence, so the ordinary pytest suite never needs a Stockfish binary.
"""

from __future__ import annotations

import json
from pathlib import Path

import chess
import pytest

from chess_decision_explorer.domain import DecisionKey, MoveKey, PositionKey
from chess_decision_explorer.engine import (
    EngineAnalysisConfig,
    EngineEvaluation,
    EngineIdentity,
    EvaluationRequest,
    SearchMode,
)
from chess_decision_explorer.assessment import (
    AssessmentStatus,
    CalibrationStatus,
    ComparisonPolicy,
    SearchLevel,
)
from chess_decision_explorer.experimental.c0 import (
    C0_EXPERIMENTAL_CALIBRATION_ID,
    C0_SCOPE_NOTE,
)
from chess_decision_explorer.assessment import _compare_levels
from chess_decision_explorer.experimental.c0.lowcost import (
    WitnessSource,
    prescribed_qualification_pair,
    resolve_fixed_witness,
    withheld_only_for_calibration_scope,
)
from chess_decision_explorer.experimental.c0.manifest import (
    REQUIRED_MANIFEST_FIELDS,
    RunManifest,
)
from chess_decision_explorer.experimental.c0.pilot import (
    CountingEvaluator,
    ReplayMiss,
    measure_root,
    run_pilot,
)
from chess_decision_explorer.experimental.c0.report import (
    REFERENCE_LEVEL_COLUMNS,
    ROOT_SUMMARY_COLUMNS,
    reference_level_rows,
    render_markdown,
    root_summary_row,
    summarize,
)
from chess_decision_explorer.experimental.c0.sampling import (
    GAME_IDENTITY_SCHEME,
    SAMPLING_REPRODUCIBILITY_GUARANTEE,
    SampledDecision,
)

_FAKE_SHA = "c" * 64

B1 = SearchLevel("B1", 1_000)
B2 = SearchLevel("B2", 4_000)
B3 = SearchLevel("B3", 16_000)
R1 = SearchLevel("R1", 64_000)
R2 = SearchLevel("R2", 256_000)
AUDIT = SearchLevel("DISCOVERY_AUDIT", 512_000)

START = PositionKey.from_board(chess.Board())
OBSERVED = MoveKey("a2a3")
WITNESS = MoveKey("e2e4")


def identity():
    return EngineIdentity(name="FakeFish 1", executable_sha256=_FAKE_SHA)


def units_wdl(units: int) -> tuple[int, int, int]:
    draws = units % 2
    wins = (units - draws) // 2
    return wins, draws, 1000 - wins - draws


class ScriptedProvider:
    def __init__(self, script):
        self.script = dict(script)
        self.requests: list[EvaluationRequest] = []

    def request_for(self, level, position, search_mode, root_move):
        return EvaluationRequest(
            position=position,
            search_mode=search_mode,
            root_move=root_move,
            engine_identity=identity(),
            config=EngineAnalysisConfig(nodes=level.nodes),
        )

    def evaluate(self, request):
        self.requests.append(request)
        unrestricted = request.search_mode is SearchMode.UNRESTRICTED
        key = "*" if unrestricted else request.root_move.uci
        spec = self.script[(request.config.nodes, key)]
        if isinstance(spec, int):
            spec = {"units": spec}
        pv = spec.get("pv", (spec["move"],) if unrestricted else (key,))
        wins, draws, losses = units_wdl(spec["units"])
        return EngineEvaluation(
            centipawn=None if spec.get("mate") is not None else spec.get("cp", 0),
            mate=spec.get("mate"),
            wdl_wins=wins,
            wdl_draws=draws,
            wdl_losses=losses,
            depth=spec.get("depth", 25),
            seldepth=spec.get("depth", 25) + 3,
            nodes=request.config.nodes,
            pv=tuple(MoveKey(uci) for uci in pv),
        )


def damaging_script(*, b3=False, audit=False):
    script = {
        (B1.nodes, "*"): {"units": 1400, "move": WITNESS.uci},
        (B1.nodes, WITNESS.uci): 1400,
        (B1.nodes, OBSERVED.uci): 900,
        (B2.nodes, "*"): {"units": 1380, "move": WITNESS.uci},
        (B2.nodes, WITNESS.uci): 1380,
        (B2.nodes, OBSERVED.uci): 900,
        (R1.nodes, WITNESS.uci): 1300,
        (R1.nodes, OBSERVED.uci): 950,
        (R2.nodes, WITNESS.uci): 1280,
        (R2.nodes, OBSERVED.uci): 980,
    }
    if b3:
        script.update(
            {
                (B3.nodes, "*"): {"units": 1370, "move": WITNESS.uci},
                (B3.nodes, WITNESS.uci): 1370,
                (B3.nodes, OBSERVED.uci): 900,
            }
        )
    if audit:
        script[(AUDIT.nodes, "*")] = {"units": 1290, "move": WITNESS.uci}
    return script


def make_policy(*, calibrated=True, b3=None, tau_units=40):
    return ComparisonPolicy(
        policy_id="c0-test",
        policy_version="v0",
        b1=B1,
        b2=B2,
        b3=b3,
        epsilon_units=10,
        tau_units=tau_units,
        max_gap_drift_units=200,
        max_alternative_drift_units=200,
        max_user_drift_units=200,
        material_negative_gap_units=150,
        calibration_status=(
            CalibrationStatus.CALIBRATED if calibrated else CalibrationStatus.UNCALIBRATED
        ),
        calibration_id=C0_EXPERIMENTAL_CALIBRATION_ID if calibrated else None,
    )


def make_sample(*, observed=OBSERVED, ordinal=0, ply_index=0):
    return SampledDecision(
        source_id="test-source",
        source_ordinal=ordinal,
        game_id=f"test-source:{ordinal}",
        decision=DecisionKey(START, observed),
        ply_index=ply_index,
        actor_color="white",
        actor_rating=1600,
        opponent_rating=1610,
        white_rating=1600,
        black_rating=1610,
        result="1-0",
        outcome_for_actor="win",
        time_control="300+0",
        time_control_category="blitz",
        event="Rated Blitz game",
        termination="Normal",
        date="2026.01.01",
        game_ply_count=40,
        eligible_root_count=38,
    )


def strip_timings(node):
    if isinstance(node, dict):
        node.pop("elapsed_seconds", None)
        node.pop("total_elapsed_seconds", None)
        node.pop("cost", None)
        for value in node.values():
            strip_timings(value)
    elif isinstance(node, list):
        for value in node:
            strip_timings(value)
    return node


# --- the low-cost trajectory ------------------------------------------------


def test_a_damaging_root_produces_s_and_a_reference_trajectory():
    record = measure_root(
        make_sample(), ScriptedProvider(damaging_script()), make_policy(), (R1, R2)
    )

    assert record.assessment.status is AssessmentStatus.DAMAGE_SUPPORTED
    assert record.witness.witness == WITNESS
    assert record.witness.source == WitnessSource.ENGINE_REGRET
    # S = min(G_B1, G_B2) = min(500, 480)
    assert record.witness.s_units == 480
    assert record.trajectory is not None
    assert [m.g_ref_units for m in record.trajectory.measurements] == [350, 300]


def test_s_comes_from_the_step_4b_engine_regret_not_a_reimplementation():
    record = measure_root(
        make_sample(), ScriptedProvider(damaging_script()), make_policy(), (R1, R2)
    )
    assert record.witness.s_units == record.assessment.regret.units
    assert record.assessment.regret.witness == record.witness.witness


def test_the_step_4b_trace_is_preserved_in_order():
    record = measure_root(
        make_sample(), ScriptedProvider(damaging_script()), make_policy(), (R1, R2)
    )
    payload = record.as_dict()["assessment"]

    assert [d["order_index"] for d in payload["discoveries"]] == [0, 1]
    assert [d["requested_nodes"] for d in payload["discoveries"]] == [B1.nodes, B2.nodes]
    assert [r["requested_nodes"] for r in payload["rounds"]] == [B1.nodes, B2.nodes]
    assert payload["rounds"][0]["signed_gap_units"] == 500
    assert payload["rounds"][1]["signed_gap_units"] == 480


def test_primitive_evidence_provenance_is_preserved_for_every_round():
    record = measure_root(
        make_sample(), ScriptedProvider(damaging_script()), make_policy(), (R1, R2)
    )
    payload = record.as_dict()["assessment"]
    for comparison in payload["rounds"]:
        for side in ("observed_evidence", "alternative_evidence"):
            evidence = comparison[side]
            assert evidence["semantic_cache_key"]["position_epd"] == START.epd
            assert evidence["semantic_cache_key"]["search_mode"] == "forced_move"
            assert evidence["evidence_depth"] > 0
            assert evidence["evidence_nodes"] > 0
            assert evidence["pv"][0] == evidence["move"]


def test_status_reasons_and_triggers_are_preserved():
    record = measure_root(
        make_sample(), ScriptedProvider(damaging_script()), make_policy(), (R1, R2)
    )
    payload = record.as_dict()["assessment"]
    assert payload["status"] == "damage_supported"
    assert payload["reasons"] == ["confirmed_by_fixed_witness"]
    assert payload["unresolved_triggers"] == []
    assert payload["resolved_triggers"] == []
    assert payload["is_engine_damage_admissible"] is True
    assert payload["admission_failures"] == []


def test_a_root_without_a_qualifying_witness_keeps_its_reason_and_fabricates_nothing():
    script = {
        (B1.nodes, "*"): {"units": 900, "move": OBSERVED.uci},
        (B1.nodes, OBSERVED.uci): 900,
    }
    record = measure_root(
        make_sample(), ScriptedProvider(script), make_policy(), (R1, R2)
    )

    assert record.assessment.status is AssessmentStatus.NO_DAMAGE_DEMONSTRATED
    assert record.witness.has_target is False
    assert record.witness.s_units is None
    assert record.trajectory is None
    assert "discovery_selected_user_move" in record.witness.no_r_target_reason
    assert record.as_dict()["r_target_available"] is False


def test_a_screened_out_root_records_the_below_tau_reason():
    script = {
        (B1.nodes, "*"): {"units": 905, "move": WITNESS.uci},
        (B1.nodes, WITNESS.uci): 905,
        (B1.nodes, OBSERVED.uci): 900,
    }
    record = measure_root(
        make_sample(), ScriptedProvider(script), make_policy(), (R1, R2)
    )
    assert record.witness.has_target is False
    assert "margin_below_tau" in record.witness.no_r_target_reason
    assert record.trajectory is None


def test_an_invalid_root_is_recorded_and_never_measured():
    checkmated = chess.Board("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3")
    sample = SampledDecision(
        **{
            **{
                field: getattr(make_sample(), field)
                for field in SampledDecision.__dataclass_fields__
            },
            "decision": DecisionKey(PositionKey.from_board(checkmated), MoveKey("e1f2")),
        }
    )
    record = measure_root(sample, ScriptedProvider({}), make_policy(), (R1, R2))

    assert record.assessment is None
    assert "RequestValidationError" in record.assessment_error
    assert record.trajectory is None
    assert record.witness.no_r_target_reason.startswith("root_rejected:")


def test_no_r_value_is_ever_computed():
    record = measure_root(
        make_sample(), ScriptedProvider(damaging_script()), make_policy(), (R1, R2)
    )
    payload = json.dumps(record.as_dict())
    assert '"r_units"' not in payload
    assert '"g_reference"' not in payload
    assert "R = S - G_reference is deliberately NOT computed" in payload


# --- witness resolution under an uncalibrated policy ------------------------


def test_an_uncalibrated_policy_still_yields_a_witness_re_derived_from_the_trace():
    record = measure_root(
        make_sample(),
        ScriptedProvider(damaging_script()),
        make_policy(calibrated=False),
        (R1, R2),
    )

    assert record.assessment.status is AssessmentStatus.INCONCLUSIVE
    assert record.assessment.witness is None
    assert record.witness.witness == WITNESS
    assert record.witness.source == WitnessSource.REDERIVED_FROM_TRACE
    assert record.witness.s_units == 480
    assert record.trajectory is not None


def test_the_re_derived_s_matches_the_engine_regret_of_the_same_measurement():
    calibrated = measure_root(
        make_sample(), ScriptedProvider(damaging_script()), make_policy(), (R1, R2)
    )
    uncalibrated = measure_root(
        make_sample(),
        ScriptedProvider(damaging_script()),
        make_policy(calibrated=False),
        (R1, R2),
    )
    assert calibrated.witness.s_units == uncalibrated.witness.s_units
    assert calibrated.witness.witness == uncalibrated.witness.witness


def test_the_prescribed_qualification_pair_is_read_off_the_trace():
    without_b3 = measure_root(
        make_sample(), ScriptedProvider(damaging_script()), make_policy(b3=B3), (R1, R2)
    )
    assert prescribed_qualification_pair(without_b3.assessment) == (B1, B2)


def test_resolve_fixed_witness_reports_no_target_without_rounds():
    script = {
        (B1.nodes, "*"): {"units": 900, "move": OBSERVED.uci},
        (B1.nodes, OBSERVED.uci): 900,
    }
    record = measure_root(
        make_sample(), ScriptedProvider(script), make_policy(calibrated=False), (R1, R2)
    )
    resolution = resolve_fixed_witness(record.assessment)
    assert resolution.has_target is False
    assert resolution.source == WitnessSource.NONE
    assert resolution.qualification_levels == ()


# --- reproducibility --------------------------------------------------------


def test_the_same_stored_evidence_reproduces_the_same_derived_measurements():
    first = measure_root(
        make_sample(), ScriptedProvider(damaging_script()), make_policy(), (R1, R2)
    )
    second = measure_root(
        make_sample(), ScriptedProvider(damaging_script()), make_policy(), (R1, R2)
    )
    assert strip_timings(first.as_dict()) == strip_timings(second.as_dict())


def test_replay_only_refuses_to_run_an_engine_analysis():
    class Inner:
        config = EngineAnalysisConfig(nodes=1000)
        identity = identity()

        def evaluate(self, position, search_mode, root_move=None):  # pragma: no cover
            raise AssertionError("the engine must not be reached in replay-only mode")

    counting = CountingEvaluator(Inner(), replay_only=True)
    with pytest.raises(ReplayMiss):
        counting.evaluate(START, SearchMode.UNRESTRICTED, None)
    assert counting.analyses == 0


def test_counting_evaluator_counts_real_analyses():
    class Inner:
        config = EngineAnalysisConfig(nodes=1000)
        identity = identity()

        def evaluate(self, position, search_mode, root_move=None):
            return "evaluation"

    counting = CountingEvaluator(Inner())
    counting.evaluate(START, SearchMode.UNRESTRICTED, None)
    counting.evaluate(START, SearchMode.UNRESTRICTED, None)
    assert counting.analyses == 2


def test_cost_accounting_separates_assessment_from_reference_requests():
    record = measure_root(
        make_sample(), ScriptedProvider(damaging_script()), make_policy(), (R1, R2)
    )
    assert record.assessment_requests == record.assessment.requests_issued
    assert record.reference_requests == 4


# --- the isolated discovery audit -------------------------------------------


def test_the_discovery_audit_is_optional_and_isolated():
    without = measure_root(
        make_sample(), ScriptedProvider(damaging_script()), make_policy(), (R1, R2)
    )
    assert without.discovery_audit is None

    with_audit = measure_root(
        make_sample(),
        ScriptedProvider(damaging_script(audit=True)),
        make_policy(),
        (R1, R2),
        discovery_audit_level=AUDIT,
    )
    assert with_audit.discovery_audit["experimental"] is True
    assert with_audit.discovery_audit["selected_move"] == WITNESS.uci
    assert [m.g_ref_units for m in with_audit.trajectory.measurements] == [350, 300]


# --- streaming pilot --------------------------------------------------------


def test_run_pilot_yields_one_record_per_sample_lazily():
    samples = [make_sample(ordinal=index) for index in range(3)]
    records = run_pilot(
        iter(samples),
        ScriptedProvider(damaging_script()),
        make_policy(),
        (R1, R2),
    )
    first = next(records)
    assert first.sample.source_ordinal == 0
    assert len(list(records)) == 2


# --- manifest ---------------------------------------------------------------


def make_manifest(**overrides):
    defaults = dict(
        run_id="c0-test-run",
        code_commit="deadbeef",
        code_tree_dirty=False,
        source_identifier="stdin",
        sampling_source_id="lichess-2024-01",
        source_sampling_declaration={"scanned_prefix_declared": True},
        seed=1234,
        eligibility_filters={"min_plies": 20},
        sample_target=10,
        engine_identity=EngineIdentity(
            name="Stockfish 19", executable_sha256=_FAKE_SHA, eval_file="nn-x.nnue"
        ),
        threads=1,
        hash_mb=64,
        policy=make_policy(b3=B3),
        reference_ladder=(R1, R2),
        audited_production_nodes=B3.nodes,
        discovery_audit_level=None,
        replay_only=False,
    )
    defaults.update(overrides)
    return RunManifest(**defaults)


def test_the_manifest_carries_every_required_reproducibility_field():
    payload = make_manifest().as_dict()
    missing = [field for field in REQUIRED_MANIFEST_FIELDS if field not in payload]
    assert missing == []


def test_the_manifest_records_the_exact_sampling_source_id_and_scheme():
    """`source_id` is an INPUT to every deterministic draw, so the manifest
    carries it as its own field along with the identity scheme and the real
    (not overstated) reproducibility guarantee."""
    payload = make_manifest().as_dict()
    assert payload["sampling_source_id"] == "lichess-2024-01"
    assert payload["game_identity_scheme"] == GAME_IDENTITY_SCHEME
    assert (
        payload["sampling_reproducibility_guarantee"]
        == SAMPLING_REPRODUCIBILITY_GUARANTEE
    )
    assert "zero_based_source_ordinal" in payload["game_identity_scheme"]


def test_the_manifest_records_engine_and_semantics_provenance():
    payload = make_manifest().as_dict()
    assert payload["engine_executable_sha256"] == _FAKE_SHA
    assert payload["engine_uci_name"] == "Stockfish 19"
    assert payload["engine_eval_file"] == "nn-x.nnue"
    assert payload["position_semantics_version"] == "CANONICAL_POSITION_V1"
    assert payload["evidence_contract_version"] == "ENGINE_EVIDENCE_V1"
    assert payload["assessment_semantics_version"] == "CANONICAL_DECISION_DAMAGE_V1"
    assert len(payload["analysis_profile_fingerprint"]) == 64
    assert payload["threads"] == 1
    assert payload["hash_mb"] == 64


def test_the_manifest_records_budgets_and_the_reference_ladder():
    payload = make_manifest().as_dict()
    assert payload["production_budgets"] == {
        "b1_nodes": B1.nodes,
        "b2_nodes": B2.nodes,
        "b3_nodes": B3.nodes,
    }
    assert payload["reference_ladder"] == [
        {"label": "R1", "nodes": R1.nodes},
        {"label": "R2", "nodes": R2.nodes},
    ]
    assert payload["audited_production_nodes"] == B3.nodes
    assert payload["production_policy"]["fingerprint"] == make_policy(b3=B3).fingerprint


def test_the_manifest_never_claims_calibration():
    payload = make_manifest().as_dict()
    assert payload["calibrated"] is False
    assert payload["ml_trained"] is False
    assert payload["personal_games_used"] is False
    assert payload["scope_note"] == C0_SCOPE_NOTE


def test_the_manifest_records_the_step_4b_calibration_scope_structurally():
    """The manifest reads calibration scope off the policy the run actually
    used, so it cannot claim one thing and run another."""
    uncalibrated = make_manifest(policy=make_policy(calibrated=False, b3=B3)).as_dict()
    assert uncalibrated["step_4b_calibration_status"] == "uncalibrated"
    assert uncalibrated["step_4b_calibration_id"] is None
    assert uncalibrated["damage_conclusions_permitted"] is False

    calibrated = make_manifest(policy=make_policy(b3=B3)).as_dict()
    assert calibrated["step_4b_calibration_status"] == "calibrated"
    assert calibrated["damage_conclusions_permitted"] is True


def test_the_manifest_round_trips_through_json(tmp_path: Path):
    manifest = make_manifest()
    manifest.finished_at = "2026-09-14T00:00:00+00:00"
    path = tmp_path / "manifest.json"
    manifest.write(path)
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert reloaded == manifest.as_dict()
    assert reloaded["started_at"]
    assert reloaded["finished_at"] == "2026-09-14T00:00:00+00:00"


# --- summary and report -----------------------------------------------------


def records_for_summary():
    provider = ScriptedProvider(damaging_script())
    damaging = measure_root(make_sample(), provider, make_policy(), (R1, R2)).as_dict()
    no_target = measure_root(
        make_sample(ordinal=1),
        ScriptedProvider(
            {
                (B1.nodes, "*"): {"units": 900, "move": OBSERVED.uci},
                (B1.nodes, OBSERVED.uci): 900,
            }
        ),
        make_policy(),
        (R1, R2),
    ).as_dict()
    return [damaging, no_target]


def test_summary_counts_targets_statuses_and_reasons():
    summary = summarize(records_for_summary())
    assert summary["roots_measured"] == 2
    assert summary["roots_with_usable_fixed_witness_and_s"] == 1
    assert summary["roots_with_reference_trajectory"] == 1
    assert summary["roots_lacking_r_target"] == 1
    assert summary["status_distribution"]["damage_supported"] == 1
    assert summary["status_distribution"]["no_damage_demonstrated"] == 1
    assert summary["invalid_evidence_count"] == 0
    assert summary["engine_damage_admissible_count"] == 1
    assert sum(summary["no_r_target_reasons"].values()) == 1


def test_summary_reports_drift_saturation_and_cost():
    summary = summarize(records_for_summary())
    assert summary["consecutive_reference_gap_drift_units"]["count"] == 1
    assert summary["consecutive_reference_gap_drift_units"]["min"] == -50
    assert summary["reference_levels_measured"] == 2
    assert summary["reference_saturation_status"]["none"] == 2
    assert summary["cost"]["per_reference_level"]["R1"]["levels_measured"] == 1
    assert summary["cost"]["reference_requests"] == 4


def test_summary_preserves_negative_reference_gaps():
    provider = ScriptedProvider(
        {
            **damaging_script(),
            (R2.nodes, WITNESS.uci): 800,
            (R2.nodes, OBSERVED.uci): 980,
        }
    )
    record = measure_root(make_sample(), provider, make_policy(), (R1, R2)).as_dict()
    summary = summarize([record])
    assert summary["reference_levels_with_negative_g"] == 1
    assert summary["final_reference_g_units"]["min"] == -180
    assert summary["reference_sign_change_steps"] == 1


def test_summary_row_and_reference_rows_match_their_columns():
    records = records_for_summary()
    row = root_summary_row(records[0])
    assert set(row) == set(ROOT_SUMMARY_COLUMNS)
    assert row["s_units"] == 480
    assert row["g_ref_first"] == 350
    assert row["g_ref_last"] == 300

    level_rows = reference_level_rows(records[0])
    assert len(level_rows) == 2
    assert set(level_rows[0]) == set(REFERENCE_LEVEL_COLUMNS)
    assert reference_level_rows(records[1]) == []


def test_the_report_states_every_required_disclaimer():
    manifest = make_manifest().as_dict()
    manifest["finished_at"] = "2026-09-14T00:00:00+00:00"
    manifest["sampling_counters"] = {
        "records_scanned": 2,
        "games_accepted_by_filters": 2,
        "decisions_sampled": 2,
        "candidate_roots_examined": 80,
        "candidate_roots_eligible": 76,
        "game_rejections": {"not_rated": 3},
        "root_rejections": {"one_legal_move": 4},
    }
    manifest["runtime"] = {"engine_analyses": 10, "cache_hits": 2}
    report = render_markdown(
        manifest, summarize(records_for_summary()), manifest["sampling_counters"]
    )

    assert "**NO epsilon was calibrated.**" in report
    assert "**NO tau was calibrated.**" in report
    assert "**NO ML model was trained.**" in report
    assert "**NO production reliability claim is made.**" in report
    assert "**NO personal games were used for fitting.**" in report
    assert "declared prefix/window" in report
    assert "complete Lichess population" in report
    assert "no convergence threshold" in report
    assert "not_rated" in report
    assert "one_legal_move" in report


# --- isolation from production ----------------------------------------------


PRODUCTION_MODULES = (
    "domain.py",
    "aggregation.py",
    "ingestion.py",
    "personal.py",
    "engine.py",
    "engine_cache.py",
    "assessment.py",
)


def test_no_production_module_imports_the_experimental_package():
    package = Path(__file__).resolve().parents[1] / "src" / "chess_decision_explorer"
    for name in PRODUCTION_MODULES:
        source = (package / name).read_text(encoding="utf-8")
        assert "experimental" not in source, f"{name} references experimental code"


# --- the re-derivation is narrow, not a second opinion -----------------------


WITNESS2 = MoveKey("d2d4")


def unstable_second_candidate_script():
    """WITNESS stays consistent; WITNESS2 alleges damage and drifts wildly at
    every prescribed pair, so Step 4B escalates to B3 and still stops with
    surviving instability triggers and no witness."""
    return {
        (B1.nodes, "*"): {"units": 1400, "move": WITNESS.uci},
        (B1.nodes, WITNESS.uci): 1400,
        (B1.nodes, WITNESS2.uci): 1400,
        (B1.nodes, OBSERVED.uci): 900,
        (B2.nodes, "*"): {"units": 1380, "move": WITNESS2.uci},
        (B2.nodes, WITNESS.uci): 1380,
        (B2.nodes, WITNESS2.uci): 1000,
        (B2.nodes, OBSERVED.uci): 900,
        (B3.nodes, "*"): {"units": 1370, "move": WITNESS.uci},
        (B3.nodes, WITNESS.uci): 1370,
        (B3.nodes, WITNESS2.uci): 1400,
        (B3.nodes, OBSERVED.uci): 900,
    }


def test_step_4b_stops_on_surviving_instability_with_no_witness():
    record = measure_root(
        make_sample(),
        ScriptedProvider(unstable_second_candidate_script()),
        make_policy(b3=B3),
        (R1, R2),
    )
    assert record.assessment.status is AssessmentStatus.INCONCLUSIVE
    assert record.assessment.witness is None
    assert "escalation_exhausted" in [
        reason.value for reason in record.assessment.reasons
    ]
    assert record.assessment.unresolved_triggers


def test_an_uncalibrated_re_derivation_never_overrides_a_substantive_refusal():
    """Calibration scope is the ONLY withholding C0 may re-derive around.

    Step 4B refused to name a witness here because an instability survived
    escalation -- not because the policy was uncalibrated. Re-deriving a
    witness from the same trace would manufacture an `S` the production
    procedure declined to produce.
    """
    record = measure_root(
        make_sample(),
        ScriptedProvider(unstable_second_candidate_script()),
        make_policy(calibrated=False, b3=B3),
        (R1, R2),
    )

    assert record.assessment.status is AssessmentStatus.INCONCLUSIVE
    assert record.witness.has_target is False
    assert record.witness.witness is None
    assert record.witness.s_units is None
    assert record.witness.source == WitnessSource.NONE
    assert record.trajectory is None
    assert "escalation_exhausted" in record.witness.no_r_target_reason


def test_calibrated_and_uncalibrated_agree_on_a_substantive_refusal():
    calibrated = measure_root(
        make_sample(),
        ScriptedProvider(unstable_second_candidate_script()),
        make_policy(b3=B3),
        (R1, R2),
    )
    uncalibrated = measure_root(
        make_sample(),
        ScriptedProvider(unstable_second_candidate_script()),
        make_policy(calibrated=False, b3=B3),
        (R1, R2),
    )
    assert calibrated.witness.as_dict() == uncalibrated.witness.as_dict()


def test_withheld_only_for_calibration_scope_isolates_the_one_re_derivable_case():
    withheld = measure_root(
        make_sample(),
        ScriptedProvider(damaging_script()),
        make_policy(calibrated=False),
        (R1, R2),
    ).assessment
    refused = measure_root(
        make_sample(),
        ScriptedProvider(unstable_second_candidate_script()),
        make_policy(calibrated=False, b3=B3),
        (R1, R2),
    ).assessment

    assert withheld_only_for_calibration_scope(withheld) is True
    assert withheld_only_for_calibration_scope(refused) is False


def test_the_re_derived_gate_reads_discovery_provenance_off_the_trace():
    """The re-derivation must feed `_compare_levels` the same
    `discovered_at_current` flag the Step 4C admission contract derives, so
    the C0 gate is the production gate rather than a more permissive copy."""
    record = measure_root(
        make_sample(),
        ScriptedProvider(damaging_script(b3=False)),
        make_policy(calibrated=False),
        (R1, R2),
    )
    assessment = record.assessment
    previous_level, current_level = prescribed_qualification_pair(assessment)
    witness = record.witness.witness

    expected = any(
        discovery.level == current_level and discovery.discovered_move == witness
        for discovery in assessment.discoveries
    )
    verdict = _compare_levels(
        assessment.round_for(previous_level, witness),
        assessment.round_for(current_level, witness),
        assessment.policy,
        discovered_at_current=expected,
    )
    assert verdict.qualifies
    assert record.witness.s_units == verdict.conservative_units
    assert record.witness.conservative_margin_units == verdict.margin_units
