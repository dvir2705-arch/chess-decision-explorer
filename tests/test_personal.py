import dataclasses

import chess
import pytest

from chess_decision_explorer import personal as personal_module
from chess_decision_explorer.domain import GameResult, MoveKey, PositionKey
from chess_decision_explorer.ingestion import GameRecord
from chess_decision_explorer.personal import (
    CohortStats,
    GameDisposition,
    PersonalAnalysisPolicy,
    PersonalGameContext,
    PositionIndex,
    TimeControlCategory,
    build_personal_analysis,
    classify_disposition,
    classify_time_control,
    resolve_personal_game_context,
)


def game_record(
    *,
    game_id: str = "src:0",
    white_player: str = "Alice",
    black_player: str = "Bob",
    white_rating: int | None = None,
    black_rating: int | None = None,
    result: GameResult = GameResult.DRAW,
    time_control: str | None = None,
    moves: tuple[str, ...] = (),
) -> GameRecord:
    return GameRecord(
        game_id=game_id,
        white_player=white_player,
        black_player=black_player,
        white_rating=white_rating,
        black_rating=black_rating,
        result=result,
        time_control=time_control,
        date=None,
        site=None,
        initial_fen=chess.Board().fen(),
        moves=tuple(chess.Move.from_uci(u) for u in moves),
    )


RUY_LOPEZ = ("e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6")
QUEENS = ("d2d4", "d7d5", "c2c4", "e7e6")

POLICY = PersonalAnalysisPolicy()


def context(
    *,
    color: chess.Color = chess.WHITE,
    rating: int | None,
    category: TimeControlCategory,
) -> PersonalGameContext:
    return PersonalGameContext(
        player_color=color,
        personal_rating=rating,
        time_control_category=category,
    )


# --- 1..9  time-control classification -------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("60", TimeControlCategory.BULLET),
        ("60+1", TimeControlCategory.BULLET),
        ("179", TimeControlCategory.BULLET),
        ("180", TimeControlCategory.BLITZ),
        ("180+2", TimeControlCategory.BLITZ),
        ("300", TimeControlCategory.BLITZ),
        ("300+5", TimeControlCategory.BLITZ),
        ("600", TimeControlCategory.RAPID),
        ("300+10", TimeControlCategory.RAPID),
        ("900+10", TimeControlCategory.RAPID),
    ],
)
def test_time_control_classification_examples(raw, expected):
    assert classify_time_control(raw) is expected


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "abc", "1/86400", "300+", "+5", "300-5", "3:00", "-"],
)
def test_missing_or_invalid_time_control_is_unknown(raw):
    assert classify_time_control(raw) is TimeControlCategory.UNKNOWN


# --- 10..17  disposition classification -----------------------------------


def test_rapid_799_is_legacy():
    ctx = context(rating=799, category=TimeControlCategory.RAPID)
    assert classify_disposition(ctx, POLICY) is GameDisposition.LEGACY


def test_rapid_800_is_core():
    ctx = context(rating=800, category=TimeControlCategory.RAPID)
    assert classify_disposition(ctx, POLICY) is GameDisposition.CORE


def test_blitz_500_is_legacy():
    ctx = context(rating=500, category=TimeControlCategory.BLITZ)
    assert classify_disposition(ctx, POLICY) is GameDisposition.LEGACY


def test_blitz_501_is_core():
    ctx = context(rating=501, category=TimeControlCategory.BLITZ)
    assert classify_disposition(ctx, POLICY) is GameDisposition.CORE


def test_bullet_599_is_legacy():
    ctx = context(rating=599, category=TimeControlCategory.BULLET)
    assert classify_disposition(ctx, POLICY) is GameDisposition.LEGACY


def test_bullet_600_is_core():
    ctx = context(rating=600, category=TimeControlCategory.BULLET)
    assert classify_disposition(ctx, POLICY) is GameDisposition.CORE


def test_missing_rating_disposition():
    ctx = context(rating=None, category=TimeControlCategory.RAPID)
    assert classify_disposition(ctx, POLICY) is GameDisposition.MISSING_RATING


def test_unknown_time_control_disposition():
    ctx = context(rating=1200, category=TimeControlCategory.UNKNOWN)
    assert classify_disposition(ctx, POLICY) is GameDisposition.UNKNOWN_TIME_CONTROL


def test_unknown_time_control_beats_missing_rating():
    ctx = context(rating=None, category=TimeControlCategory.UNKNOWN)
    assert classify_disposition(ctx, POLICY) is GameDisposition.UNKNOWN_TIME_CONTROL


