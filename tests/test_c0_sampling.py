"""Step 4C.0 pilot: deterministic streaming sampling and rejection accounting.

Synthetic only -- no Stockfish, no corpus.
"""

from __future__ import annotations

import io

import pytest

from chess_decision_explorer.experimental.c0.sampling import (
    GAME_IDENTITY_SCHEME,
    SAMPLING_REPRODUCIBILITY_GUARANTEE,
    EligibilityFilters,
    GameRejection,
    PilotSampler,
    RootRejection,
    SamplingCounters,
    ScanTermination,
    eligible_roots,
    is_rated_event,
    iter_raw_games,
)
from chess_decision_explorer.ingestion import _build_record
from chess_decision_explorer.personal import TimeControlCategory

MOVES = (
    "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 6. Re1 b5 7. Bb3 d6 "
    "8. c3 O-O 9. h3 Nb8 10. d4 Nbd7 11. Nbd2 Bb7 12. Bc2 Re8 13. Nf1 Bf8 "
    "14. Ng3 g6 15. b3 Bg7 16. d5 c6 17. c4 c5 18. a4 Nb6 19. Qe2 Bc8 20. Bd2 "
)


ALT_MOVES = (
    "1. d4 d5 2. c4 e6 3. Nc3 Nf6 4. Bg5 Be7 5. e3 O-O 6. Nf3 h6 7. Bh4 b6 "
    "8. cxd5 Nxd5 9. Bxe7 Qxe7 10. Nxd5 exd5 11. Rc1 Be6 12. Qa4 c5 13. Qa3 "
    "Rc8 14. Bb5 a6 "
)


def game_text(
    *,
    event="Rated Blitz game",
    white="alpha",
    black="beta",
    white_elo="1600",
    black_elo="1610",
    time_control="300+0",
    result="1-0",
    termination="Normal",
    moves=MOVES,
):
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
    if termination is not None:
        headers.append(f'[Termination "{termination}"]')
    return "\n".join(headers) + "\n\n" + moves + result + "\n\n"


def stream(*games: str) -> io.StringIO:
    return io.StringIO("".join(games))


def make_filters(**overrides):
    defaults = dict(min_plies=10, min_rating=1000, max_rating=2500)
    defaults.update(overrides)
    return EligibilityFilters(**defaults)


def make_sampler(*, seed=7, sample_size=10, filters=None, **overrides):
    return PilotSampler(
        source_id="test-source",
        filters=filters or make_filters(),
        seed=seed,
        sample_size=sample_size,
        **overrides,
    )


class CountingStream:
    """A read-only text stream that records how much of it was consumed."""

    def __init__(self, text: str) -> None:
        self._inner = io.StringIO(text)
        self.characters_read = 0

    def readline(self, *args):
        chunk = self._inner.readline(*args)
        self.characters_read += len(chunk)
        return chunk

    def read(self, *args):
        chunk = self._inner.read(*args)
        self.characters_read += len(chunk)
        return chunk


# --- rated-event detection --------------------------------------------------


@pytest.mark.parametrize(
    "event,expected",
    [
        ("Rated Blitz game", True),
        ("rated rapid game", True),
        ("Rated Blitz tournament https://lichess.org/tournament/x", True),
        ("Casual Blitz game", False),
        ("Live Chess", False),
        ("", False),
        (None, False),
        ("Unrated Blitz game", False),
    ],
)
def test_is_rated_event(event, expected):
    assert is_rated_event(event) is expected


# --- deterministic sampling -------------------------------------------------


def test_sampling_is_deterministic_for_the_same_seed():
    games = [game_text(white=f"w{i}", black=f"b{i}") for i in range(5)]
    first = list(make_sampler(seed=1234).iter_sample(stream(*games)))
    second = list(make_sampler(seed=1234).iter_sample(stream(*games)))

    assert [decision.root_id for decision in first] == [
        decision.root_id for decision in second
    ]
    assert [decision.decision for decision in first] == [
        decision.decision for decision in second
    ]


