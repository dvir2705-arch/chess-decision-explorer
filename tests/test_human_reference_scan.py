"""Step 5C-lite: the streaming external human-reference scan.

Synthetic only -- no external corpus, no network, no engine. Every PGN here
is built in-process.
"""

from __future__ import annotations

import io

import chess
import chess.pgn
import pytest

from chess_decision_explorer.aggregation import PositionIndex
from chess_decision_explorer.domain import MoveKey, Outcome, PositionKey
from chess_decision_explorer.human_reference.scan import (
    GAME_IDENTITY_SCHEME,
    ReferenceEligibilityFilters,
    ReferenceRejection,
    ReferenceScanner,
    RecordStream,
    ScanTermination,
    header_rejection,
    is_bot_player,
    is_rated_event,
    matched_observations,
)
from chess_decision_explorer.human_reference.target import build_target_set
from chess_decision_explorer.ingestion import (
    _build_record,
    extract_player_observations,
)
from chess_decision_explorer.personal import TimeControlCategory

# --- fixtures ---------------------------------------------------------------


def pgn(
    moves: str,
    *,
    event="Rated Blitz game",
    white="alpha",
    black="beta",
    white_elo="1600",
    black_elo="1610",
    time_control="300+0",
    result="1-0",
    extra: tuple[str, ...] = (),
) -> str:
    headers = [
        f'[Event "{event}"]',
        '[Site "https://example.invalid/x"]',
        '[Date "2026.01.01"]',
        f'[White "{white}"]',
        f'[Black "{black}"]',
        f'[Result "{result}"]',
    ]
    if white_elo is not None:
        headers.append(f'[WhiteElo "{white_elo}"]')
    if black_elo is not None:
        headers.append(f'[BlackElo "{black_elo}"]')
    if time_control is not None:
        headers.append(f'[TimeControl "{time_control}"]')
    headers.extend(extra)
    return "\n".join(headers + ["", f"{moves} {result}", ""])


def personal_pgn(moves: str, *, white: str, black: str, result="1-0") -> str:
    return pgn(moves, white=white, black=black, result=result)


def key_after(*ucis: str) -> PositionKey:
    board = chess.Board()
    for uci in ucis:
        board.push_uci(uci)
    return PositionKey.from_board(board)


START = key_after()
AFTER_E4 = key_after("e2e4")
AFTER_E4_E5 = key_after("e2e4", "e7e5")


def hero_target_set(min_distinct_games: int = 2):
    """Recurring positions for a player who is White in two games and Black
    in two others.

    White-to-move recurrences come from the games hero played as White; the
    Black-to-move recurrence comes from the games hero played as Black. Both
    sides are needed to test actor-relative outcomes.
    """
    games = [
        # hero as White
        ("hero", "rival", "1. e4 e5 2. Nf3 Nc6"),
        ("hero", "rival", "1. e4 e5 2. Nf3 Nf6"),
        # hero as Black
        ("rival", "hero", "1. e4 e5 2. Nf3 Nc6"),
        ("rival", "hero", "1. e4 c5 2. Nf3 d6"),
    ]
    index = PositionIndex()
    for ordinal, (white, black, moves) in enumerate(games):
        game = chess.pgn.read_game(
            io.StringIO(personal_pgn(moves, white=white, black=black))
        )
        record = _build_record(game, f"personal:{ordinal}")
        observations = extract_player_observations(record, "hero")
        if observations:
            index.add_game(observations)
    return build_target_set(
        index,
        cohort="blitz",
        player_label="hero",
        min_distinct_games=min_distinct_games,
    )


def black_only_target_set():
    """Recurring positions for a player who is always Black.

    Every standard game begins from the start position, so a target set built
    from games the player had as White always contains it and no reference
    game can miss it. A Black-only target set has no such universal member,
    which is what makes "a game that matches nothing" testable at all.
    """
    games = [
        ("rival", "hero", "1. e4 e5 2. Nf3 Nc6"),
        ("rival", "hero", "1. e4 e5 2. Nf3 Nf6"),
    ]
    index = PositionIndex()
    for ordinal, (white, black, moves) in enumerate(games):
        game = chess.pgn.read_game(
            io.StringIO(personal_pgn(moves, white=white, black=black))
        )
        record = _build_record(game, f"personal:{ordinal}")
        index.add_game(extract_player_observations(record, "hero"))
    return build_target_set(
        index, cohort="blitz", player_label="hero", min_distinct_games=2
    )


def scan_texts(texts, target=None, *, target_games=100, **kwargs):
    target = target if target is not None else hero_target_set()
    scanner = ReferenceScanner(
        target_set=target,
        source_id="synthetic",
        target_games=target_games,
        **kwargs,
    )
    return scanner, scanner.scan(io.StringIO("\n".join(texts)))


def test_the_fixture_target_set_is_what_the_tests_assume():
    target = hero_target_set()
    assert set(target.positions) == {START, AFTER_E4, AFTER_E4_E5}
    assert START.epd.split()[1] == "w"
    assert AFTER_E4.epd.split()[1] == "b"
    assert AFTER_E4_E5.epd.split()[1] == "w"


# --- exact matching and discarding -----------------------------------------


def test_matching_position_is_retained_with_the_move_that_was_played():
    _, result = scan_texts([pgn("1. e4 e5 2. Nf3 Nc6 3. Bb5 a6")])
    stats = result.index[START]
    assert stats.occurrence_count == 1
    assert stats.distinct_game_count == 1
    assert set(stats.decisions) == {MoveKey("e2e4")}


