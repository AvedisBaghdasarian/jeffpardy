"""Two complete games played through the state machine, quirks included.

Domain-level integration only: no HTTP, no sockets — just the game API the
routers call. Each game is scripted top to bottom (lobby -> Jeffpardy ->
Bonus -> Ultimate -> review -> closed) with the table behaviors real players
produce: host handoffs, view-only seats, buzzer mashing, skips, re-opens,
undo mid-clue, late arrivals, and empty rounds.
"""

import asyncio

import pytest

from jeffpardy.errors import InvalidAction
from jeffpardy.game import GameManager, snapshot
from jeffpardy.states import (
    Answering,
    Asking,
    Board,
    BuzzersOpen,
    QuestAnswering,
    QuestWager,
    Review,
    Round,
    RoundIntermission,
)

from test_game import action_names, make_game, seated, to_board


def test_full_game_chaotic_table():
    """Four friends, a promoted host, a viewer, and lots of second chances."""
    game = seated()  # host Alice; players Bob, Carol
    game.join("Dana")

    # -- lobby politics ------------------------------------------------------
    game.perform("Alice", "set_viewer", player_id="Carol", viewer=True)
    assert game.viewers == {"Carol"}
    game.perform("Alice", "promote_host", player_id="Bob")
    assert game.host == "Bob"
    assert game.players == {"Bob": 0, "Carol": 0, "Dana": 0, "Alice": 0}
    with pytest.raises(InvalidAction, match="only the host runs the show"):
        game.perform("Alice", "start_game")  # authority moved with the hat

    # -- Main round: the viewer and the new host sit out the buzzer -----------
    game.perform("Bob", "start_game")
    game.perform("Bob", "finish_intro")
    view = snapshot(game, "Bob")
    assert view["round"] == "main"
    assert view["roundLabel"] == "Jeffpardy"
    assert view["viewers"] == ["Carol"]

    game.perform("Bob", "pick_clue", clue_id="j-c0-200")
    game.perform("Bob", "open_buzzers")
    with pytest.raises(InvalidAction, match="audience watches"):
        game.buzz("Carol")
    with pytest.raises(InvalidAction, match="reads the clues"):
        game.buzz("Bob")
    game.buzz("Dana")
    game.buzz("ALICE")  # identity is case-insensitive
    assert game.buzz_queue == ["Dana", "Alice"]
    game.perform("Bob", "select_buzzer", player_id=" alice ")  # ...and space-blind
    assert game.buzz_queue == []  # leaving buzzers_open flushes the queue

    # wrong answer: the buzzer re-opens for everyone who hasn't answered yet —
    # and the miss locks Alice out of this clue, so she can't re-litter the queue
    game.perform("Bob", "incorrect")
    assert game.players["Alice"] == -200
    with pytest.raises(InvalidAction, match="already had this clue"):
        game.buzz("alice")
    game.buzz("dana")
    game.buzz("dana")  # mash — already queued is a no-op
    assert game.buzz_queue == ["Dana"]  # only eligible contenders land in line
    with pytest.raises(InvalidAction, match="already had this clue"):
        game.perform("Bob", "select_buzzer", player_id="Alice")
    game.perform("Bob", "select_buzzer", player_id="Dana")
    game.perform("Bob", "correct")
    assert game.players == {"Bob": 0, "Carol": 0, "Dana": 200, "Alice": -200}

    # -- pause freezes the room; benching still waits for the resume --------
    game.perform("Bob", "pause")
    with pytest.raises(InvalidAction, match="closed while paused"):
        game.buzz("Dana")
    with pytest.raises(InvalidAction, match="isn't on tonight"):
        game.perform("Bob", "set_viewer", player_id="Dana", viewer=True)
    game.perform("Bob", "resume")

    # -- skip quirk: reveal without buzzing, close, the clue comes back ------
    game.perform("Bob", "pick_clue", clue_id="j-c0-400")
    game.perform("Bob", "reveal_answer")
    game.perform("Bob", "close_clue")
    game.perform("Bob", "pick_clue", clue_id="j-c0-400")  # skipped = re-pickable
    game.perform("Bob", "open_buzzers")
    game.buzz("Alice")
    game.perform("Bob", "select_buzzer", player_id="Alice")
    game.perform("Bob", "correct")  # Alice +400 -> 200

    # undo quirk: rewind the grade, then re-issue it
    game.perform("Bob", "undo")
    assert isinstance(game.state, Answering)
    assert game.players["Alice"] == -200
    game.perform("Bob", "correct")
    assert game.players["Alice"] == 200

    # -- everyone-wrong quirk: two strikes, then the host clears the clue ----
    game.perform("Bob", "pick_clue", clue_id="j-c0-600")
    game.perform("Bob", "open_buzzers")
    game.buzz("Alice")
    game.buzz("Dana")
    game.perform("Bob", "select_buzzer", player_id="Alice")
    game.perform("Bob", "incorrect")  # Alice -> -400, buzzer re-opens
    game.buzz("Dana")
    game.perform("Bob", "select_buzzer", player_id="Dana")
    game.perform("Bob", "incorrect")  # Dana -> -400
    with pytest.raises(InvalidAction, match="already had this clue"):
        game.perform("Bob", "select_buzzer", player_id="Alice")
    game.perform("Bob", "close_clue")

    # -- view-only toggles at any point, scores ride along --------------------
    game.perform("Bob", "set_viewer", player_id="Carol", viewer=False)
    game.perform("Bob", "set_viewer", player_id="Alice", viewer=True)
    assert game.players["Alice"] == -400  # benched, not stripped

    # -- Bonus round: the ex-viewer scores, the new viewer can't --------------
    game.perform("Bob", "finish_round")
    assert isinstance(game.state, RoundIntermission)
    game.perform("Bob", "start_next_round")
    assert game.round is Round.BONUS
    assert snapshot(game, "Carol")["roundLabel"] == "Bonus Jeffpardy"

    game.perform("Bob", "pick_clue", clue_id="dj-c1-400")
    game.perform("Bob", "open_buzzers")
    with pytest.raises(InvalidAction, match="audience watches"):
        game.buzz("Alice")
    with pytest.raises(InvalidAction, match="sitting this one out with the audience"):
        game.perform("Bob", "select_buzzer", player_id="Alice")
    game.buzz("Carol")
    game.perform("Bob", "select_buzzer", player_id="Carol")
    game.perform("Bob", "correct")  # Carol +400 -> 400

    game.perform("Bob", "pick_clue", clue_id="dj-c1-600")
    game.perform("Bob", "open_buzzers")
    game.buzz("Carol")
    game.perform("Bob", "select_buzzer", player_id="Carol")
    with pytest.raises(InvalidAction, match="finish answering"):
        game.perform("Bob", "set_viewer", player_id="Carol", viewer=True)
    game.perform("Bob", "correct")  # Carol +600 -> 1000
    game.perform("Bob", "finish_round")  # Bonus board done -> Ultimate

    assert game.round is Round.ULTIMATE
    assert snapshot(game, "Bob")["roundLabel"] == "Ultimate Jeffpardy"

    # -- Ultimate: the audience sits out the wagers and the responses --------
    game.perform("Bob", "open_final_wagering")
    with pytest.raises(InvalidAction, match="audience watches"):
        game.wager("Alice", 100)
    with pytest.raises(InvalidAction, match="between 0 and 0"):
        game.wager("Dana", 1)  # broke: only $0 is legal
    game.wager("Dana", 0)
    game.wager("Carol", 1000)  # everything she's got
    game.perform("Bob", "close_final_wagering")
    with pytest.raises(InvalidAction, match="audience watches"):
        game.final_answer("Alice", "what is Jeffpardy?")
    game.final_answer("Carol", "what is Jeffpardy?")
    game.final_answer("Dana", "no idea")

    game.perform("Bob", "start_final_reveal")
    with pytest.raises(InvalidAction, match="right or wrong"):
        game.perform("Bob", "grade_final", player_id="Carol", correct="yes")
    game.perform("Bob", "grade_final", player_id="Carol", correct=True)  # 1000 -> 2000
    game.perform("Bob", "grade_final", player_id="Dana", correct=False)
    game.perform("Bob", "end_game")

    # -- review: fix the scores, un-bench, then close for good ---------------
    assert isinstance(game.state, Review)
    game.perform("Bob", "adjust_score", player_id="Alice", value=400)  # -400 -> 0
    game.perform("Bob", "set_viewer", player_id="Alice", viewer=False)
    game.perform("Bob", "close_game")

    assert game.closed
    assert game.players == {"Bob": 0, "Carol": 2000, "Dana": -400, "Alice": 0}
    assert game.viewers == set()
    with pytest.raises(InvalidAction, match="isn't on tonight"):
        game.perform("Bob", "adjust_score", player_id="Carol", value=50)
    with pytest.raises(InvalidAction, match="show has ended"):
        game.perform("Bob", "undo")
    with pytest.raises(InvalidAction, match="show has ended"):
        game.join("Mallory")
    with pytest.raises(InvalidAction, match="show has ended"):
        game.leave("Carol")


