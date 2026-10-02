"""Deliberate abuse of the game API: hostile payloads, hidden surface probing,
capability probing, and undo games.

Everything here drives `Game` directly — the same entry points the routers
call — with inputs no honest UI would send. Router and cookie auth are out of
scope (they live in test_api); this is about the game holding its own against
a programmatic caller.
"""

import copy

import pytest

from jeffpardy.errors import InvalidAction
from jeffpardy.game import snapshot
from jeffpardy.states import (
    Answering,
    Asking,
    Board,
    BuzzersOpen,
    FinalReveal,
    Lobby,
    QuestAnswering,
    QuestWager,
)

from test_game import make_game, seated, to_board

# ---------------------------------------------------------------------------
# Fixtures: parked in whichever state the abuse needs
# ---------------------------------------------------------------------------


@pytest.fixture
def lobby():
    return seated()


@pytest.fixture
def board(lobby):
    to_board(lobby)
    return lobby


@pytest.fixture
def buzzers(board):
    board.perform("Alice", "pick_clue", clue_id="j-c0-200")
    board.perform("Alice", "open_buzzers")
    return board


@pytest.fixture
def wagering(board):
    """Both rounds burned through, sitting in FinalWagering."""
    board.perform("Alice", "finish_round")
    board.perform("Alice", "start_next_round")
    board.perform("Alice", "finish_round")
    board.perform("Alice", "open_final_wagering")
    return board


@pytest.fixture
def final_reveal(wagering):
    wagering.wager("Bob", 0)  # Bob is broke — $0 is his only legal bid
    wagering.perform("Alice", "close_final_wagering")
    wagering.final_answer("Bob", "a guess")
    wagering.perform("Alice", "start_final_reveal")
    return wagering


@pytest.fixture
def closed(final_reveal):
    final_reveal.perform("Alice", "end_game")
    final_reveal.perform("Alice", "close_game")
    return final_reveal


# ---------------------------------------------------------------------------
# The API surface: what `perform` will and won't dispatch by name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["", "nonsense", "close", "hack_the_score", "help"])
def test_unknown_action_names_are_refused(lobby, action):
    with pytest.raises(InvalidAction, match="isn't a move on this show"):
        lobby.perform("Alice", action)


@pytest.mark.parametrize(
    "action",
    [
        pytest.param(p, id=p)
        for p in (
            "join", "leave", "buzz", "wager", "perform",
            "_host_run", "_commit", "_event", "mark_viewer",
            "transfer_host", "score_correct", "require_selectable",
            "__class__", "__init__",
        )
    ],
)
def test_internals_are_callable_but_never_dispatchable(lobby, action):
    """Real methods (and dunders) exist on the object, but only advertised
    capabilities pass the allow-list — probing getattr leaks nothing."""
    with pytest.raises(InvalidAction, match="isn't on tonight"):
        lobby.perform("Alice", action)


@pytest.mark.parametrize(
    "attr",
    ["state", "players", "host", "viewers", "code", "_history", "boards"],
)
def test_non_callable_attributes_are_unknown_actions(lobby, attr):
    with pytest.raises(InvalidAction, match="isn't a move on this show"):
        lobby.perform("Alice", attr)


@pytest.mark.parametrize(
    "action, payload, message",
    [
        pytest.param("pick_clue", {}, "fizzled", id="missing-kwargs"),
        pytest.param(
            "pick_clue",
            {"clue_id": "j-c0-200", "sender": "Bob"},
            "fizzled",
            id="sender-injection",
        ),
        pytest.param(
            "adjust_score",
            {"player_id": "Bob", "value": 100, "host": "Mallory"},
            "fizzled",
            id="host-shadowing",
        ),
        pytest.param(
            "set_viewer",
            {"player_id": "Bob", "viewer": "yes"},
            "audience or contender",
            id="string-bool",
        ),
        pytest.param(
            "adjust_score", {"player_id": "Bob", "value": "100"}, "whole dollars",
            id="string-score",
        ),
        pytest.param(
            "adjust_score", {"player_id": "Bob", "value": True}, "whole dollars",
            id="bool-score",
        ),
        pytest.param(
            "adjust_score", {"player_id": "Nobody", "value": 10}, "contender on this show",
            id="ghost-member",
        ),
    ],
)
def test_hostile_payloads_become_domain_errors(board, action, payload, message):
    with pytest.raises(InvalidAction, match=message):
        board.perform("Alice", action, **payload)