# --- 18..22  personal context resolution ---------------------------------


def test_white_personal_context_selects_white_rating():
    game = game_record(
        white_player="Alice",
        black_player="Bob",
        white_rating=1500,
        black_rating=1490,
        time_control="600",
    )
    ctx = resolve_personal_game_context(game, "Alice")
    assert ctx.player_color == chess.WHITE
    assert ctx.personal_rating == 1500
    assert ctx.time_control_category is TimeControlCategory.RAPID


def test_black_personal_context_selects_black_rating():
    game = game_record(
        white_player="Alice",
        black_player="Bob",
        white_rating=1500,
        black_rating=1490,
        time_control="180",
    )
    ctx = resolve_personal_game_context(game, "Bob")
    assert ctx.player_color == chess.BLACK
    assert ctx.personal_rating == 1490
    assert ctx.time_control_category is TimeControlCategory.BLITZ


def test_player_matching_is_case_insensitive():
    game = game_record(white_player="AlIcE", black_player="Bob", white_rating=1234)
    ctx = resolve_personal_game_context(game, "alice")
    assert ctx.player_color == chess.WHITE
    assert ctx.personal_rating == 1234


def test_absent_player_is_rejected():
    game = game_record(white_player="Alice", black_player="Bob")
    with pytest.raises(ValueError):
        resolve_personal_game_context(game, "Carol")


def test_ambiguous_player_is_rejected():
    game = game_record(white_player="Sam", black_player="sam")
    with pytest.raises(ValueError):
        resolve_personal_game_context(game, "Sam")


# --- 23  LEGACY never reaches the index ---------------------------------


def test_legacy_game_never_reaches_position_index():
    game = game_record(
        white_rating=400, time_control="300", moves=RUY_LOPEZ,
        result=GameResult.WHITE_WIN,
    )
    result = build_personal_analysis([game], "Alice", POLICY)

    assert result.blitz.stats.legacy_games == 1
    assert result.blitz.stats.core_eligible_games == 0
    assert len(result.blitz.index) == 0


# --- 24  CORE with observations ----------------------------------------


def test_core_game_with_observations_updates_counters_and_index():
    game = game_record(
        white_rating=1500, time_control="600", moves=RUY_LOPEZ,
        result=GameResult.WHITE_WIN,
    )
    result = build_personal_analysis([game], "Alice", POLICY)
    stats = result.rapid.stats

    assert stats.games_seen == 1
    assert stats.core_eligible_games == 1
    assert stats.indexed_games == 1
    assert stats.zero_decision_games == 0
    assert stats.personal_decisions == 3  # three white decisions

    start = PositionKey.from_board(chess.Board())
    assert result.rapid.index[start].decisions[MoveKey("e2e4")].outcomes.wins == 1


# --- 25  CORE zero-decision game --------------------------------------


def test_core_zero_decision_game_is_not_indexed():
    # Black in a game with no moves: CORE by rating, but no personal decisions.
    game = game_record(
        black_player="Zoe", black_rating=1500, time_control="600",
        moves=(), result=GameResult.WHITE_WIN,
    )
    result = build_personal_analysis([game], "Zoe", POLICY)
    stats = result.rapid.stats

    assert stats.core_eligible_games == 1
    assert stats.zero_decision_games == 1
    assert stats.indexed_games == 0
    assert stats.personal_decisions == 0
    assert len(result.rapid.index) == 0


# --- 26  core_eligible == indexed + zero_decision ----------------------


def test_core_eligible_equals_indexed_plus_zero_decision():
    indexed = game_record(
        game_id="src:0", white_rating=1500, time_control="600",
        moves=RUY_LOPEZ, result=GameResult.WHITE_WIN,
    )
    zero = game_record(
        game_id="src:1", white_rating=1500, time_control="600",
        moves=(), result=GameResult.WHITE_WIN,
    )
    result = build_personal_analysis([indexed, zero], "Alice", POLICY)
    stats = result.rapid.stats

    assert stats.core_eligible_games == 2
    assert stats.core_eligible_games == stats.indexed_games + stats.zero_decision_games
    assert stats.games_seen == (
        stats.core_eligible_games + stats.legacy_games + stats.missing_rating_games
    )


# --- 27  separate indexes per category -------------------------------


