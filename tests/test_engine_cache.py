from __future__ import annotations

import sqlite3

import chess
import pytest

import chess_decision_explorer.engine as engine_module
from chess_decision_explorer.domain import MoveKey, PositionKey
from chess_decision_explorer.engine import (
    EngineAnalysisConfig,
    EngineEvaluation,
    EngineEvidenceError,
    EngineIdentity,
    EvaluationRequest,
    SearchMode,
)
from chess_decision_explorer.engine_cache import (
    SQLITE_SCHEMA_VERSION,
    CachedEvaluator,
    CacheIntegrityError,
    CacheSchemaError,
    InMemoryEvaluationCache,
    SQLiteEvaluationCache,
    TieredEvaluationCache,
    _ALL_COLUMNS,
    _key_columns,
)

_FAKE_SHA = "a" * 64
_OTHER_SHA = "b" * 64


def make_identity(name="Stockfish 19", eval_file=None, sha=_FAKE_SHA):
    return EngineIdentity(name=name, executable_sha256=sha, eval_file=eval_file)


def make_key(
    *,
    position=None,
    search_mode=SearchMode.UNRESTRICTED,
    root_move=None,
    engine_name="Stockfish 19",
    eval_file=None,
    sha=_FAKE_SHA,
    nodes=100000,
    threads=1,
    hash_mb=64,
):
    if position is None:
        position = PositionKey.from_board(chess.Board())
    return EvaluationRequest(
        position,
        search_mode,
        root_move,
        make_identity(name=engine_name, eval_file=eval_file, sha=sha),
        EngineAnalysisConfig(nodes=nodes, threads=threads, hash_mb=hash_mb),
    )


def _raw_insert(cache, key, *, pv_json, **value_overrides):
    """Insert a row straight past SQLiteEvaluationCache.put() -- used to
    simulate on-disk corruption that the write path would reject."""
    values = dict(
        centipawn=25,
        mate=None,
        wdl_wins=500,
        wdl_draws=300,
        wdl_losses=200,
        depth=20,
        seldepth=25,
        actual_nodes=100011,
    )
    values.update(value_overrides)
    params = _key_columns(key) + (
        values["centipawn"],
        values["mate"],
        values["wdl_wins"],
        values["wdl_draws"],
        values["wdl_losses"],
        values["depth"],
        values["seldepth"],
        values["actual_nodes"],
        pv_json,
    )
    sql = (
        f"INSERT INTO evaluations ({', '.join(_ALL_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in _ALL_COLUMNS)})"
    )
    with cache._connection:
        cache._connection.execute(sql, params)


def make_evaluation(**overrides):
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
    return EngineEvaluation(**kwargs)


# --- InMemoryEvaluationCache -----------------------------------------


def test_in_memory_cache_hit():
    cache = InMemoryEvaluationCache()
    key = make_key()
    evaluation = make_evaluation()
    cache.put(key, evaluation)
    assert cache.get(key) == evaluation


def test_in_memory_cache_miss():
    cache = InMemoryEvaluationCache()
    assert cache.get(make_key()) is None


def test_in_memory_cache_instances_do_not_share_state():
    cache_a = InMemoryEvaluationCache()
    cache_b = InMemoryEvaluationCache()
    key = make_key()
    cache_a.put(key, make_evaluation())
    assert cache_b.get(key) is None


# --- SQLiteEvaluationCache: basic round trip --------------------------


def test_sqlite_cache_round_trip(tmp_path):
    db_path = tmp_path / "cache.sqlite3"
    cache = SQLiteEvaluationCache(db_path)
    key = make_key()
    evaluation = make_evaluation()

    cache.put(key, evaluation)
    result = cache.get(key)

    assert result == evaluation
    cache.close()


def test_sqlite_cache_miss(tmp_path):
    cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    assert cache.get(make_key()) is None
    cache.close()


def test_sqlite_cache_persists_after_close_and_reopen(tmp_path):
    db_path = tmp_path / "cache.sqlite3"
    key = make_key()
    evaluation = make_evaluation()

    cache_a = SQLiteEvaluationCache(db_path)
    cache_a.put(key, evaluation)
    cache_a.close()

    cache_b = SQLiteEvaluationCache(db_path)
    result = cache_b.get(key)
    cache_b.close()

    assert result == evaluation


