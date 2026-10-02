"""Unit tests for the pure game domain (no I/O, no framework).

Covers the capability-mixin state machine, host/player actor rules, undo
history (including the event-merge semantics), pause, rounds, and the
Ultimate Jeffpardy flow.
"""

import pytest

from jeffpardy.errors import InvalidAction, RoomError
from jeffpardy.game import (
    CLUE_VALUES,
    FinalData,
    Game,
    GameManager,
    build_board,
    snapshot,
)
from jeffpardy.states import (
    Answering,
    Asking,
    Board,
    BuzzersOpen,
    FinalReveal,
    GameOver,
    Lobby,
    Paused,
    Review,
    Round,
    RoundIntermission,
)

GROUPED = {
    f"Cat{i}": [(f"Q{i}-{v}", f"A{i}-{v}") for v in CLUE_VALUES] for i in range(6)
}
FINAL = FinalData("Final Cat", "What is the final question?", "What is the answer?")


def make_game(code: str = "1234") -> Game:
    return Game(
        code=code,
        boards={
            Round.MAIN: build_board(GROUPED, prefix="j-"),
            Round.BONUS: build_board(GROUPED, prefix="dj-"),
        },
        final=FINAL,
    )


def seated() -> Game:
    """Host Alice leading, players Bob and Carol at the table."""
    game = make_game()
    game.join("Alice")
    game.join("Bob")
    game.join("Carol")
    return game


def to_board(game: Game) -> None:
    game.perform("Alice", "start_game")
    game.perform("Alice", "finish_intro")


def action_names(game: Game) -> set[str]:
    return {a.name for a in game.available_host_actions}


# -- capability discovery ----------------------------------------------------


def test_lobby_advertises_its_capabilities():
    game = seated()
    assert isinstance(game.state, Lobby)
    assert action_names(game) == {"start_game", "adjust_score", "set_viewer", "promote_host"}
    labels = {a.name: a.label for a in game.available_host_actions}
    assert labels["start_game"] == "Start the Show"  # host button copy, not the vision doc


def test_each_state_advertises_exactly_its_capabilities():
    game = seated()
    # set_viewer rides on every pausable state (view-only for the main TV);
    # promote_host is lobby-only.
    expected = {
        "intro": {"finish_intro", "pause", "set_viewer"},
        "board": {"pick_clue", "finish_round", "adjust_score", "pause", "set_viewer", "set_picker"},
        "asking": {"open_buzzers", "reveal_answer", "close_clue", "pause", "set_viewer"},
        "buzzers_open": {"select_buzzer", "close_clue", "reveal_answer", "pause", "set_viewer"},
        "answering": {"correct", "incorrect", "pause", "set_viewer"},
        "round_intermission": {"start_next_round", "adjust_score", "pause", "set_viewer"},
        "final_category": {"open_final_wagering", "pause", "set_viewer"},
        "final_wagering": {"close_final_wagering", "pause", "set_viewer"},
        "final_clue": {"start_final_reveal", "pause", "set_viewer"},
        "final_reveal": {"grade_final", "end_game", "pause", "set_viewer"},
        "review": {"adjust_score", "close_game", "set_viewer"},
        "game_over": set(),
        "paused": {"resume"},
    }
    to_board(game)
    assert action_names(game) == expected["board"]
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    assert action_names(game) == expected["asking"]
    game.perform("Alice", "open_buzzers")
    assert action_names(game) == expected["buzzers_open"]
    game.perform("Alice", "select_buzzer", player_id="Bob")
    assert action_names(game) == expected["answering"]
    game.perform("Alice", "incorrect")
    assert action_names(game) == expected["buzzers_open"]
    game.perform("Alice", "close_clue")
    game.perform("Alice", "finish_round")
    assert action_names(game) == expected["round_intermission"]
    game.perform("Alice", "start_next_round")
    game.perform("Alice", "finish_round")
    assert action_names(game) == expected["final_category"]
    game.perform("Alice", "open_final_wagering")
    assert action_names(game) == expected["final_wagering"]
    game.perform("Alice", "close_final_wagering")
    assert action_names(game) == expected["final_clue"]
    game.perform("Alice", "start_final_reveal")
    assert action_names(game) == expected["final_reveal"]
    game.perform("Alice", "end_game")
    assert action_names(game) == expected["review"]
    game.perform("Alice", "close_game")
    assert action_names(game) == expected["game_over"]


def test_intro_advertises_finish_and_pause():
    game = seated()
    game.perform("Alice", "start_game")
    assert action_names(game) == {"finish_intro", "pause", "set_viewer"}


# -- actors: host vs players -------------------------------------------------


def test_first_joiner_becomes_host_and_does_not_play():
    game = make_game()
    game.join("Alice")
    game.join("Bob")
    assert game.host == "Alice"
    assert game.players == {"Bob": 0}  # host has no score slot


def test_rejoin_is_idempotent_for_host_and_players():
    game = seated()
    game.join("alice")  # case-insensitive host reconnect
    game.join("BOB")
    assert game.host == "Alice"
    assert game.players == {"Bob": 0, "Carol": 0}


def test_only_host_may_perform_actions():
    game = seated()
    for sender in ("Bob", "Carol", "nobody", None):
        with pytest.raises(InvalidAction, match="only the host runs the show"):
            game.perform(sender, "start_game")


