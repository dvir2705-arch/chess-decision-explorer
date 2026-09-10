from __future__ import annotations

import hashlib
import os
from types import SimpleNamespace

import chess
import chess.engine
import pytest

import chess_decision_explorer.engine as engine_module
from chess_decision_explorer.domain import MoveKey, PositionKey
from chess_decision_explorer.engine import (
    ANALYSIS_PROFILE,
    ANALYSIS_PROFILE_FINGERPRINT,
    ENGINE_EVIDENCE_VERSION,
    POSITION_SEMANTICS_VERSION,
    EngineAnalysisConfig,
    EngineCapabilityError,
    EngineEvaluation,
    EngineEvidenceError,
    EngineIdentity,
    EngineStartupError,
    EvaluationRequest,
    RequestValidationError,
    SearchMode,
    StockfishEvaluator,
    compute_analysis_profile_fingerprint,
    hash_executable,
    reconstruct_board,
    resolve_executable,
)

_FAKE_SHA = "a" * 64


# --- test doubles ------------------------------------------------------


class FakeAnalysis:
    """Context-manager iterable standing in for python-chess's streaming
    ``SimpleEngine.analysis()`` result."""

    def __init__(self, infos):
        self._infos = list(infos)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def __iter__(self):
        return iter(self._infos)


class FakeUciEngine:
    """Minimal stand-in for chess.engine.SimpleEngine covering only what
    StockfishEvaluator uses, so the normal test suite never needs a real
    Stockfish binary."""

    def __init__(self, id_map=None, options=None, analyse_result=None):
        self.id = dict(id_map if id_map is not None else {"name": "Fake Engine 1.0"})
        self.options = dict(
            options
            if options is not None
            else {"UCI_ShowWDL": chess.engine.Option("UCI_ShowWDL", "check", False, None, None, None)}
        )
        self._analyse_result = analyse_result
        self.configure_calls: list[dict] = []
        self.analyse_calls: list[SimpleNamespace] = []
        self.quit_called = False

    def configure(self, options):
        self.configure_calls.append(dict(options))

    def analysis(self, board, limit, *, game=None, root_moves=None, **kwargs):
        self.analyse_calls.append(
            SimpleNamespace(
                board=board.copy(),
                limit=limit,
                game=game,
                root_moves=root_moves,
            )
        )
        result = self._analyse_result
        if callable(result):
            result = result(board, limit, game=game, root_moves=root_moves)
        if result is None:
            infos = []
        elif isinstance(result, list):
            infos = result
        else:
            infos = [result]
        return FakeAnalysis(infos)

    def quit(self):
        self.quit_called = True


def make_option(name, type_="check", default=False):
    return chess.engine.Option(name, type_, default, None, None, None)


def default_options():
    return {
        "UCI_ShowWDL": make_option("UCI_ShowWDL"),
        "EvalFile": make_option("EvalFile", "string", "nn-1111111111.nnue"),
    }


def make_info(
    *,
    cp=None,
    mate=None,
    turn=chess.WHITE,
    wins=500,
    draws=300,
    losses=200,
    depth=20,
    seldepth=25,
    nodes=100011,
    pv=None,
    bound=None,
    include_wdl=True,
    include_score=True,
    include_seldepth=True,
    include_pv=True,
    include_depth=True,
    include_nodes=True,
):
    info: chess.engine.InfoDict = {}
    if include_score:
        score_obj = chess.engine.Mate(mate) if mate is not None else chess.engine.Cp(cp or 0)
        info["score"] = chess.engine.PovScore(score_obj, turn)
    if bound is not None:
        info[bound] = True
    if include_wdl:
        info["wdl"] = chess.engine.PovWdl(chess.engine.Wdl(wins, draws, losses), turn)
    if include_depth:
        info["depth"] = depth
    if include_seldepth:
        info["seldepth"] = seldepth
    if include_nodes:
        info["nodes"] = nodes
    if include_pv:
        info["pv"] = pv if pv is not None else [chess.Move.from_uci("e2e4")]
    return info


def start_evaluator(monkeypatch, engine, config=None):
    if config is None:
        config = EngineAnalysisConfig(nodes=1000)
    monkeypatch.setattr(
        chess.engine.SimpleEngine, "popen_uci", lambda command, **kwargs: engine
    )
    monkeypatch.setattr(engine_module, "hash_executable", lambda path: _FAKE_SHA)
    monkeypatch.setattr(engine_module, "resolve_executable", lambda reference: reference)
    evaluator = StockfishEvaluator("/fake/path/to/stockfish", config)
    evaluator.start()
    return evaluator