def test_full_game_speedrun_with_late_arrival():
    """Empty boards, a mid-game joiner, and the only wager a pauper has."""
    game = make_game()
    game.join("Ann")  # host
    game.join("Bea")

    # quirk: burn straight through an empty Jeffpardy board
    game.perform("Ann", "start_game")
    game.perform("Ann", "finish_intro")
    game.perform("Ann", "finish_round")  # nothing picked, nothing scored
    game.perform("Ann", "start_next_round")

    # quirk: someone wanders in during Bonus and immediately scores
    game.join("Max")
    assert game.players["Max"] == 0
    game.perform("Ann", "pick_clue", clue_id="dj-c0-200")
    game.perform("Ann", "open_buzzers")
    game.buzz("Max")
    game.perform("Ann", "select_buzzer", player_id="Max")
    game.perform("Ann", "correct")  # Max +200
    game.perform("Ann", "finish_round")  # Bonus done -> Ultimate directly
    assert game.round is Round.ULTIMATE

    # quirk: the final runs even for a table that never played a clue
    game.perform("Ann", "open_final_wagering")
    game.wager("Bea", 0)  # her entire net worth
    with pytest.raises(InvalidAction, match="between 0 and 200"):
        game.wager("Max", 201)  # one dollar over the cap
    game.wager("Max", 200)
    game.perform("Ann", "close_final_wagering")
    with pytest.raises(InvalidAction, match="write your response"):
        game.final_answer("Bea", "   ")
    game.final_answer("Bea", "who knows")
    game.final_answer("Max", "the answer")
    game.perform("Ann", "start_final_reveal")
    game.perform("Ann", "grade_final", player_id="Max", correct=True)  # 200 -> 400
    with pytest.raises(InvalidAction, match="already called"):
        game.perform("Ann", "grade_final", player_id="Max", correct=False)
    game.perform("Ann", "grade_final", player_id="Bea", correct=False)
    game.perform("Ann", "end_game")
    assert snapshot(game, "Ann")["players"] == {"Bea": 0, "Max": 400}
    game.perform("Ann", "close_game")
    assert game.closed


