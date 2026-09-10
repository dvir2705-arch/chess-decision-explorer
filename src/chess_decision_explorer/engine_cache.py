from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Protocol

from .domain import MoveKey
from .engine import (
    EngineEvaluation,
    EngineEvidenceError,
    EvaluationRequest,
    SemanticCacheKey,
    StockfishEvaluator,
    validate_evaluation_evidence,
    validate_request,
)

SQLITE_SCHEMA_VERSION = 2
"""Current on-disk schema version for the evaluation cache. The cache is
disposable computed data, so there are no migrations: an unsupported version
is rejected and the file should be deleted and rebuilt."""


class CacheSchemaError(RuntimeError):
    """Raised when a SQLite evaluation-cache database is not compatible with
    this build's schema. The database is disposable computed data and should
    be deleted and rebuilt from the engine."""


class CacheIntegrityError(RuntimeError):
    """Raised when persistent cache content is corrupt or conflicts with a
    previously stored value. Persistent cache corruption fails loudly; it is
    never silently turned into a cache miss or a repaired value."""


class EvaluationCache(Protocol):
    """Narrow cache interface. Business logic depends only on this, never
    directly on SQLite (or any other storage) APIs."""

    def get(self, key: EvaluationRequest) -> EngineEvaluation | None: ...

    def put(self, key: EvaluationRequest, evaluation: EngineEvaluation) -> None: ...


class InMemoryEvaluationCache:
    """Simple dictionary-backed L1 cache. Keyed on the same
    :class:`SemanticCacheKey` as the persistent cache, so ``None`` and ``""``
    optional identity fields collapse identically in both layers."""

    def __init__(self) -> None:
        self._store: dict[SemanticCacheKey, EngineEvaluation] = {}

    def get(self, key: EvaluationRequest) -> EngineEvaluation | None:
        return self._store.get(SemanticCacheKey.from_request(key))

    def put(self, key: EvaluationRequest, evaluation: EngineEvaluation) -> None:
        self._store[SemanticCacheKey.from_request(key)] = evaluation


# --- SQLite L2 -----------------------------------------------------------

_SEMANTIC_KEY_COLUMNS = (
    "position_semantics_version",
    "position_epd",
    "search_mode",
    "root_move",
    "engine_executable_sha256",
    "engine_name",
    "engine_eval_file",
    "nodes",
    "threads",
    "hash_mb",
    "analysis_profile_fingerprint",
    "evidence_contract_version",
)

_VALUE_COLUMNS = (
    "centipawn",
    "mate",
    "wdl_wins",
    "wdl_draws",
    "wdl_losses",
    "depth",
    "seldepth",
    "actual_nodes",
    "pv_json",
)

_CREATE_TABLE_SQL = """
CREATE TABLE evaluations (
    position_semantics_version TEXT NOT NULL,
    position_epd TEXT NOT NULL,
    search_mode TEXT NOT NULL,
    root_move TEXT NOT NULL,
    engine_executable_sha256 TEXT NOT NULL,
    engine_name TEXT NOT NULL,
    engine_eval_file TEXT NOT NULL,
    nodes INTEGER NOT NULL,
    threads INTEGER NOT NULL,
    hash_mb INTEGER NOT NULL,
    analysis_profile_fingerprint TEXT NOT NULL,
    evidence_contract_version TEXT NOT NULL,
    centipawn INTEGER,
    mate INTEGER,
    wdl_wins INTEGER NOT NULL,
    wdl_draws INTEGER NOT NULL,
    wdl_losses INTEGER NOT NULL,
    depth INTEGER NOT NULL,
    seldepth INTEGER,
    actual_nodes INTEGER NOT NULL,
    pv_json TEXT NOT NULL,
    CHECK (search_mode IN ('unrestricted', 'forced_move')),
    CHECK (
        (search_mode = 'unrestricted' AND root_move = '')
        OR (search_mode = 'forced_move' AND root_move <> '')
    ),
    CHECK ((centipawn IS NULL) + (mate IS NULL) = 1),
    CHECK (wdl_wins >= 0 AND wdl_draws >= 0 AND wdl_losses >= 0),
    CHECK (wdl_wins + wdl_draws + wdl_losses = 1000),
    CHECK (depth >= 0),
    CHECK (actual_nodes >= 0),
    PRIMARY KEY (
        position_semantics_version, position_epd, search_mode, root_move,
        engine_executable_sha256, engine_name, engine_eval_file,
        nodes, threads, hash_mb,
        analysis_profile_fingerprint, evidence_contract_version
    )
)
"""