def test_sqlite_cache_preserves_mate_and_null_centipawn(tmp_path):
    cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    key = make_key()
    evaluation = make_evaluation(centipawn=None, mate=3)

    cache.put(key, evaluation)
    result = cache.get(key)

    assert result.centipawn is None
    assert result.mate == 3
    cache.close()


def test_sqlite_put_rejects_empty_pv_for_nonterminal_position(tmp_path):
    cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    with pytest.raises(EngineEvidenceError):
        cache.put(make_key(), make_evaluation(pv=()))
    cache.close()


# --- SQLiteEvaluationCache: non-collision across key dimensions -------


def test_sqlite_unrestricted_and_forced_move_keys_do_not_collide(tmp_path):
    cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    position = PositionKey.from_board(chess.Board())
    unrestricted_key = make_key(position=position, search_mode=SearchMode.UNRESTRICTED)
    forced_key = make_key(
        position=position,
        search_mode=SearchMode.FORCED_MOVE,
        root_move=MoveKey("e2e4"),
    )

    cache.put(unrestricted_key, make_evaluation(centipawn=1))
    cache.put(forced_key, make_evaluation(centipawn=2))

    assert cache.get(unrestricted_key).centipawn == 1
    assert cache.get(forced_key).centipawn == 2
    cache.close()


def test_sqlite_different_positions_do_not_collide(tmp_path):
    cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    board = chess.Board()
    key_a = make_key(position=PositionKey.from_board(board))
    board.push_uci("e2e4")
    key_b = make_key(position=PositionKey.from_board(board))

    cache.put(key_a, make_evaluation(centipawn=1))
    cache.put(key_b, make_evaluation(centipawn=-40, pv=(MoveKey("e7e5"),)))

    assert cache.get(key_a).centipawn == 1
    assert cache.get(key_b).centipawn == -40
    cache.close()


def test_sqlite_different_root_moves_do_not_collide(tmp_path):
    cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    position = PositionKey.from_board(chess.Board())
    key_e4 = make_key(
        position=position, search_mode=SearchMode.FORCED_MOVE, root_move=MoveKey("e2e4")
    )
    key_d4 = make_key(
        position=position, search_mode=SearchMode.FORCED_MOVE, root_move=MoveKey("d2d4")
    )

    cache.put(key_e4, make_evaluation(centipawn=30, pv=(MoveKey("e2e4"),)))
    cache.put(key_d4, make_evaluation(centipawn=35, pv=(MoveKey("d2d4"),)))

    assert cache.get(key_e4).centipawn == 30
    assert cache.get(key_d4).centipawn == 35
    cache.close()


def test_sqlite_different_node_budgets_do_not_collide(tmp_path):
    cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    key_low = make_key(nodes=1000)
    key_high = make_key(nodes=1000000)

    cache.put(key_low, make_evaluation(centipawn=10))
    cache.put(key_high, make_evaluation(centipawn=15))

    assert cache.get(key_low).centipawn == 10
    assert cache.get(key_high).centipawn == 15
    cache.close()


def test_sqlite_different_engine_identities_do_not_collide(tmp_path):
    cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    key_a = make_key(engine_name="Stockfish 19")
    key_b = make_key(engine_name="Stockfish 20")

    cache.put(key_a, make_evaluation(centipawn=10))
    cache.put(key_b, make_evaluation(centipawn=99))

    assert cache.get(key_a).centipawn == 10
    assert cache.get(key_b).centipawn == 99
    cache.close()


def test_sqlite_different_eval_files_do_not_collide(tmp_path):
    cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    key_a = make_key(engine_name="Stockfish 19", eval_file="nn-a.nnue")
    key_b = make_key(engine_name="Stockfish 19", eval_file="nn-b.nnue")

    cache.put(key_a, make_evaluation(centipawn=1))
    cache.put(key_b, make_evaluation(centipawn=2))

    assert cache.get(key_a).centipawn == 1
    assert cache.get(key_b).centipawn == 2
    cache.close()


