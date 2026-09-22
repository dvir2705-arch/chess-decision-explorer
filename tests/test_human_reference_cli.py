"""Step 5C-lite: the CLI, the run manifest, and the report.

Synthetic only -- every PGN is written into tmp_path. No network, no
external corpus, no engine.
"""

from __future__ import annotations

import csv
import io
import json

import pytest

from chess_decision_explorer.human_reference.cli import (
    RUN_ARTEFACTS,
    build_parser,
    main,
    refuse_existing_run,
    resolve_source_id,
)
from chess_decision_explorer.human_reference.manifest import (
    REQUIRED_MANIFEST_FIELDS,
)
from chess_decision_explorer.human_reference.report import (
    MOVE_COLUMNS,
    POSITION_COLUMNS,
)


def pgn(moves, *, white="alpha", black="beta", result="1-0", event="Rated Blitz game"):
    return "\n".join(
        [
            f'[Event "{event}"]',
            '[Site "https://example.invalid/x"]',
            '[Date "2026.01.01"]',
            f'[White "{white}"]',
            f'[Black "{black}"]',
            f'[Result "{result}"]',
            '[WhiteElo "1600"]',
            '[BlackElo "1610"]',
            '[TimeControl "300+0"]',
            "",
            f"{moves} {result}",
            "",
        ]
    )


PERSONAL_GAMES = [
    "1. e4 e5 2. Nf3 Nc6",
    "1. e4 c5 2. Nf3 d6",
    "1. d4 d5 2. c4 e6",
    "1. Nf3 d5 2. g3 Nf6",
]

# The four ineligible (casual) records come FIRST on purpose: a scan that
# meets its target stops reading, so trailing rejects would never be seen and
# "records scanned" would equal "games accepted" by accident.
REFERENCE_GAMES = (
    [pgn("1. e4 e5 2. Nf3 Nc6", event="Casual Blitz game") for _ in range(4)]
    + [pgn("1. e4 e5 2. Nf3 Nc6", result="1-0") for _ in range(5)]
    + [pgn("1. d4 d5 2. c4 e6", result="0-1") for _ in range(3)]
    + [pgn("1. c4 e5 2. Nc3 Nf6", result="1/2-1/2") for _ in range(2)]
)


@pytest.fixture
def corpus(tmp_path):
    personal_dir = tmp_path / "personal"
    personal_dir.mkdir()
    for index, moves in enumerate(PERSONAL_GAMES):
        (personal_dir / f"2026-{index + 1:02d}.pgn").write_text(
            pgn(moves, white="hero"), encoding="utf-8"
        )
    reference = tmp_path / "reference.pgn"
    reference.write_text("\n".join(REFERENCE_GAMES), encoding="utf-8")
    return personal_dir, reference


def run_cli(tmp_path, corpus, *extra, run_id="run-1", target_games=10):
    personal_dir, reference = corpus
    argv = [
        "--personal-pgn", str(personal_dir),
        "--player", "hero",
        "--cohort", "blitz",
        "--source", str(reference),
        "--target-games", str(target_games),
        "--out-dir", str(tmp_path / "runs"),
        "--run-id", run_id,
        *extra,
    ]
    assert main(argv) == 0
    return tmp_path / "runs" / run_id


# --- end to end -------------------------------------------------------------


def test_run_writes_every_artefact(tmp_path, corpus):
    run_dir = run_cli(tmp_path, corpus)
    for name in RUN_ARTEFACTS:
        assert (run_dir / name).exists(), name


def test_positions_and_moves_csv_schemas(tmp_path, corpus):
    run_dir = run_cli(tmp_path, corpus)

    with open(run_dir / "positions.csv", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0]) == list(POSITION_COLUMNS)
    assert len(rows) == 1
    assert rows[0]["personal_occurrence_count"] == "4"
    assert rows[0]["reference_occurrence_count"] == "10"
    assert rows[0]["personal_primary_move"] == "e2e4"
    assert rows[0]["reference_top_move"] == "e2e4"
    assert rows[0]["side_to_move"] == "white"

    with open(run_dir / "moves.csv", newline="", encoding="utf-8") as handle:
        move_rows = list(csv.DictReader(handle))
    assert list(move_rows[0]) == list(MOVE_COLUMNS)
    assert [row["move"] for row in move_rows] == ["e2e4", "d2d4", "c2c4", "g1f3"]
    assert move_rows[0]["reference_wins"] == "5"
    assert move_rows[0]["reference_score_rate"] == "1.0"
    # A personal move absent from the reference leaves its rates empty, not 0.
    g1f3 = move_rows[-1]
    assert g1f3["move"] == "g1f3"
    assert g1f3["reference_move_rate"] == ""
    assert g1f3["reference_score_rate"] == ""
    assert g1f3["reference_rank"] == ""


