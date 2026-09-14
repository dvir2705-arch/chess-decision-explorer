"""Step 4C.0 CLI: argument contract, level binding, and the output pipeline.

Synthetic only. The engine pool is replaced by a scripted provider, so the
ordinary pytest suite exercises the whole CLI -- manifest, JSONL, CSV tables,
summary, and Markdown report -- without a Stockfish binary.
"""

from __future__ import annotations

import json
from pathlib import Path

import chess
import pytest

from chess_decision_explorer.domain import MoveKey
from chess_decision_explorer.engine import (
    EngineAnalysisConfig,
    EngineEvaluation,
    EngineIdentity,
    EvaluationRequest,
    SearchMode,
    reconstruct_board,
)
from chess_decision_explorer.assessment import CalibrationStatus, SearchLevel
from chess_decision_explorer.experimental.c0 import C0_EXPERIMENTAL_CALIBRATION_ID
from chess_decision_explorer.experimental.c0 import cli as c0_cli
from chess_decision_explorer.experimental.c0.reference import ReferenceLadderError

_FAKE_SHA = "d" * 64

PGN = """[Event "Rated Blitz game"]
[Site "https://example.invalid/1"]
[Date "2026.01.01"]
[White "alpha"]
[Black "beta"]
[Result "1-0"]
[WhiteElo "1600"]
[BlackElo "1605"]
[TimeControl "300+0"]
[Termination "Normal"]

1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 6. Re1 b5 7. Bb3 d6 8. c3
O-O 9. h3 Nb8 10. d4 Nbd7 11. c4 c6 12. cxb5 axb5 13. Nc3 Bb7 14. Bg5 b4 1-0

[Event "Rated Rapid game"]
[Site "https://example.invalid/2"]
[Date "2026.01.02"]
[White "gamma"]
[Black "delta"]
[Result "0-1"]
[WhiteElo "1700"]
[BlackElo "1690"]
[TimeControl "600+0"]
[Termination "Normal"]

1. d4 d5 2. c4 e6 3. Nc3 Nf6 4. Bg5 Be7 5. e3 O-O 6. Nf3 h6 7. Bh4 b6 8. cxd5
Nxd5 9. Bxe7 Qxe7 10. Nxd5 exd5 11. Rc1 Be6 12. Qa4 c5 13. Qa3 Rc8 14. Bb5 a6
0-1
"""

BASE_ARGS = (
    "--seed", "7",
    "--sample-size", "2",
    "--min-plies", "20",
    "--b1-nodes", "1000",
    "--b2-nodes", "4000",
    "--epsilon-units", "10",
    "--tau-units", "40",
    "--max-gap-drift-units", "200",
    "--max-alternative-drift-units", "200",
    "--max-user-drift-units", "200",
    "--material-negative-gap-units", "150",
    "--reference-nodes", "16000", "64000",
    "--stockfish", "/nonexistent/stockfish",
)


def args_for(tmp_path: Path, *extra: str) -> list[str]:
    pgn_path = tmp_path / "corpus.pgn"
    pgn_path.write_text(PGN, encoding="utf-8")
    return [
        "--source", str(pgn_path),
        "--source-id", "cli-test",
        "--cache-db", str(tmp_path / "cache.sqlite"),
        "--out-dir", str(tmp_path / "runs"),
        "--run-id", "cli-run",
        *BASE_ARGS,
        *extra,
    ]


def parse(*extra: str):
    return c0_cli.build_parser().parse_args([*BASE_ARGS, *extra])


# --- policy and ladder construction -----------------------------------------


def test_every_threshold_and_budget_is_a_required_argument():
    required = {
        "--seed",
        "--sample-size",
        "--min-plies",
        "--b1-nodes",
        "--b2-nodes",
        "--epsilon-units",
        "--tau-units",
        "--max-gap-drift-units",
        "--max-alternative-drift-units",
        "--max-user-drift-units",
        "--material-negative-gap-units",
        "--reference-nodes",
        "--stockfish",
    }
    for flag in required:
        remaining = list(BASE_ARGS)
        index = remaining.index(flag)
        del remaining[index]
        while index < len(remaining) and not remaining[index].startswith("--"):
            del remaining[index]
        with pytest.raises(SystemExit):
            c0_cli.build_parser().parse_args(remaining)