def test_perform_rejects_unknown_or_unavailable_actions():
    game = seated()
    with pytest.raises(InvalidAction, match="isn't on tonight"):
        game.perform("Alice", "close_clue")  # Lobby has no close_clue
    to_board(game)
    with pytest.raises(InvalidAction, match="isn't on tonight"):
        game.perform("Alice", "start_game")  # already started
    with pytest.raises(InvalidAction, match="isn't a move on this show"):
        game.perform("Alice", "self_destruct")
    with pytest.raises(InvalidAction, match="fizzled"):
        game.perform("Alice", "pick_clue")  # missing clue_id


def test_host_cannot_be_selected_or_scored():
    game = seated()
    to_board(game)
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    game.perform("Alice", "open_buzzers")
    with pytest.raises(InvalidAction, match="reads the clues"):
        game.perform("Alice", "select_buzzer", player_id="Alice")
    with pytest.raises(InvalidAction, match="reads the clues"):
        game.buzz("Alice")


# -- happy path + scoring ----------------------------------------------------


def test_full_game_happy_path_through_review_to_closed():
    game = seated()
    to_board(game)
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    game.perform("Alice", "open_buzzers")
    game.buzz("Bob")
    game.perform("Alice", "select_buzzer", player_id="Bob")
    game.perform("Alice", "correct")
    assert game.players == {"Bob": 200, "Carol": 0}
    game.perform("Alice", "finish_round")
    assert isinstance(game.state, RoundIntermission)
    game.perform("Alice", "start_next_round")
    assert game.round is Round.BONUS
    game.perform("Alice", "finish_round")
    assert game.round is Round.ULTIMATE
    game.perform("Alice", "open_final_wagering")
    game.wager("Bob", 100)
    game.wager("Carol", 0)
    game.perform("Alice", "close_final_wagering")
    game.final_answer("Bob", "something")
    game.perform("Alice", "start_final_reveal")
    assert isinstance(game.state, FinalReveal)
    game.perform("Alice", "grade_final", player_id="Bob", correct=True)
    game.perform("Alice", "grade_final", player_id="Carol", correct=False)
    assert game.players == {"Bob": 300, "Carol": 0}
    game.perform("Alice", "end_game")
    assert isinstance(game.state, Review)
    game.perform("Alice", "close_game")
    assert isinstance(game.state, GameOver)
    assert game.closed is True


def test_incorrect_passes_the_floor_and_spends_the_clue_when_closed():
    game = seated()
    to_board(game)
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    game.perform("Alice", "open_buzzers")
    game.buzz("Bob")
    game.buzz("Carol")
    game.perform("Alice", "select_buzzer", player_id="Bob")
    game.perform("Alice", "incorrect")
    assert game.players == {"Bob": -200, "Carol": 0}
    assert isinstance(game.state, BuzzersOpen)
    # Bob already had this clue this clue: the host can't hand it back to him.
    with pytest.raises(InvalidAction, match="already had this clue"):
        game.perform("Alice", "select_buzzer", player_id="Bob")
    game.perform("Alice", "select_buzzer", player_id="Carol")
    game.perform("Alice", "incorrect")
    assert game.players == {"Bob": -200, "Carol": -200}
    game.perform("Alice", "close_clue")
    assert game.clue_status["j-c0-200"].value == "answered"  # scored => spent
    assert isinstance(game.state, Board)


# -- undo --------------------------------------------------------------------


def test_undo_restores_state_and_scores_after_correct():
    game = seated()
    to_board(game)
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    game.perform("Alice", "open_buzzers")
    game.buzz("Bob")
    game.perform("Alice", "select_buzzer", player_id="Bob")
    game.perform("Alice", "correct")
    assert game.players["Bob"] == 200
    game.undo()  # un-grade: back to Answering, score rewound
    assert isinstance(game.state, Answering)
    assert game.players == {"Bob": 0, "Carol": 0}
    game.undo()  # back to BuzzersOpen with Bob still queued
    assert isinstance(game.state, BuzzersOpen)
    assert game.buzz_queue == ["Bob"]
    assert game.clue_status["j-c0-200"].value == "active"


def test_undo_without_history_is_refused():
    game = seated()
    with pytest.raises(InvalidAction, match="nothing to take back"):
        game.undo()


def test_failed_action_changes_nothing_and_pushes_no_history():
    game = seated()
    to_board(game)
    before = action_names(game)
    with pytest.raises(InvalidAction, match="isn't on this board"):
        game.perform("Alice", "pick_clue", clue_id="j-nope-123")
    assert isinstance(game.state, Board)
    assert action_names(game) == before
    game.undo()  # un-do the (failed) nothing -> back before finish_intro
    assert game.state.name == "intro"
    game.undo()
    assert isinstance(game.state, Lobby)  # only intro actions were in history


def test_undo_never_rewinds_membership():
    game = seated()
    to_board(game)
    game.perform("Alice", "adjust_score", player_id="Bob", value=100)
    game.join("Dave")  # Dave arrives after the action
    game.leave("Carol")  # Carol leaves after the action
    game.undo()  # rewinds Bob's adjust...
    assert game.players == {"Bob": 0, "Dave": 0}  # ...but not the roster


def test_undo_rewinds_buzzes_made_after_the_checkpoint():
    game = seated()
    to_board(game)
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    game.perform("Alice", "open_buzzers")
    game.buzz("Bob")
    game.perform("Alice", "reveal_answer")  # checkpoint: queue was ["Bob"]
    game.buzz("Carol")  # post-checkpoint events are ordinary record state now
    game.undo()  # un-reveal — the whole record rewinds, Carol's buzz with it
    assert game.answer_revealed is False
    assert game.buzz_queue == ["Bob"]