def test_non_target_positions_are_discarded():
    """A reference game that never reaches a target position contributes
    nothing at all -- not even an empty entry.

    Uses the Black-only target set: a target set containing the start
    position is reached by every standard game by construction, so it cannot
    demonstrate discarding.
    """
    _, result = scan_texts(
        [pgn("1. d4 d5 2. c4 e6 3. Nc3 Nf6")], black_only_target_set()
    )
    assert len(result.index) == 0
    assert result.counters.games_accepted == 1
    assert result.counters.games_with_match == 0
    assert result.counters.decisions_examined == 6
    assert result.counters.decisions_matched == 0


def test_only_the_matching_plies_of_a_matching_game_are_kept():
    _, result = scan_texts([pgn("1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6")])
    assert set(dict(result.index.items())) == {START, AFTER_E4, AFTER_E4_E5}
    assert result.counters.decisions_examined == 8
    assert result.counters.decisions_matched == 3


def test_aggregate_never_exceeds_the_target_set():
    """The architectural invariant: no database of every corpus position.

    Fifty structurally different games are scanned; the aggregate stays
    bounded by the three target positions.
    """
    openings = [
        "1. d4 d5 2. c4", "1. c4 e5 2. Nc3", "1. Nf3 d5 2. g3",
        "1. e4 c5 2. Nf3", "1. e4 e6 2. d4", "1. e4 c6 2. d4",
        "1. b3 e5 2. Bb2", "1. f4 d5 2. Nf3", "1. g3 d5 2. Bg2",
        "1. e4 e5 2. Nf3 Nc6",
    ]
    texts = [
        pgn(openings[index % len(openings)], result="1-0" if index % 2 else "0-1")
        for index in range(50)
    ]
    scanner, result = scan_texts(texts)
    assert result.counters.games_accepted == 50
    assert len(result.index) <= len(scanner.target_set)
    assert len(result.index) == 3
    assert result.counters.decisions_examined > result.counters.decisions_matched


def test_scanner_declares_that_it_materialises_nothing():
    scanner, _ = scan_texts([pgn("1. e4 e5")])
    declaration = scanner.as_manifest_dict()
    assert declaration["materialises_corpus"] is False
    assert declaration["retains_external_game_ids"] is False
    assert declaration["game_identity_scheme"] == GAME_IDENTITY_SCHEME


# --- one-pass streaming -----------------------------------------------------


def test_scan_stops_reading_once_the_target_is_reached():
    """One pass, and not one record more than needed."""
    texts = [pgn("1. e4 e5 2. Nf3 Nc6") for _ in range(10)]
    handle = io.StringIO("\n".join(texts))
    scanner = ReferenceScanner(
        target_set=hero_target_set(), source_id="synthetic", target_games=2
    )
    result = scanner.scan(handle)

    assert result.counters.games_accepted == 2
    assert result.termination is ScanTermination.TARGET_REACHED
    assert result.target_met is True
    # The rest of the stream was never read.
    assert handle.read().strip() != ""
    assert result.counters.records_scanned == 2


def test_each_record_is_read_exactly_once():
    class CountingHandle(io.StringIO):
        def __init__(self, text):
            super().__init__(text)
            self.readline_calls = 0

        def readline(self, *args, **kwargs):
            self.readline_calls += 1
            return super().readline(*args, **kwargs)

    text = "\n".join(pgn("1. e4 e5 2. Nf3 Nc6") for _ in range(5))
    handle = CountingHandle(text)
    scanner = ReferenceScanner(
        target_set=hero_target_set(), source_id="synthetic", target_games=5
    )
    result = scanner.scan(handle)
    assert result.counters.records_scanned == 5
    # A single forward pass: roughly one readline per physical line, never a
    # multiple of the corpus (which a re-read would produce).
    assert handle.readline_calls <= text.count("\n") + 10


# --- the target counts eligible games, not records --------------------------


def test_target_counts_eligible_games_not_raw_records():
    texts = []
    for index in range(6):
        texts.append(pgn("1. d4 d5", event="Casual Blitz game"))  # ineligible
        texts.append(pgn("1. e4 e5 2. Nf3 Nc6"))  # eligible
    handle = io.StringIO("\n".join(texts))
    scanner = ReferenceScanner(
        target_set=hero_target_set(), source_id="synthetic", target_games=3
    )
    result = scanner.scan(handle)

    assert result.counters.games_accepted == 3
    assert result.counters.records_scanned == 6
    assert result.counters.rejections[ReferenceRejection.NOT_RATED.value] == 3
    assert result.target_met is True


def test_input_exhaustion_before_target_is_reported_not_hidden():
    texts = [pgn("1. e4 e5 2. Nf3 Nc6") for _ in range(4)]
    _, result = scan_texts(texts, target_games=10)

    assert result.counters.games_accepted == 4
    assert result.target_met is False
    assert result.shortfall == 6
    assert result.termination is ScanTermination.STREAM_EXHAUSTED
    payload = result.as_dict()
    assert payload["target_eligible_games"] == 10
    assert payload["eligible_games_accepted"] == 4
    assert payload["target_met"] is False


def test_scan_limit_is_a_declared_prefix():
    texts = [pgn("1. e4 e5 2. Nf3 Nc6") for _ in range(10)]
    _, result = scan_texts(texts, target_games=10, max_records_scanned=3)
    assert result.termination is ScanTermination.SCAN_LIMIT_REACHED
    assert result.termination.is_declared_prefix is True
    assert result.counters.records_scanned == 3
    assert result.target_met is False


def test_stream_exhaustion_is_not_a_declared_prefix():
    _, result = scan_texts([pgn("1. e4 e5")], target_games=10)
    assert result.termination.is_declared_prefix is False


