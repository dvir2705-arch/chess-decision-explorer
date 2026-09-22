"""Step 5C-lite: the personal recurring-position target set.

Synthetic only -- no external corpus, no network, no engine.
"""

from __future__ import annotations

import io
import json

import chess.pgn
import pytest

from chess_decision_explorer.aggregation import PositionIndex
from chess_decision_explorer.domain import MoveKey, PositionKey
from chess_decision_explorer.human_reference import (
    POSITION_KEY_SEMANTICS_VERSION,
    TARGET_SET_FORMAT_VERSION,
)
from chess_decision_explorer.human_reference.target import (
    PersonalMoveProfile,
    PersonalPositionProfile,
    RecurringTargetSet,
    build_target_set,
)
from chess_decision_explorer.ingestion import (
    _build_record,
    extract_player_observations,
)

START_EPD = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"


def pgn(moves: str, *, white="hero", black="rival", result="1-0") -> str:
    return "\n".join(
        [
            '[Event "Rated Blitz game"]',
            '[Site "https://example.invalid/x"]',
            '[Date "2026.01.01"]',
            f'[White "{white}"]',
            f'[Black "{black}"]',
            f'[Result "{result}"]',
            '[WhiteElo "1600"]',
            '[BlackElo "1600"]',
            '[TimeControl "300+0"]',
            "",
            f"{moves} {result}",
            "",
        ]
    )


def personal_index(texts: list[str], player: str = "hero") -> PositionIndex:
    index = PositionIndex()
    for ordinal, text in enumerate(texts):
        game = chess.pgn.read_game(io.StringIO(text))
        record = _build_record(game, f"personal:{ordinal}")
        observations = extract_player_observations(record, player)
        if observations:
            index.add_game(observations)
    return index


def target_of(texts: list[str], *, min_distinct_games=2, player="hero"):
    return build_target_set(
        personal_index(texts, player),
        cohort="blitz",
        player_label=player,
        min_distinct_games=min_distinct_games,
    )


# --- recurrence criterion ---------------------------------------------------


def test_position_in_two_distinct_games_is_recurring():
    target = target_of(
        [
            pgn("1. e4 e5 2. Nf3 Nc6 3. Bb5 a6"),
            pgn("1. e4 e5 2. Nf3 Nc6 3. Bc4 Bc5"),
        ]
    )
    profile = target.get(PositionKey(START_EPD))
    assert profile is not None
    assert profile.distinct_game_count == 2
    assert profile.occurrence_count == 2


def test_position_seen_once_is_not_recurring():
    target = target_of([pgn("1. e4 e5 2. Nf3 Nc6")])
    assert len(target) == 0
    assert PositionKey(START_EPD) not in target


def test_repeats_inside_one_personal_game_are_not_sufficient_recurrence():
    """Two occurrences, one distinct game: the position must be excluded.

    A knight shuffle returns the board to the starting position with the same
    castling rights, so the PositionKey genuinely recurs -- inside a single
    game. That is exactly the case the criterion rejects.
    """
    target = target_of([pgn("1. Nf3 Nf6 2. Ng1 Ng8 3. d4 d5")])

    index = personal_index([pgn("1. Nf3 Nf6 2. Ng1 Ng8 3. d4 d5")])
    stats = index[PositionKey(START_EPD)]
    assert stats.occurrence_count == 2
    assert stats.distinct_game_count == 1

    assert PositionKey(START_EPD) not in target
    assert len(target) == 0


def test_both_counts_are_preserved_when_a_position_qualifies():
    """A position repeated in one game AND present in a second game keeps the
    occurrence count and the distinct-game count separately."""
    target = target_of(
        [
            pgn("1. Nf3 Nf6 2. Ng1 Ng8 3. d4 d5"),
            pgn("1. e4 e5 2. Nf3 Nc6"),
        ]
    )
    profile = target.get(PositionKey(START_EPD))
    assert profile is not None
    assert profile.occurrence_count == 3
    assert profile.distinct_game_count == 2


def test_threshold_is_configurable():
    texts = [
        pgn("1. e4 e5 2. Nf3 Nc6"),
        pgn("1. e4 e5 2. Nf3 Nf6"),
    ]
    assert len(target_of(texts, min_distinct_games=2)) > 0
    assert len(target_of(texts, min_distinct_games=3)) == 0


def test_min_distinct_games_must_be_a_positive_int():
    index = personal_index([pgn("1. e4 e5")])
    with pytest.raises(ValueError, match="positive int"):
        build_target_set(index, cohort="blitz", player_label="hero", min_distinct_games=0)


def test_build_does_not_mutate_the_personal_index():
    index = personal_index(
        [pgn("1. e4 e5 2. Nf3 Nc6"), pgn("1. e4 e5 2. Nf3 Nf6")]
    )
    before = {
        position: (stats.occurrence_count, stats.distinct_game_count)
        for position, stats in index.items()
    }
    build_target_set(index, cohort="blitz", player_label="hero")
    after = {
        position: (stats.occurrence_count, stats.distinct_game_count)
        for position, stats in index.items()
    }
    assert before == after


# --- position key semantics -------------------------------------------------


def test_position_key_carries_no_cohort_rating_or_source():
    """Nothing about the personal population leaks into the key itself."""
    target = target_of(
        [pgn("1. e4 e5 2. Nf3 Nc6"), pgn("1. e4 e5 2. Nf3 Nf6")]
    )
    for position in target.positions:
        assert position.epd == position.epd.strip()
        assert "blitz" not in position.epd
        assert "hero" not in position.epd
        assert "1600" not in position.epd
    assert PositionKey(START_EPD) in target