def test_csv_artefacts_are_byte_identical_across_runs(tmp_path, corpus):
    first = run_cli(tmp_path, corpus, run_id="a")
    second = run_cli(tmp_path, corpus, run_id="b")
    for name in ("positions.csv", "moves.csv", "target_set.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


# --- manifest / provenance --------------------------------------------------


def test_manifest_carries_every_required_field(tmp_path, corpus):
    run_dir = run_cli(tmp_path, corpus)
    manifest = json.loads((run_dir / "reference_manifest.json").read_text())
    missing = [field for field in REQUIRED_MANIFEST_FIELDS if field not in manifest]
    assert missing == []


def test_manifest_records_the_run_configuration(tmp_path, corpus):
    run_dir = run_cli(tmp_path, corpus)
    manifest = json.loads((run_dir / "reference_manifest.json").read_text())

    assert manifest["source_id"] == "reference"
    assert manifest["source_declaration"]["stream"].endswith("reference.pgn")
    assert manifest["personal_cohort"] == "blitz"
    assert manifest["personal_target_set"]["player_label"] == "hero"
    assert manifest["recurring_position_threshold"] == 2
    assert manifest["position_key_semantics_version"] == "POSITION_KEY_V1"
    assert manifest["eligibility_filters"]["require_rated"] is True
    assert manifest["eligibility_filters"]["require_human_players"] is True
    assert "BOT" in manifest["eligibility_filters"]["bot_rule"]
    assert manifest["eligibility_filters"]["eligible_time_control_categories"] == [
        "rapid",
        "blitz",
    ]
    assert manifest["started_at"] and manifest["finished_at"]
    assert manifest["engine_evidence_used"] is False
    assert manifest["calibrated"] is False
    assert manifest["rating_matched_cohort"] is False
    assert manifest["significance_tested"] is False


def test_manifest_separates_scanned_from_accepted(tmp_path, corpus):
    run_dir = run_cli(tmp_path, corpus, target_games=10)
    manifest = json.loads((run_dir / "reference_manifest.json").read_text())

    assert manifest["records_scanned"] == 14
    assert manifest["eligible_games_accepted"] == 10
    assert manifest["target_eligible_games"] == 10
    assert manifest["target_met"] is True
    assert manifest["rejections"]["not_rated"] == 4


def test_manifest_records_runtime_instrumentation(tmp_path, corpus):
    run_dir = run_cli(tmp_path, corpus)
    runtime = json.loads((run_dir / "reference_manifest.json").read_text())["runtime"]
    for key in (
        "scan_seconds",
        "records_scanned_per_second",
        "eligible_games_per_second",
        "decisions_examined_per_second",
        "approx_peak_memory_mb",
        "aggregate_positions",
        "aggregate_move_entries",
    ):
        assert key in runtime, key
    assert runtime["aggregate_positions"] == 1


def test_manifest_is_written_before_the_scan_and_updated_after(tmp_path, corpus):
    """An interrupted run still leaves a record of what it was attempting."""
    run_dir = run_cli(tmp_path, corpus)
    manifest = json.loads((run_dir / "reference_manifest.json").read_text())
    assert manifest["finished_at"] is not None
    assert manifest["scan_termination"] == "target_reached"


# --- the target is eligible games, never records ----------------------------


def test_shortfall_is_reported_and_never_claimed_as_the_target(tmp_path, corpus):
    run_dir = run_cli(tmp_path, corpus, run_id="short", target_games=500)
    manifest = json.loads((run_dir / "reference_manifest.json").read_text())

    assert manifest["target_eligible_games"] == 500
    assert manifest["eligible_games_accepted"] == 10
    assert manifest["target_met"] is False
    assert manifest["shortfall"] == 490
    assert manifest["scan_termination"] == "stream_exhausted"

    report = (run_dir / "report.md").read_text()
    assert "TARGET NOT MET" in report
    assert "10 eligible reference games were accepted" in report
    assert "not** a 500-game reference dataset" in report


def test_report_always_separates_scanned_from_accepted(tmp_path, corpus):
    run_dir = run_cli(tmp_path, corpus)
    report = (run_dir / "report.md").read_text()
    assert "14 PGN records were **scanned**" in report
    assert "10 of them were **accepted as eligible reference games**" in report
    assert "| eligible_games_accepted | 10 |" in report
    assert "| records_scanned | 14 |" in report


def test_report_states_what_it_does_not_claim(tmp_path, corpus):
    run_dir = run_cli(tmp_path, corpus)
    report = (run_dir / "report.md").read_text()
    assert "No engine evaluation" in report
    assert "Not a rating-matched cohort" in report
    assert "No statistical-significance claim" in report


def test_report_flags_a_declared_prefix(tmp_path, corpus):
    run_dir = run_cli(tmp_path, corpus, run_id="prefix", target_games=3)
    report = (run_dir / "report.md").read_text()
    assert "declared prefix" in report
    summary = json.loads((run_dir / "reference_summary.json").read_text())
    assert summary["scan"]["scanned_prefix_declared"] is True


def test_stdout_headline_names_both_counts(tmp_path, corpus, capsys):
    run_cli(tmp_path, corpus)
    out = capsys.readouterr().out
    assert "records scanned:" in out
    assert "ELIGIBLE games accepted:" in out


def test_stdout_warns_when_the_target_is_not_met(tmp_path, corpus, capsys):
    run_cli(tmp_path, corpus, run_id="warn", target_games=500)
    out = capsys.readouterr().out
    assert "TARGET NOT MET" in out
    assert "NOT a 500-game reference dataset" in out


# --- summary ----------------------------------------------------------------


def test_summary_reports_coverage_and_agreement(tmp_path, corpus):
    run_dir = run_cli(tmp_path, corpus)
    summary = json.loads((run_dir / "reference_summary.json").read_text())

    assert summary["positions"]["target_positions"] == 1
    assert summary["positions"]["reference_covered"] == 1
    assert summary["positions"]["position_coverage_rate"] == 1.0
    assert summary["moves"]["personal_and_reference"] == 2
    assert summary["moves"]["personal_only"] == 1
    assert summary["moves"]["reference_only"] == 1

    primary = summary["personal_primary_move_vs_reference"]
    assert primary["primary_move_agreement"] == 1
    assert primary["reference_rank_distribution"] == {"rank_1": 1}


# --- source identity --------------------------------------------------------


def test_stdin_requires_an_explicit_source_id():
    with pytest.raises(SystemExit, match="source-id is required"):
        resolve_source_id("-", None, True)


def test_stdin_accepts_a_declared_source_id():
    assert resolve_source_id("-", "lichess_2024_01", True) == "lichess_2024_01"


def test_a_blank_source_id_is_refused():
    with pytest.raises(SystemExit, match="must not be empty"):
        resolve_source_id("-", "   ", True)


def test_a_file_source_defaults_to_its_stem():
    assert resolve_source_id("/data/corpus_2024.pgn", None, False) == "corpus_2024"


def test_stdin_run_uses_the_declared_source_id(tmp_path, corpus, monkeypatch):
    personal_dir, reference = corpus
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(reference.read_text(encoding="utf-8"))
    )
    argv = [
        "--personal-pgn", str(personal_dir),
        "--player", "hero",
        "--cohort", "blitz",
        "--source", "-",
        "--source-id", "declared_corpus",
        "--target-games", "5",
        "--out-dir", str(tmp_path / "runs"),
        "--run-id", "stdin-run",
    ]
    assert main(argv) == 0
    manifest = json.loads(
        (tmp_path / "runs" / "stdin-run" / "reference_manifest.json").read_text()
    )
    assert manifest["source_id"] == "declared_corpus"
    assert manifest["source_declaration"]["stream"] == "stdin"
    assert manifest["source_declaration"]["corpus_hashed"] is False