def test_skip_records_consumes_ordinals_without_accepting():
    texts = [pgn("1. e4 e5 2. Nf3 Nc6") for _ in range(5)]
    _, result = scan_texts(texts, target_games=10, skip_records=2)
    assert result.counters.records_scanned == 5
    assert result.counters.games_accepted == 3
    assert result.counters.rejections[ReferenceRejection.SKIPPED_BY_OFFSET.value] == 2


# --- eligibility filters ----------------------------------------------------


def test_rapid_and_blitz_are_eligible_bullet_is_not():
    texts = [
        pgn("1. e4 e5 2. Nf3 Nc6", time_control="300+0"),  # blitz
        pgn("1. e4 e5 2. Nf3 Nc6", time_control="600+0"),  # rapid
        pgn("1. e4 e5 2. Nf3 Nc6", time_control="60+0"),  # bullet
        pgn("1. e4 e5 2. Nf3 Nc6", time_control="1/86400"),  # unknown
    ]
    _, result = scan_texts(texts)
    assert result.counters.games_accepted == 2
    assert (
        result.counters.rejections[
            ReferenceRejection.TIME_CONTROL_NOT_ELIGIBLE.value
        ]
        == 2
    )


def test_eligible_categories_are_configurable():
    texts = [pgn("1. e4 e5 2. Nf3 Nc6", time_control="60+0")]
    filters = ReferenceEligibilityFilters(
        eligible_categories=(TimeControlCategory.BULLET,)
    )
    _, result = scan_texts(texts, filters=filters)
    assert result.counters.games_accepted == 1


def test_unrated_games_are_rejected_by_default():
    texts = [
        pgn("1. e4 e5 2. Nf3 Nc6", event="Casual Blitz game"),
        pgn("1. e4 e5 2. Nf3 Nc6", event="Rated Blitz game"),
        pgn("1. e4 e5 2. Nf3 Nc6", event=""),
    ]
    _, result = scan_texts(texts)
    assert result.counters.games_accepted == 1
    assert result.counters.rejections[ReferenceRejection.NOT_RATED.value] == 2


def test_require_rated_can_be_switched_off():
    texts = [pgn("1. e4 e5 2. Nf3 Nc6", event="Casual Blitz game")]
    filters = ReferenceEligibilityFilters(require_rated=False)
    _, result = scan_texts(texts, filters=filters)
    assert result.counters.games_accepted == 1


def test_incomplete_results_are_rejected():
    texts = [
        pgn("1. e4 e5 2. Nf3 Nc6", result="*"),
        pgn("1. e4 e5 2. Nf3 Nc6", result="1-0"),
    ]
    _, result = scan_texts(texts)
    assert result.counters.games_accepted == 1
    assert (
        result.counters.rejections[ReferenceRejection.RESULT_NOT_COMPLETED.value] == 1
    )


def test_non_standard_variants_are_rejected():
    texts = [
        pgn("1. e4 e5 2. Nf3 Nc6", extra=('[Variant "Crazyhouse"]',)),
        pgn("1. e4 e5 2. Nf3 Nc6", extra=('[Variant "Chess960"]',)),
        pgn("1. e4 e5 2. Nf3 Nc6", extra=('[Variant "Totally Unknown Variant"]',)),
        pgn("1. e4 e5 2. Nf3 Nc6"),
    ]
    _, result = scan_texts(texts)
    assert result.counters.games_accepted == 1
    assert (
        result.counters.rejections[ReferenceRejection.VARIANT_NOT_STANDARD.value] == 3
    )


def test_rating_band_is_applied_to_both_players():
    texts = [
        pgn("1. e4 e5 2. Nf3 Nc6", white_elo="1600", black_elo="1610"),
        pgn("1. e4 e5 2. Nf3 Nc6", white_elo="900", black_elo="1610"),
        pgn("1. e4 e5 2. Nf3 Nc6", white_elo="1600", black_elo="2400"),
    ]
    filters = ReferenceEligibilityFilters(min_rating=1000, max_rating=2000)
    _, result = scan_texts(texts, filters=filters)
    assert result.counters.games_accepted == 1
    assert result.counters.rejections[ReferenceRejection.RATING_OUT_OF_BAND.value] == 2


def test_missing_ratings_pass_by_default_and_can_be_required():
    texts = [pgn("1. e4 e5 2. Nf3 Nc6", white_elo=None, black_elo=None)]
    _, permissive = scan_texts(texts)
    assert permissive.counters.games_accepted == 1

    _, strict = scan_texts(
        texts, filters=ReferenceEligibilityFilters(require_both_ratings=True)
    )
    assert strict.counters.games_accepted == 0
    assert strict.counters.rejections[ReferenceRejection.RATING_MISSING.value] == 1


def test_min_plies_filter():
    texts = [pgn("1. e4 e5", result="1-0"), pgn("1. e4 e5 2. Nf3 Nc6")]
    filters = ReferenceEligibilityFilters(min_plies=4)
    _, result = scan_texts(texts, filters=filters)
    assert result.counters.games_accepted == 1
    assert result.counters.rejections[ReferenceRejection.TOO_FEW_PLIES.value] == 1


def test_termination_exclusion_is_off_by_default_and_configurable():
    texts = [pgn("1. e4 e5 2. Nf3 Nc6", extra=('[Termination "Abandoned"]',))]
    _, default_run = scan_texts(texts)
    assert default_run.counters.games_accepted == 1

    filters = ReferenceEligibilityFilters(excluded_terminations=frozenset({"abandoned"}))
    _, filtered = scan_texts(texts, filters=filters)
    assert filtered.counters.games_accepted == 0
    assert (
        filtered.counters.rejections[ReferenceRejection.TERMINATION_EXCLUDED.value] == 1
    )