def test_rapid_blitz_bullet_indexes_are_separate_objects():
    result = build_personal_analysis([], "Alice", POLICY)
    indexes = {id(result.rapid.index), id(result.blitz.index), id(result.bullet.index)}
    assert len(indexes) == 3


# --- 28  a Rapid position does not leak into Blitz -------------------


def test_position_does_not_leak_between_cohorts():
    rapid_game = game_record(
        game_id="src:0", white_rating=1500, time_control="600",
        moves=RUY_LOPEZ, result=GameResult.WHITE_WIN,
    )
    blitz_game = game_record(
        game_id="src:1", white_rating=1500, time_control="300",
        moves=QUEENS, result=GameResult.DRAW,
    )
    result = build_personal_analysis([rapid_game, blitz_game], "Alice", POLICY)

    board = chess.Board()
    board.push_uci("e2e4")
    board.push_uci("e7e5")
    after_e4_e5 = PositionKey.from_board(board)

    assert after_e4_e5 in result.rapid.index
    assert after_e4_e5 not in result.blitz.index

    # The start position is independently contributed by the Blitz game.
    start = PositionKey.from_board(chess.Board())
    assert start in result.rapid.index
    assert start in result.blitz.index


# --- 29  dataset totals derive from cohort stats -------------------


def test_dataset_totals_derive_from_cohort_stats():
    games = [
        # Rapid CORE with decisions
        game_record(
            game_id="src:0", white_rating=1500, time_control="600",
            moves=RUY_LOPEZ, result=GameResult.WHITE_WIN,
        ),
        # Rapid CORE zero-decision
        game_record(
            game_id="src:1", white_rating=1500, time_control="600",
            moves=(), result=GameResult.WHITE_WIN,
        ),
        # Blitz LEGACY
        game_record(
            game_id="src:2", white_rating=400, time_control="300",
            moves=QUEENS, result=GameResult.DRAW,
        ),
        # Bullet MISSING_RATING
        game_record(
            game_id="src:3", white_rating=None, time_control="60",
            moves=QUEENS, result=GameResult.DRAW,
        ),
        # UNKNOWN time control
        game_record(
            game_id="src:4", white_rating=1500, time_control="1/86400",
            moves=QUEENS, result=GameResult.DRAW,
        ),
    ]
    result = build_personal_analysis(games, "Alice", POLICY)
    totals = result.totals

    assert totals.total_games_seen == 5
    assert totals.total_core_eligible == 2
    assert totals.total_legacy == 1
    assert totals.total_missing_rating == 1
    assert totals.total_unknown_time_control == 1
    assert totals.total_zero_decision == 1
    assert totals.total_indexed == 1
    assert totals.total_personal_decisions == 3

    # Totals must equal the manual per-cohort sums.
    known = (result.rapid.stats, result.blitz.stats, result.bullet.stats)
    assert totals.total_core_eligible == sum(s.core_eligible_games for s in known)
    assert totals.total_games_seen == (
        sum(s.games_seen for s in known) + result.unknown_time_control.games_seen
    )


def test_cohort_stats_defaults_are_zero():
    stats = CohortStats()
    assert (
        stats.games_seen
        == stats.core_eligible_games
        == stats.legacy_games
        == stats.missing_rating_games
        == stats.unknown_time_control_games
        == stats.zero_decision_games
        == stats.indexed_games
        == stats.personal_decisions
        == 0
    )


# --- Policy threshold validation ---------------------------------------


def test_default_policy_thresholds_unchanged():
    policy = PersonalAnalysisPolicy()
    assert policy.rapid_min_rating == 800
    assert policy.blitz_min_rating == 501
    assert policy.bullet_min_rating == 600


def test_valid_custom_policy_is_accepted():
    policy = PersonalAnalysisPolicy(
        rapid_min_rating=0, blitz_min_rating=1000, bullet_min_rating=750
    )
    assert policy.minimum_rating(TimeControlCategory.RAPID) == 0
    assert policy.minimum_rating(TimeControlCategory.BLITZ) == 1000
    assert policy.minimum_rating(TimeControlCategory.BULLET) == 750