_ALL_COLUMNS = _SEMANTIC_KEY_COLUMNS + _VALUE_COLUMNS

_SELECT_SQL = (
    f"SELECT {', '.join(_VALUE_COLUMNS)} FROM evaluations WHERE "
    + " AND ".join(f"{column} = ?" for column in _SEMANTIC_KEY_COLUMNS)
)

_INSERT_SQL = (
    f"INSERT INTO evaluations ({', '.join(_ALL_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _ALL_COLUMNS)})"
)


def _key_columns(key: EvaluationRequest) -> tuple:
    semantic = SemanticCacheKey.from_request(key)
    return (
        semantic.position_semantics_version,
        semantic.position_epd,
        semantic.search_mode,
        semantic.root_move,
        semantic.engine_executable_sha256,
        semantic.engine_name,
        semantic.engine_eval_file,
        semantic.nodes,
        semantic.threads,
        semantic.hash_mb,
        semantic.analysis_profile_fingerprint,
        semantic.evidence_contract_version,
    )


def _reconstruct_row(row: tuple, request: EvaluationRequest) -> EngineEvaluation:
    """Rebuild and fully re-validate a stored evaluation. Any corruption --
    malformed JSON, wrong JSON shape, invalid UCI, structurally invalid
    fields, or inconsistency with the request -- raises
    :class:`CacheIntegrityError`."""
    (
        centipawn,
        mate,
        wdl_wins,
        wdl_draws,
        wdl_losses,
        depth,
        seldepth,
        actual_nodes,
        pv_json,
    ) = row

    try:
        pv_raw = json.loads(pv_json)
    except (TypeError, ValueError) as exc:
        raise CacheIntegrityError(
            f"cached principal variation is not valid JSON: {pv_json!r}"
        ) from exc
    if not isinstance(pv_raw, list) or not all(
        isinstance(move, str) for move in pv_raw
    ):
        raise CacheIntegrityError(
            "cached principal variation JSON must be a list of UCI strings, got "
            f"{pv_raw!r}"
        )

    try:
        pv = tuple(MoveKey(move) for move in pv_raw)
        evaluation = EngineEvaluation(
            centipawn=centipawn,
            mate=mate,
            wdl_wins=wdl_wins,
            wdl_draws=wdl_draws,
            wdl_losses=wdl_losses,
            depth=depth,
            seldepth=seldepth,
            nodes=actual_nodes,
            pv=pv,
        )
    except ValueError as exc:
        raise CacheIntegrityError(
            f"cached evaluation row is structurally invalid: {exc}"
        ) from exc

    try:
        board = validate_request(request)
        validate_evaluation_evidence(evaluation, request, board)
    except (EngineEvidenceError, ValueError) as exc:
        raise CacheIntegrityError(
            f"cached evaluation is inconsistent with its request: {exc}"
        ) from exc

    return evaluation