# --- human players only (Lichess Bot API accounts) --------------------------


def test_a_white_bot_is_rejected():
    texts = [
        pgn("1. e4 e5 2. Nf3 Nc6", extra=('[WhiteTitle "BOT"]',)),
        pgn("1. e4 e5 2. Nf3 Nc6"),
    ]
    _, result = scan_texts(texts)
    assert result.counters.games_accepted == 1
    assert result.counters.rejections[ReferenceRejection.BOT_PLAYER.value] == 1


def test_a_black_bot_is_rejected():
    texts = [
        pgn("1. e4 e5 2. Nf3 Nc6", extra=('[BlackTitle "BOT"]',)),
        pgn("1. e4 e5 2. Nf3 Nc6"),
    ]
    _, result = scan_texts(texts)
    assert result.counters.games_accepted == 1
    assert result.counters.rejections[ReferenceRejection.BOT_PLAYER.value] == 1


def test_a_bot_on_either_side_contributes_no_statistics():
    """A rejected bot game must not reach the aggregate at all."""
    texts = [
        pgn("1. e4 e5 2. Nf3 Nc6", extra=('[WhiteTitle "BOT"]',)),
        pgn("1. e4 e5 2. Nf3 Nc6", extra=('[BlackTitle "BOT"]',)),
    ]
    _, result = scan_texts(texts)
    assert result.counters.games_accepted == 0
    assert result.counters.decisions_examined == 0
    assert len(result.index) == 0
    assert result.counters.rejections[ReferenceRejection.BOT_PLAYER.value] == 2


def test_titled_human_players_are_not_rejected():
    """The rule targets the BOT title only. Every other title marks a titled
    HUMAN, who belongs in a human reference cohort."""
    titles = ("GM", "IM", "FM", "CM", "NM", "WGM", "WIM", "WFM", "LM")
    texts = [
        pgn("1. e4 e5 2. Nf3 Nc6", extra=(f'[WhiteTitle "{title}"]',))
        for title in titles
    ] + [
        pgn("1. e4 e5 2. Nf3 Nc6", extra=(f'[BlackTitle "{title}"]',))
        for title in titles
    ]
    _, result = scan_texts(texts)
    assert result.counters.games_accepted == len(titles) * 2
    assert result.counters.rejections[ReferenceRejection.BOT_PLAYER.value] == 0


def test_untitled_players_are_accepted():
    _, result = scan_texts([pgn("1. e4 e5 2. Nf3 Nc6")])
    assert result.counters.games_accepted == 1
    assert result.counters.rejections[ReferenceRejection.BOT_PLAYER.value] == 0


def test_bot_title_match_is_case_insensitive_and_exact():
    accepted_titles = ("BOTANIST", "ROBOT", "B O T")
    texts = [
        pgn("1. e4 e5 2. Nf3 Nc6", extra=('[WhiteTitle "bot"]',)),
        pgn("1. e4 e5 2. Nf3 Nc6", extra=('[WhiteTitle " BOT "]',)),
    ] + [
        pgn("1. e4 e5 2. Nf3 Nc6", extra=(f'[BlackTitle "{title}"]',))
        for title in accepted_titles
    ]
    _, result = scan_texts(texts)
    # The two BOT spellings are rejected; the look-alikes are not bots.
    assert result.counters.rejections[ReferenceRejection.BOT_PLAYER.value] == 2
    assert result.counters.games_accepted == len(accepted_titles)


def test_bot_rejection_can_be_switched_off():
    texts = [pgn("1. e4 e5 2. Nf3 Nc6", extra=('[WhiteTitle "BOT"]',))]
    filters = ReferenceEligibilityFilters(require_human_players=False)
    _, result = scan_texts(texts, filters=filters)
    assert result.counters.games_accepted == 1
    assert result.counters.rejections[ReferenceRejection.BOT_PLAYER.value] == 0


def test_is_bot_player_helper():
    def headers(**kwargs):
        game = chess.pgn.read_game(
            io.StringIO(
                pgn(
                    "1. e4 e5",
                    extra=tuple(f'[{k} "{v}"]' for k, v in kwargs.items()),
                )
            )
        )
        return game.headers

    assert is_bot_player(headers(WhiteTitle="BOT")) is True
    assert is_bot_player(headers(BlackTitle="BOT")) is True
    assert is_bot_player(headers(WhiteTitle="bot")) is True
    assert is_bot_player(headers(WhiteTitle="GM", BlackTitle="IM")) is False
    assert is_bot_player(headers()) is False


def test_human_only_rule_is_declared_in_the_filter_manifest():
    declaration = ReferenceEligibilityFilters().as_manifest_dict()
    assert declaration["require_human_players"] is True
    assert "BOT" in declaration["bot_rule"]
    assert "titled HUMAN" in declaration["bot_rule"]


def test_is_rated_event_is_word_level():
    assert is_rated_event("Rated Blitz game") is True
    assert is_rated_event("Rated-Blitz tournament https://x") is True
    assert is_rated_event("Casual Blitz game") is False
    assert is_rated_event("Unrated Blitz game") is False
    assert is_rated_event(None) is False
    assert is_rated_event("") is False


def test_rated_rule_agrees_with_the_step_4c0_scanner():
    """The rule is restated in the production module rather than imported
    from the experimental package; this pins the two together."""
    from chess_decision_explorer.experimental.c0.sampling import (
        is_rated_event as c0_is_rated_event,
    )

    for event in (
        "Rated Blitz game",
        "Casual Blitz game",
        "Unrated Blitz game",
        "Rated-Rapid tournament",
        "rated",
        "",
        None,
    ):
        assert is_rated_event(event) == c0_is_rated_event(event)