# --- EngineAnalysisConfig ------------------------------------------------


def test_config_defaults():
    config = EngineAnalysisConfig(nodes=100000)
    assert config.threads == 1
    assert config.hash_mb == 64


@pytest.mark.parametrize("field", ["nodes", "threads", "hash_mb"])
def test_config_rejects_non_positive(field):
    kwargs = {"nodes": 100000, "threads": 1, "hash_mb": 64}
    kwargs[field] = 0
    with pytest.raises(ValueError):
        EngineAnalysisConfig(**kwargs)


@pytest.mark.parametrize("field", ["nodes", "threads", "hash_mb"])
def test_config_rejects_bool(field):
    kwargs = {"nodes": 100000, "threads": 1, "hash_mb": 64}
    kwargs[field] = True
    with pytest.raises(ValueError):
        EngineAnalysisConfig(**kwargs)


@pytest.mark.parametrize("field", ["nodes", "threads", "hash_mb"])
def test_config_rejects_non_int(field):
    kwargs = {"nodes": 100000, "threads": 1, "hash_mb": 64}
    kwargs[field] = 100.0
    with pytest.raises(ValueError):
        EngineAnalysisConfig(**kwargs)


def test_config_rejects_negative():
    with pytest.raises(ValueError):
        EngineAnalysisConfig(nodes=-1)


# --- Board reconstruction -------------------------------------------------


def test_position_key_round_trips_through_board_reconstruction():
    board = chess.Board()
    for uci in ["e2e4", "a7a6", "e4e5", "d7d5"]:
        board.push_uci(uci)
    original_key = PositionKey.from_board(board)

    reconstructed = reconstruct_board(original_key)

    assert PositionKey.from_board(reconstructed) == original_key
    assert reconstructed.turn == board.turn
    assert reconstructed.castling_rights == board.castling_rights
    assert reconstructed.ep_square == board.ep_square
    assert reconstructed.board_fen() == board.board_fen()


def test_reconstructed_board_black_to_move():
    board = chess.Board()
    board.push_uci("e2e4")
    key = PositionKey.from_board(board)

    reconstructed = reconstruct_board(key)

    assert reconstructed.turn == chess.BLACK


# --- SearchMode / EvaluationRequest invariants ----------------------------


def _identity():
    return EngineIdentity(name="Fake Engine 1.0", executable_sha256=_FAKE_SHA)


def _config():
    return EngineAnalysisConfig(nodes=100000)


def test_unrestricted_request_accepts_no_root_move():
    key = EvaluationRequest(
        PositionKey.from_board(chess.Board()),
        SearchMode.UNRESTRICTED,
        None,
        _identity(),
        _config(),
    )
    assert key.root_move is None


def test_unrestricted_request_rejects_root_move():
    with pytest.raises(ValueError):
        EvaluationRequest(
            PositionKey.from_board(chess.Board()),
            SearchMode.UNRESTRICTED,
            MoveKey("e2e4"),
            _identity(),
            _config(),
        )


def test_forced_move_request_requires_root_move():
    with pytest.raises(ValueError):
        EvaluationRequest(
            PositionKey.from_board(chess.Board()),
            SearchMode.FORCED_MOVE,
            None,
            _identity(),
            _config(),
        )


def test_forced_move_request_accepts_legal_root_move():
    key = EvaluationRequest(
        PositionKey.from_board(chess.Board()),
        SearchMode.FORCED_MOVE,
        MoveKey("e2e4"),
        _identity(),
        _config(),
    )
    assert key.root_move == MoveKey("e2e4")


def test_forced_move_request_rejects_illegal_root_move():
    with pytest.raises(ValueError):
        EvaluationRequest(
            PositionKey.from_board(chess.Board()),
            SearchMode.FORCED_MOVE,
            MoveKey("e2e5"),  # illegal: not a legal opening move
            _identity(),
            _config(),
        )


def test_evaluation_request_is_hashable_and_usable_as_dict_key():
    key = EvaluationRequest(
        PositionKey.from_board(chess.Board()),
        SearchMode.UNRESTRICTED,
        None,
        _identity(),
        _config(),
    )
    {key: "value"}  # must not raise


# --- EngineEvaluation ------------------------------------------------------


def _valid_eval_kwargs(**overrides):
    kwargs = dict(
        centipawn=25,
        mate=None,
        wdl_wins=500,
        wdl_draws=300,
        wdl_losses=200,
        depth=20,
        seldepth=25,
        nodes=100011,
        pv=(MoveKey("e2e4"),),
    )
    kwargs.update(overrides)
    return kwargs