def test_gamely_jeff_quest_flow():
    """The jackpot clue: the host flips it to whoever holds the board — that
    player risks alone, judged on the risk (show rules, our flavor)."""
    game = seated()
    to_board(game)

    # placement: home square of the current board; re-armed per round; none at Ultimate
    quest = game.active_quest
    assert quest is not None and quest in game.board.clues
    assert quest == list(game.board.clues)[-1]

    # nobody holds the board, so the flip can't happen yet — the host decides
    with pytest.raises(InvalidAction, match="give someone the pick"):
        game.perform("Alice", "pick_clue", clue_id=quest)
    assert isinstance(game.state, Board)

    # free reign: the host hands the quest to whoever they want before the flip
    game.perform("Alice", "adjust_score", player_id="Bob", value=600)
    game.perform("Alice", "set_picker", player_id="Bob")
    assert snapshot(game)["picker"] == "Bob"
    game.perform("Alice", "pick_clue", clue_id=quest)
    assert isinstance(game.state, QuestWager)
    assert game.state.player_id == "Bob"  # the picker owns it — no buzz race
    assert action_names(game) == {"begin_quest_answer", "pause", "set_viewer"}

    with pytest.raises(InvalidAction, match="waiting on.*wager"):
        game.perform("Alice", "begin_quest_answer")  # the host can't jump the gun
    with pytest.raises(InvalidAction, match="only Bob plays this quest"):
        game.quest_wager("Carol", 100)
    with pytest.raises(InvalidAction, match="between 0 and 600"):
        game.quest_wager("Bob", 601)

    # undoing the flip rewinds the wager, back to the pick round — the clock stays
    game.quest_wager("Bob", 400)
    game.perform("Alice", "undo")
    assert isinstance(game.state, Board)
    assert game.quest_wagers == {}
    assert game.picker == "Bob"  # the clock rides the snapshot too

    # re-flip; a made wager survives an undo that only rewinds "begin"
    game.perform("Alice", "pick_clue", clue_id=quest)
    game.quest_wager("Bob", 400)
    game.perform("Alice", "begin_quest_answer")
    assert isinstance(game.state, QuestAnswering)
    assert action_names(game) == {"correct", "incorrect", "pause", "set_viewer"}
    game.perform("Alice", "undo")
    assert isinstance(game.state, QuestWager)
    assert game.quest_wagers == {"Bob": 400}

    # graded on the wager (not the $1000 clue); a miss spends the quest for
    # everyone and leaves the clock where it was
    game.perform("Alice", "begin_quest_answer")
    game.perform("Alice", "incorrect")
    assert game.players["Bob"] == 200  # 600 − 400
    assert isinstance(game.state, Board)
    assert game.picker == "Bob"
    with pytest.raises(InvalidAction, match="already played"):
        game.perform("Alice", "pick_clue", clue_id=quest)  # spent for everyone

    # the Bonus board gets its own quest — broke Carol may match the table top
    # (show rule), and a hit puts the holder on the board (shared judged-hook)
    game.perform("Alice", "finish_round")
    game.perform("Alice", "start_next_round")
    assert game.active_quest == list(game.board.clues)[-1]
    game.perform("Alice", "set_picker", player_id="Carol")
    game.perform("Alice", "pick_clue", clue_id=game.active_quest)
    assert isinstance(game.state, QuestWager)
    assert game.state.player_id == "Carol"
    game.quest_wager("Carol", 150)  # broke: table max is Bob's 200
    with pytest.raises(InvalidAction, match="between 0 and 200"):
        game.quest_wager("Carol", 601)
    game.perform("Alice", "begin_quest_answer")
    game.perform("Alice", "correct")
    assert game.players == {"Bob": 200, "Carol": 150}
    assert isinstance(game.state, Board)
    assert game.picker == "Carol"  # right answer (or quest hit) picks next

    # ...the Bonus quest spends like any other, Ultimate has none
    with pytest.raises(InvalidAction, match="already played"):
        game.perform("Alice", "pick_clue", clue_id=game.active_quest)
    game.perform("Alice", "finish_round")
    assert game.active_quest is None