def test_a_pilot_policy_is_always_uncalibrated():
    """C0 measures; it does not calibrate. The pilot path must leave Step 4B
    free to answer INCONCLUSIVE / UNCALIBRATED_POLICY."""
    policy = c0_cli.build_policy(parse())
    assert policy.calibration_status is CalibrationStatus.UNCALIBRATED
    assert policy.calibration_id is None
    assert policy.permits_damage_conclusion is False


def test_no_cli_switch_can_make_the_pilot_policy_calibrated():
    """There is no `--policy-calibration`, and no other flag reaches
    `calibration_status`. The experimental calibration id must not appear in
    the CLI's own source at all."""
    with pytest.raises(SystemExit):
        c0_cli.build_parser().parse_args(
            [*BASE_ARGS, "--policy-calibration", "calibrated"]
        )

    source = Path(c0_cli.__file__).read_text(encoding="utf-8")
    assert "C0_EXPERIMENTAL_CALIBRATION_ID" not in source
    assert "CalibrationStatus.CALIBRATED" not in source

    for extra in (
        (),
        ("--policy-id", "anything"),
        ("--policy-version", "v9"),
        ("--b3-nodes", "9000"),
        ("--max-active-alternatives", "1"),
    ):
        assert (
            c0_cli.build_policy(parse(*extra)).calibration_status
            is CalibrationStatus.UNCALIBRATED
        )


def test_the_experimental_calibration_id_is_a_test_only_parity_fixture():
    """It still exists so a test can build a CALIBRATED twin and check that
    the re-derived S equals a genuine EngineRegret -- but nothing in the
    pilot package's runtime path may use it."""
    assert C0_EXPERIMENTAL_CALIBRATION_ID.startswith("C0-EXPERIMENTAL")
    package = Path(c0_cli.__file__).parent
    users = [
        module.name
        for module in sorted(package.glob("*.py"))
        if "C0_EXPERIMENTAL_CALIBRATION_ID" in module.read_text(encoding="utf-8")
    ]
    assert users == ["__init__.py"], f"unexpected users: {users}"


def test_the_policy_carries_exactly_the_supplied_budgets_and_thresholds():
    policy = c0_cli.build_policy(parse())
    assert (policy.b1.nodes, policy.b2.nodes, policy.b3) == (1000, 4000, None)
    assert policy.epsilon_units == 10
    assert policy.tau_units == 40


def test_an_optional_b3_is_bound_when_supplied():
    policy = c0_cli.build_policy(parse("--b3-nodes", "9000"))
    assert policy.b3 == SearchLevel("B3", 9000)


def test_the_ladder_audits_the_highest_production_qualification_level():
    without_b3 = parse()
    _, audited = c0_cli.build_ladder(without_b3, c0_cli.build_policy(without_b3))
    assert audited == 4000

    with_b3 = parse("--b3-nodes", "9000")
    ladder, audited = c0_cli.build_ladder(with_b3, c0_cli.build_policy(with_b3))
    assert audited == 9000
    assert [level.nodes for level in ladder] == [16000, 64000]
    assert [level.label for level in ladder] == ["R1", "R2"]


def test_a_reference_ladder_weaker_than_the_audited_level_is_refused():
    args = parse("--b3-nodes", "20000")
    with pytest.raises(ReferenceLadderError):
        c0_cli.build_ladder(args, c0_cli.build_policy(args))


def test_an_unordered_reference_ladder_is_refused():
    args = parse("--reference-nodes", "64000", "16000")
    with pytest.raises(ReferenceLadderError):
        c0_cli.build_ladder(args, c0_cli.build_policy(args))


def test_the_discovery_audit_must_be_stronger_than_the_audited_level(tmp_path):
    with pytest.raises(SystemExit):
        c0_cli.main(args_for(tmp_path, "--discovery-audit-nodes", "4000"))


def test_two_bound_levels_may_not_share_a_node_budget(tmp_path):
    """Two levels with the same budget are indistinguishable to the
    evaluation cache. The CLI must name the clash itself rather than let the
    Step 4A pool raise after a run directory already exists."""
    with pytest.raises(SystemExit) as excinfo:
        c0_cli.main(args_for(tmp_path, "--discovery-audit-nodes", "64000"))
    assert "distinct node budget" in str(excinfo.value)
    assert "64000" in str(excinfo.value)


# --- end-to-end output pipeline ---------------------------------------------