def test_expected_score_calculation():
    ev = EngineEvaluation(**_valid_eval_kwargs(wdl_wins=600, wdl_draws=200, wdl_losses=200))
    assert ev.expected_score == pytest.approx((600 + 0.5 * 200) / 1000)


def test_centipawn_representation():
    ev = EngineEvaluation(**_valid_eval_kwargs(centipawn=-150, mate=None))
    assert ev.centipawn == -150
    assert ev.mate is None


def test_mate_representation():
    ev = EngineEvaluation(**_valid_eval_kwargs(centipawn=None, mate=3))
    assert ev.mate == 3
    assert ev.centipawn is None


def test_evaluation_rejects_both_centipawn_and_mate_set():
    with pytest.raises(ValueError):
        EngineEvaluation(**_valid_eval_kwargs(centipawn=100, mate=3))


def test_evaluation_rejects_neither_centipawn_nor_mate_set():
    with pytest.raises(ValueError):
        EngineEvaluation(**_valid_eval_kwargs(centipawn=None, mate=None))


@pytest.mark.parametrize("field", ["wdl_wins", "wdl_draws", "wdl_losses"])
def test_evaluation_rejects_negative_wdl(field):
    with pytest.raises(ValueError):
        EngineEvaluation(**_valid_eval_kwargs(**{field: -1}))


def test_evaluation_rejects_zero_total_wdl():
    with pytest.raises(ValueError):
        EngineEvaluation(**_valid_eval_kwargs(wdl_wins=0, wdl_draws=0, wdl_losses=0))


def test_evaluation_rejects_bool_wdl():
    with pytest.raises(ValueError):
        EngineEvaluation(**_valid_eval_kwargs(wdl_wins=True))


def test_evaluation_requires_wdl_total_exactly_1000():
    # Step 4A stores the engine-reported Stockfish WDL, whose scale is 1000.
    with pytest.raises(ValueError):
        EngineEvaluation(**_valid_eval_kwargs(wdl_wins=3, wdl_draws=1, wdl_losses=1))


def test_mate_zero_is_structurally_representable():
    ev = EngineEvaluation(
        centipawn=None,
        mate=0,
        wdl_wins=0,
        wdl_draws=0,
        wdl_losses=1000,
        depth=1,
        seldepth=None,
        nodes=1,
        pv=(MoveKey("e2e4"),),
    )
    assert ev.mate == 0
    assert ev.centipawn is None


def test_evaluation_rejects_non_tuple_pv():
    with pytest.raises(ValueError):
        EngineEvaluation(**_valid_eval_kwargs(pv=[MoveKey("e2e4")]))


# --- POV normalization ------------------------------------------------


def test_pov_normalization_white_to_move(monkeypatch):
    engine = FakeUciEngine(
        options=default_options(),
        analyse_result=make_info(cp=150, turn=chess.WHITE),
    )
    evaluator = start_evaluator(monkeypatch, engine)

    result = evaluator.evaluate(
        PositionKey.from_board(chess.Board()), SearchMode.UNRESTRICTED
    )

    assert result.centipawn == 150


def test_pov_normalization_black_to_move(monkeypatch):
    board = chess.Board()
    board.push_uci("e2e4")
    position = PositionKey.from_board(board)  # Black to move

    # The raw score object is reported relative to White (turn=WHITE) even
    # though the root position is Black to move; normalization must flip
    # the sign to Black's point of view rather than passing it through.
    engine = FakeUciEngine(
        options=default_options(),
        analyse_result=make_info(
            cp=150, turn=chess.WHITE, pv=[chess.Move.from_uci("e7e5")]
        ),
    )
    evaluator = start_evaluator(monkeypatch, engine)

    result = evaluator.evaluate(position, SearchMode.UNRESTRICTED)

    assert result.centipawn == -150


def test_pov_normalization_flips_wdl_for_black_to_move(monkeypatch):
    board = chess.Board()
    board.push_uci("e2e4")
    position = PositionKey.from_board(board)

    engine = FakeUciEngine(
        options=default_options(),
        analyse_result=make_info(
            cp=0,
            turn=chess.WHITE,
            wins=700,
            draws=200,
            losses=100,
            pv=[chess.Move.from_uci("e7e5")],
        ),
    )
    evaluator = start_evaluator(monkeypatch, engine)

    result = evaluator.evaluate(position, SearchMode.UNRESTRICTED)

    # From Black's POV, White's wins become Black's losses and vice versa.
    assert result.wdl_wins == 100
    assert result.wdl_draws == 200
    assert result.wdl_losses == 700