# -- pause -------------------------------------------------------------------


def test_pause_wraps_current_state_and_resumes_exactly():
    game = seated()
    to_board(game)
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    game.perform("Alice", "reveal_answer")
    game.perform("Alice", "pause")
    assert isinstance(game.state, Paused)
    assert isinstance(game.state.previous, Asking)
    assert game.state.previous.clue_id == "j-c0-200"
    assert game.state.name == "paused"
    assert game.answer_revealed is True  # flags survive the wrap
    game.perform("Alice", "resume")
    assert isinstance(game.state, Asking)
    assert game.state.clue_id == "j-c0-200"


def test_pause_freezes_everything_but_resume():
    game = seated()
    to_board(game)
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    game.perform("Alice", "open_buzzers")
    game.perform("Alice", "pause")
    with pytest.raises(InvalidAction, match="isn't on tonight"):
        game.perform("Alice", "select_buzzer", player_id="Bob")
    with pytest.raises(InvalidAction, match="buzzers"):
        game.buzz("Bob")
    game.join("Late")  # arrivals are still welcome while paused
    assert "Late" in game.players


# -- bizarre Jeffpardy situations ---------------------------------------------


def test_nobody_answers_host_skips_and_clue_returns_to_the_board():
    game = seated()
    to_board(game)
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    game.perform("Alice", "open_buzzers")
    # Silence. Nobody buzzed. Host may reveal for drama, then close it out.
    game.perform("Alice", "reveal_answer")
    game.perform("Alice", "close_clue")
    assert isinstance(game.state, Board)
    assert game.clue_status["j-c0-200"].value == "board"  # unplayed: still fair
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")  # picked again, fine


def test_host_can_close_straight_from_asking_without_opening():
    game = seated()
    to_board(game)
    game.perform("Alice", "pick_clue", clue_id="j-c3-600")
    game.perform("Alice", "close_clue")  # wrong clue clicked
    assert isinstance(game.state, Board)
    assert game.clue_status["j-c3-600"].value == "board"


def test_every_player_leaves_mid_clue():
    game = seated()
    to_board(game)
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    game.perform("Alice", "open_buzzers")
    game.buzz("Bob")
    game.leave("Bob")  # the queue is a game fact — his buzz stays
    game.leave("Carol")
    assert game.players == {}  # nobody seated…
    assert game.buzz_queue == ["Bob"]  # …but the record remembers
    # The host can still grade the departed buzzer: identity outlives the seat.
    game.perform("Alice", "select_buzzer", player_id="Bob")
    game.perform("Alice", "correct")  # grades and retires the clue
    assert game.core.scores["Bob"] == 200  # ledger credits someone who left
    assert game.picker is None  # no seated player to hand the clock to
    # A fresh player can walk in mid-game and join the next clue.
    game.join("Dave")
    assert game.players == {"Dave": 0}


def test_answerer_must_finish_before_leaving():
    game = seated()
    to_board(game)
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    game.perform("Alice", "open_buzzers")
    game.perform("Alice", "select_buzzer", player_id="Bob")
    with pytest.raises(InvalidAction, match="finish answering first"):
        game.leave("Bob")
    game.perform("Alice", "incorrect")  # Bob is off the hook...
    game.leave("Bob")  # ...and may now leave (with his -200)
    assert game.players == {"Carol": 0}


def test_host_can_never_leave():
    game = seated()
    with pytest.raises(InvalidAction, match="emcees till the end"):
        game.leave("Alice")
    to_board(game)
    with pytest.raises(InvalidAction, match="emcees till the end"):
        game.leave("alice")


# -- room lifecycle: idle cull, show clock, goodbyes ---------------------------


def test_quiet_room_closes_after_twenty_minutes():
    game = make_game()
    game.join("Alice")  # host only — nobody ever sat down, so "empty" can't fire
    assert game.cull_reason(now=game.last_activity + 19 * 60) is None
    assert game.cull_reason(now=game.last_activity + 21 * 60) == "idle"


def test_two_hour_show_clock_closes_the_room():
    game = seated()
    assert game.should_cull_overtime(now=game.created_at + 2 * 60 * 60 - 1) is False
    assert game.should_cull_overtime(now=game.created_at + 2 * 60 * 60 + 1) is True
    # …and a live table reads it as overtime, not empty or idle
    t = game.created_at + 2 * 60 * 60 + 1
    game.heartbeat("Alice", now=t)
    game.heartbeat("Bob", now=t)
    game.heartbeat("Carol", now=t)
    assert game.cull_reason(now=t) == "overtime"


def test_goodbye_counts_after_five_minutes_not_before():
    game = seated()
    t0 = game.last_activity
    game.leave("Bob", now=t0)
    assert game.has_left("Bob", now=t0 + 4 * 60) is False
    assert game.has_left("Bob", now=t0 + 6 * 60) is True
    game.heartbeat("Carol", now=t0 + 6 * 60)  # Carol still at the table
    assert game.cull_reason(now=t0 + 6 * 60) is None


def test_room_closes_when_every_seat_has_gone():
    game = seated()
    t0 = game.last_activity
    game.leave("Bob", now=t0)
    game.leave("Carol", now=t0)
    assert game.cull_reason(now=t0 + 6 * 60) == "empty"