@pytest.mark.parametrize(
    "player_id",
    [pytest.param(v, id=type(v).__name__) for v in (None, 42, ["Bob"], {"n": "x"})],
)
def test_select_buzzer_needs_a_string_member(buzzers, player_id):
    with pytest.raises(InvalidAction, match="fizzled"):
        buzzers.perform("Alice", "select_buzzer", player_id=player_id)


# ---------------------------------------------------------------------------
# pick_clue: anything but a real clue on this round's board
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "clue_id",
    [
        pytest.param(v, id=repr(v))
        for v in (None, 42, "", "j-c9-999", "dj-c0-200", "j-c0-200; DROP TABLE clues")
    ],
)
def test_pick_clue_wants_a_real_clue(board, clue_id):
    with pytest.raises(InvalidAction, match="isn't on this board"):
        board.perform("Alice", "pick_clue", clue_id=clue_id)


@pytest.mark.parametrize(
    "clue_id", [pytest.param(v, id=type(v).__name__) for v in ([], {}, set())]
)
def test_unhashable_clue_ids_do_not_explode(board, clue_id):
    with pytest.raises(InvalidAction, match="fizzled"):
        board.perform("Alice", "pick_clue", clue_id=clue_id)


# ---------------------------------------------------------------------------
# Wagers: type rubber-stamping and range checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "amount, message",
    [
        pytest.param("100", "whole dollars", id="string"),
        pytest.param(100.0, "whole dollars", id="float"),
        pytest.param(True, "whole dollars", id="bool"),
        pytest.param(None, "whole dollars", id="null"),
        pytest.param(float("nan"), "whole dollars", id="nan"),
        pytest.param(float("inf"), "whole dollars", id="inf"),
        pytest.param(-1, "between 0 and", id="negative"),
        pytest.param(10**18, "between 0 and", id="absurd"),
    ],
)
def test_wager_payloads_are_rubber_stamped(wagering, amount, message):
    with pytest.raises(InvalidAction, match=message):
        wagering.wager("Bob", amount)


def test_wager_rejects_second_bid_and_host(wagering):
    wagering.wager("Bob", 0)
    with pytest.raises(InvalidAction, match="already on the table"):
        wagering.wager("Bob", 0)
    with pytest.raises(InvalidAction, match="reads the clues"):
        wagering.wager("Alice", 0)


# ---------------------------------------------------------------------------
# grade_final: booleans only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "correct",
    [pytest.param(v, id=repr(v)) for v in ("yes", "no", 1, 0, None, [], "true")],
)
def test_grade_final_demands_a_boolean(final_reveal, correct):
    with pytest.raises(InvalidAction, match="right or wrong"):
        final_reveal.perform("Alice", "grade_final", player_id="Bob", correct=correct)


def test_grading_twice_is_refused(final_reveal):
    final_reveal.perform("Alice", "grade_final", player_id="Bob", correct=True)
    with pytest.raises(InvalidAction, match="already called"):
        final_reveal.perform("Alice", "grade_final", player_id="Bob", correct=False)


# ---------------------------------------------------------------------------
# View-only and host transfer rules
# ---------------------------------------------------------------------------


def test_set_viewer_probe_sequence(board):
    board.perform("Alice", "set_viewer", player_id="Bob", viewer=True)
    with pytest.raises(InvalidAction, match="already out with the audience"):
        board.perform("Alice", "set_viewer", player_id="Bob", viewer=True)
    board.perform("Alice", "set_viewer", player_id="Bob", viewer=False)
    with pytest.raises(InvalidAction, match="already at the podium"):
        board.perform("Alice", "set_viewer", player_id="Bob", viewer=False)


@pytest.mark.parametrize(
    "target, message",
    [
        pytest.param("Mallory", "contender on this show", id="ghost"),
        pytest.param("Alice", "contender on this show", id="the-host-is-not-a-player"),
    ],
)
def test_set_viewer_needs_a_seated_member(board, target, message):
    with pytest.raises(InvalidAction, match=message):
        board.perform("Alice", "set_viewer", player_id=target, viewer=True)