def test_a_different_seed_selects_different_roots():
    games = [game_text(white=f"w{i}", black=f"b{i}") for i in range(8)]
    first = list(make_sampler(seed=1).iter_sample(stream(*games)))
    second = list(make_sampler(seed=999).iter_sample(stream(*games)))

    assert len(first) == len(second) == 8
    assert [d.ply_index for d in first] != [d.ply_index for d in second]


def test_starting_the_scan_later_in_the_same_stream_preserves_every_draw():
    """The draw is keyed on game identity, which is
    ``source_id + ':' + zero_based_source_ordinal``. `skip_records` preserves
    ordinals, so every record the offset scan reaches keeps the identity -- and
    therefore the draw -- it had in the full scan. This is stability against
    where the SCAN starts, not against stream order; see
    `test_reordering_the_source_changes_the_draw`."""
    games = [game_text(white=f"w{i}", black=f"b{i}") for i in range(4)]
    full = list(make_sampler(seed=55).iter_sample(stream(*games)))
    offset = list(make_sampler(seed=55, skip_records=2).iter_sample(stream(*games)))

    assert [d.root_id for d in offset] == [d.root_id for d in full[2:]]
    assert [d.ply_index for d in offset] == [d.ply_index for d in full[2:]]


def test_reordering_the_source_changes_the_identity_and_the_draw():
    """Game identity carries the zero-based source ordinal, so the guarantee
    is bounded by source ORDERING and content -- a reordered corpus re-binds
    every identity and therefore samples different decisions."""
    games = [game_text(moves=MOVES), game_text(moves=ALT_MOVES, result="0-1")]
    forward = list(make_sampler(seed=55).iter_sample(stream(*games)))
    reversed_order = list(
        make_sampler(seed=55).iter_sample(stream(*reversed(games)))
    )

    # The same two ids are handed out either way...
    assert [d.game_id for d in forward] == [d.game_id for d in reversed_order]
    # ...but they now name different games, so different decisions are drawn.
    assert [d.decision for d in forward] != [d.decision for d in reversed_order]


def test_the_source_id_participates_in_the_draw():
    """`source_id` is a sampling input, not decoration: change it and the same
    stream yields different roots."""
    games = [game_text(white=f"w{i}", black=f"b{i}") for i in range(6)]
    first = list(
        PilotSampler(
            source_id="corpus-a", filters=make_filters(), seed=55, sample_size=6
        ).iter_sample(stream(*games))
    )
    second = list(
        PilotSampler(
            source_id="corpus-b", filters=make_filters(), seed=55, sample_size=6
        ).iter_sample(stream(*games))
    )

    assert [d.ply_index for d in first] != [d.ply_index for d in second]


def test_a_custom_source_id_is_used_consistently_in_sampled_identity():
    """Every identity a sampled record carries is built from the SAME declared
    source id, so the manifest value and the record ids can never disagree."""
    games = [game_text(white=f"w{i}", black=f"b{i}") for i in range(3)]
    sampler = PilotSampler(
        source_id="lichess-2024-01", filters=make_filters(), seed=8, sample_size=3
    )
    sampled = list(sampler.iter_sample(stream(*games)))

    declared = sampler.as_manifest_dict()["source_id"]
    assert declared == "lichess-2024-01"
    for ordinal, decision in enumerate(sampled):
        assert decision.source_id == declared
        assert decision.source_ordinal == ordinal
        assert decision.game_id == f"{declared}:{ordinal}"
        assert decision.root_id == f"{declared}:{ordinal}@{decision.ply_index}"
        assert decision.as_dict()["game_id"] == f"{declared}:{ordinal}"


def test_an_empty_source_id_is_rejected():
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="source_id"):
            PilotSampler(
                source_id=bad, filters=make_filters(), seed=1, sample_size=1
            )


def test_at_most_one_decision_per_game():
    games = [game_text(white=f"w{i}", black=f"b{i}") for i in range(6)]
    sampled = list(make_sampler(seed=3).iter_sample(stream(*games)))

    game_ids = [decision.game_id for decision in sampled]
    assert len(game_ids) == len(set(game_ids)) == 6