# --- actor-relative outcomes ------------------------------------------------


def test_white_win_is_a_win_for_white_to_move_and_a_loss_for_black_to_move():
    _, result = scan_texts([pgn("1. e4 e5 2. Nf3 Nc6", result="1-0")])

    white_root = result.index[START].decisions[MoveKey("e2e4")]
    assert (white_root.outcomes.wins, white_root.outcomes.draws, white_root.outcomes.losses) == (1, 0, 0)

    black_root = result.index[AFTER_E4].decisions[MoveKey("e7e5")]
    assert (black_root.outcomes.wins, black_root.outcomes.draws, black_root.outcomes.losses) == (0, 0, 1)


def test_black_win_is_a_win_for_black_to_move_and_a_loss_for_white_to_move():
    _, result = scan_texts([pgn("1. e4 e5 2. Nf3 Nc6", result="0-1")])

    white_root = result.index[START].decisions[MoveKey("e2e4")]
    assert (white_root.outcomes.wins, white_root.outcomes.losses) == (0, 1)

    black_root = result.index[AFTER_E4].decisions[MoveKey("e7e5")]
    assert (black_root.outcomes.wins, black_root.outcomes.losses) == (1, 0)


def test_draw_is_a_draw_for_both_sides_to_move():
    _, result = scan_texts([pgn("1. e4 e5 2. Nf3 Nc6", result="1/2-1/2")])

    white_root = result.index[START].decisions[MoveKey("e2e4")]
    black_root = result.index[AFTER_E4].decisions[MoveKey("e7e5")]
    assert (white_root.outcomes.wins, white_root.outcomes.draws, white_root.outcomes.losses) == (0, 1, 0)
    assert (black_root.outcomes.wins, black_root.outcomes.draws, black_root.outcomes.losses) == (0, 1, 0)


def test_statistics_are_not_white_centric():
    """Over a corpus of White wins, the Black-to-move position must show
    losses, not wins. A White-centric bug would show the opposite."""
    texts = [pgn("1. e4 e5 2. Nf3 Nc6", result="1-0") for _ in range(5)]
    _, result = scan_texts(texts)

    black_stats = result.index[AFTER_E4]
    assert black_stats.outcomes.wins == 0
    assert black_stats.outcomes.losses == 5
    assert result.index[START].outcomes.wins == 5


def test_matched_observations_uses_the_side_to_move_as_the_actor():
    game = chess.pgn.read_game(io.StringIO(pgn("1. e4 e5 2. Nf3 Nc6", result="1-0")))
    record = _build_record(game, "synthetic:0")
    observations, examined, matched = matched_observations(record, hero_target_set())

    assert examined == 4
    assert matched == 3
    by_move = {obs.decision.move.uci: obs.outcome for obs in observations}
    assert by_move["e2e4"] is Outcome.WIN
    assert by_move["e7e5"] is Outcome.LOSS
    assert by_move["g1f3"] is Outcome.WIN


# --- occurrence vs distinct-game counting -----------------------------------


def test_occurrence_and_distinct_game_counts_differ_across_games():
    texts = [pgn("1. e4 e5 2. Nf3 Nc6") for _ in range(3)]
    _, result = scan_texts(texts)
    stats = result.index[START]
    assert stats.occurrence_count == 3
    assert stats.distinct_game_count == 3


def test_a_position_repeated_inside_one_reference_game_counts_once_per_game():
    """Two occurrences, one distinct game, one recorded outcome."""
    text = pgn("1. Nf3 Nf6 2. Ng1 Ng8 3. e4 e5 4. Nf3 Nc6", result="1-0")
    _, result = scan_texts([text])

    stats = result.index[START]
    assert stats.occurrence_count == 2
    assert stats.distinct_game_count == 1
    assert stats.outcomes.game_count == 1
    assert stats.outcomes.wins == 1
    assert set(stats.decisions) == {MoveKey("g1f3"), MoveKey("e2e4")}
    for move in (MoveKey("g1f3"), MoveKey("e2e4")):
        decision = stats.decisions[move]
        assert decision.occurrence_count == 1
        assert decision.distinct_game_count == 1

    assert result.counters.decisions_matched == 4


def test_a_game_ending_right_after_a_move_records_no_decision_there():
    """"Positions before moves" means a game's FINAL position is never a
    decision position.

    Observed on real Lichess data: 674 reference games played 1.e4 from the
    start position, but only 673 produced a decision at the position after
    1.e4 -- one game ended immediately after White's first move. The
    one-game difference is correct, not a counting bug.
    """
    texts = [
        pgn("1. e4", result="1-0"),  # Black never moved
        pgn("1. e4 e5 2. Nf3 Nc6", result="1-0"),
    ]
    _, result = scan_texts(texts)

    start = result.index[START]
    assert start.decisions[MoveKey("e2e4")].distinct_game_count == 2
    # Only the game that continued has a decision at the successor position.
    assert result.index[AFTER_E4].distinct_game_count == 1

    # The truncated game is still a fully accepted eligible game.
    assert result.counters.games_accepted == 2