def test_promote_host_is_lobby_only(board):
    with pytest.raises(InvalidAction, match="isn't on tonight"):
        board.perform("Alice", "promote_host", player_id="Bob")


def test_promote_needs_a_seated_member(lobby):
    with pytest.raises(InvalidAction, match="contender on this show"):
        lobby.perform("Alice", "promote_host", player_id="Mallory")


# ---------------------------------------------------------------------------
# Player events with garbage identities
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", [pytest.param(v, id=type(v).__name__) for v in (None, 42, ["Bob"])]
)
def test_buzz_needs_a_string_name(buzzers, name):
    with pytest.raises(InvalidAction):
        buzzers.buzz(name)


@pytest.mark.parametrize(
    "name", [pytest.param(v, id=type(v).__name__) for v in (None, 42, {"name": "Bob"})]
)
def test_wager_needs_a_string_name(wagering, name):
    with pytest.raises(InvalidAction):
        wagering.wager(name, 0)


@pytest.mark.parametrize(
    "name", [pytest.param(v, id=repr(v)) for v in ("", "   ", "x" * 25, "\n\t")]
)
def test_join_rejects_bad_names(lobby, name):
    with pytest.raises(InvalidAction, match="tell the room|stage names"):
        lobby.join(name)


def test_join_accepts_the_boundary(lobby):
    lobby.join("x" * 24)
    assert "x" * 24 in lobby.players


@pytest.mark.parametrize(
    "variant", ["Bob", "  bob  ", "BOB", "bOb"]
)
def test_identity_normalization_round_trips(lobby, variant):
    assert lobby.player_key(variant) == "Bob"


# ---------------------------------------------------------------------------
# Capability gate: foreign actions for the state you're in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [
        pytest.param(a, id=a)
        for a in (
            "open_buzzers", "correct", "incorrect", "grade_final", "finish_intro",
            "close_final_wagering", "end_game", "close_game", "resume",
            "promote_host", "start_next_round",
        )
    ],
)
def test_board_refuses_foreign_capabilities(board, action):
    with pytest.raises(InvalidAction, match="isn't on tonight"):
        board.perform("Alice", action)


# ---------------------------------------------------------------------------
# Failed actions are no-ops
# ---------------------------------------------------------------------------


def test_failed_actions_leave_the_snapshot_untouched(buzzers):
    before = snapshot(buzzers, "Alice")
    with pytest.raises(InvalidAction):
        buzzers.perform("Alice", "select_buzzer", player_id="Nobody")
    with pytest.raises(InvalidAction):
        buzzers.perform("Alice", "adjust_score", player_id="Bob", value="x")
    with pytest.raises(InvalidAction):
        buzzers.perform("Alice", "pick_clue", clue_id="j-c0-200")  # foreign state
    after = snapshot(buzzers, "Alice")
    assert after["rev"] > before["rev"]  # rollback still publishes as newer
    after.pop("rev")
    before.pop("rev")
    assert after == before  # domain untouched


# ---------------------------------------------------------------------------
# Undo abuse
# ---------------------------------------------------------------------------


def test_undo_needs_the_host(lobby):
    with pytest.raises(InvalidAction, match="only the host runs the show"):
        lobby.perform("Bob", "undo")


def test_undo_with_no_history(lobby):
    with pytest.raises(InvalidAction, match="nothing to take back"):
        lobby.perform("Alice", "undo")


def test_deep_undo_walks_back_to_the_initial_snapshot():
    game = seated()
    game.join("Dana")
    initial = snapshot(game, "Alice")
    game.perform("Alice", "set_viewer", player_id="Carol", viewer=True)
    game.perform("Alice", "start_game")
    game.perform("Alice", "finish_intro")
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    game.perform("Alice", "open_buzzers")
    # no buzz here: buzzes are events (never rewound), so a buzz would still be
    # queued after the full walk-back — that merge rule is test_game's turf.
    game.perform("Alice", "select_buzzer", player_id="Bob")
    game.perform("Alice", "correct")
    game.perform("Alice", "adjust_score", player_id="Carol", value=-50)
    while game.can_undo:
        game.perform("Alice", "undo")
    final = snapshot(game, "Alice")
    assert final["rev"] > initial["rev"]  # domain rewound; the clock never is
    for key in ("rev", "viewers"):  # the transport clock and env are no Core
        final.pop(key)
        initial.pop(key)
    assert final == initial