def test_sampled_decision_carries_source_provenance():
    sampled = list(make_sampler(seed=3).iter_sample(stream(game_text())))
    decision = sampled[0]

    assert decision.source_id == "test-source"
    assert decision.source_ordinal == 0
    assert decision.game_id == "test-source:0"
    assert decision.root_id == f"test-source:0@{decision.ply_index}"
    assert decision.time_control_category == "blitz"
    assert decision.actor_color in {"white", "black"}
    assert decision.game_ply_count == 39


def test_outcome_is_normalised_to_the_actor():
    sampled = list(make_sampler(seed=11).iter_sample(stream(game_text(result="1-0"))))
    decision = sampled[0]
    expected = "win" if decision.actor_color == "white" else "loss"
    assert decision.outcome_for_actor == expected


# --- eligibility / rejection accounting -------------------------------------


def test_rejection_reasons_are_counted_by_category():
    games = [
        game_text(event="Casual Blitz game"),
        game_text(time_control="1/86400"),
        game_text(white_elo=None),
        game_text(white_elo="3000"),
        game_text(moves="1. e4 e5 ", result="1-0"),
        game_text(termination="Abandoned"),
        game_text(result="*"),
        game_text(white="ok", black="fine"),
    ]
    sampler = make_sampler(seed=5)
    sampled = list(sampler.iter_sample(stream(*games)))
    counters = sampler.counters

    assert len(sampled) == 1
    assert counters.records_scanned == 8
    assert counters.games_accepted_by_filters == 1
    rejections = counters.game_rejections
    assert rejections[GameRejection.NOT_RATED.value] == 1
    assert rejections[GameRejection.TIME_CONTROL_NOT_ELIGIBLE.value] == 1
    assert rejections[GameRejection.RATING_MISSING.value] == 1
    assert rejections[GameRejection.RATING_OUT_OF_BAND.value] == 1
    assert rejections[GameRejection.TOO_FEW_PLIES.value] == 1
    assert rejections[GameRejection.TERMINATION_EXCLUDED.value] == 1
    assert rejections[GameRejection.PARSE_REJECTED.value] == 1


def test_accounting_is_complete_every_scanned_record_is_explained():
    games = [
        game_text(event="Casual Blitz game"),
        game_text(result="*"),
        game_text(white="ok", black="fine"),
        game_text(white="x", black="y"),
    ]
    sampler = make_sampler(seed=5)
    sampled = list(sampler.iter_sample(stream(*games)))
    counters = sampler.counters

    assert counters.records_scanned == counters.rejected_games + len(sampled)
    assert counters.decisions_sampled == len(sampled)


def test_rating_band_is_configurable_and_applies_to_both_players():
    inside = game_text(white_elo="1500", black_elo="1500")
    outside = game_text(white_elo="1500", black_elo="2400")
    sampler = make_sampler(
        seed=5, filters=make_filters(min_rating=1400, max_rating=1600)
    )
    sampled = list(sampler.iter_sample(stream(inside, outside)))

    assert len(sampled) == 1
    assert sampler.counters.game_rejections[GameRejection.RATING_OUT_OF_BAND.value] == 1


def test_minimum_ply_count_is_configurable():
    short_game = game_text(moves="1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 ", result="1-0")
    sampler = make_sampler(seed=5, filters=make_filters(min_plies=6))
    assert len(list(sampler.iter_sample(stream(short_game)))) == 1

    sampler = make_sampler(seed=5, filters=make_filters(min_plies=7))
    assert list(sampler.iter_sample(stream(short_game))) == []
    assert sampler.counters.game_rejections[GameRejection.TOO_FEW_PLIES.value] == 1


def test_only_configured_time_control_categories_are_eligible():
    bullet = game_text(time_control="60+0")
    blitz = game_text(time_control="300+0")

    sampler = make_sampler(seed=5)
    assert len(list(sampler.iter_sample(stream(bullet, blitz)))) == 1

    sampler = make_sampler(
        seed=5,
        filters=make_filters(eligible_categories=(TimeControlCategory.BULLET,)),
    )
    assert len(list(sampler.iter_sample(stream(bullet, blitz)))) == 1