def test_sqlite_different_threads_and_hash_do_not_collide(tmp_path):
    cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    key_a = make_key(threads=1, hash_mb=64)
    key_b = make_key(threads=4, hash_mb=256)

    cache.put(key_a, make_evaluation(centipawn=1))
    cache.put(key_b, make_evaluation(centipawn=2))

    assert cache.get(key_a).centipawn == 1
    assert cache.get(key_b).centipawn == 2
    cache.close()


# --- TieredEvaluationCache ---------------------------------------------


def test_tiered_cache_memory_hit_avoids_sqlite(tmp_path, monkeypatch):
    sqlite_cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    memory_cache = InMemoryEvaluationCache()
    tiered = TieredEvaluationCache(memory_cache, sqlite_cache)
    key = make_key()
    evaluation = make_evaluation()
    memory_cache.put(key, evaluation)

    calls = []
    original_get = sqlite_cache.get
    sqlite_cache.get = lambda k: calls.append(k) or original_get(k)  # type: ignore[method-assign]

    result = tiered.get(key)

    assert result == evaluation
    assert calls == []
    sqlite_cache.close()


def test_tiered_cache_l2_hit_repopulates_l1(tmp_path):
    sqlite_cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    memory_cache = InMemoryEvaluationCache()
    key = make_key()
    evaluation = make_evaluation()
    sqlite_cache.put(key, evaluation)  # only in L2
    tiered = TieredEvaluationCache(memory_cache, sqlite_cache)

    result = tiered.get(key)

    assert result == evaluation
    assert memory_cache.get(key) == evaluation
    sqlite_cache.close()


def test_tiered_cache_miss_when_absent_from_both(tmp_path):
    sqlite_cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    tiered = TieredEvaluationCache(InMemoryEvaluationCache(), sqlite_cache)

    assert tiered.get(make_key()) is None
    sqlite_cache.close()


def test_tiered_cache_put_writes_both_layers(tmp_path):
    sqlite_cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    memory_cache = InMemoryEvaluationCache()
    tiered = TieredEvaluationCache(memory_cache, sqlite_cache)
    key = make_key()
    evaluation = make_evaluation()

    tiered.put(key, evaluation)

    assert memory_cache.get(key) == evaluation
    assert sqlite_cache.get(key) == evaluation
    sqlite_cache.close()


# --- CachedEvaluator -----------------------------------------------------


class FakeStockfishEvaluator:
    """Records evaluate() calls; used to verify CachedEvaluator's flow
    without touching a real or fake UCI engine boundary."""

    def __init__(self, identity, config, evaluation=None, raise_error=None):
        self.identity = identity
        self.config = config
        self._evaluation = evaluation
        self._raise_error = raise_error
        self.evaluate_calls = []

    def evaluate(self, position, search_mode, root_move=None):
        self.evaluate_calls.append((position, search_mode, root_move))
        if self._raise_error is not None:
            raise self._raise_error
        return self._evaluation


def test_cached_evaluator_hit_avoids_engine_invocation():
    identity = make_identity()
    config = EngineAnalysisConfig(nodes=100000)
    position = PositionKey.from_board(chess.Board())
    key = EvaluationRequest(position, SearchMode.UNRESTRICTED, None, identity, config)
    evaluation = make_evaluation()

    cache = InMemoryEvaluationCache()
    cache.put(key, evaluation)
    fake_evaluator = FakeStockfishEvaluator(identity, config)
    cached_evaluator = CachedEvaluator(fake_evaluator, cache)

    result = cached_evaluator.evaluate(position, SearchMode.UNRESTRICTED)

    assert result == evaluation
    assert fake_evaluator.evaluate_calls == []


def test_cached_evaluator_miss_invokes_engine_once_then_stores_result():
    identity = make_identity()
    config = EngineAnalysisConfig(nodes=100000)
    position = PositionKey.from_board(chess.Board())
    evaluation = make_evaluation()

    cache = InMemoryEvaluationCache()
    fake_evaluator = FakeStockfishEvaluator(identity, config, evaluation=evaluation)
    cached_evaluator = CachedEvaluator(fake_evaluator, cache)

    result = cached_evaluator.evaluate(position, SearchMode.UNRESTRICTED)

    assert result == evaluation
    assert len(fake_evaluator.evaluate_calls) == 1

    key = EvaluationRequest(position, SearchMode.UNRESTRICTED, None, identity, config)
    assert cache.get(key) == evaluation

    # A second call must be served from the cache, not the engine again.
    cached_evaluator.evaluate(position, SearchMode.UNRESTRICTED)
    assert len(fake_evaluator.evaluate_calls) == 1