def test_promote_host_is_environment_not_undoable(lobby):
    before = lobby.core
    lobby.perform("Alice", "promote_host", player_id="Bob")
    assert lobby.host == "Bob" and "Alice" in lobby.players
    assert lobby.core is before  # the crown is environment: the record never changed
    lobby.perform("Bob", "start_game")  # the new host runs the show
    lobby.perform("Bob", "undo")  # unwinds the game — not the crowning
    assert lobby.host == "Bob"
    assert isinstance(lobby.state, Lobby)
    assert "Alice" in lobby.players  # seats are environment too: nobody un-joins


def test_cores_are_never_mutated_in_place():
    """Undo-by-rebind only works if committed Cores are inert forever: one
    in-place container write anywhere would silently corrupt history."""
    game = seated()
    to_board(game)
    taken: list[tuple] = []

    def record():
        taken.append((game.core, copy.deepcopy(game.core)))

    record()
    game.perform("Alice", "pick_clue", clue_id="j-c0-200")
    record()
    game.perform("Alice", "open_buzzers")
    record()
    game.buzz("Bob")
    record()
    game.perform("Alice", "select_buzzer", player_id="Bob")
    record()
    game.perform("Alice", "incorrect")  # reopens the race, mutates queue/scores
    record()
    game.buzz("Carol")
    record()
    game.perform("Alice", "reveal_answer")
    record()
    game.perform("Alice", "undo")
    record()
    game.perform("Alice", "undo")
    record()
    for i, (core, original) in enumerate(taken):
        assert core == original, f"core #{i} was mutated in place"


def test_failed_host_action_leaves_the_core_object_untouched():
    game = seated()
    to_board(game)
    before = game.core
    checkpoints = len(game._history)
    with pytest.raises(InvalidAction):
        game.perform("Alice", "adjust_score", player_id="Ghost", value=100)
    assert game.core is before  # immutable: refusal means literally nothing happened
    assert len(game._history) == checkpoints  # ...and no history checkpoint either


# ---------------------------------------------------------------------------
# Closed means closed
# ---------------------------------------------------------------------------


def _every_capability_name() -> list[str]:
    states = [
        Lobby(),
        Board(),
        Asking(clue_id="x"),
        BuzzersOpen(clue_id="x"),
        Answering(clue_id="x", player_id="x"),
        FinalReveal(),
    ]
    names = {a.name for st in states for a in st.available_host_actions}
    return sorted(names | {"undo"})


@pytest.mark.parametrize("action", _every_capability_name())
def test_closed_game_refuses_every_host_capability(closed, action):
    with pytest.raises(InvalidAction):
        closed.perform("Alice", action)


def test_closed_game_refuses_player_events(closed):
    with pytest.raises(InvalidAction, match="show has ended"):
        closed.join("Ghost")
    with pytest.raises(InvalidAction, match="show has ended"):
        closed.leave("Bob")
    with pytest.raises(InvalidAction):
        closed.buzz("Bob")
    with pytest.raises(InvalidAction):
        closed.wager("Bob", 0)
    with pytest.raises(InvalidAction):
        closed.final_answer("Bob", "hi")


# ---------------------------------------------------------------------------
# Gamely Jeff Quest payloads
# ---------------------------------------------------------------------------


@pytest.fixture
def quest_claimed(board):
    """The quest has been flipped to Bob (score 400); he's picking a risk."""
    board.perform("Alice", "adjust_score", player_id="Bob", value=400)
    board.perform("Alice", "set_picker", player_id="Bob")
    board.perform("Alice", "pick_clue", clue_id=board.active_quest)
    assert isinstance(board.state, QuestWager)
    return board