class FakePool:
    """A `LeveledEvidenceProvider` over scripted, position-derived evidence.

    The strongest move is defined as the last legal move in UCI order, so
    the script is deterministic for any root the sampler draws without the
    test needing to know which root that is.
    """

    def __init__(self):
        self.requests: list[EvaluationRequest] = []

    @property
    def engine_identity(self) -> EngineIdentity:
        return EngineIdentity(name="FakeFish 1", executable_sha256=_FAKE_SHA)

    def request_for(self, level, position, search_mode, root_move):
        return EvaluationRequest(
            position=position,
            search_mode=search_mode,
            root_move=root_move,
            engine_identity=self.engine_identity,
            config=EngineAnalysisConfig(nodes=level.nodes),
        )

    @staticmethod
    def _best(position) -> MoveKey:
        board: chess.Board = reconstruct_board(position)
        return MoveKey(max(move.uci() for move in board.legal_moves))

    def evaluate(self, request) -> EngineEvaluation:
        self.requests.append(request)
        best = self._best(request.position)
        if request.search_mode is SearchMode.UNRESTRICTED:
            units, pv = 1400, (best,)
        else:
            units = 1400 if request.root_move == best else 900
            pv = (request.root_move,)
        draws = units % 2
        wins = (units - draws) // 2
        return EngineEvaluation(
            centipawn=units - 1000,
            mate=None,
            wdl_wins=wins,
            wdl_draws=draws,
            wdl_losses=1000 - wins - draws,
            depth=20,
            seldepth=24,
            nodes=request.config.nodes,
            pv=pv,
        )


class FakeCounter:
    analyses = 0


class FakeEvaluator:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def fake_engine(monkeypatch):
    pool = FakePool()
    evaluators = [FakeEvaluator()]
    built: dict[str, object] = {}

    def fake_build_engine_pool(executable_path, levels, cache, **kwargs):
        levels = list(levels)
        built["executable_path"] = executable_path
        built["levels"] = levels
        built["kwargs"] = kwargs
        return pool, {level: FakeCounter() for level in levels}, evaluators

    monkeypatch.setattr(c0_cli, "build_engine_pool", fake_build_engine_pool)
    return pool, evaluators, built


def run_cli(tmp_path: Path, *extra: str) -> Path:
    assert c0_cli.main(args_for(tmp_path, *extra)) == 0
    return tmp_path / "runs" / "cli-run"


def run_cli_as(tmp_path: Path, run_id: str, *extra: str) -> Path:
    """Run with an explicit run id, everything else unchanged -- the shape a
    replay takes: same source, same cache db, same policy and ladder, its own
    run directory."""
    args = args_for(tmp_path, *extra)
    args[args.index("--run-id") + 1] = run_id
    assert c0_cli.main(args) == 0
    return tmp_path / "runs" / run_id


def artefact_bytes(run_dir: Path) -> dict[str, bytes]:
    return {
        name: (run_dir / name).read_bytes() for name in c0_cli.C0_RUN_ARTEFACTS
    }


# --- source identity --------------------------------------------------------


def test_stdin_requires_an_explicit_source_id():
    """`source_id` keys every deterministic sampling draw, so a stdin stream
    must be declared rather than silently identified as "stdin"."""
    args = parse("--source", "-")
    with pytest.raises(SystemExit) as excinfo:
        c0_cli.resolve_source_id(args, from_stdin=True)
    message = str(excinfo.value)
    assert "--source-id is required" in message
    assert "stdin" in message


def test_stdin_never_falls_back_to_the_literal_stdin_identity(tmp_path, fake_engine):
    pgn_path = tmp_path / "corpus.pgn"
    pgn_path.write_text(PGN, encoding="utf-8")
    args = [
        "--source", "-",
        "--cache-db", str(tmp_path / "cache.sqlite"),
        "--out-dir", str(tmp_path / "runs"),
        "--run-id", "stdin-run",
        *BASE_ARGS,
    ]
    with pytest.raises(SystemExit):
        c0_cli.main(args)
    assert not (tmp_path / "runs" / "stdin-run").exists()


def test_an_explicit_source_id_is_accepted_for_stdin():
    args = parse("--source", "-", "--source-id", "lichess-2024-01")
    assert c0_cli.resolve_source_id(args, from_stdin=True) == "lichess-2024-01"


def test_a_file_source_still_defaults_to_its_unambiguous_file_stem():
    args = parse("--source", "/corpora/lichess_2024_01.pgn")
    assert c0_cli.resolve_source_id(args, from_stdin=False) == "lichess_2024_01"


def test_a_blank_source_id_is_refused():
    args = parse("--source", "-", "--source-id", "   ")
    with pytest.raises(SystemExit):
        c0_cli.resolve_source_id(args, from_stdin=True)