def test_stdin_run_without_a_source_id_is_refused(tmp_path, corpus):
    personal_dir, _ = corpus
    argv = [
        "--personal-pgn", str(personal_dir),
        "--player", "hero",
        "--cohort", "blitz",
        "--source", "-",
        "--target-games", "5",
        "--out-dir", str(tmp_path / "runs"),
        "--run-id", "nope",
    ]
    with pytest.raises(SystemExit, match="source-id is required"):
        main(argv)


# --- target set handling ----------------------------------------------------


def test_build_target_set_only_writes_the_set_and_stops(tmp_path, corpus):
    personal_dir, _ = corpus
    run_dir = tmp_path / "runs" / "target-only"
    argv = [
        "--personal-pgn", str(personal_dir),
        "--player", "hero",
        "--cohort", "blitz",
        "--build-target-set-only",
        "--out-dir", str(tmp_path / "runs"),
        "--run-id", "target-only",
    ]
    assert main(argv) == 0
    assert (run_dir / "target_set.json").exists()
    assert not (run_dir / "reference_manifest.json").exists()
    assert not (run_dir / "positions.csv").exists()


def test_an_exported_target_set_can_drive_a_later_run(tmp_path, corpus):
    personal_dir, reference = corpus
    assert (
        main(
            [
                "--personal-pgn", str(personal_dir),
                "--player", "hero",
                "--cohort", "blitz",
                "--build-target-set-only",
                "--out-dir", str(tmp_path / "runs"),
                "--run-id", "export",
            ]
        )
        == 0
    )
    exported = tmp_path / "runs" / "export" / "target_set.json"

    assert (
        main(
            [
                "--target-set", str(exported),
                "--source", str(reference),
                "--target-games", "10",
                "--out-dir", str(tmp_path / "runs"),
                "--run-id", "reuse",
            ]
        )
        == 0
    )
    manifest = json.loads(
        (tmp_path / "runs" / "reuse" / "reference_manifest.json").read_text()
    )
    assert manifest["personal_cohort"] == "blitz"
    assert manifest["eligible_games_accepted"] == 10