def test_the_board_follows_the_show_and_the_host_can_give_it():
    """Right answer chooses next (automatic hook); the host's button shares it."""
    game = seated()
    to_board(game)
    assert game.picker is None  # nobody holds the board yet

    # the host's button: set, change, and clear (None = open board again)
    game.set_picker("Bob")
    assert game.picker == "Bob"
    game.set_picker("Carol")
    assert game.picker == "Carol"
    game.perform("Alice", "set_picker", player_id=None)
    assert game.picker is None

    # the automatic way: a right answer gives the winner the board
    game.set_picker("Bob")
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    game.perform("Alice", "open_buzzers")
    game.buzz("Carol")
    game.perform("Alice", "select_buzzer", player_id="Carol")
    game.perform("Alice", "correct")
    assert game.picker == "Carol"  # same hook, same field

    # a miss (and the skip that follows) leaves the board where it was
    game.perform("Alice", "pick_clue", clue_id="j-c1-400")
    game.perform("Alice", "open_buzzers")
    game.buzz("Bob")
    game.perform("Alice", "select_buzzer", player_id="Bob")
    game.perform("Alice", "incorrect")
    game.perform("Alice", "close_clue")
    assert game.picker == "Carol"

    # undo hands the board back
    game.perform("Alice", "set_picker", player_id="Bob")
    assert game.picker == "Bob"
    game.perform("Alice", "undo")
    assert game.picker == "Carol"

    # leaving (or joining the audience) gives the board back
    game.set_picker("Bob")
    game.leave("Bob")
    assert game.picker is None
    game.set_picker("Carol")
    game.perform("Alice", "set_viewer", player_id="Carol", viewer=True)
    assert game.picker is None