def test_the_manifest_records_the_source_id_sampling_actually_used(
    tmp_path, fake_engine
):
    """The manifest field, the sampling declaration, and every sampled game id
    must name the same source id -- otherwise the run is not reproducible from
    its own manifest."""
    run_dir = run_cli(tmp_path)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["sampling_source_id"] == "cli-test"
    assert manifest["source_sampling_declaration"]["source_id"] == "cli-test"
    assert "zero_based_source_ordinal" in manifest["game_identity_scheme"]
    assert manifest["sampling_reproducibility_guarantee"]

    for line in (run_dir / "roots.jsonl").read_text().splitlines():
        sample = json.loads(line)["sample"]
        assert sample["source_id"] == "cli-test"
        assert sample["game_id"] == f"cli-test:{sample['source_ordinal']}"
        assert sample["root_id"].startswith(f"cli-test:{sample['source_ordinal']}@")


def test_a_custom_source_id_flows_into_the_manifest_and_every_identity(
    tmp_path, fake_engine
):
    args = args_for(tmp_path)
    args[args.index("--source-id") + 1] = "lichess-2024-01"
    assert c0_cli.main(args) == 0

    run_dir = tmp_path / "runs" / "cli-run"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["sampling_source_id"] == "lichess-2024-01"
    assert manifest["source_sampling_declaration"]["source_id"] == "lichess-2024-01"
    assert "lichess-2024-01" in (run_dir / "report.md").read_text()

    ids = [
        json.loads(line)["sample"]["game_id"]
        for line in (run_dir / "roots.jsonl").read_text().splitlines()
    ]
    assert ids and all(game_id.startswith("lichess-2024-01:") for game_id in ids)


# --- run artefact immutability ----------------------------------------------