# --- node limits / node counts -------------------------------------------


def test_node_limit_sent_correctly(monkeypatch):
    engine = FakeUciEngine(options=default_options(), analyse_result=make_info())
    config = EngineAnalysisConfig(nodes=100000)
    evaluator = start_evaluator(monkeypatch, engine, config)

    evaluator.evaluate(PositionKey.from_board(chess.Board()), SearchMode.UNRESTRICTED)

    assert engine.analyse_calls[0].limit.nodes == 100000


def test_actual_node_count_preserved_even_when_exceeding_request(monkeypatch):
    engine = FakeUciEngine(
        options=default_options(), analyse_result=make_info(nodes=100011)
    )
    config = EngineAnalysisConfig(nodes=100000)
    evaluator = start_evaluator(monkeypatch, engine, config)

    result = evaluator.evaluate(
        PositionKey.from_board(chess.Board()), SearchMode.UNRESTRICTED
    )

    assert result.nodes == 100011


# --- PV handling -----------------------------------------------------------


def test_unrestricted_pv_handling(monkeypatch):
    pv_moves = [chess.Move.from_uci("e2e4"), chess.Move.from_uci("e7e5")]
    engine = FakeUciEngine(
        options=default_options(), analyse_result=make_info(pv=pv_moves)
    )
    evaluator = start_evaluator(monkeypatch, engine)

    result = evaluator.evaluate(
        PositionKey.from_board(chess.Board()), SearchMode.UNRESTRICTED
    )

    assert result.pv == (MoveKey("e2e4"), MoveKey("e7e5"))


def test_forced_pv_handling(monkeypatch):
    forced_move = chess.Move.from_uci("e2e4")
    engine = FakeUciEngine(
        options=default_options(),
        analyse_result=make_info(pv=[forced_move, chess.Move.from_uci("e7e5")]),
    )
    evaluator = start_evaluator(monkeypatch, engine)

    result = evaluator.evaluate(
        PositionKey.from_board(chess.Board()),
        SearchMode.FORCED_MOVE,
        MoveKey("e2e4"),
    )

    assert engine.analyse_calls[0].root_moves == [forced_move]
    assert result.pv[0] == MoveKey("e2e4")


def test_missing_pv_rejected_on_nonterminal_position(monkeypatch):
    engine = FakeUciEngine(
        options=default_options(), analyse_result=make_info(include_pv=False)
    )
    evaluator = start_evaluator(monkeypatch, engine)

    with pytest.raises(EngineEvidenceError):
        evaluator.evaluate(
            PositionKey.from_board(chess.Board()), SearchMode.UNRESTRICTED
        )


def test_missing_score_raises_capability_error(monkeypatch):
    engine = FakeUciEngine(
        options=default_options(), analyse_result=make_info(include_score=False)
    )
    evaluator = start_evaluator(monkeypatch, engine)

    with pytest.raises(EngineCapabilityError):
        evaluator.evaluate(PositionKey.from_board(chess.Board()), SearchMode.UNRESTRICTED)


def test_missing_wdl_raises_capability_error(monkeypatch):
    engine = FakeUciEngine(
        options=default_options(), analyse_result=make_info(include_wdl=False)
    )
    evaluator = start_evaluator(monkeypatch, engine)

    with pytest.raises(EngineCapabilityError):
        evaluator.evaluate(PositionKey.from_board(chess.Board()), SearchMode.UNRESTRICTED)


def test_evaluate_forced_move_rejects_illegal_move(monkeypatch):
    engine = FakeUciEngine(options=default_options(), analyse_result=make_info())
    evaluator = start_evaluator(monkeypatch, engine)

    with pytest.raises(ValueError):
        evaluator.evaluate(
            PositionKey.from_board(chess.Board()),
            SearchMode.FORCED_MOVE,
            MoveKey("e2e5"),
        )
    assert engine.analyse_calls == []


def test_evaluate_forced_move_requires_root_move(monkeypatch):
    engine = FakeUciEngine(options=default_options(), analyse_result=make_info())
    evaluator = start_evaluator(monkeypatch, engine)

    with pytest.raises(ValueError):
        evaluator.evaluate(PositionKey.from_board(chess.Board()), SearchMode.FORCED_MOVE)


def test_evaluate_unrestricted_rejects_root_move(monkeypatch):
    engine = FakeUciEngine(options=default_options(), analyse_result=make_info())
    evaluator = start_evaluator(monkeypatch, engine)

    with pytest.raises(ValueError):
        evaluator.evaluate(
            PositionKey.from_board(chess.Board()),
            SearchMode.UNRESTRICTED,
            MoveKey("e2e4"),
        )