@pytest.mark.parametrize(
    "amount, message",
    [
        pytest.param("50", "whole dollars", id="string"),
        pytest.param(50.0, "whole dollars", id="float"),
        pytest.param(True, "whole dollars", id="bool"),
        pytest.param(None, "whole dollars", id="null"),
        pytest.param(float("nan"), "whole dollars", id="nan"),
        pytest.param(-1, "between 0 and", id="negative"),
        pytest.param(401, "between 0 and", id="over-cap"),
    ],
)
def test_quest_wager_payloads_are_rubber_stamped(quest_claimed, amount, message):
    with pytest.raises(InvalidAction, match=message):
        quest_claimed.quest_wager("Bob", amount)


def test_quest_wager_is_claimant_only_and_strangers_rejected(quest_claimed):
    with pytest.raises(InvalidAction, match="only Bob plays this quest"):
        quest_claimed.quest_wager("Carol", 0)
    with pytest.raises(InvalidAction, match="reads the clues"):
        quest_claimed.quest_wager("Alice", 0)
    with pytest.raises(InvalidAction, match="take a seat"):
        quest_claimed.quest_wager("Ghost", 0)


def test_begin_quest_answer_requires_a_wager(quest_claimed):
    with pytest.raises(InvalidAction, match="waiting on.*wager"):
        quest_claimed.perform("Alice", "begin_quest_answer")
    quest_claimed.quest_wager("Bob", 100)
    quest_claimed.perform("Alice", "begin_quest_answer")
    assert isinstance(quest_claimed.state, QuestAnswering)


def test_quest_wager_outside_the_quest_is_refused(board):
    with pytest.raises(InvalidAction, match="quest wager isn't on the table"):
        board.quest_wager("Bob", 0)


@pytest.mark.parametrize(
    "action",
    [pytest.param(a, id=a) for a in ("begin_quest_answer", "select_buzzer", "correct")],
)
def test_board_refuses_quest_capabilities(board, action):
    with pytest.raises(InvalidAction, match="isn't on tonight"):
        board.perform("Alice", action)


# ---------------------------------------------------------------------------
# Buzz epochs: a buzz must name the race it saw
# ---------------------------------------------------------------------------


def test_a_buzz_names_the_race_it_saw(buzzers):
    game = buzzers
    race = game.race_token
    assert race  # opening the buzzers mints one
    game.buzz("Bob", clue_id=game.state.clue_id, buzz_token=race)
    # same race, same token: the next player sails through — no false rejections
    game.buzz("Carol", clue_id=game.state.clue_id, buzz_token=race)
    assert game.buzz_queue == ["Bob", "Carol"]
    game.join("Dana")  # a fresh face at the table
    with pytest.raises(InvalidAction, match="has left the board"):
        game.buzz("Dana", clue_id="dj-c0-9999", buzz_token=race)
    with pytest.raises(InvalidAction, match="ring-in has passed"):
        game.buzz("Dana", clue_id=game.state.clue_id, buzz_token="stale-token")
    assert "Dana" not in game.buzz_queue


def test_undo_back_into_a_race_kills_first_pass_buzzes(buzzers):
    game = buzzers
    game.buzz("Bob")
    first = game.race_token
    game.perform("Alice", "select_buzzer", player_id="Bob")
    assert game.race_token is None  # race over
    game.perform("Alice", "undo")  # back into the open race
    assert isinstance(game.state, BuzzersOpen)
    assert game.race_token and game.race_token != first  # a fresh pass
    with pytest.raises(InvalidAction, match="ring-in has passed"):
        game.buzz("Carol", clue_id=game.state.clue_id, buzz_token=first)
    game.buzz("Carol", clue_id=game.state.clue_id, buzz_token=game.race_token)
    assert "Carol" in game.buzz_queue  # post-undo buzzes are accepted


def test_a_failed_host_action_leaves_the_race_token_alone(buzzers):
    game = buzzers
    game.buzz("Bob")
    token = game.race_token
    with pytest.raises(InvalidAction):
        game.perform("Alice", "adjust_score", player_id="Ghost", value=100)  # rolls back
    assert game.race_token == token  # the race itself didn't change
    game.buzz("Carol", clue_id=game.state.clue_id, buzz_token=token)
    assert "Carol" in game.buzz_queue  # in-flight buzzes for it stay valid