def test_cohort_and_player_are_metadata_beside_the_keys():
    target = target_of(
        [pgn("1. e4 e5 2. Nf3 Nc6"), pgn("1. e4 e5 2. Nf3 Nf6")]
    )
    manifest = target.as_manifest_dict()
    assert manifest["personal_cohort"] == "blitz"
    assert manifest["player_label"] == "hero"
    assert manifest["min_distinct_games"] == 2
    assert manifest["position_key_semantics_version"] == POSITION_KEY_SEMANTICS_VERSION


# --- move profiles ----------------------------------------------------------


def test_moves_are_ordered_most_played_first_with_a_total_tiebreak():
    target = target_of(
        [
            pgn("1. e4 e5 2. Nf3 Nc6"),
            pgn("1. e4 e5 2. Nf3 Nf6"),
            pgn("1. d4 d5 2. c4 e6"),
        ]
    )
    profile = target.get(PositionKey(START_EPD))
    assert [p.move.uci for p in profile.moves] == ["e2e4", "d2d4"]
    assert profile.primary_move == MoveKey("e2e4")


def test_move_rate_is_occurrence_based_and_none_for_an_unplayed_move():
    target = target_of(
        [
            pgn("1. e4 e5 2. Nf3 Nc6"),
            pgn("1. e4 e5 2. Nf3 Nf6"),
            pgn("1. d4 d5 2. c4 e6"),
        ]
    )
    profile = target.get(PositionKey(START_EPD))
    assert profile.occurrence_count == 3
    assert profile.move_rate(MoveKey("e2e4")) == pytest.approx(2 / 3)
    assert profile.move_rate(MoveKey("d2d4")) == pytest.approx(1 / 3)
    assert profile.move_rate(MoveKey("g1f3")) is None


def test_ordered_profiles_is_a_total_deterministic_order():
    target = target_of(
        [
            pgn("1. e4 e5 2. Nf3 Nc6 3. Bb5 a6"),
            pgn("1. e4 e5 2. Nf3 Nc6 3. Bc4 Bc5"),
            pgn("1. e4 e5 2. Nf3 Nc6 3. d4 exd4"),
        ]
    )
    first = [p.position.epd for p in target.ordered_profiles()]
    second = [p.position.epd for p in target.ordered_profiles()]
    assert first == second
    counts = [
        (p.distinct_game_count, p.occurrence_count)
        for p in target.ordered_profiles()
    ]
    assert counts == sorted(counts, reverse=True)


# --- validation -------------------------------------------------------------


def test_move_profile_rejects_impossible_counts():
    with pytest.raises(ValueError):
        PersonalMoveProfile(MoveKey("e2e4"), occurrence_count=1, distinct_game_count=2)
    with pytest.raises(ValueError):
        PersonalMoveProfile(MoveKey("e2e4"), occurrence_count=0, distinct_game_count=0)


def test_position_profile_requires_at_least_one_move():
    with pytest.raises(ValueError, match="at least one personal move"):
        PersonalPositionProfile(
            position=PositionKey(START_EPD),
            occurrence_count=2,
            distinct_game_count=2,
            moves=(),
        )


def test_target_set_rejects_empty_cohort_or_player():
    with pytest.raises(ValueError, match="cohort"):
        RecurringTargetSet({}, 2, "  ", "hero", {})
    with pytest.raises(ValueError, match="player_label"):
        RecurringTargetSet({}, 2, "blitz", "", {})


# --- serialisation ----------------------------------------------------------


def test_target_set_round_trips_through_json(tmp_path):
    target = target_of(
        [
            pgn("1. e4 e5 2. Nf3 Nc6"),
            pgn("1. e4 e5 2. Nf3 Nf6"),
            pgn("1. e4 e5 2. d4 exd4"),
        ]
    )
    path = tmp_path / "target_set.json"
    target.write_json(path)
    restored = RecurringTargetSet.read_json(path)

    assert len(restored) == len(target)
    assert restored.cohort == target.cohort
    assert restored.player_label == target.player_label
    assert restored.min_distinct_games == target.min_distinct_games
    for profile in target.ordered_profiles():
        other = restored.get(profile.position)
        assert other is not None
        assert other.occurrence_count == profile.occurrence_count
        assert other.distinct_game_count == profile.distinct_game_count
        assert [m.move.uci for m in other.moves] == [m.move.uci for m in profile.moves]


def test_export_is_byte_stable(tmp_path):
    texts = [
        pgn("1. e4 e5 2. Nf3 Nc6"),
        pgn("1. e4 e5 2. Nf3 Nf6"),
        pgn("1. d4 d5 2. c4 e6"),
    ]
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    target_of(texts).write_json(first)
    target_of(texts).write_json(second)
    assert first.read_bytes() == second.read_bytes()


def test_loading_refuses_foreign_position_key_semantics(tmp_path):
    target = target_of(
        [pgn("1. e4 e5 2. Nf3 Nc6"), pgn("1. e4 e5 2. Nf3 Nf6")]
    )
    payload = target.as_json_dict()
    payload["position_key_semantics_version"] = "SOMETHING_ELSE_V9"
    path = tmp_path / "target_set.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="not comparable"):
        RecurringTargetSet.read_json(path)


def test_loading_refuses_a_foreign_file_format(tmp_path):
    target = target_of(
        [pgn("1. e4 e5 2. Nf3 Nc6"), pgn("1. e4 e5 2. Nf3 Nf6")]
    )
    payload = target.as_json_dict()
    payload["target_set_format_version"] = "OTHER_V2"
    path = tmp_path / "target_set.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported target set format"):
        RecurringTargetSet.read_json(path)
    assert TARGET_SET_FORMAT_VERSION != "OTHER_V2"