# --- engine lifecycle -------------------------------------------------


def test_engine_not_started_at_construction(monkeypatch):
    started = []
    monkeypatch.setattr(
        chess.engine.SimpleEngine,
        "popen_uci",
        lambda command, **kwargs: started.append(command) or FakeUciEngine(),
    )

    StockfishEvaluator("/fake/path", EngineAnalysisConfig(nodes=1000))

    assert started == []


def test_evaluate_before_start_raises():
    evaluator = StockfishEvaluator("/fake/path", EngineAnalysisConfig(nodes=1000))
    with pytest.raises(RuntimeError):
        evaluator.evaluate(PositionKey.from_board(chess.Board()), SearchMode.UNRESTRICTED)


def test_identity_before_start_raises():
    evaluator = StockfishEvaluator("/fake/path", EngineAnalysisConfig(nodes=1000))
    with pytest.raises(RuntimeError):
        evaluator.identity


def test_engine_lifecycle_closes_cleanly(monkeypatch):
    engine = FakeUciEngine(options=default_options(), analyse_result=make_info())
    evaluator = start_evaluator(monkeypatch, engine)

    evaluator.close()

    assert engine.quit_called is True


def test_context_manager_closes_engine(monkeypatch):
    engine = FakeUciEngine(options=default_options(), analyse_result=make_info())
    config = EngineAnalysisConfig(nodes=1000)
    monkeypatch.setattr(
        chess.engine.SimpleEngine, "popen_uci", lambda command, **kwargs: engine
    )
    monkeypatch.setattr(engine_module, "hash_executable", lambda path: _FAKE_SHA)
    monkeypatch.setattr(engine_module, "resolve_executable", lambda reference: reference)

    with StockfishEvaluator("/fake/path", config) as evaluator:
        evaluator.evaluate(PositionKey.from_board(chess.Board()), SearchMode.UNRESTRICTED)

    assert engine.quit_called is True