def test_custom_thresholds_change_disposition():
    ctx = context(rating=900, category=TimeControlCategory.BLITZ)
    # Default Blitz minimum 501 -> CORE.
    assert classify_disposition(ctx, PersonalAnalysisPolicy()) is GameDisposition.CORE
    # Stricter custom minimum 1000 -> LEGACY for the same game.
    strict = PersonalAnalysisPolicy(blitz_min_rating=1000)
    assert classify_disposition(ctx, strict) is GameDisposition.LEGACY


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rapid_min_rating": -1},
        {"blitz_min_rating": -100},
        {"bullet_min_rating": -1},
        {"rapid_min_rating": 800.5},
        {"blitz_min_rating": 501.0},
        {"bullet_min_rating": "600"},
        {"rapid_min_rating": True},
        {"blitz_min_rating": False},
        {"bullet_min_rating": None},
    ],
)
def test_invalid_policy_thresholds_are_rejected(kwargs):
    with pytest.raises(ValueError):
        PersonalAnalysisPolicy(**kwargs)


def test_zero_threshold_is_valid():
    policy = PersonalAnalysisPolicy(
        rapid_min_rating=0, blitz_min_rating=0, bullet_min_rating=0
    )
    ctx = context(rating=0, category=TimeControlCategory.RAPID)
    assert classify_disposition(ctx, policy) is GameDisposition.CORE


def test_policy_is_frozen():
    policy = PersonalAnalysisPolicy()
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.rapid_min_rating = 1234  # type: ignore[misc]


def test_policy_is_slotted():
    policy = PersonalAnalysisPolicy()
    assert not hasattr(policy, "__dict__")


# --- CORE per-game transactional accounting --------------------------


CORE_RAPID_KW = dict(white_rating=1500, time_control="600")


def test_extraction_failure_propagates_and_indexes_nothing():
    # Two legal moves then an illegal one: replay fails inside
    # extract_player_observations for this CORE game.
    bad = game_record(
        moves=("e2e4", "e7e5", "e4e5"),
        result=GameResult.WHITE_WIN,
        **CORE_RAPID_KW,
    )
    with pytest.raises(ValueError):
        build_personal_analysis([bad], "Alice", POLICY)


def test_extraction_failure_does_not_stop_a_clean_later_run():
    # Sanity: the same clean game accounts normally on its own, so the raise
    # above is caused by the bad game, not by shared state.
    good = game_record(
        moves=RUY_LOPEZ, result=GameResult.WHITE_WIN, **CORE_RAPID_KW
    )
    result = build_personal_analysis([good], "Alice", POLICY)
    assert result.rapid.stats.games_seen == 1
    assert result.rapid.stats.core_eligible_games == 1
    assert result.rapid.stats.indexed_games == 1


def test_position_index_failure_propagates(monkeypatch):
    def boom(self, observations):
        raise RuntimeError("add_game exploded")

    monkeypatch.setattr(PositionIndex, "add_game", boom)

    game = game_record(
        moves=RUY_LOPEZ, result=GameResult.WHITE_WIN, **CORE_RAPID_KW
    )
    with pytest.raises(RuntimeError, match="add_game exploded"):
        build_personal_analysis([game], "Alice", POLICY)


def test_zero_decision_core_game_never_calls_add_game(monkeypatch):
    # add_game is patched to raise; a zero-decision CORE game must still be
    # accounted, proving add_game is not on that path.
    def boom(self, observations):  # pragma: no cover - must not run
        raise RuntimeError("add_game should not be called")

    monkeypatch.setattr(PositionIndex, "add_game", boom)

    game = game_record(
        black_player="Zoe", black_rating=1500, time_control="600",
        moves=(), result=GameResult.WHITE_WIN,
    )
    result = build_personal_analysis([game], "Zoe", POLICY)
    assert result.rapid.stats.core_eligible_games == 1
    assert result.rapid.stats.zero_decision_games == 1
    assert result.rapid.stats.indexed_games == 0


def test_core_success_commits_all_four_counters_after_add_game():
    calls = []
    real_add_game = PositionIndex.add_game

    def tracking_add_game(self, observations):
        obs = list(observations)
        calls.append(len(obs))
        return real_add_game(self, obs)

    game = game_record(
        moves=RUY_LOPEZ, result=GameResult.WHITE_WIN, **CORE_RAPID_KW
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(PositionIndex, "add_game", tracking_add_game)
        result = build_personal_analysis([game], "Alice", POLICY)

    assert calls == [3]  # add_game ran once with the 3 white decisions
    stats = result.rapid.stats
    assert stats.games_seen == 1
    assert stats.core_eligible_games == 1
    assert stats.indexed_games == 1
    assert stats.personal_decisions == 3


def test_module_exposes_position_index_symbol():
    # The transactional tests monkeypatch this symbol; guard the import path.
    assert personal_module.PositionIndex is PositionIndex