def test_cached_evaluator_does_not_cache_failed_evaluation():
    identity = make_identity()
    config = EngineAnalysisConfig(nodes=100000)
    position = PositionKey.from_board(chess.Board())

    cache = InMemoryEvaluationCache()
    fake_evaluator = FakeStockfishEvaluator(
        identity, config, raise_error=RuntimeError("engine crashed")
    )
    cached_evaluator = CachedEvaluator(fake_evaluator, cache)

    with pytest.raises(RuntimeError):
        cached_evaluator.evaluate(position, SearchMode.UNRESTRICTED)

    key = EvaluationRequest(position, SearchMode.UNRESTRICTED, None, identity, config)
    assert cache.get(key) is None


def test_cached_evaluator_forced_move_uses_forced_key():
    identity = make_identity()
    config = EngineAnalysisConfig(nodes=100000)
    position = PositionKey.from_board(chess.Board())
    evaluation = make_evaluation()

    cache = InMemoryEvaluationCache()
    fake_evaluator = FakeStockfishEvaluator(identity, config, evaluation=evaluation)
    cached_evaluator = CachedEvaluator(fake_evaluator, cache)

    cached_evaluator.evaluate(position, SearchMode.FORCED_MOVE, MoveKey("e2e4"))

    forced_key = EvaluationRequest(
        position, SearchMode.FORCED_MOVE, MoveKey("e2e4"), identity, config
    )
    unrestricted_key = EvaluationRequest(
        position, SearchMode.UNRESTRICTED, None, identity, config
    )
    assert cache.get(forced_key) == evaluation
    assert cache.get(unrestricted_key) is None


def test_cached_evaluator_invalid_request_fails_before_cache_lookup():
    identity = make_identity()
    config = EngineAnalysisConfig(nodes=100000)
    board = chess.Board()
    for uci in ["f2f3", "e7e5", "g2g4", "d8h4"]:  # fool's mate
        board.push_uci(uci)
    position = PositionKey.from_board(board)

    lookups = []

    class SpyCache:
        def get(self, key):
            lookups.append(key)
            return None

        def put(self, key, evaluation):  # pragma: no cover - never reached
            raise AssertionError("put must not be reached")

    fake_evaluator = FakeStockfishEvaluator(identity, config, evaluation=make_evaluation())
    cached_evaluator = CachedEvaluator(fake_evaluator, SpyCache())

    with pytest.raises(ValueError):
        cached_evaluator.evaluate(position, SearchMode.UNRESTRICTED)
    assert lookups == []
    assert fake_evaluator.evaluate_calls == []


# --- semantic cache-key dimensions -----------------------------------


def test_l1_treats_none_and_empty_eval_file_identically():
    cache = InMemoryEvaluationCache()
    cache.put(make_key(eval_file=None), make_evaluation(centipawn=7))
    assert cache.get(make_key(eval_file="")).centipawn == 7


def test_executable_sha256_participates_in_cache_key(tmp_path):
    cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    cache.put(make_key(sha=_FAKE_SHA), make_evaluation(centipawn=10))
    assert cache.get(make_key(sha=_OTHER_SHA)) is None
    assert cache.get(make_key(sha=_FAKE_SHA)).centipawn == 10
    cache.close()


@pytest.mark.parametrize(
    "attribute, replacement",
    [
        ("POSITION_SEMANTICS_VERSION", "CANONICAL_POSITION_V2"),
        ("ENGINE_EVIDENCE_VERSION", "ENGINE_EVIDENCE_V2"),
        ("ANALYSIS_PROFILE_FINGERPRINT", "f" * 64),
    ],
)
def test_semantic_version_dimensions_participate_in_cache_key(
    tmp_path, monkeypatch, attribute, replacement
):
    cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    key = make_key()
    cache.put(key, make_evaluation())
    assert cache.get(key) is not None

    monkeypatch.setattr(engine_module, attribute, replacement)
    assert cache.get(make_key()) is None
    cache.close()