def test_quiet_drop_counts_after_five_minutes_and_reaps_the_seat():
    game = seated()
    t0 = game.last_activity
    game.heartbeat("Alice", now=t0)
    game.heartbeat("Carol", now=t0 + 6 * 60)  # Carol still at the table
    # no goodbye — Bob's seat just goes quiet past the same wait
    assert game.has_left("Bob", now=t0 + 6 * 60) is True
    reaped = game.sweep_departed(now=t0 + 6 * 60)
    assert reaped == ["Bob"]
    assert game.players == {"Carol": 0}


def test_lifecycle_close_is_final_and_keeps_scores():
    import asyncio

    async def scenario():
        manager = GameManager()
        game = make_game()
        game.join("Alice")
        game.join("Bob")
        manager._games["1234"] = game
        culled = await manager.sweep(now=game.created_at + 2 * 60 * 60 + 1)
        assert culled == ["1234"]
        with pytest.raises(RoomError, match="not found"):
            await manager.get("1234")

    asyncio.run(scenario())
    game = seated()
    game.close_room("overtime")
    assert game.closed is True
    assert game.close_reason == "overtime"
    with pytest.raises(InvalidAction, match="the show has ended"):
        game.join("Dave")

def test_buzz_window_and_dedupe_rules():
    game = seated()
    to_board(game)
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    with pytest.raises(InvalidAction, match="buzzers"):
        game.buzz("Bob")  # buzzers not open yet — rejected, not swallowed
    game.perform("Alice", "open_buzzers")
    game.buzz("Bob")
    game.buzz("Bob")  # the buzzer is dumb: mashing is fine, already queued = no-op
    game.buzz("Bob")
    with pytest.raises(InvalidAction, match="reads the clues"):
        game.buzz("Alice")
    with pytest.raises(InvalidAction, match="take a seat"):
        game.buzz("Zed")
    assert game.buzz_queue == ["Bob"]
    game.perform("Alice", "select_buzzer", player_id="Bob")
    assert game.buzz_queue == []  # leaving buzzers_open flushes the queue


def test_select_nonexistent_player_is_refused():
    game = seated()
    to_board(game)
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    game.perform("Alice", "open_buzzers")
    with pytest.raises(InvalidAction, match="contender on this show"):
        game.perform("Alice", "select_buzzer", player_id="Zed")


def test_finish_round_only_from_board_and_round_guards():
    game = seated()
    to_board(game)
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    with pytest.raises(InvalidAction, match="isn't on tonight"):
        game.perform("Alice", "finish_round")  # mid-clue: nope
    game.perform("Alice", "close_clue")
    game.perform("Alice", "finish_round")  # J -> intermission
    with pytest.raises(InvalidAction, match="isn't on tonight"):
        game.perform("Alice", "finish_round")  # intermission: nope
    game.perform("Alice", "start_next_round")
    # Bonus Jeffpardy clue ids are meaningless on the Jeffpardy board and vice
    # versa; a skipped clue returns to the board and may be re-picked:
    game.perform("Alice", "pick_clue", clue_id="dj-c0-200")
    game.perform("Alice", "close_clue")
    assert game.clue_status["dj-c0-200"].value == "board"
    game.perform("Alice", "pick_clue", clue_id="dj-c0-200")
    game.perform("Alice", "close_clue")
    with pytest.raises(InvalidAction, match="isn't on this board"):
        game.perform("Alice", "pick_clue", clue_id="j-nope")  # never existed


def to_final_wagering(game: Game, bob_score: int = 500) -> None:
    """Drive the game to Ultimate Jeffpardy with Bob bankrolled and wagering open."""
    to_board(game)
    if bob_score:
        game.perform("Alice", "adjust_score", player_id="Bob", value=bob_score)
    game.perform("Alice", "finish_round")
    game.perform("Alice", "start_next_round")
    game.perform("Alice", "finish_round")
    game.perform("Alice", "open_final_wagering")


def test_wager_window_and_limits():
    game = seated()
    to_board(game)
    game.perform("Alice", "adjust_score", player_id="Bob", value=500)
    game.perform("Alice", "finish_round")
    game.perform("Alice", "start_next_round")
    game.perform("Alice", "finish_round")
    with pytest.raises(InvalidAction, match="wagering isn't open"):
        game.wager("Bob", 10)  # wagering waits for the Ultimate category
    game.perform("Alice", "open_final_wagering")
    with pytest.raises(InvalidAction, match="reads the clues"):
        game.wager("Alice", 10)
    with pytest.raises(InvalidAction, match="take a seat"):
        game.wager("Zed", 10)
    with pytest.raises(InvalidAction, match="between 0 and 500"):
        game.wager("Bob", 501)
    with pytest.raises(InvalidAction, match="between 0 and 500"):
        game.wager("Bob", -5)
    with pytest.raises(InvalidAction, match="whole dollars"):
        game.wager("Bob", 10.5)  # type: ignore[arg-type]
    with pytest.raises(InvalidAction, match="whole dollars"):
        game.wager("Bob", True)  # bools are not money
    game.wager("Bob", 500)
    with pytest.raises(InvalidAction, match="already on the table"):
        game.wager("Bob", 1)
    # Carol has $0: her only legal wager is 0.
    with pytest.raises(InvalidAction, match="between 0 and 0"):
        game.wager("Carol", 1)
    game.wager("Carol", 0)