def test_set_picker_rubber_stamps_hostile_payloads():
    game = seated()
    to_board(game)
    with pytest.raises(InvalidAction, match="contender on this show"):
        game.set_picker("Ghost")
    with pytest.raises(InvalidAction, match="reads the clues"):
        game.set_picker("Alice")
    with pytest.raises(InvalidAction, match="name a contender"):
        game.set_picker(42)
    game.perform("Alice", "set_viewer", player_id="Bob", viewer=True)
    with pytest.raises(InvalidAction, match="hold the pick"):
        game.set_picker("Bob")


def test_the_revision_clock_only_moves_forward():
    """Undo rewinds the domain; the transport clock never rewinds with it."""
    game = seated()
    to_board(game)
    r0 = game.rev
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    game.perform("Alice", "open_buzzers")
    assert game.rev == r0 + 2
    game.buzz("Bob")
    assert game.rev == r0 + 3  # player events advance it too
    game.perform("Alice", "undo")  # back to Asking…
    assert isinstance(game.state, Asking)
    assert game.rev == r0 + 4  # …but the clock moved on (published as newer)
    with pytest.raises(InvalidAction):
        game.perform("Alice", "set_viewer", player_id="Ghost", viewer=True)  # rolls back
    assert game.rev == r0 + 5  # failed action: domain restored, clock still forward


def test_mutations_must_be_synchronous():
    """run() rejects awaitables — sync-on-the-loop *is* the serialization."""

    async def scenario():
        mgr = GameManager()
        game = seated()
        mgr._games[game.code] = game

        async def sneaky(_game):
            return None

        with pytest.raises(TypeError, match="synchronous"):
            await mgr.run(game.code, lambda g: sneaky(g))
        await mgr.run(game.code, lambda g: g.join("Dana"))  # plain sync actions still run
        assert "Dana" in game.players

    asyncio.run(scenario())


def test_score_adjust_is_a_delta_so_a_stale_screen_needs_no_revision_gate():
    """adjust_score adds, never sets: two clicks from one render both land."""
    game = seated()
    to_board(game)
    game.perform("Alice", "adjust_score", player_id="Bob", value=200)
    game.perform("Alice", "adjust_score", player_id="Bob", value=200)
    assert game.players["Bob"] == 400