# --- persistent cache corruption fails loudly -----------------------


def test_sqlite_malformed_pv_json_fails_loudly(tmp_path):
    cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    key = make_key()
    _raw_insert(cache, key, pv_json="not-json{{")
    with pytest.raises(CacheIntegrityError):
        cache.get(key)
    cache.close()


def test_sqlite_pv_json_object_instead_of_list_fails_loudly(tmp_path):
    cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    key = make_key()
    _raw_insert(cache, key, pv_json='{"move": "e2e4"}')
    with pytest.raises(CacheIntegrityError):
        cache.get(key)
    cache.close()


def test_sqlite_illegal_cached_pv_fails_loudly(tmp_path):
    cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    key = make_key()  # start position, White to move
    _raw_insert(cache, key, pv_json='["e7e5"]')  # illegal for White
    with pytest.raises(CacheIntegrityError):
        cache.get(key)
    cache.close()


def test_sqlite_cached_forced_pv_mismatch_fails_loudly(tmp_path):
    cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    key = make_key(search_mode=SearchMode.FORCED_MOVE, root_move=MoveKey("d2d4"))
    _raw_insert(cache, key, pv_json='["e2e4"]')
    with pytest.raises(CacheIntegrityError):
        cache.get(key)
    cache.close()


# --- checked SQLite schema versioning -------------------------------


def test_fresh_sqlite_db_initializes_current_schema(tmp_path):
    db_path = tmp_path / "cache.sqlite3"
    cache = SQLiteEvaluationCache(db_path)
    version = cache._connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == SQLITE_SCHEMA_VERSION
    assert SQLITE_SCHEMA_VERSION == 2
    cache.close()


def test_supported_schema_version_reopens(tmp_path):
    db_path = tmp_path / "cache.sqlite3"
    SQLiteEvaluationCache(db_path).close()
    reopened = SQLiteEvaluationCache(db_path)  # must not raise
    reopened.close()


def test_incompatible_schema_version_is_rejected(tmp_path):
    db_path = tmp_path / "cache.sqlite3"
    SQLiteEvaluationCache(db_path).close()
    connection = sqlite3.connect(str(db_path))
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()

    with pytest.raises(CacheSchemaError):
        SQLiteEvaluationCache(db_path)


def test_unversioned_nonempty_db_is_rejected(tmp_path):
    db_path = tmp_path / "cache.sqlite3"
    connection = sqlite3.connect(str(db_path))
    connection.execute("CREATE TABLE evaluations (x INTEGER)")
    connection.commit()
    connection.close()

    with pytest.raises(CacheSchemaError):
        SQLiteEvaluationCache(db_path)


# --- immutable persistent cache entries ----------------------------


def test_sqlite_put_is_idempotent_for_the_same_evidence(tmp_path):
    cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    key = make_key()
    evaluation = make_evaluation()
    cache.put(key, evaluation)
    cache.put(key, evaluation)  # must not raise
    assert cache.get(key) == evaluation
    cache.close()


def test_sqlite_put_conflict_raises_and_keeps_original(tmp_path):
    cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    key = make_key()
    cache.put(key, make_evaluation(centipawn=10))
    with pytest.raises(CacheIntegrityError):
        cache.put(key, make_evaluation(centipawn=20))
    assert cache.get(key).centipawn == 10
    cache.close()


def test_tiered_put_conflict_leaves_l1_untouched(tmp_path):
    sqlite_cache = SQLiteEvaluationCache(tmp_path / "cache.sqlite3")
    memory_cache = InMemoryEvaluationCache()
    key = make_key()
    sqlite_cache.put(key, make_evaluation(centipawn=10))
    tiered = TieredEvaluationCache(memory_cache, sqlite_cache)

    with pytest.raises(CacheIntegrityError):
        tiered.put(key, make_evaluation(centipawn=20))

    assert memory_cache.get(key) is None
    assert sqlite_cache.get(key).centipawn == 10
    sqlite_cache.close()