def test_require_rated_is_configurable_for_non_lichess_sources():
    chess_com_style = game_text(event="Live Chess")
    sampler = make_sampler(seed=5, filters=make_filters(require_rated=False))
    assert len(list(sampler.iter_sample(stream(chess_com_style)))) == 1


def test_a_rejected_record_still_consumes_its_source_ordinal():
    games = [game_text(result="*"), game_text(white="ok", black="fine")]
    sampled = list(make_sampler(seed=5).iter_sample(stream(*games)))

    assert len(sampled) == 1
    assert sampled[0].source_ordinal == 1
    assert sampled[0].game_id == "test-source:1"


def test_accept_rate_deselects_deterministically():
    games = [game_text(white=f"w{i}", black=f"b{i}") for i in range(40)]
    first = list(make_sampler(seed=42, sample_size=40, accept_rate=0.5).iter_sample(stream(*games)))
    second = list(make_sampler(seed=42, sample_size=40, accept_rate=0.5).iter_sample(stream(*games)))

    assert 0 < len(first) < 40
    assert [d.game_id for d in first] == [d.game_id for d in second]


# --- root-level eligibility -------------------------------------------------


def test_one_legal_move_roots_are_rejected_and_counted():
    """After 2. Qh5+ and after 3. Qxg6+ Black has exactly one legal reply, so
    neither root is a decision and neither may enter the measurement sample."""
    forced = game_text(moves="1. e4 f6 2. Qh5+ g6 3. Qxg6+ hxg6 4. Nf3 ", result="0-1")
    counters = SamplingCounters()
    record = None
    for _, game_id, game in iter_raw_games(io.StringIO(forced), "src"):
        record = _build_record(game, game_id)
    roots = eligible_roots(record, counters)

    assert counters.root_rejections[RootRejection.ONE_LEGAL_MOVE.value] == 2
    assert {decision.move.uci for _, decision, _ in roots}.isdisjoint({"g7g6", "h7g6"})
    assert counters.candidate_roots_examined == len(record.moves) == 7
    assert counters.candidate_roots_eligible == len(roots) == 5


def test_eligible_roots_cover_both_colours():
    counters = SamplingCounters()
    record = None
    for _, game_id, game in iter_raw_games(io.StringIO(game_text()), "src"):
        record = _build_record(game, game_id)
    roots = eligible_roots(record, counters)

    assert {actor_is_white for _, _, actor_is_white in roots} == {True, False}


# --- streaming behaviour ----------------------------------------------------


def test_sampler_reads_from_a_file_handle(tmp_path):
    path = tmp_path / "corpus.pgn"
    path.write_text(game_text() + game_text(white="c", black="d"), encoding="utf-8")
    with open(path, "r", encoding="utf-8") as handle:
        sampled = list(make_sampler(seed=5).iter_sample(handle))
    assert len(sampled) == 2


def test_sampler_reads_from_a_stdin_like_stream():
    text = game_text() + game_text(white="c", black="d")
    sampled = list(make_sampler(seed=5).iter_sample(io.StringIO(text)))
    assert len(sampled) == 2


def test_iter_raw_games_is_a_generator_and_never_materialises_the_corpus():
    games = "".join(game_text(white=f"w{i}", black=f"b{i}") for i in range(50))
    handle = CountingStream(games)

    stopped_early = None
    for ordinal, _, _ in iter_raw_games(handle, "src"):
        if ordinal == 1:
            stopped_early = handle.characters_read
            break

    assert stopped_early is not None
    assert stopped_early < len(games) / 10


def test_sampling_stops_scanning_once_the_target_is_reached():
    games = "".join(game_text(white=f"w{i}", black=f"b{i}") for i in range(50))
    handle = CountingStream(games)
    sampler = make_sampler(seed=5, sample_size=2)

    sampled = list(sampler.iter_sample(handle))

    assert len(sampled) == 2
    assert sampler.counters.records_scanned == 2
    assert handle.characters_read < len(games) / 5
    assert sampler.termination is ScanTermination.SAMPLE_TARGET_REACHED
    assert sampler.termination.is_declared_prefix is True