def test_transposition_merges_move_orders_into_one_position():
    """Different move orders reaching the same position are one position.

    Consequence, seen on real data: a successor position can hold MORE games
    than any single in-edge supplies, because other move orders also feed it.
    """
    target = hero_target_set()
    # AFTER_E4_E5 is reached here by 1.e4 e5 and by 1.Nf3 e5 2.e4 -- wait,
    # use two genuine orders into the same key.
    texts = [
        pgn("1. e4 e5 2. Nf3 Nc6", result="1-0"),
        pgn("1. Nf3 Nc6 2. e4 e5", result="1-0"),
    ]
    _, result = scan_texts(texts, target)

    after_e4_e5_nf3_nc6 = key_after("e2e4", "e7e5", "g1f3", "b8c6")
    stats = result.index.get(after_e4_e5_nf3_nc6)
    if stats is not None:  # only if that position is in the target set
        assert stats.distinct_game_count == 2

    # The decisive, order-independent check: both games reach the same key.
    board_a = chess.Board()
    for uci in ("e2e4", "e7e5", "g1f3", "b8c6"):
        board_a.push_uci(uci)
    board_b = chess.Board()
    for uci in ("g1f3", "b8c6", "e2e4", "e7e5"):
        board_b.push_uci(uci)
    assert PositionKey.from_board(board_a) == PositionKey.from_board(board_b)


def test_repeated_position_across_many_games_does_not_inflate_game_counts():
    texts = [
        pgn("1. Nf3 Nf6 2. Ng1 Ng8 3. e4 e5 4. Nf3 Nc6", result="1-0")
        for _ in range(4)
    ]
    _, result = scan_texts(texts)
    stats = result.index[START]
    assert stats.occurrence_count == 8
    assert stats.distinct_game_count == 4
    assert stats.outcomes.game_count == 4


def test_no_external_game_ids_are_retained():
    """`PositionIndex` keeps only counts, so a scan cannot accumulate a set
    of corpus game ids."""
    texts = [pgn("1. e4 e5 2. Nf3 Nc6") for _ in range(3)]
    _, result = scan_texts(texts)
    for _, stats in result.index.items():
        assert not any(
            "synthetic" in repr(value)
            for value in (stats.outcomes, stats.decisions)
        )


# --- counters and coverage --------------------------------------------------


def test_counters_report_match_rate_and_coverage():
    texts = [pgn("1. e4 e5 2. Nf3 Nc6"), pgn("1. d4 d5 2. c4 e6")]
    _, result = scan_texts(texts)
    counters = result.counters
    # 4 plies each. The e4 game matches all three target positions; the d4
    # game matches only the start position, which every game passes through.
    assert counters.decisions_examined == 8
    assert counters.decisions_matched == 4
    assert counters.match_rate == pytest.approx(4 / 8)
    assert result.matched_positions == 3
    assert result.position_coverage_rate == pytest.approx(1.0)


def test_match_rate_is_none_when_nothing_was_examined():
    _, result = scan_texts([pgn("1. e4 e5", event="Casual Blitz game")])
    assert result.counters.decisions_examined == 0
    assert result.counters.match_rate is None
    assert result.as_dict()["counters"]["match_rate"] is None


def test_coverage_counts_only_positions_the_corpus_reached():
    _, result = scan_texts([pgn("1. e4 c5 2. Nf3 d6")])
    assert result.matched_positions == 2  # START and AFTER_E4, not AFTER_E4_E5
    assert result.position_coverage_rate == pytest.approx(2 / 3)


# --- header-first scanning --------------------------------------------------

BAD_MOVETEXT = "1. e4 e5 2. Nf6 Nc6"
"""Movetext python-chess rejects (illegal SAN), so `_build_record` refuses
the record with PARSE_REJECTED -- but only if it is ever parsed at all."""


class NonSeekableStream:
    """A stdin-like stream: readline only, seeking and telling are errors.

    Models `curl | zstd | python`, where the scanner cannot go back to a
    record it has already read past.
    """

    def __init__(self, text):
        self._inner = io.StringIO(text)
        self.readline_calls = 0

    def readline(self, *args, **kwargs):
        self.readline_calls += 1
        return self._inner.readline(*args, **kwargs)

    def seek(self, *args, **kwargs):  # pragma: no cover - must never run
        raise OSError("stream is not seekable")

    def tell(self, *args, **kwargs):  # pragma: no cover - must never run
        raise OSError("stream is not seekable")

    def read(self, *args, **kwargs):  # pragma: no cover - must never run
        raise OSError("scanner must not slurp the stream")


def test_counters_partition_records_scanned():
    texts = [
        pgn("1. e4 e5 2. Nf3 Nc6", time_control="60+0"),  # header-ineligible
        pgn("1. e4 e5 2. Nf3 Nc6", event="Casual Blitz game"),
        pgn("1. e4 e5 2. Nf3 Nc6"),  # eligible
    ]
    _, result = scan_texts(texts)
    c = result.counters
    assert c.records_scanned == 3
    assert c.records_header_rejected == 2
    assert c.records_fully_parsed == 1
    assert c.records_header_rejected + c.records_fully_parsed == c.records_scanned


def test_header_ineligible_records_are_never_fully_parsed():
    """The point of header-first scanning.

    Each ineligible record carries movetext python-chess would reject. If it
    were parsed, it would be counted PARSE_REJECTED. It is counted under its
    HEADER reason instead, which proves the movetext was never tokenised.
    """
    texts = [
        pgn(BAD_MOVETEXT, time_control="60+0"),            # wrong speed
        pgn(BAD_MOVETEXT, event="Casual Blitz game"),      # unrated
        pgn(BAD_MOVETEXT, extra=('[WhiteTitle "BOT"]',)),  # bot
        pgn(BAD_MOVETEXT, result="*"),                     # incomplete
        pgn(BAD_MOVETEXT, extra=('[Variant "Crazyhouse"]',)),  # variant
    ]
    _, result = scan_texts(texts)
    c = result.counters

    assert c.records_scanned == 5
    assert c.records_header_rejected == 5
    assert c.records_fully_parsed == 0
    assert c.rejections[ReferenceRejection.PARSE_REJECTED.value] == 0
    assert c.rejections[ReferenceRejection.TIME_CONTROL_NOT_ELIGIBLE.value] == 1
    assert c.rejections[ReferenceRejection.NOT_RATED.value] == 1
    assert c.rejections[ReferenceRejection.BOT_PLAYER.value] == 1
    assert c.rejections[ReferenceRejection.RESULT_NOT_COMPLETED.value] == 1
    assert c.rejections[ReferenceRejection.VARIANT_NOT_STANDARD.value] == 1
    assert c.decisions_examined == 0


