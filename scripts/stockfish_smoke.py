"""Manual smoke helper for the Step 4A Stockfish engine boundary.

Not part of the mandatory pytest suite -- pytest never imports or collects
this file. Run it by hand, against a caller-supplied Stockfish executable,
to sanity-check the real engine + cache path end to end:

    .venv/bin/python scripts/stockfish_smoke.py /path/to/stockfish

The executable path is always a command-line argument; no filesystem
location is hard-coded here or in production code.

It exercises:
  A. a White-to-move canonical position
  B. a Black-to-move canonical position
  C. UNRESTRICTED analysis
  D. FORCED_MOVE analysis using the first PV move of the unrestricted result
  E. L1 / L2 cache-hit behaviour (a repeat call returns an identical result)
  F. closing and reopening the SQLite cache and re-reading a persisted result
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import chess

from chess_decision_explorer.domain import PositionKey
from chess_decision_explorer.engine import (
    EngineAnalysisConfig,
    EvaluationRequest,
    SearchMode,
    StockfishEvaluator,
)
from chess_decision_explorer.engine_cache import (
    CachedEvaluator,
    InMemoryEvaluationCache,
    SQLiteEvaluationCache,
    TieredEvaluationCache,
)


def _run_position(cached_evaluator, board: chess.Board, label: str) -> PositionKey:
    position = PositionKey.from_board(board)
    mover = "White" if board.turn == chess.WHITE else "Black"
    print(f"\n=== {label}: {mover} to move ===")
    print(f"  position: {position}")

    first = cached_evaluator.evaluate(position, SearchMode.UNRESTRICTED)
    print(f"  UNRESTRICTED (engine): {first}")
    print(f"  engine-model expected score ({mover} POV): {first.expected_score:.3f}")

    second = cached_evaluator.evaluate(position, SearchMode.UNRESTRICTED)
    assert second == first, "cache hit must return an identical evaluation"
    print("  UNRESTRICTED (cache hit): identical, as expected")

    forced_move = first.pv[0]
    forced = cached_evaluator.evaluate(position, SearchMode.FORCED_MOVE, forced_move)
    assert forced.pv[0] == forced_move
    print(f"  FORCED_MOVE ({forced_move}): {forced}")

    return position


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "executable_path", help="Path to a local Stockfish UCI executable"
    )
    parser.add_argument(
        "--nodes", type=int, default=300_000, help="Node budget (default: 300000)"
    )
    args = parser.parse_args()

    config = EngineAnalysisConfig(nodes=args.nodes)

    white_board = chess.Board()
    white_board.push_uci("e2e4")
    white_board.push_uci("c7c5")  # Sicilian, White to move

    black_board = chess.Board()
    black_board.push_uci("d2d4")  # Black to move

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "smoke_cache.sqlite3"

        with StockfishEvaluator(args.executable_path, config) as evaluator:
            identity = evaluator.identity
            print("engine identity:")
            print(f"  UCI name              : {identity.name}")
            print(f"  executable SHA-256    : {identity.executable_sha256}")
            print(f"  default EvalFile      : {identity.eval_file}")

            with SQLiteEvaluationCache(db_path) as sqlite_cache:
                cache = TieredEvaluationCache(InMemoryEvaluationCache(), sqlite_cache)
                cached_evaluator = CachedEvaluator(evaluator, cache)

                white_position = _run_position(
                    cached_evaluator, white_board, "canonical position A"
                )
                _run_position(cached_evaluator, black_board, "canonical position B")

            print("\n=== reopen SQLite and re-read a persisted evaluation ===")
            with SQLiteEvaluationCache(db_path) as reopened:
                request = EvaluationRequest(
                    white_position,
                    SearchMode.UNRESTRICTED,
                    None,
                    identity,
                    config,
                )
                persisted = reopened.get(request)
                assert persisted is not None, "persisted evaluation missing after reopen"
                print(f"  reopened evaluation: {persisted}")

    print("\nsmoke helper completed successfully")


if __name__ == "__main__":
    main()