def test_personal_pgn_and_target_set_are_mutually_exclusive(tmp_path, corpus):
    personal_dir, reference = corpus
    with pytest.raises(SystemExit, match="exactly one of"):
        main(
            [
                "--personal-pgn", str(personal_dir),
                "--target-set", "whatever.json",
                "--source", str(reference),
                "--target-games", "1",
                "--out-dir", str(tmp_path / "runs"),
                "--run-id", "x",
            ]
        )


def test_player_and_cohort_are_required_with_personal_pgns(tmp_path, corpus):
    personal_dir, reference = corpus
    with pytest.raises(SystemExit, match="--player is required"):
        main(
            [
                "--personal-pgn", str(personal_dir),
                "--source", str(reference),
                "--target-games", "1",
                "--out-dir", str(tmp_path / "runs"),
                "--run-id", "y",
            ]
        )


def test_target_games_is_required_for_a_scan(tmp_path, corpus):
    personal_dir, reference = corpus
    with pytest.raises(SystemExit, match="--target-games is required"):
        main(
            [
                "--personal-pgn", str(personal_dir),
                "--player", "hero",
                "--cohort", "blitz",
                "--source", str(reference),
                "--out-dir", str(tmp_path / "runs"),
                "--run-id", "z",
            ]
        )


def test_a_cohort_with_no_recurring_positions_stops_with_a_clear_error(
    tmp_path, corpus
):
    personal_dir, reference = corpus
    with pytest.raises(SystemExit, match="no recurring positions"):
        main(
            [
                "--personal-pgn", str(personal_dir),
                "--player", "hero",
                "--cohort", "rapid",
                "--source", str(reference),
                "--target-games", "1",
                "--out-dir", str(tmp_path / "runs"),
                "--run-id", "empty",
            ]
        )


# --- run immutability -------------------------------------------------------


def test_an_existing_run_directory_is_never_overwritten(tmp_path, corpus):
    run_dir = run_cli(tmp_path, corpus, run_id="once")
    assert run_dir.exists()
    with pytest.raises(SystemExit, match="already contains run artefacts"):
        run_cli(tmp_path, corpus, run_id="once")


def test_refuse_existing_run_allows_a_fresh_directory(tmp_path):
    refuse_existing_run(tmp_path / "does-not-exist")
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    refuse_existing_run(fresh)


def test_refuse_existing_run_rejects_a_non_directory(tmp_path):
    path = tmp_path / "file"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit, match="is not a directory"):
        refuse_existing_run(path)


def test_parser_defaults_match_the_documented_v1_policy():
    args = build_parser().parse_args(["--target-games", "1"])
    assert args.source == "-"
    assert args.categories == ["rapid", "blitz"]
    assert args.require_rated is True
    assert args.require_human_players is True
    assert args.min_distinct_games == 2
    assert args.exclude_termination == []


def test_allow_bot_players_flag_is_recorded(tmp_path, corpus):
    run_dir = run_cli(tmp_path, corpus, "--allow-bot-players", run_id="bots-ok")
    manifest = json.loads((run_dir / "reference_manifest.json").read_text())
    assert manifest["eligibility_filters"]["require_human_players"] is False