def test_rating_band_rejection_skips_move_parsing_too():
    texts = [pgn(BAD_MOVETEXT, white_elo="800", black_elo="900")]
    filters = ReferenceEligibilityFilters(min_rating=1200, max_rating=1400)
    _, result = scan_texts(texts, filters=filters)
    assert result.counters.records_fully_parsed == 0
    assert result.counters.rejections[ReferenceRejection.RATING_OUT_OF_BAND.value] == 1
    assert result.counters.rejections[ReferenceRejection.PARSE_REJECTED.value] == 0


def test_full_game_trees_are_built_only_for_header_eligible_records(monkeypatch):
    """Direct instrumentation of the expensive work.

    `chess.pgn.read_headers` IS `read_game` with a headers-only visitor, so
    counting `read_game` calls alone proves nothing. What distinguishes the
    cheap visit from the expensive one is the VISITOR: `HeadersBuilder`
    walks headers and skips movetext, `GameBuilder` tokenises every move
    into a node tree. Only the latter may run for header-eligible records.
    """
    import chess.pgn as pgn_module

    # Built BEFORE instrumenting: personal ingestion parses games too, and
    # counting those would hide what the reference scan actually does.
    target = hero_target_set()

    calls = {"full_tree": 0, "headers_only": 0}
    real_read_game = pgn_module.read_game

    def counting_read_game(handle, **kwargs):
        visitor = kwargs.get("Visitor", pgn_module.GameBuilder)
        if visitor is pgn_module.GameBuilder:
            calls["full_tree"] += 1
        else:
            calls["headers_only"] += 1
        return real_read_game(handle, **kwargs)

    monkeypatch.setattr(pgn_module, "read_game", counting_read_game)

    texts = [pgn(BAD_MOVETEXT, time_control="60+0") for _ in range(9)]
    texts.append(pgn("1. e4 e5 2. Nf3 Nc6"))
    _, result = scan_texts(texts, target)

    assert result.counters.records_scanned == 10
    assert result.counters.records_fully_parsed == 1
    # Every record gets a cheap headers-only visit (plus the EOF probe).
    assert calls["headers_only"] >= 10
    # Exactly one record was tokenised into a full game tree.
    assert calls["full_tree"] == 1


def test_an_accepted_game_still_receives_a_full_parse():
    """Header eligibility is a gate, not a substitute: the accepted record is
    fully parsed and replayed, and its statistics are complete."""
    _, result = scan_texts([pgn("1. e4 e5 2. Nf3 Nc6", result="1-0")])
    c = result.counters
    assert c.records_fully_parsed == 1
    assert c.games_accepted == 1
    assert c.decisions_examined == 4
    assert c.decisions_matched == 3
    stats = result.index[START]
    assert stats.decisions[MoveKey("e2e4")].outcomes.wins == 1


def test_malformed_movetext_in_an_eligible_record_is_still_rejected():
    """A record that passes every header rule is parsed, and a movetext
    error is caught exactly as before."""
    texts = [pgn(BAD_MOVETEXT), pgn("1. e4 e5 2. Nf3 Nc6")]
    _, result = scan_texts(texts)
    c = result.counters
    assert c.records_fully_parsed == 2
    assert c.games_accepted == 1
    assert c.rejections[ReferenceRejection.PARSE_REJECTED.value] == 1
    assert len(result.index) == 3  # only the good game contributed


def test_malformed_movetext_behind_a_header_rejection_is_attributed_to_headers():
    """DOCUMENTED SEMANTIC BOUNDARY of header-first scanning.

    A record that fails BOTH a header rule and a movetext rule is now
    attributed to the HEADER reason, because its movetext is never read.
    Before header-first scanning it was attributed to PARSE_REJECTED.

    The ACCEPTED POPULATION is unaffected -- such a record was rejected
    either way, by both rules. Only the reason recorded for it changes, and
    only ever from a parse reason to a header reason.
    """
    texts = [pgn(BAD_MOVETEXT, time_control="60+0")]
    _, result = scan_texts(texts)
    c = result.counters
    assert c.games_accepted == 0  # unchanged: still rejected
    assert c.rejections[ReferenceRejection.TIME_CONTROL_NOT_ELIGIBLE.value] == 1
    assert c.rejections[ReferenceRejection.PARSE_REJECTED.value] == 0