def test_final_answer_rules():
    game = seated()
    to_final_wagering(game)
    with pytest.raises(InvalidAction, match="isn't up yet"):
        game.final_answer("Bob", "too early")  # still wagering
    game.perform("Alice", "close_final_wagering")
    with pytest.raises(InvalidAction, match="write your response"):
        game.final_answer("Bob", "   ")
    with pytest.raises(InvalidAction, match="reads the clues"):
        game.final_answer("Alice", "nope")
    game.final_answer("Bob", "what is Jeffpardy?")
    game.final_answer("Bob", "what is Jeffpardy?")  # last write wins (typo fix)
    assert game.final_answers["Bob"] == "what is Jeffpardy?"
    assert "Carol" not in game.final_answers  # she may just... not answer


def test_final_grading_applies_plus_and_minus_wagers():
    game = seated()
    to_final_wagering(game)
    game.wager("Bob", 300)
    game.wager("Carol", 0)
    game.perform("Alice", "close_final_wagering")
    game.perform("Alice", "start_final_reveal")
    game.perform("Alice", "grade_final", player_id="Bob", correct=True)
    game.perform("Alice", "grade_final", player_id="Carol", correct=True)
    assert game.players == {"Bob": 800, "Carol": 0}
    with pytest.raises(InvalidAction, match="already called"):
        game.perform("Alice", "grade_final", player_id="Bob", correct=False)
    with pytest.raises(InvalidAction, match="contender on this show"):
        game.perform("Alice", "grade_final", player_id="Zed", correct=True)


# -- adjust ------------------------------------------------------------------


def test_adjust_only_between_plays_and_undoable():
    game = seated()
    to_board(game)
    game.perform("Alice", "adjust_score", player_id="Bob", value=-200)
    assert game.players["Bob"] == -200
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    with pytest.raises(InvalidAction, match="isn't on tonight"):
        game.perform("Alice", "adjust_score", player_id="Bob", value=1)
    game.perform("Alice", "close_clue")
    with pytest.raises(InvalidAction, match="contender on this show"):
        game.perform("Alice", "adjust_score", player_id="Zed", value=1)
    game.undo()  # un-close -> back to the active clue
    game.undo()  # un-pick -> back to the board (still adjusted)
    game.undo()  # un-adjust
    assert game.players["Bob"] == 0


# -- closing is final --------------------------------------------------------


def test_closed_game_is_truly_final():
    game = seated()
    to_board(game)
    game.perform("Alice", "finish_round")
    game.perform("Alice", "start_next_round")
    game.perform("Alice", "finish_round")
    game.perform("Alice", "open_final_wagering")
    game.perform("Alice", "close_final_wagering")
    game.perform("Alice", "start_final_reveal")
    game.perform("Alice", "end_game")
    game.perform("Alice", "close_game")
    assert game.closed is True
    with pytest.raises(InvalidAction, match="show has ended"):
        game.join("Eve")
    with pytest.raises(InvalidAction, match="isn't on tonight"):
        game.perform("Alice", "adjust_score", player_id="Bob", value=1)
    with pytest.raises(InvalidAction, match="show has ended"):
        game.perform("Alice", "undo")  # no resurrecting the dead
    with pytest.raises(InvalidAction, match="buzzers"):
        game.buzz("Bob")
    state = snapshot(game, viewer="Alice")
    assert state["closed"] is True
    assert state["state"] == "game_over"
    assert state["availableHostActions"] == []
    assert state["canUndo"] is False


# -- viewer-aware snapshots --------------------------------------------------


def test_host_sees_answers_players_do_not_until_revealed():
    game = seated()
    to_board(game)
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    host_view = snapshot(game, viewer="Alice")
    bob_view = snapshot(game, viewer="Bob")
    assert host_view["answer"] == "A0-200"  # the host runs the game
    assert bob_view["answer"] is None  # not revealed to the room yet
    assert bob_view["question"] is not None
    assert snapshot(game, viewer=None)["answer"] is None  # anonymous: player view
    game.perform("Alice", "reveal_answer")
    assert snapshot(game, viewer="Bob")["answer"] == "A0-200"
    game.perform("Alice", "pause")  # paused views unwrap to the underlying state
    assert snapshot(game, viewer="Alice")["state"] == "paused"
    assert snapshot(game, viewer="Alice")["pausedIn"] == "asking"
    assert snapshot(game, viewer="Bob")["answer"] == "A0-200"


def test_final_secrets_are_viewer_filtered():
    game = seated()
    to_final_wagering(game)
    game.wager("Bob", 300)
    game.wager("Carol", 0)
    game.perform("Alice", "close_final_wagering")
    game.final_answer("Bob", "secret bob")
    game.final_answer("Carol", "secret carol")
    # Before the reveal: each player sees only their own wager/answer;
    # the host (who runs the show) sees everything.
    bob_view = snapshot(game, viewer="Bob")
    assert bob_view["final"]["wagers"] == {"Bob": 300}
    assert bob_view["final"]["answers"] == {"Bob": "secret bob"}
    assert bob_view["final"]["question"] is not None  # clue is open
    host_view = snapshot(game, viewer="Alice")
    assert host_view["final"]["wagers"] == {"Bob": 300, "Carol": 0}
    assert set(host_view["final"]["answers"]) == {"Bob", "Carol"}
    game.perform("Alice", "start_final_reveal")
    assert snapshot(game, viewer="Carol")["final"]["wagers"] == {"Bob": 300, "Carol": 0}