def test_a_scan_window_reads_exactly_its_limit_and_no_further():
    """The window is exact: the limit is checked before the next record is
    pulled, so a scan of N records consumes N records off the stream and
    leaves the handle positioned at record N (0-indexed) for a caller that
    wants to continue where it stopped."""
    games = [game_text(white=f"w{i}", black=f"b{i}") for i in range(10)]
    handle = stream(*games)
    sampler = make_sampler(seed=5, sample_size=100, max_records_scanned=3)

    list(sampler.iter_sample(handle))

    assert sampler.counters.records_scanned == 3
    assert sampler.termination is ScanTermination.SCAN_LIMIT_REACHED
    # The 4th record is still unread, so resuming the same handle sees it.
    remaining = list(iter_raw_games(handle, "src"))
    assert len(remaining) == 7
    assert remaining[0][2].headers["White"] == "w3"


def test_scan_window_is_declared_as_a_prefix():
    games = [game_text(white=f"w{i}", black=f"b{i}") for i in range(10)]
    sampler = make_sampler(seed=5, sample_size=100, max_records_scanned=3)
    sampled = list(sampler.iter_sample(stream(*games)))

    assert len(sampled) == 3
    assert sampler.termination is ScanTermination.SCAN_LIMIT_REACHED
    assert sampler.termination.is_declared_prefix is True


def test_exhausting_the_stream_is_not_a_declared_prefix():
    sampler = make_sampler(seed=5, sample_size=100)
    list(sampler.iter_sample(stream(game_text())))

    assert sampler.termination is ScanTermination.STREAM_EXHAUSTED
    assert sampler.termination.is_declared_prefix is False


# --- configuration validation ------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_plies": 0},
        {"min_plies": "10"},
        {"min_rating": -1},
        {"min_rating": 2000, "max_rating": 1000},
        {"eligible_categories": ()},
    ],
)
def test_invalid_eligibility_filters_are_rejected(kwargs):
    defaults = dict(min_plies=10)
    defaults.update(kwargs)
    with pytest.raises(ValueError):
        EligibilityFilters(**defaults)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"seed": "7"},
        {"sample_size": 0},
        {"accept_rate": 0.0},
        {"accept_rate": 1.5},
        {"max_records_scanned": 0},
        {"skip_records": -1},
    ],
)
def test_invalid_sampler_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        make_sampler(**kwargs)


def test_sampling_counters_report_every_reason_key():
    counters = SamplingCounters()
    payload = counters.as_dict()
    assert set(payload["game_rejections"]) == {r.value for r in GameRejection}
    assert set(payload["root_rejections"]) == {r.value for r in RootRejection}


def test_sampler_manifest_declaration_records_the_selection_rule():
    declaration = make_sampler(seed=9, sample_size=4).as_manifest_dict()
    assert declaration["seed"] == 9
    assert declaration["sample_target"] == 4
    assert declaration["one_decision_per_game"] is True
    assert "blake2b" in declaration["selection_key"]


def test_sampler_manifest_declaration_records_the_identity_scheme():
    """The declaration is self-contained: it names the exact source id used
    and how game identity is formed from it."""
    declaration = make_sampler(seed=9).as_manifest_dict()
    assert declaration["source_id"] == "test-source"
    assert declaration["game_identity_scheme"] == GAME_IDENTITY_SCHEME
    assert "zero_based_source_ordinal" in declaration["game_identity_scheme"]
    assert (
        declaration["reproducibility_guarantee"]
        == SAMPLING_REPRODUCIBILITY_GUARANTEE
    )


def test_the_declared_guarantee_names_every_input_it_depends_on():
    """The recorded guarantee must state the real one -- source id, source
    ordering/content, seed, sampling configuration -- and must not claim
    independence from stream order."""
    guarantee = SAMPLING_REPRODUCIBILITY_GUARANTEE
    for required in (
        "source_id",
        "ordering",
        "seed",
        "sampling configuration",
        "ordinal",
    ):
        assert required in guarantee, required