class SQLiteEvaluationCache:
    """Persistent SQLite-backed L2 cache.

    The database is a disposable computed artifact, never a source of truth:
    it holds only engine evaluations, never raw PGNs or personal games. The
    primary key spans every semantic dimension (see
    :class:`SemanticCacheKey`), completed evidence for a key is immutable,
    and corruption fails loudly rather than silently degrading to a miss.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._connection = sqlite3.connect(str(db_path))
        try:
            self._initialize_schema()
        except Exception:
            self._connection.close()
            raise

    def _initialize_schema(self) -> None:
        connection = self._connection
        version = connection.execute("PRAGMA user_version").fetchone()[0]

        if version == SQLITE_SCHEMA_VERSION:
            return

        if version != 0:
            raise CacheSchemaError(
                f"evaluation cache database is at schema version {version}, but "
                f"this build supports version {SQLITE_SCHEMA_VERSION}. The "
                "evaluation cache is disposable computed data, not a source of "
                "truth: delete the database file and let it be rebuilt from the "
                "engine."
            )

        existing_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if existing_tables:
            raise CacheSchemaError(
                "evaluation cache database has no schema version "
                "(user_version = 0) but already contains tables "
                f"{sorted(name for (name,) in existing_tables)!r}. Refusing to "
                "initialize over unknown data; delete the database file to "
                "rebuild it from the engine."
            )

        with connection:
            connection.execute(_CREATE_TABLE_SQL)
            connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SQLiteEvaluationCache":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get(self, key: EvaluationRequest) -> EngineEvaluation | None:
        row = self._connection.execute(_SELECT_SQL, _key_columns(key)).fetchone()
        if row is None:
            return None
        return _reconstruct_row(row, key)

    def put(self, key: EvaluationRequest, evaluation: EngineEvaluation) -> None:
        """Persist completed engine evidence as immutable derived data.

        A key that does not exist is inserted. A key that already exists with
        the *same* evaluation is an idempotent no-op. A key that already
        exists with a *different* evaluation raises
        :class:`CacheIntegrityError` -- the old row is never overwritten.
        """
        board = validate_request(key)
        validate_evaluation_evidence(evaluation, key, board)

        existing = self.get(key)
        if existing is not None:
            if existing == evaluation:
                return
            raise CacheIntegrityError(
                "refusing to overwrite an existing cached evaluation with a "
                f"different value for {SemanticCacheKey.from_request(key)!r}"
            )

        params = _key_columns(key) + (
            evaluation.centipawn,
            evaluation.mate,
            evaluation.wdl_wins,
            evaluation.wdl_draws,
            evaluation.wdl_losses,
            evaluation.depth,
            evaluation.seldepth,
            evaluation.nodes,
            json.dumps([move.uci for move in evaluation.pv]),
        )
        try:
            with self._connection:
                self._connection.execute(_INSERT_SQL, params)
        except sqlite3.IntegrityError as exc:
            raise CacheIntegrityError(
                f"failed to persist evaluation (constraint violation): {exc}"
            ) from exc


class TieredEvaluationCache:
    """Composes an in-memory L1 cache in front of a persistent L2 cache.

    GET: memory hit returns immediately; otherwise check L2, and on an L2
    hit repopulate L1 before returning. PUT: persist to L2 first, then
    populate L1 -- so if the persistent put raises (a conflict, or corrupt
    existing data) L1 is never populated with a value L2 does not hold.
    """

    def __init__(
        self, memory_cache: InMemoryEvaluationCache, persistent_cache: EvaluationCache
    ) -> None:
        self._memory = memory_cache
        self._persistent = persistent_cache

    def get(self, key: EvaluationRequest) -> EngineEvaluation | None:
        hit = self._memory.get(key)
        if hit is not None:
            return hit

        hit = self._persistent.get(key)
        if hit is not None:
            self._memory.put(key, hit)
        return hit

    def put(self, key: EvaluationRequest, evaluation: EngineEvaluation) -> None:
        self._persistent.put(key, evaluation)
        self._memory.put(key, evaluation)


class CachedEvaluator:
    """High-level evaluation path: check the cache, and only invoke
    Stockfish on a miss.

    The :class:`EvaluationRequest` is built (and therefore validated) before
    the cache is consulted, so an invalid request fails before any cache
    lookup. A completed evaluation is written to the cache only after a
    successful Stockfish run; a failed evaluation is never cached.
    """

    def __init__(self, evaluator: StockfishEvaluator, cache: EvaluationCache) -> None:
        self._evaluator = evaluator
        self._cache = cache

    def evaluate(
        self,
        position,
        search_mode,
        root_move: MoveKey | None = None,
    ) -> EngineEvaluation:
        key = EvaluationRequest(
            position=position,
            search_mode=search_mode,
            root_move=root_move,
            engine_identity=self._evaluator.identity,
            config=self._evaluator.config,
        )

        cached = self._cache.get(key)
        if cached is not None:
            return cached

        evaluation = self._evaluator.evaluate(position, search_mode, root_move)
        self._cache.put(key, evaluation)
        return evaluation