def test_snapshot_advertises_undo_only_when_history_exists():
    game = seated()
    assert snapshot(game)["canUndo"] is False
    assert "undo" not in {a["name"] for a in snapshot(game)["availableHostActions"]}
    game.perform("Alice", "start_game")
    view = snapshot(game, viewer="Alice")
    assert view["canUndo"] is True
    assert {"name": "undo", "label": "Take It Back"} in view["availableHostActions"]
    assert view["host"] == "Alice"
    assert view["roundLabel"] == "Jeffpardy"


# -- snapshot layering: host truth + per-viewer masks --------------------------


def test_snapshot_is_host_truth_masked_per_viewer():
    from jeffpardy.game import host_snapshot, is_host_viewer, mask_snapshot, snapshots_for

    game = seated()
    to_board(game)
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    base = host_snapshot(game)
    assert is_host_viewer(game, "Alice") is True
    assert is_host_viewer(game, "alice") is True  # identity is case-insensitive
    assert is_host_viewer(game, "Bob") is False
    assert is_host_viewer(game, None) is False
    # The truth carries the answer; the mask redacts it; the shape is stable.
    assert base["answer"] == "A0-200"
    bob = mask_snapshot(game, base, "Bob")
    assert bob["answer"] is None
    assert bob["question"] is not None  # the room still hears the clue
    assert set(bob) == set(base)
    # snapshot() is truth-or-mask by role; anonymous is a masked spectator.
    assert snapshot(game, viewer="Alice") == base
    assert snapshot(game, viewer="Bob") == bob
    assert snapshot(game, viewer=None)["answer"] is None


def test_viewers_share_the_player_mask_not_the_host_truth():
    game = seated()
    to_board(game)
    game.perform("Alice", "set_viewer", player_id="Carol", viewer=True)
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    assert snapshot(game, viewer="Carol") == snapshot(game, viewer="Bob")
    assert snapshot(game, viewer="Carol")["answer"] is None
    assert snapshot(game, viewer="Alice")["answer"] == "A0-200"


def test_snapshots_for_batches_one_truth_per_identity():
    from jeffpardy.game import snapshots_for

    game = seated()
    to_final_wagering(game)
    game.wager("Bob", 300)
    game.wager("Carol", 0)
    game.perform("Alice", "close_final_wagering")
    views = snapshots_for(game, ["Alice", "Bob", "BOB", "Carol", None, "Ghost"])
    # Two sockets, one identity: shared dict. Spectators share the None mask.
    assert views["Bob"] is views["BOB"]
    assert views[None] is views["Ghost"]
    assert views["Alice"] is not views["Bob"]
    assert views["Alice"]["final"]["wagers"] == {"Bob": 300, "Carol": 0}
    assert views["Bob"]["final"]["wagers"] == {"Bob": 300}
    assert views[None]["final"]["wagers"] == {}
    for viewer in ("Alice", "Bob", "Carol", None):
        assert snapshot(game, viewer=viewer) == views[viewer]


# -- host modes: middleware scaffolding (manual only) --------------------------


def test_manual_mode_is_the_default_and_advertises_everything():
    from jeffpardy.hosting import HostMode

    game = seated()
    assert game.host_mode is HostMode.MANUAL
    assert game.host_policy.mode is HostMode.MANUAL
    assert snapshot(game, viewer="Alice")["hostMode"] == "manual"


def test_reserved_host_modes_are_named_but_refused():
    from jeffpardy.hosting import HostMode, get_host_policy

    with pytest.raises(InvalidAction, match="isn't on the menu yet"):
        get_host_policy(HostMode.AUTO)


def test_custom_policies_plug_in_without_touching_perform():
    from jeffpardy.hosting import (
        HostMode,
        HostPolicy,
        ManualHostPolicy,
        register_host_policy,
    )

    game = seated()

    class NoUndo(HostPolicy):
        mode = HostMode.MANUAL

        def authorize(self, game, sender, action, payload):
            if action == "undo":
                raise InvalidAction("take back in this mode")

        def advertise(self, game, actions):
            return tuple(a for a in actions if a.name != "adjust_score")

    register_host_policy(NoUndo())
    try:
        game.perform("Alice", "start_game")
        with pytest.raises(InvalidAction, match="take back"):
            game.perform("Alice", "undo")
        hidden = {a.name for a in game.available_host_actions}
        assert "adjust_score" not in hidden  # advertise + perform share the policy
        assert "adjust_score" not in {
            a["name"] for a in snapshot(game, viewer="Alice")["availableHostActions"]
        }
        with pytest.raises(InvalidAction, match="isn't on tonight"):
            game.perform("Alice", "adjust_score", player_id="Bob", value=10)
    finally:
        # Manual restores itself: policies are a registry, not a code path.
        register_host_policy(ManualHostPolicy())


# -- assisted host mode: delegation + auto-advance ---------------------------


def assisted() -> Game:
    game = seated()
    game.perform("Alice", "set_host_mode", mode="assisted")
    return game


def to_assisted_board(game: Game) -> None:
    game.perform("Alice", "start_game")
    game.perform("Alice", "finish_intro")


def everyone_wrong_board() -> Game:
    """A fresh board where Bob and Carol both miss the $200 clue: answering
    marks them spent, and the host rules both wrong — the clue is now dead
    and the floor is Bob's."""
    game = seated()
    to_board(game)
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    game.perform("Alice", "open_buzzers")
    for who in ("Bob", "Carol"):
        game.perform("Alice", "select_buzzer", player_id=who)
        game.perform("Alice", "incorrect")
    return game