def test_header_rejection_agrees_with_the_full_parse_verdict():
    """Every header rule must give the same verdict whether it reads
    `read_headers` output or a fully parsed game's headers."""
    filters = ReferenceEligibilityFilters(
        eligible_categories=(TimeControlCategory.RAPID,),
        min_rating=1200,
        max_rating=1400,
        require_both_ratings=True,
    )
    texts = [
        pgn("1. e4 e5 2. Nf3 Nc6", time_control="600+0", white_elo="1300", black_elo="1300"),
        pgn("1. e4 e5 2. Nf3 Nc6", time_control="60+0"),
        pgn("1. e4 e5 2. Nf3 Nc6", event="Casual Rapid game"),
        pgn("1. e4 e5 2. Nf3 Nc6", extra=('[BlackTitle "BOT"]',)),
        pgn("1. e4 e5 2. Nf3 Nc6", result="*"),
        pgn("1. e4 e5 2. Nf3 Nc6", white_elo="900", black_elo="1300"),
        pgn("1. e4 e5 2. Nf3 Nc6", white_elo=None, black_elo=None),
        pgn("1. e4 e5 2. Nf3 Nc6", white_elo="abc", black_elo="1300"),
    ]
    blob = "\n".join(texts)

    from_headers = []
    stream = RecordStream(io.StringIO(blob), "verdict")
    for _ordinal, _game_id, headers in stream:
        from_headers.append(header_rejection(headers, filters))

    from_games = []
    handle = io.StringIO(blob)
    while (game := chess.pgn.read_game(handle)) is not None:
        from_games.append(header_rejection(game.headers, filters))

    assert from_headers == from_games
    assert from_headers[0] is None  # the one eligible record
    assert from_headers[7] is ReferenceRejection.PARSE_REJECTED  # malformed rating


# --- non-seekable sources ---------------------------------------------------


def test_scan_works_on_a_non_seekable_stdin_like_stream():
    texts = [
        pgn("1. e4 e5 2. Nf3 Nc6", time_control="60+0"),
        pgn("1. e4 e5 2. Nf3 Nc6", result="1-0"),
        pgn("1. e4 e5 2. Nf3 Nc6", result="0-1"),
    ]
    handle = NonSeekableStream("\n".join(texts))
    scanner = ReferenceScanner(
        target_set=hero_target_set(), source_id="stdin-like", target_games=10
    )
    result = scanner.scan(handle)

    assert result.counters.records_scanned == 3
    assert result.counters.games_accepted == 2
    assert result.counters.records_header_rejected == 1
    assert result.index[START].distinct_game_count == 2
    assert handle.readline_calls > 0


def test_record_stream_never_seeks_or_slurps():
    handle = NonSeekableStream("\n".join(pgn("1. e4 e5 2. Nf3 Nc6") for _ in range(4)))
    stream = RecordStream(handle, "stdin-like")
    seen = 0
    for _ordinal, _game_id, _headers in stream:
        stream.parse_current()  # full parse must also avoid seeking
        seen += 1
    assert seen == 4


def test_record_stream_yields_the_same_records_as_plain_read_game():
    texts = [
        pgn("1. e4 e5 2. Nf3 Nc6", result="1-0"),
        pgn("1. d4 d5 2. c4 e6 3. Nc3 Nf6", result="0-1"),
        pgn("1. Nf3 d5 2. g3", result="1/2-1/2"),
    ]
    blob = "\n".join(texts)

    direct = []
    handle = io.StringIO(blob)
    while (game := chess.pgn.read_game(handle)) is not None:
        direct.append((dict(game.headers), [m.uci() for m in game.mainline_moves()]))

    buffered = []
    stream = RecordStream(NonSeekableStream(blob), "x")
    for _ordinal, _game_id, _headers in stream:
        game = stream.parse_current()
        buffered.append((dict(game.headers), [m.uci() for m in game.mainline_moves()]))

    assert buffered == direct


def test_record_stream_buffers_only_one_record():
    """Memory stays bounded by a single record, never the corpus."""
    blob = "\n".join(pgn("1. e4 e5 2. Nf3 Nc6") for _ in range(50))
    stream = RecordStream(io.StringIO(blob), "x")
    sizes = []
    for _ordinal, _game_id, _headers in stream:
        sizes.append(len(stream.parse_current().headers))
        # The buffer holds one record; its text never grows with the corpus.
        assert len(stream._reader.record_text()) < len(blob) // 10
    assert len(sizes) == 50


def test_record_stream_ordinals_advance_for_rejected_records_too():
    texts = [
        pgn("1. e4 e5", time_control="60+0"),
        pgn("1. e4 e5", event="Casual Blitz game"),
        pgn("1. e4 e5 2. Nf3 Nc6"),
    ]
    stream = RecordStream(io.StringIO("\n".join(texts)), "src")
    ids = [game_id for _ordinal, game_id, _headers in stream]
    assert ids == ["src:0", "src:1", "src:2"]


def test_record_stream_requires_a_source_id():
    with pytest.raises(ValueError, match="source_id"):
        RecordStream(io.StringIO(""), "   ")


def test_scanner_declares_the_header_first_strategy():
    scanner, _ = scan_texts([pgn("1. e4 e5")])
    declaration = scanner.as_manifest_dict()
    assert declaration["scanner"] == "HUMAN_REFERENCE_HEADER_FIRST_SCANNER_V2"
    assert "header-first" in declaration["scan_strategy"]
    assert declaration["materialises_corpus"] is False


# --- validation -------------------------------------------------------------


def test_scanner_rejects_an_invalid_configuration():
    target = hero_target_set()
    with pytest.raises(ValueError, match="source_id"):
        ReferenceScanner(target_set=target, source_id="  ", target_games=1)
    with pytest.raises(ValueError, match="target_games"):
        ReferenceScanner(target_set=target, source_id="x", target_games=0)
    with pytest.raises(ValueError, match="skip_records"):
        ReferenceScanner(
            target_set=target, source_id="x", target_games=1, skip_records=-1
        )


def test_filters_validate_their_own_configuration():
    with pytest.raises(ValueError, match="min_rating"):
        ReferenceEligibilityFilters(min_rating=2000, max_rating=1000)
    with pytest.raises(ValueError, match="at least one eligible"):
        ReferenceEligibilityFilters(eligible_categories=())
    with pytest.raises(ValueError, match="min_plies"):
        ReferenceEligibilityFilters(min_plies=-1)