def test_start_wraps_popen_failure(monkeypatch):
    def raise_oserror(command, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(chess.engine.SimpleEngine, "popen_uci", raise_oserror)
    monkeypatch.setattr(engine_module, "resolve_executable", lambda reference: reference)

    evaluator = StockfishEvaluator("/nonexistent/stockfish", EngineAnalysisConfig(nodes=1000))
    with pytest.raises(EngineStartupError):
        evaluator.start()


def test_start_requires_uci_show_wdl_capability(monkeypatch):
    engine = FakeUciEngine(options={}, analyse_result=make_info())
    monkeypatch.setattr(
        chess.engine.SimpleEngine, "popen_uci", lambda command, **kwargs: engine
    )
    monkeypatch.setattr(engine_module, "resolve_executable", lambda reference: reference)

    evaluator = StockfishEvaluator("/fake/path", EngineAnalysisConfig(nodes=1000))
    with pytest.raises(EngineCapabilityError):
        evaluator.start()
    assert engine.quit_called is True


def test_start_configures_approved_profile(monkeypatch):
    engine = FakeUciEngine(options=default_options(), analyse_result=make_info())
    config = EngineAnalysisConfig(nodes=1000, threads=4, hash_mb=256)
    evaluator = start_evaluator(monkeypatch, engine, config)

    assert engine.configure_calls == [
        {
            "Threads": 4,
            "Hash": 256,
            "Skill Level": 20,
            "UCI_LimitStrength": False,
            "UCI_ShowWDL": True,
        }
    ]
    assert "MultiPV" not in engine.configure_calls[0]
    assert evaluator.identity.name == "Fake Engine 1.0"


# --- fresh search state / transposition hash -------------------------


def test_fresh_search_state_before_each_independent_analysis(monkeypatch):
    engine = FakeUciEngine(options=default_options(), analyse_result=make_info())
    evaluator = start_evaluator(monkeypatch, engine)
    position = PositionKey.from_board(chess.Board())

    evaluator.evaluate(position, SearchMode.UNRESTRICTED)
    evaluator.evaluate(position, SearchMode.UNRESTRICTED)

    assert len(engine.analyse_calls) == 2
    game_a = engine.analyse_calls[0].game
    game_b = engine.analyse_calls[1].game
    assert game_a is not None
    assert game_b is not None
    assert game_a is not game_b


# --- EngineIdentity ------------------------------------------------------


def test_engine_identity_includes_eval_file_when_available(monkeypatch):
    engine = FakeUciEngine(options=default_options(), analyse_result=make_info())
    evaluator = start_evaluator(monkeypatch, engine)

    assert evaluator.identity.eval_file == "nn-1111111111.nnue"


def test_engine_identity_eval_file_none_when_not_reported(monkeypatch):
    options = {"UCI_ShowWDL": make_option("UCI_ShowWDL")}
    engine = FakeUciEngine(options=options, analyse_result=make_info())
    evaluator = start_evaluator(monkeypatch, engine)

    assert evaluator.identity.eval_file is None


def test_engine_identity_rejects_empty_name():
    with pytest.raises(ValueError):
        EngineIdentity(name="", executable_sha256=_FAKE_SHA)


def test_engine_identity_from_engine_requires_name():
    engine = FakeUciEngine(id_map={}, options=default_options())
    with pytest.raises(EngineCapabilityError):
        EngineIdentity.from_engine(engine, "/fake/path/to/stockfish")


def test_engine_identity_rejects_bad_sha256():
    with pytest.raises(ValueError):
        EngineIdentity(name="Stockfish", executable_sha256="not-a-digest")


def test_hash_executable_streams_and_matches_sha256(tmp_path):
    payload = b"stockfish-executable-bytes" * 100000
    binary = tmp_path / "stockfish"
    binary.write_bytes(payload)

    assert hash_executable(binary) == hashlib.sha256(payload).hexdigest()


# --- executable path resolution -------------------------------------


def test_resolve_executable_keeps_absolute_path(tmp_path):
    binary = tmp_path / "stockfish"
    binary.write_bytes(b"x")

    assert resolve_executable(str(binary)) == str(binary)


def test_resolve_executable_accepts_path_object(tmp_path):
    binary = tmp_path / "stockfish"
    binary.write_bytes(b"x")

    assert resolve_executable(binary) == str(binary)


def test_resolve_executable_resolves_relative_path_with_separator(tmp_path, monkeypatch):
    nested = tmp_path / "bin"
    nested.mkdir()
    binary = nested / "stockfish"
    binary.write_bytes(b"x")
    monkeypatch.chdir(tmp_path)

    resolved = resolve_executable(os.path.join("bin", "stockfish"))

    assert resolved == str(binary)
    assert os.path.isabs(resolved)


def test_resolve_executable_resolves_bare_command_name_through_path(
    tmp_path, monkeypatch
):
    binary = tmp_path / "stockfish"
    binary.write_bytes(b"x")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert resolve_executable("stockfish") == str(binary)


def test_resolve_executable_bare_name_not_on_path_fails_clearly(monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(EngineStartupError):
        resolve_executable("definitely-not-a-real-engine-xyz")


def test_resolve_executable_missing_absolute_path_fails_clearly(tmp_path):
    with pytest.raises(EngineStartupError):
        resolve_executable(str(tmp_path / "does-not-exist"))


def test_resolve_executable_rejects_empty_reference():
    with pytest.raises(EngineStartupError):
        resolve_executable("")


def test_start_hashes_and_launches_the_same_resolved_path(monkeypatch, tmp_path):
    # A bare command name must be resolved once, then used for both the
    # SHA-256 identity and the process launch.
    binary = tmp_path / "stockfish"
    binary.write_bytes(b"real-stockfish-bytes")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    launched_with: list[str] = []
    engine = FakeUciEngine(options=default_options(), analyse_result=make_info())
    monkeypatch.setattr(
        chess.engine.SimpleEngine,
        "popen_uci",
        lambda command, **kwargs: launched_with.append(command) or engine,
    )

    evaluator = StockfishEvaluator("stockfish", EngineAnalysisConfig(nodes=1000))
    evaluator.start()

    assert launched_with == [str(binary)]
    assert evaluator.identity.executable_sha256 == hashlib.sha256(
        b"real-stockfish-bytes"
    ).hexdigest()


def test_engine_identity_from_engine_hashes_executable_bytes(monkeypatch, tmp_path):
    binary = tmp_path / "stockfish"
    binary.write_bytes(b"abc")
    engine = FakeUciEngine(options=default_options(), analyse_result=make_info())
    monkeypatch.setattr(
        chess.engine.SimpleEngine, "popen_uci", lambda command, **kwargs: engine
    )

    evaluator = StockfishEvaluator(str(binary), EngineAnalysisConfig(nodes=1000))
    evaluator.start()

    assert evaluator.identity.executable_sha256 == hashlib.sha256(b"abc").hexdigest()


# --- fixed analysis profile -------------------------------------------


def test_analysis_profile_records_single_pv_semantics():
    assert ANALYSIS_PROFILE["single_pv"] is True
    assert ANALYSIS_PROFILE["multipv"] == 1


def test_analysis_profile_fingerprint_is_deterministic():
    assert ANALYSIS_PROFILE_FINGERPRINT == compute_analysis_profile_fingerprint(
        ANALYSIS_PROFILE
    )
    assert len(ANALYSIS_PROFILE_FINGERPRINT) == 64


def test_analysis_profile_fingerprint_changes_when_a_policy_changes():
    for field, changed in (
        ("skill_level", 19),
        ("uci_show_wdl", False),
        ("syzygy_tablebases", True),
        ("single_pv", False),
    ):
        modified = dict(ANALYSIS_PROFILE)
        modified[field] = changed
        assert (
            compute_analysis_profile_fingerprint(modified)
            != ANALYSIS_PROFILE_FINGERPRINT
        )


def test_otherwise_identical_profiles_do_not_collide():
    a = dict(ANALYSIS_PROFILE)
    b = dict(ANALYSIS_PROFILE)
    b["skill_level"] = a["skill_level"] - 1
    assert compute_analysis_profile_fingerprint(a) != compute_analysis_profile_fingerprint(b)


def test_semantic_version_identifiers_are_stable():
    assert POSITION_SEMANTICS_VERSION == "CANONICAL_POSITION_V1"
    assert ENGINE_EVIDENCE_VERSION == "ENGINE_EVIDENCE_V1"


# --- canonical request validation -----------------------------------


def _unrestricted_request(position):
    return EvaluationRequest(
        position, SearchMode.UNRESTRICTED, None, _identity(), _config()
    )


def test_canonical_four_field_position_key_is_accepted():
    board = chess.Board()
    board.push_uci("e2e4")
    request = _unrestricted_request(PositionKey.from_board(board))
    assert request.position == PositionKey.from_board(board)


def test_epd_with_hmvc_fmvn_operations_is_rejected():
    epd = chess.Board().epd() + " hmvc 0; fmvn 1;"
    with pytest.raises(RequestValidationError):
        _unrestricted_request(PositionKey(epd))


def test_structurally_invalid_board_is_rejected():
    # Two white kings -- python-chess reports the board as invalid.
    with pytest.raises(RequestValidationError):
        _unrestricted_request(PositionKey("4k3/8/8/8/8/8/8/RK2K3 w - -"))


def test_checkmate_position_is_rejected():
    board = chess.Board()
    for uci in ["f2f3", "e7e5", "g2g4", "d8h4"]:
        board.push_uci(uci)
    assert board.is_checkmate()
    with pytest.raises(RequestValidationError):
        _unrestricted_request(PositionKey.from_board(board))


def test_stalemate_position_is_rejected():
    with pytest.raises(RequestValidationError):
        _unrestricted_request(PositionKey("7k/5Q2/6K1/8/8/8/8/8 b - -"))


def test_insufficient_material_position_is_rejected():
    with pytest.raises(RequestValidationError):
        _unrestricted_request(PositionKey("7k/8/6K1/8/8/8/8/8 w - -"))


def test_canonical_validation_does_not_invent_history_draws():
    # In some historical game this could be a 50-move or threefold draw;
    # canonically it is a normal decision position with a reset clock.
    board = chess.Board("8/8/4k3/8/4K3/8/8/7R w - - 40 80")
    request = _unrestricted_request(PositionKey.from_board(board))
    canonical = reconstruct_board(request.position)
    assert canonical.halfmove_clock == 0
    assert canonical.fullmove_number == 1
    assert not canonical.is_fifty_moves()


def test_unknown_search_mode_does_not_fall_through_to_unrestricted():
    class BogusMode:
        value = "bogus"

    with pytest.raises(ValueError):
        EvaluationRequest(
            PositionKey.from_board(chess.Board()),
            BogusMode(),
            None,
            _identity(),
            _config(),
        )


def test_evaluator_rejects_unknown_search_mode_before_analysis(monkeypatch):
    engine = FakeUciEngine(options=default_options(), analyse_result=make_info())
    evaluator = start_evaluator(monkeypatch, engine)

    class BogusMode:
        value = "bogus"

    with pytest.raises(ValueError):
        evaluator.evaluate(PositionKey.from_board(chess.Board()), BogusMode())
    assert engine.analyse_calls == []


def test_invalid_request_fails_before_any_cache_lookup(monkeypatch):
    engine = FakeUciEngine(options=default_options(), analyse_result=make_info())
    evaluator = start_evaluator(monkeypatch, engine)
    board = chess.Board()
    for uci in ["f2f3", "e7e5", "g2g4", "d8h4"]:
        board.push_uci(uci)

    with pytest.raises(RequestValidationError):
        evaluator.evaluate(PositionKey.from_board(board), SearchMode.UNRESTRICTED)
    assert engine.analyse_calls == []


# --- request-aware engine-evidence validation -----------------------


def test_missing_depth_is_rejected(monkeypatch):
    engine = FakeUciEngine(
        options=default_options(), analyse_result=make_info(include_depth=False)
    )
    evaluator = start_evaluator(monkeypatch, engine)
    with pytest.raises(EngineEvidenceError):
        evaluator.evaluate(PositionKey.from_board(chess.Board()), SearchMode.UNRESTRICTED)


def test_missing_nodes_is_rejected(monkeypatch):
    engine = FakeUciEngine(
        options=default_options(), analyse_result=make_info(include_nodes=False)
    )
    evaluator = start_evaluator(monkeypatch, engine)
    with pytest.raises(EngineEvidenceError):
        evaluator.evaluate(PositionKey.from_board(chess.Board()), SearchMode.UNRESTRICTED)


@pytest.mark.parametrize("bound", ["lowerbound", "upperbound"])
def test_bound_only_result_is_rejected(monkeypatch, bound):
    engine = FakeUciEngine(
        options=default_options(), analyse_result=make_info(bound=bound)
    )
    evaluator = start_evaluator(monkeypatch, engine)
    with pytest.raises(EngineEvidenceError):
        evaluator.evaluate(PositionKey.from_board(chess.Board()), SearchMode.UNRESTRICTED)


def test_exact_line_is_preferred_over_a_trailing_bound_line(monkeypatch):
    exact = make_info(cp=31, nodes=116631)
    trailing_bound = make_info(cp=30, nodes=200277, bound="upperbound")
    engine = FakeUciEngine(
        options=default_options(), analyse_result=[exact, trailing_bound]
    )
    evaluator = start_evaluator(monkeypatch, engine)

    result = evaluator.evaluate(
        PositionKey.from_board(chess.Board()), SearchMode.UNRESTRICTED
    )
    assert result.centipawn == 31
    assert result.nodes == 116631


def test_illegal_sequential_pv_is_rejected(monkeypatch):
    pv = [chess.Move.from_uci("e2e4"), chess.Move.from_uci("e2e4")]
    engine = FakeUciEngine(options=default_options(), analyse_result=make_info(pv=pv))
    evaluator = start_evaluator(monkeypatch, engine)
    with pytest.raises(EngineEvidenceError):
        evaluator.evaluate(PositionKey.from_board(chess.Board()), SearchMode.UNRESTRICTED)


def test_forced_pv_first_move_mismatch_is_rejected(monkeypatch):
    engine = FakeUciEngine(
        options=default_options(),
        analyse_result=make_info(pv=[chess.Move.from_uci("d2d4")]),
    )
    evaluator = start_evaluator(monkeypatch, engine)
    with pytest.raises(EngineEvidenceError):
        evaluator.evaluate(
            PositionKey.from_board(chess.Board()),
            SearchMode.FORCED_MOVE,
            MoveKey("e2e4"),
        )


def test_valid_forced_pv_is_accepted(monkeypatch):
    engine = FakeUciEngine(
        options=default_options(),
        analyse_result=make_info(
            pv=[chess.Move.from_uci("e2e4"), chess.Move.from_uci("e7e5")]
        ),
    )
    evaluator = start_evaluator(monkeypatch, engine)
    result = evaluator.evaluate(
        PositionKey.from_board(chess.Board()),
        SearchMode.FORCED_MOVE,
        MoveKey("e2e4"),
    )
    assert result.pv[0] == MoveKey("e2e4")


def test_mate_zero_engine_evidence_is_rejected_as_inconsistent(monkeypatch):
    engine = FakeUciEngine(
        options=default_options(),
        analyse_result=make_info(mate=0, pv=[chess.Move.from_uci("e2e4")]),
    )
    evaluator = start_evaluator(monkeypatch, engine)
    with pytest.raises(EngineEvidenceError):
        evaluator.evaluate(PositionKey.from_board(chess.Board()), SearchMode.UNRESTRICTED)


def test_engine_wdl_not_summing_to_1000_is_rejected(monkeypatch):
    engine = FakeUciEngine(
        options=default_options(),
        analyse_result=make_info(wins=100, draws=100, losses=100),
    )
    evaluator = start_evaluator(monkeypatch, engine)
    with pytest.raises(EngineEvidenceError):
        evaluator.evaluate(PositionKey.from_board(chess.Board()), SearchMode.UNRESTRICTED)