def test_a_viewer_can_never_take_the_floor():
    game = seated()
    game.perform("Alice", "set_viewer", player_id="Bob", viewer=True)
    to_board(game)
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    game.perform("Alice", "open_buzzers")
    with pytest.raises(InvalidAction, match="audience watches this one"):
        game.buzz("Bob")  # in the audience: no buzzer
    with pytest.raises(InvalidAction, match="sitting this one out"):
        game.perform("Alice", "select_buzzer", player_id="Bob")  # …and no floor


def test_dead_clue_is_about_the_floor_not_the_hosts_toss_up():
    """`dead_clue` answers "could anyone still take this clue" — the shared
    hosting auto-skip reads it. The host's "Back to the Board" is never a
    no-op: closing a wholly-answered clue spends it rather than returning it.
    """
    game = everyone_wrong_board()
    assert game.dead_clue("j-c0-200")  # nobody left who can take it
    game.perform("Alice", "close_clue")  # still grades as a wrong answer on the books
    assert game.clue_status["j-c0-200"].value == "answered"  # spent, not re-offered
    assert game.players == {"Bob": -200, "Carol": -200}


def test_assisted_mode_switch_is_host_only_and_refuses_unknown_modes():
    game = seated()
    with pytest.raises(InvalidAction, match="only the host runs the show"):
        game.perform("Bob", "set_host_mode", mode="assisted")
    with pytest.raises(InvalidAction, match="isn't on the menu"):
        game.perform("Alice", "set_host_mode", mode="turbo")
    game.perform("Alice", "set_host_mode", mode="assisted")
    assert snapshot(game, viewer="Alice")["hostMode"] == "co-hosted"
    # old wire value still lands: "assisted" is the deprecated alias
    game.perform("Alice", "set_host_mode", mode="manual")
    game.perform("Alice", "set_host_mode", mode="co-hosted")
    assert snapshot(game, viewer="Alice")["hostMode"] == "co-hosted"
    game.perform("Alice", "set_host_mode", mode="manual")
    assert snapshot(game, viewer="Alice")["hostMode"] == "manual"


def test_shared_hosting_player_choice_opens_the_buzzers_and_holds_the_host():
    from jeffpardy.states import BuzzersOpen

    game = assisted()
    to_assisted_board(game)
    game.perform("Alice", "set_picker", player_id="Bob")
    assert "pick_clue" not in action_names(game)  # holding the board: hidden from the host
    with pytest.raises(InvalidAction, match="stays off your buttons"):
        game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    with pytest.raises(InvalidAction, match="calls it"):
        game.pick("Carol", clue_id="j-c0-200")
    game.pick("Bob", clue_id="j-c0-200")  # the holder names the clue…
    assert isinstance(game.state, BuzzersOpen)  # …and the buzzers open on their own
    assert "open_buzzers" not in action_names(game)
    assert game.rev >= 3  # pick + auto-open each bumped the clock


def test_shared_hosting_quest_choice_needs_the_board():
    game = assisted()
    to_assisted_board(game)
    quest = game.active_quest
    assert quest
    with pytest.raises(InvalidAction, match="give someone the pick"):
        game.pick("Bob", clue_id=quest)
    # …and the host UI never offers the shared flip while sharing
    assert "pick_clue" not in action_names(game)


def test_shared_hosting_player_choice_stays_hold_but_settling_still_opens():
    from jeffpardy.states import BuzzersOpen

    game = assisted()
    to_assisted_board(game)
    game.pick("Bob", clue_id="j-c0-200")  # open board: anyone may choose
    assert isinstance(game.state, BuzzersOpen)


def test_shared_hosting_dead_clue_closes_itself_out():
    from jeffpardy.states import Answering, Board, BuzzersOpen

    game = assisted()
    to_assisted_board(game)
    game.pick("Bob", clue_id="j-c0-200")
    assert isinstance(game.state, BuzzersOpen)
    game.perform("Alice", "select_buzzer", player_id="Bob")
    assert isinstance(game.state, Answering)
    game.perform("Alice", "incorrect")  # Bob is out — Carol still could answer…
    assert isinstance(game.state, BuzzersOpen)
    game.buzz("Carol")
    game.perform("Alice", "select_buzzer", player_id="Carol")
    game.perform("Alice", "incorrect")  # …now nobody is left: auto-close reaps it
    assert isinstance(game.state, Board)
    # Both misses scored (clue_scored), so the dead clue is spent, not fair.
    assert game.clue_status["j-c0-200"].value == "answered"


def test_shared_hosting_undo_lands_in_between_and_does_not_replay():
    from jeffpardy.states import Asking, Board, BuzzersOpen

    game = assisted()
    to_assisted_board(game)
    game.pick("Bob", clue_id="j-c0-200")
    assert isinstance(game.state, BuzzersOpen)
    game.perform("Alice", "undo")  # rewind the auto-open only
    assert isinstance(game.state, Asking)  # the in-between: settled, not replayed
    assert "open_buzzers" not in action_names(game)  # still auto-locked for the host
    with pytest.raises(InvalidAction, match="stays off your buttons"):
        game.perform("Alice", "open_buzzers")
    game.perform("Alice", "undo")  # now rewind the pick itself
    assert isinstance(game.state, Board)