def test_reusing_an_existing_run_id_is_rejected(tmp_path, fake_engine):
    run_dir = run_cli(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        c0_cli.main(args_for(tmp_path))
    message = str(excinfo.value)
    assert "already contains C0 run artefacts" in message
    assert str(run_dir) in message
    assert "--force" in message


def test_a_rejected_rerun_leaves_every_artefact_byte_for_byte_unchanged(
    tmp_path, fake_engine
):
    run_dir = run_cli(tmp_path)
    before = artefact_bytes(run_dir)
    listing_before = sorted(path.name for path in run_dir.iterdir())

    with pytest.raises(SystemExit):
        c0_cli.main(args_for(tmp_path))

    assert artefact_bytes(run_dir) == before
    assert sorted(path.name for path in run_dir.iterdir()) == listing_before


def test_an_interrupted_run_directory_is_also_protected(tmp_path, fake_engine):
    """Only a manifest survived -- an interrupted run. It is still a run, and
    is still never overwritten."""
    partial = tmp_path / "runs" / "cli-run"
    partial.mkdir(parents=True)
    (partial / "manifest.json").write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        c0_cli.main(args_for(tmp_path))
    assert "manifest.json" in str(excinfo.value)
    assert (partial / "manifest.json").read_text() == "{}"


def test_an_empty_directory_is_not_an_existing_run(tmp_path, fake_engine):
    (tmp_path / "runs" / "cli-run").mkdir(parents=True)
    run_dir = run_cli(tmp_path)
    assert (run_dir / "manifest.json").is_file()


def test_there_is_no_force_option():
    """Run artefacts are immutable evidence; no switch may overwrite them."""
    parser = c0_cli.build_parser()
    registered = {
        option for action in parser._actions for option in action.option_strings
    }
    assert "--force" not in registered
    with pytest.raises(SystemExit):
        parser.parse_args([*BASE_ARGS, "--force"])


def test_a_replay_uses_a_different_run_id_and_reuses_the_same_cache(
    tmp_path, fake_engine
):
    """The supported replay shape: same source, same source id, same cache db,
    same policy and ladder -- its own run id. Both runs' artefacts survive."""
    _, _, built = fake_engine
    measure_dir = run_cli_as(tmp_path, "c0-pilot-8-measure")
    measured = artefact_bytes(measure_dir)

    replay_dir = run_cli_as(tmp_path, "c0-pilot-8-replay", "--replay-only")

    assert measure_dir != replay_dir
    assert artefact_bytes(measure_dir) == measured
    assert (replay_dir / "manifest.json").is_file()

    measure_manifest = json.loads((measure_dir / "manifest.json").read_text())
    replay_manifest = json.loads((replay_dir / "manifest.json").read_text())
    assert replay_manifest["replay_only"] is True
    assert measure_manifest["replay_only"] is False
    assert built["kwargs"]["replay_only"] is True
    # One shared evaluation cache, two distinct run directories.
    assert (
        replay_manifest["runtime"]["cache_db"]
        == measure_manifest["runtime"]["cache_db"]
    )
    assert replay_manifest["sampling_source_id"] == (
        measure_manifest["sampling_source_id"]
    )
    assert (
        replay_manifest["production_policy"]["fingerprint"]
        == measure_manifest["production_policy"]["fingerprint"]
    )
    assert replay_manifest["reference_ladder"] == measure_manifest["reference_ladder"]


def test_a_replay_reproduces_the_same_derived_measurements(tmp_path, fake_engine):
    measure_dir = run_cli_as(tmp_path, "c0-pilot-8-measure")
    replay_dir = run_cli_as(tmp_path, "c0-pilot-8-replay")

    def derived(run_dir: Path) -> list:
        records = []
        for line in (run_dir / "roots.jsonl").read_text().splitlines():
            record = json.loads(line)
            record.pop("cost", None)
            _strip_timings(record)
            records.append(record)
        return records

    assert derived(measure_dir) == derived(replay_dir)


def _strip_timings(node):
    if isinstance(node, dict):
        node.pop("elapsed_seconds", None)
        node.pop("total_elapsed_seconds", None)
        for value in node.values():
            _strip_timings(value)
    elif isinstance(node, list):
        for value in node:
            _strip_timings(value)


def test_a_run_writes_every_artefact(tmp_path, fake_engine):
    run_dir = run_cli(tmp_path)
    for name in (
        "manifest.json",
        "roots.jsonl",
        "summary.json",
        "roots_summary.csv",
        "reference_levels.csv",
        "report.md",
    ):
        assert (run_dir / name).is_file(), f"{name} was not written"


def test_every_bound_level_is_started_exactly_once(tmp_path, fake_engine):
    _, evaluators, built = fake_engine
    run_cli(tmp_path, "--discovery-audit-nodes", "128000")

    budgets = [level.nodes for level in built["levels"]]
    assert budgets == [1000, 4000, 16000, 64000, 128000]
    assert len(set(budgets)) == len(budgets)
    assert built["executable_path"] == "/nonexistent/stockfish"
    assert built["kwargs"] == {"threads": 1, "hash_mb": 64, "replay_only": False}
    assert all(evaluator.closed for evaluator in evaluators)


def test_replay_only_reaches_the_engine_pool(tmp_path, fake_engine):
    _, _, built = fake_engine
    run_cli(tmp_path, "--replay-only")
    assert built["kwargs"]["replay_only"] is True
    manifest = json.loads((tmp_path / "runs" / "cli-run" / "manifest.json").read_text())
    assert manifest["replay_only"] is True


def test_the_run_measures_the_sampled_roots_and_records_a_trajectory(
    tmp_path, fake_engine
):
    run_dir = run_cli(tmp_path)
    records = [
        json.loads(line)
        for line in (run_dir / "roots.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(records) == 2
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["roots_measured"] == 2

    trajectories = [r for r in records if r["reference_trajectory"]]
    assert trajectories, "no root produced a reference trajectory"
    for record in trajectories:
        trajectory = record["reference_trajectory"]
        assert [level["requested_nodes"] for level in trajectory["levels"]] == [
            16000,
            64000,
        ]
        # One fixed witness, one observed move, at every reference level.
        assert {level["witness"]["move"] for level in trajectory["levels"]} == {
            trajectory["witness"]
        }
        assert {level["observed"]["move"] for level in trajectory["levels"]} == {
            trajectory["observed_move"]
        }
        assert record["r_target_available"] is True


def test_a_normal_run_can_never_emit_a_calibrated_step_4b_conclusion(
    tmp_path, fake_engine
):
    """The end-to-end guarantee: experimental epsilon/tau inputs that would
    have qualified a witness produce a RESEARCH measurement, never a
    production damage conclusion."""
    run_dir = run_cli(tmp_path)
    records = [
        json.loads(line)
        for line in (run_dir / "roots.jsonl").read_text().splitlines()
        if line.strip()
    ]

    measured = [r for r in records if r["assessment"] is not None]
    assert measured, "no root was measured"
    for record in measured:
        assessment = record["assessment"]
        assert assessment["status"] != "damage_supported"
        assert assessment["is_engine_damage_admissible"] is False
        assert assessment["admission_failures"]
        assert assessment["engine_regret_units"] is None
        assert assessment["witness"] is None

    # S still exists as a research measurement, and is always tagged as one.
    with_s = [r for r in records if r["witness_resolution"]["s_units"] is not None]
    assert with_s, "no root produced a research S"
    for record in with_s:
        assert record["witness_resolution"]["witness_source"] == "rederived_from_trace"

    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["engine_damage_admissible_count"] == 0
    assert "damage_supported" not in summary["status_distribution"]
    assert set(summary["witness_source_distribution"]) <= {
        "rederived_from_trace",
        "none",
    }

    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["step_4b_calibration_status"] == "uncalibrated"
    assert manifest["step_4b_calibration_id"] is None
    assert manifest["damage_conclusions_permitted"] is False
    assert manifest["production_policy"]["calibration_id"] is None

    report = (run_dir / "report.md").read_text()
    assert "The Step 4B policy is **UNCALIBRATED**" in report
    assert "RESEARCH MEASUREMENT" in report


def test_no_record_or_report_ever_carries_an_r_value(tmp_path, fake_engine):
    run_dir = run_cli(tmp_path)
    payload = (run_dir / "roots.jsonl").read_text()
    for record in (json.loads(line) for line in payload.splitlines() if line.strip()):
        assert "r_units" not in json.dumps(record)
        assert "g_reference" not in json.dumps(record)
    report = (run_dir / "report.md").read_text()
    assert "`R = S - G_reference` was **not** computed" in report


def test_the_report_and_manifest_agree_with_the_run(tmp_path, fake_engine):
    run_dir = run_cli(tmp_path)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    report = (run_dir / "report.md").read_text()

    assert manifest["run_id"] == "cli-run"
    assert manifest["calibrated"] is False
    assert manifest["ml_trained"] is False
    assert manifest["personal_games_used"] is False
    assert manifest["production_budgets"] == {
        "b1_nodes": 1000,
        "b2_nodes": 4000,
        "b3_nodes": None,
    }
    assert manifest["audited_production_nodes"] == 4000
    assert [level["nodes"] for level in manifest["reference_ladder"]] == [16000, 64000]
    assert manifest["finished_at"] is not None
    assert manifest["scan_termination"] == "sample_target_reached"
    assert manifest["runtime"]["roots_recorded"] == 2
    assert "NO epsilon was calibrated" in report
    assert "R1=16000, R2=64000" in report


def test_a_stopped_scan_is_declared_a_prefix_everywhere(tmp_path, fake_engine):
    run_dir = run_cli(tmp_path)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    declaration = manifest["source_sampling_declaration"]

    assert declaration["scan_termination"] == "sample_target_reached"
    assert declaration["scanned_prefix_declared"] is True
    assert declaration["records_scanned"] == 2
    assert "FEASIBILITY SAMPLE ONLY" in declaration["representativeness"]
    report = (run_dir / "report.md").read_text()
    assert "declared prefix/window of the source stream" in report


def test_the_csv_tables_match_their_declared_columns(tmp_path, fake_engine):
    import csv

    run_dir = run_cli(tmp_path)
    with open(run_dir / "roots_summary.csv", newline="", encoding="utf-8") as handle:
        roots = list(csv.DictReader(handle))
    with open(run_dir / "reference_levels.csv", newline="", encoding="utf-8") as handle:
        levels = list(csv.DictReader(handle))

    assert len(roots) == 2
    assert tuple(roots[0]) == c0_cli.ROOT_SUMMARY_COLUMNS
    assert levels, "no reference-level rows were written"
    assert tuple(levels[0]) == c0_cli.REFERENCE_LEVEL_COLUMNS
    assert {row["root_id"] for row in levels} <= {row["root_id"] for row in roots}


def test_a_source_id_is_derived_from_the_file_when_not_supplied(tmp_path, fake_engine):
    pgn_path = tmp_path / "corpus.pgn"
    pgn_path.write_text(PGN, encoding="utf-8")
    assert (
        c0_cli.main(
            [
                "--source", str(pgn_path),
                "--cache-db", str(tmp_path / "cache.sqlite"),
                "--out-dir", str(tmp_path / "runs"),
                "--run-id", "derived-id",
                *BASE_ARGS,
            ]
        )
        == 0
    )
    records = (tmp_path / "runs" / "derived-id" / "roots.jsonl").read_text()
    for line in records.splitlines():
        if line.strip():
            assert json.loads(line)["sample"]["game_id"].startswith("corpus:")