def test_shared_hosting_finals_run_once_every_seat_has_moved():
    from jeffpardy.states import Board as BoardState
    from jeffpardy.states import FinalClue, FinalReveal, FinalWagering, RoundIntermission

    game = assisted()
    to_assisted_board(game)
    game.perform("Alice", "finish_round")  # MAIN -> the break stays manual…
    assert isinstance(game.state, RoundIntermission)
    game.perform("Alice", "start_next_round")  # …the host starts the bonus board…
    assert isinstance(game.state, BoardState)
    game.perform("Alice", "set_host_mode", mode="manual")
    game.perform("Alice", "finish_round")  # BONUS -> FinalCategory…
    game.perform("Alice", "set_host_mode", mode="assisted")
    assert isinstance(game.state, FinalWagering)  # …settles straight to wagering
    game.wager("Bob", 0)
    assert isinstance(game.state, FinalWagering)  # one seat moved: still waiting
    assert "close_final_wagering" not in action_names(game)  # never host-offered…
    with pytest.raises(InvalidAction, match="isn't on tonight|stays off your buttons"):
        game.perform("Alice", "close_final_wagering")  # …nor host-runnable
    game.wager("Carol", 0)  # last seat in: the event settle closes wagering…
    assert isinstance(game.state, FinalClue)
    game.final_answer("Bob", "what is it")
    assert isinstance(game.state, FinalClue)
    game.final_answer("Carol", "what is it")  # last answer in: auto-reveal…
    assert isinstance(game.state, FinalReveal)
    game.perform("Alice", "grade_final", player_id="Bob", correct=True)
    assert isinstance(game.state, FinalReveal)  # grading is judgment: stays put


def test_shared_hosting_pause_holds_the_show_and_mode_flip_settles():
    from jeffpardy.states import Asking, Board, BuzzersOpen, Paused

    game = assisted()
    to_assisted_board(game)
    # Hold the flip open by hand: manual keeps Asking at rest…
    game.perform("Alice", "set_host_mode", mode="manual")
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    assert isinstance(game.state, Asking)
    game.perform("Alice", "pause")
    assert isinstance(game.state, Paused)
    # …pause freezes the chain even after flipping to assisted…
    game.perform("Alice", "set_host_mode", mode="assisted")
    assert isinstance(game.state, Paused)
    game.perform("Alice", "resume")  # …and the resume commit settles it open
    assert isinstance(game.state, BuzzersOpen)
    # Undo rewinds one checkpoint — the auto-open — holding the in-between
    # with no replay: the flip sits open, still host-locked, for the host.
    game.perform("Alice", "undo")
    assert isinstance(game.state, Asking)
    assert "open_buzzers" not in action_names(game)


def test_shared_hosting_quest_choice_opens_the_wager_not_the_buzzers():
    """Quest flip while sharing: the holder's choice lands on the wager and
    waits for their risk — no buzzers. The locked risk then opens the clue
    by itself."""
    from jeffpardy.states import QuestAnswering, QuestWager

    game = assisted()
    to_assisted_board(game)
    quest = game.active_quest
    assert quest
    game.perform("Alice", "adjust_score", player_id="Bob", value=400)
    game.perform("Alice", "set_picker", player_id="Bob")
    game.pick("Bob", clue_id=quest)  # the marked card: straight to the wager
    assert isinstance(game.state, QuestWager)
    assert game.state.player_id == "Bob"
    game.quest_wager("Bob", 400)  # the locked risk settles the answer open
    assert isinstance(game.state, QuestAnswering)
    game.perform("Alice", "correct")  # graded on the wager, back to the board
    assert game.players["Bob"] == 800  # 400 bankroll + 400 stake
    assert game.picker == "Bob"  # holder picks next


def test_shared_hosting_quest_choice_needs_the_board():
    game = assisted()
    to_assisted_board(game)
    quest = game.active_quest
    assert quest
    with pytest.raises(InvalidAction, match="give someone the pick"):
        game.pick("Bob", clue_id=quest)
    # …and the host UI never offers the shared flip while sharing
    assert "pick_clue" not in action_names(game)


def test_shared_hosting_open_board_lets_anyone_choose_then_winner_holds_it():
    """Open board: anyone may choose (the host left it open).
    A right answer hands the winner the board — the next choice is theirs."""
    from jeffpardy.states import Answering, Board, BuzzersOpen

    game = assisted()
    to_assisted_board(game)
    assert game.picker is None
    game.pick("Carol", clue_id="j-c0-200")  # open board: first choice wins the clue
    assert isinstance(game.state, BuzzersOpen)
    game.buzz("Carol")
    game.perform("Alice", "select_buzzer", player_id="Carol")
    assert isinstance(game.state, Answering)
    game.perform("Alice", "correct")  # right answer chooses: Carol holds the board
    assert isinstance(game.state, Board)
    assert game.picker == "Carol"
    with pytest.raises(InvalidAction, match="calls it"):
        game.pick("Bob", clue_id="j-c1-200")

# -- async registry ----------------------------------------------------------


async def test_manager_creates_four_digit_codes_and_runs_actions():
    codes = iter(["1234", "1234", "5678"])
    manager = GameManager(code_factory=lambda: next(codes))
    from jeffpardy.game import FinalData as _FD

    boards = {Round.MAIN: build_board(GROUPED, prefix="j-")}
    game = await manager.create(boards, _FD("F", "Q", "A"))
    assert game.code == "1234"
    # Second create collides on 1234 and retries into 5678.
    other = await manager.create(boards, _FD("F", "Q", "A"))
    assert other.code == "5678"
    got = await manager.get(" 1234 ")
    assert got is game
    await manager.run("1234", lambda g: g.join("First"))
    assert (await manager.get("1234")).host == "First"
    with pytest.raises(RoomError, match="not found"):
        await manager.get("0000")
