"""Integration tests: REST + WebSocket against the real FastAPI app.

Rooms are created with the offline fallback board (``use_fallback``) so the
suite is deterministic and network-free. Each simulated person gets their own
``TestClient`` — cookies are identity, so one client per actor.

Creative scenarios covered: nobody answers, everyone answers wrong, the
answerer leaves mid-clue, everyone leaves, rejoining after a leave, late
joiners watching over WebSocket, viewer-filtered answer visibility, pause
blocking buzzes, undo over the wire, and the closed room refusing all life.
"""

import pytest
from fastapi.testclient import TestClient

from jeffpardy.main import create_app

CLUE = "j-c0-200"


@pytest.fixture()
def app(monkeypatch):
    async def fake_categories():
        return [{"id": 1, "name": "Mocked"}]

    monkeypatch.setattr("jeffpardy.main.fetch_categories", fake_categories)
    return create_app()


@pytest.fixture()
def client(app):
    return TestClient(app)


def new_room(client) -> str:
    res = client.post("/api/rooms", json={"use_fallback": True})
    assert res.status_code == 200, res.text
    return res.json()["code"]


def join(client, code: str, name: str) -> dict:
    res = client.post(f"/api/rooms/{code}/join", json={"name": name})
    assert res.status_code == 200, res.text
    return res.json()


def host_action(client, code: str, action: str, **payload) -> dict:
    return client.post(f"/api/rooms/{code}/host", json={"action": action, **payload})


def player_event(client, code: str, action: str, **payload) -> dict:
    return client.post(f"/api/rooms/{code}/player", json={"action": action, **payload})


def table(app, *names: str):
    """A room with the first caller as host, plus one TestClient per name."""
    host_client = TestClient(app)
    code = new_room(host_client)
    clients = {name: TestClient(app) for name in names}
    join(host_client, code, "Host")
    for name, c in clients.items():
        join(c, code, name)
    return code, host_client, clients


def to_board(host_client, code: str) -> None:
    assert host_action(host_client, code, "start_game").status_code == 200
    assert host_action(host_client, code, "finish_intro").status_code == 200


def room_state(client, code: str) -> dict:
    res = client.get(f"/api/rooms/{code}")
    assert res.status_code == 200, res.text
    return res.json()


# -- basics ------------------------------------------------------------------


def test_health_and_categories(client):
    assert client.get("/api/health").json() == {"status": "ok", "game": "jeffpardy"}
    cats = client.get("/api/categories").json()["categories"]
    assert cats == [{"id": 1, "name": "Mocked"}]


def test_unknown_room_is_404_everywhere(client):
    assert client.get("/api/rooms/9999").status_code == 404
    assert client.post("/api/rooms/9999/join", json={"name": "X"}).status_code == 404
    assert host_action(client, "9999", "start_game").status_code == 404


def test_create_room_starts_in_the_lobby_with_full_boards(client):
    res = client.post("/api/rooms", json={"use_fallback": True})
    assert res.status_code == 200
    data = res.json()
    assert len(data["code"]) == 4 and data["code"].isdigit()
    assert data["state"] == "lobby"
    assert data["host"] is None
    assert data["players"] == {}
    assert data["round"] == "main"
    assert data["canUndo"] is False
    assert "start_game" in {a["name"] for a in data["availableHostActions"]}
    assert len(data["clues"]) == 30  #6x5 Jeffpardy grid
    assert all(c["id"].startswith("j-") for c in data["clues"])
    assert data["final"]["category"]  # final clue is dealt at creation


def test_first_joiner_leads_and_others_play(client):
    code = new_room(client)
    as_host = join(client, code, "Alice")
    assert as_host["host"] == "Alice"
    assert as_host["players"] == {}  # the host does not play
    assert client.get("/api/me").json() == {"name": "Alice"}
    other = TestClient(app := client.app)
    as_player = join(other, code, "Bob")
    assert as_player["players"] == {"Bob": 0}
    # Anonymous spectator still sees the leader.
    anon = TestClient(app)
    assert room_state(anon, code)["host"] == "Alice"
    # Rejoining with the same identity changes nothing.
    assert join(other, code, "BOB")["players"] == {"Bob": 0}


def test_me_reflects_the_identity_cookie(client):
    assert client.get("/api/me").json() == {"name": None}
    code = new_room(client)
    join(client, code, "Solo")
    assert client.get("/api/me").json() == {"name": "Solo"}


# -- host authority ----------------------------------------------------------


def test_only_the_host_may_drive_the_game(app):
    code, host_client, players = table(app, "Bob")
    bob = players["Bob"]
    res = host_action(bob, code, "start_game")
    assert res.status_code == 400
    assert "only the host runs the show" in res.json()["detail"]
    # A client that never joined has no identity either.
    anon = TestClient(app)
    assert host_action(anon, code, "start_game").status_code == 400
    assert host_action(host_client, code, "start_game").status_code == 200
    assert room_state(host_client, code)["state"] == "intro"


def test_capability_gate_over_rest(app):
    code, host_client, _ = table(app)
    res = host_action(host_client, code, "close_clue")  # Lobby lacks it
    assert res.status_code == 400
    assert "isn't on tonight" in res.json()["detail"]
    assert host_action(host_client, code, "start_game").status_code == 200
    res = host_action(host_client, code, "start_game")  # already started
    assert res.status_code == 400


# -- WebSocket ---------------------------------------------------------------


def test_ws_delivers_state_and_only_host_can_send_host_messages(app):
    code, host_client, players = table(app, "Bob")
    bob = players["Bob"]
    with host_client.websocket_connect(f"/ws/rooms/{code}?name=Host") as hw:
        first = hw.receive_json()
        assert first["type"] == "state"
        assert first["state"]["state"] == "lobby"
        with bob.websocket_connect(f"/ws/rooms/{code}?name=Bob") as bw:
            assert bw.receive_json()["state"]["players"] == {"Bob": 0}
            # The player tries to start the game himself:
            bw.send_json({"type": "host", "action": "start_game"})
            err = bw.receive_json()
            assert err["type"] == "error"
            assert "only the host runs the show" in err["message"]
            # The host does it properly; both sockets get the new state.
            hw.send_json({"type": "host", "action": "start_game"})
            assert hw.receive_json()["state"]["state"] == "intro"
            assert bw.receive_json()["state"]["state"] == "intro"


def test_ws_rejects_garbled_messages_without_killing_the_socket(app):
    code, host_client, _ = table(app)
    with host_client.websocket_connect(f"/ws/rooms/{code}?name=Host") as ws:
        ws.receive_json()  # initial state
        ws.send_json({"type": "breakdance"})
        err = ws.receive_json()
        assert err["type"] == "error"
        ws.send_json(["not", "an", "object"])
        assert ws.receive_json()["type"] == "error"
        # Still alive:
        ws.send_json({"type": "host", "action": "start_game"})
        assert ws.receive_json()["state"]["state"] == "intro"


def test_late_join_broadcast_reaches_connected_players(app):
    code, host_client, players = table(app, "Bob")
    bob = players["Bob"]
    with host_client.websocket_connect(f"/ws/rooms/{code}?name=Host") as hw:
        hw.receive_json()
        carol = TestClient(app)
        join(carol, code, "Carol")  # REST join while Host watches
        seen = hw.receive_json()["state"]["players"]
        assert set(seen) == {"Bob", "Carol"}
    assert room_state(bob, code)["players"]["Carol"] == 0


def test_answer_is_visible_to_host_but_not_players_until_revealed(app):
    code, host_client, players = table(app, "Bob")
    bob = players["Bob"]
    to_board(host_client, code)
    with host_client.websocket_connect(f"/ws/rooms/{code}?name=Host") as hw, \
            bob.websocket_connect(f"/ws/rooms/{code}?name=Bob") as bw:
        hw.receive_json()
        bw.receive_json()
        assert host_action(host_client, code, "pick_clue", clue_id=CLUE).status_code == 200
        host_view = hw.receive_json()["state"]
        bob_view = bw.receive_json()["state"]
        assert host_view["answer"] == "H2O"  # fallback Science $200
        assert bob_view["answer"] is None
        assert bob_view["question"]  # but the clue itself is on screen
        host_action(host_client, code, "reveal_answer")
        assert hw.receive_json()["state"]["answer"] == "H2O"
        assert bw.receive_json()["state"]["answer"] == "H2O"


def test_answer_stays_secret_while_a_player_has_the_floor(app):
    """The reveal is the only door: a contender answering (and everyone else
    who could still buzz after a miss) must not read the answer off the clue
    card, so the room can't be handed the solution before the host says it."""
    code, host_client, players = table(app, "Bob", "Carol")
    to_board(host_client, code)
    host_action(host_client, code, "pick_clue", clue_id=CLUE)
    host_action(host_client, code, "open_buzzers")
    assert player_event(players["Bob"], code, "buzz").status_code == 200
    assert host_action(host_client, code, "select_buzzer", player_id="Bob").status_code == 200

    assert room_state(host_client, code)["state"] == "answering"
    assert room_state(host_client, code)["answer"] == "H2O"  # host runs the show
    for who in ("Bob", "Carol"):  # the answerer and the still-eligible observer
        view = room_state(players[who], code)
        assert view["answer"] is None
        assert view["question"]  # the clue text is still on screen
    assert room_state(players["Bob"], code)["answerer"] == "Bob"  # they know it's their turn

    # The reveal is the only door, and it isn't even offered mid-answer — the
    # host must rule (or back out to the buzzers) before the room may read it.
    assert host_action(host_client, code, "reveal_answer").status_code == 400
    assert host_action(host_client, code, "incorrect").status_code == 200  # back to the buzzers
    assert room_state(players["Bob"], code)["answer"] is None
    host_action(host_client, code, "reveal_answer")
    assert room_state(players["Bob"], code)["answer"] == "H2O"
    assert room_state(players["Carol"], code)["answer"] == "H2O"
    assert room_state(host_client, code)["answer"] == "H2O"


# -- creative game situations ------------------------------------------------


def test_quest_marker_is_host_truth_not_room_knowledge(app):
    """The Gamely Jeff Quest is this game's Daily Double: its home square is
    host truth. The host needs it to run the flip; every other seat sees an
    unmarked board so the jackpot is found by playing, not by reading."""
    code, host_client, players = table(app, "Bob", "Carol")
    to_board(host_client, code)

    quest = room_state(host_client, code)["activeQuest"]
    assert quest  # armed for the board round, and the host can see where
    assert room_state(players["Bob"], code)["activeQuest"] is None
    assert room_state(players["Carol"], code)["activeQuest"] is None

    # The square stays hidden while the room plays other clues…
    host_action(host_client, code, "pick_clue", clue_id="j-c0-200")
    assert room_state(players["Bob"], code)["activeQuest"] is None
    host_action(host_client, code, "reveal_answer")
    host_action(host_client, code, "close_clue")
    assert room_state(players["Bob"], code)["activeQuest"] is None

    # …and the ordered flip still works for the host, who holds the address.
    assert host_action(host_client, code, "adjust_score", player_id="Bob", value=400).status_code == 200
    host_action(host_client, code, "set_picker", player_id="Bob")
    assert host_action(host_client, code, "pick_clue", clue_id=quest).status_code == 200
    assert room_state(host_client, code)["state"] == "quest_wager"


def test_nobody_answers_host_skips_and_the_clue_stays_playable(app):
    code, host_client, players = table(app, "Bob")
    to_board(host_client, code)
    assert host_action(host_client, code, "pick_clue", clue_id=CLUE).status_code == 200
    assert host_action(host_client, code, "open_buzzers").status_code == 200
    # Cricket noises. Host reveals for the room, then closes it out.
    assert host_action(host_client, code, "reveal_answer").status_code == 200
    assert host_action(host_client, code, "close_clue").status_code == 200
    state = room_state(host_client, code)
    assert state["state"] == "board"
    assert next(c for c in state["clues"] if c["id"] == CLUE)["state"] == "board"
    # Nobody scored: the clue may simply be picked again.
    assert host_action(host_client, code, "pick_clue", clue_id=CLUE).status_code == 200


def test_everyone_answers_wrong_then_host_spends_the_clue(app):
    code, host_client, players = table(app, "Bob", "Carol")
    to_board(host_client, code)
    host_action(host_client, code, "pick_clue", clue_id=CLUE)
    host_action(host_client, code, "open_buzzers")
    assert player_event(players["Bob"], code, "buzz").status_code == 200
    assert player_event(players["Carol"], code, "buzz").status_code == 200
    assert room_state(host_client, code)["buzzQueue"] == ["Bob", "Carol"]
    host_action(host_client, code, "select_buzzer", player_id="Bob")
    assert host_action(host_client, code, "incorrect").status_code == 200
    # Bob can't be handed the clue again...
    res = host_action(host_client, code, "select_buzzer", player_id="Bob")
    assert res.status_code == 400 and "already had this clue" in res.json()["detail"]
    host_action(host_client, code, "select_buzzer", player_id="Carol")
    host_action(host_client, code, "incorrect")
    host_action(host_client, code, "close_clue")
    state = room_state(host_client, code)
    assert state["players"] == {"Bob": -200, "Carol": -200}
    assert next(c for c in state["clues"] if c["id"] == CLUE)["state"] == "answered"


def test_answerer_cannot_leave_mid_clue_but_can_after(app):
    code, host_client, players = table(app, "Bob")
    bob = players["Bob"]
    to_board(host_client, code)
    host_action(host_client, code, "pick_clue", clue_id=CLUE)
    host_action(host_client, code, "open_buzzers")
    host_action(host_client, code, "select_buzzer", player_id="Bob")
    res = player_event(bob, code, "leave")
    assert res.status_code == 400 and "finish answering first" in res.json()["detail"]
    host_action(host_client, code, "incorrect")
    assert player_event(bob, code, "leave").status_code == 200
    state = room_state(host_client, code)
    assert state["players"] == {}  # Bob exits with his -200 gone entirely
    assert state["buzzQueue"] == []  # and his queue spot evaporates


def test_leaving_host_is_refused(app):
    code, host_client, _ = table(app)
    res = player_event(host_client, code, "leave")
    assert res.status_code == 400
    assert "emcees till the end" in res.json()["detail"]


def test_player_rejoining_after_leave_keeps_their_ledger(app):
    code, host_client, players = table(app, "Bob")
    bob = players["Bob"]
    to_board(host_client, code)
    host_action(host_client, code, "pick_clue", clue_id=CLUE)
    host_action(host_client, code, "open_buzzers")
    host_action(host_client, code, "select_buzzer", player_id="Bob")
    host_action(host_client, code, "correct")  # Bob: $200
    assert room_state(bob, code)["players"]["Bob"] == 200
    player_event(bob, code, "leave")
    rejoined = join(bob, code, "Bob")
    assert rejoined["players"] == {"Bob": 200}  # scores follow identity, not connection


def test_undo_over_rest_rewinds_score_and_state(app):
    code, host_client, players = table(app, "Bob")
    to_board(host_client, code)
    host_action(host_client, code, "pick_clue", clue_id=CLUE)
    host_action(host_client, code, "open_buzzers")
    player_event(players["Bob"], code, "buzz")
    host_action(host_client, code, "select_buzzer", player_id="Bob")
    host_action(host_client, code, "correct")
    assert room_state(host_client, code)["players"]["Bob"] == 200
    res = host_action(host_client, code, "undo")
    assert res.status_code == 200
    state = res.json()
    assert state["state"] == "answering"
    assert state["players"]["Bob"] == 0
    assert state["canUndo"] is True
    host_action(host_client, code, "undo")  # back to buzzers_open
    host_action(host_client, code, "undo")  # back to asking
    host_action(host_client, code, "undo")  # back to board
    res = host_action(host_client, code, "undo")
    assert res.status_code == 200  # un-pick
    assert room_state(host_client, code)["state"] == "intro"


def test_pause_blocks_buzzes_until_resume(app):
    code, host_client, players = table(app, "Bob")
    to_board(host_client, code)
    host_action(host_client, code, "pick_clue", clue_id=CLUE)
    host_action(host_client, code, "open_buzzers")
    assert host_action(host_client, code, "pause").status_code == 200
    assert room_state(host_client, code)["state"] == "paused"
    res = player_event(players["Bob"], code, "buzz")
    assert res.status_code == 400 and "buzzers" in res.json()["detail"]
    assert host_action(host_client, code, "select_buzzer", player_id="Bob").status_code == 400
    assert host_action(host_client, code, "resume").status_code == 200
    assert room_state(host_client, code)["state"] == "buzzers_open"
    assert player_event(players["Bob"], code, "buzz").status_code == 200


def test_adjust_between_plays_only(app):
    code, host_client, players = table(app, "Bob")
    to_board(host_client, code)
    res = host_action(host_client, code, "adjust_score", player_id="Bob", value=300)
    assert res.status_code == 200
    assert res.json()["players"]["Bob"] == 300
    host_action(host_client, code, "pick_clue", clue_id=CLUE)
    res = host_action(host_client, code, "adjust_score", player_id="Bob", value=10)
    assert res.status_code == 400 and "isn't on tonight" in res.json()["detail"]


def test_full_game_lifecycle_over_rest(app):
    code, host, players = table(app, "Bob", "Carol")
    assert room_state(host, code)["state"] == "lobby"
    to_board(host, code)

    # --- Jeffpardy round: Bob scores the $200 clue
    assert host_action(host, code, "pick_clue", clue_id=CLUE).status_code == 200
    assert host_action(host, code, "open_buzzers").status_code == 200
    assert player_event(players["Bob"], code, "buzz").status_code == 200
    assert host_action(host, code, "select_buzzer", player_id="Bob").status_code == 200
    graded = host_action(host, code, "correct")
    assert graded.json()["players"]["Bob"] == 200

    # --- Intermission: round label flips only when Double actually starts
    assert host_action(host, code, "finish_round").status_code == 200
    between = room_state(host, code)
    assert between["state"] == "round_intermission"
    assert between["round"] == "main"
    assert host_action(host, code, "adjust_score", player_id="Carol", value=400).status_code == 200
    assert host_action(host, code, "start_next_round").status_code == 200
    dj = room_state(host, code)
    assert dj["round"] == "bonus"
    assert all(c["id"].startswith("dj-") for c in dj["clues"])

    # --- Bonus Jeffpardy: Carol scores her own clue
    host_action(host, code, "pick_clue", clue_id="dj-c1-400")
    host_action(host, code, "open_buzzers")
    player_event(players["Carol"], code, "buzz")
    host_action(host, code, "select_buzzer", player_id="Carol")
    assert host_action(host, code, "correct").json()["players"]["Carol"] == 800

    # --- Into Final: category for everyone, question host-only until open
    assert host_action(host, code, "finish_round").status_code == 200
    entering = room_state(host, code)
    assert entering["state"] == "final_category"
    assert entering["round"] == "ultimate"
    assert entering["final"]["category"]
    assert room_state(players["Bob"], code)["final"]["question"] is None
    assert room_state(host, code)["final"]["question"] is not None

    # --- Wagering: limits enforced, secrets kept
    assert host_action(host, code, "open_final_wagering").status_code == 200
    res = player_event(players["Bob"], code, "wager", amount=99_999)
    assert res.status_code == 400 and "between 0 and 200" in res.json()["detail"]
    assert player_event(players["Bob"], code, "wager", amount=200).status_code == 200
    assert player_event(players["Carol"], code, "wager", amount=800).status_code == 200
    res = player_event(players["Bob"], code, "wager", amount=1)
    assert res.status_code == 400 and "already on the table" in res.json()["detail"]
    assert host_action(host, code, "close_final_wagering").status_code == 200

    # --- Final clue: answers submitted, empty refused, secrecy preserved
    res = player_event(players["Bob"], code, "final_answer", text="   ")
    assert res.status_code == 400
    assert player_event(players["Bob"], code, "final_answer", text="what is Paris").status_code == 200
    assert player_event(players["Carol"], code, "final_answer", text="who is Beethoven").status_code == 200
    bob_view = room_state(players["Bob"], code)
    assert bob_view["final"]["answers"] == {"Bob": "what is Paris"}

    # --- Reveal: everyone sees everything; host grades
    assert host_action(host, code, "start_final_reveal").status_code == 200
    revealed = room_state(players["Bob"], code)
    assert set(revealed["final"]["answers"]) == {"Bob", "Carol"}
    assert revealed["final"]["wagers"] == {"Bob": 200, "Carol": 800}
    assert host_action(host, code, "grade_final", player_id="Bob", correct=True).status_code == 200
    assert host_action(host, code, "grade_final", player_id="Carol", correct=False).status_code == 200
    res = host_action(host, code, "grade_final", player_id="Bob", correct=True)
    assert res.status_code == 400 and "already called" in res.json()["detail"]
    assert room_state(host, code)["players"] == {"Bob": 400, "Carol": 0}

    # --- Review, then close for good
    assert host_action(host, code, "end_game").status_code == 200
    assert room_state(host, code)["state"] == "review"
    assert host_action(host, code, "adjust_score", player_id="Bob", value=-100).status_code == 200
    assert host_action(host, code, "close_game").status_code == 200
    final_state = room_state(host, code)
    assert final_state["closed"] is True
    assert final_state["state"] == "game_over"
    assert final_state["players"] == {"Bob": 300, "Carol": 0}
    assert final_state["canUndo"] is False  # no resurrecting the dead

    # --- The room refuses all further life
    res = TestClient(app).post(f"/api/rooms/{code}/join", json={"name": "Late"})
    assert res.status_code == 400 and "show has ended" in res.json()["detail"]
    res = host_action(host, code, "adjust_score", player_id="Bob", value=1)
    assert res.status_code == 400
    res = host_action(host, code, "undo")
    assert res.status_code == 400 and "show has ended" in res.json()["detail"]
    res = player_event(players["Bob"], code, "buzz")
    assert res.status_code == 400


def test_final_question_and_wager_window_over_ws(app):
    code, host, players = table(app, "Bob")
    to_board(host, code)
    host_action(host, code, "finish_round")
    host_action(host, code, "start_next_round")
    host_action(host, code, "finish_round")
    with host.websocket_connect(f"/ws/rooms/{code}?name=Host") as hw, \
            players["Bob"].websocket_connect(f"/ws/rooms/{code}?name=Bob") as bw:
        hw.receive_json()
        bw.receive_json()
        host_action(host, code, "open_final_wagering")
        hw_view = hw.receive_json()["state"]
        bw_view = bw.receive_json()["state"]
        assert hw_view["final"]["question"]  # host pre-reads the clue
        assert bw_view["final"]["question"] is None  # players wait for FinalClue
        assert bw_view["final"]["wagerOpen"] is True
        # Bob has $0: his only legal wager is 0 — he just declares it.
        player_event(players["Bob"], code, "wager", amount=0)
        hw_view = hw.receive_json()["state"]
        bw_view = bw.receive_json()["state"]
        assert hw_view["final"]["wagers"] == {"Bob": 0}
        assert bw_view["final"]["wagers"] == {"Bob": 0}
        host_action(host, code, "close_final_wagering")
        hw.receive_json()
        bw_view = bw.receive_json()["state"]
        assert bw_view["final"]["question"]  # clue is open to players now
        assert bw_view["final"]["answerOpen"] is True


# -- Gamely Jeff Quest -------------------------------------------------------


def test_quest_wager_travels_the_rest_player_endpoint(app):
    """PlayerAction's schema and the _apply whitelist must admit quest_wager."""
    code, host, players = table(app, "Bob", "Cat")
    to_board(host, code)
    quest = room_state(host, code)["activeQuest"]
    assert quest  # armed for the board round
    assert room_state(host, code)["picker"] is None  # clock starts empty

    # the flip needs someone holding the board first
    res = host_action(host, code, "pick_clue", clue_id=quest)
    assert res.status_code == 400 and "give someone the pick" in res.json()["detail"]

    # free reign: the host gives the quest to Bob, then flips the marked card
    assert host_action(host, code, "adjust_score", player_id="Bob", value=400).status_code == 200
    assert host_action(host, code, "set_picker", player_id="Bob").status_code == 200
    assert room_state(host, code)["picker"] == "Bob"
    assert host_action(host, code, "pick_clue", clue_id=quest).status_code == 200
    assert room_state(host, code)["state"] == "quest_wager"  # flip straight to the wager

    # the schema passes both through; the domain is what says no
    res = player_event(players["Cat"], code, "quest_wager", amount=100)
    assert res.status_code == 400 and "only Bob plays this quest" in res.json()["detail"]
    res = player_event(players["Bob"], code, "quest_wager", amount="all-in")
    assert res.status_code == 400 and "whole dollars" in res.json()["detail"]

    assert player_event(players["Bob"], code, "quest_wager", amount=400).status_code == 200
    assert room_state(host, code)["questWager"] == 400
    assert host_action(host, code, "begin_quest_answer").status_code == 200
    assert room_state(host, code)["state"] == "quest_answering"
    assert host_action(host, code, "correct").status_code == 200
    assert room_state(host, code)["players"]["Bob"] == 800  # 400 bankroll + 400 stake
    assert room_state(host, code)["picker"] == "Bob"  # holder picks next
    spent = next(c for c in room_state(host, code)["clues"] if c["id"] == quest)
    assert spent["state"] == "answered"  # quest spent (UI drops the chip on answered)


def test_player_action_schema_and_dispatch_share_one_table():
    """Drift tripwire: the Literal can't disagree with the dispatch table."""
    from typing import get_args

    from jeffpardy.game import Game
    from jeffpardy.models import PLAYER_EVENTS, PlayerAction

    assert set(get_args(PlayerAction)) == set(PLAYER_EVENTS)
    for method, _keys in PLAYER_EVENTS.values():
        assert callable(getattr(Game, method, None)), f"Game.{method} missing"


def test_assisted_pick_travels_the_rest_player_endpoint(app):
    """The pick route is transport-generic: schema + dispatch admit it, the
    domain decides. Manual rooms refuse it; assisted rooms run the chain."""
    code, host, players = table(app, "Bob", "Cat")
    to_board(host, code)

    # manual: the domain refuses — nobody chooses but the host
    res = player_event(players["Bob"], code, "pick", clue_id=CLUE)
    assert res.status_code == 400 and "calling the clues tonight" in res.json()["detail"]

    assert host_action(host, code, "set_host_mode", mode="assisted").status_code == 200
    assert room_state(host, code)["hostMode"] == "co-hosted"

    # the board-holder rule rides the wire: Bob chooses, Cat is refused
    assert host_action(host, code, "set_picker", player_id="Bob").status_code == 200
    res = player_event(players["Cat"], code, "pick", clue_id=CLUE)
    assert res.status_code == 400 and "calls it" in res.json()["detail"]

    # the delegated tap runs the same transition + auto-chain as the host flip
    res = player_event(players["Bob"], code, "pick", clue_id=CLUE)
    assert res.status_code == 200, res.text
    room = room_state(host, code)
    assert room["state"] == "buzzers_open"
    assert room["activeClueId"] == CLUE
    # …and the host UI lost the delegated action: no race, by construction
    assert "pick_clue" not in {a["name"] for a in room["availableHostActions"]}
    assert "open_buzzers" not in {a["name"] for a in room["availableHostActions"]}
    res = host_action(host, code, "pick_clue", clue_id=CLUE)
    assert res.status_code == 400


def test_stale_buzz_dies_at_the_transport_door(app):
    """The registry forwards race fields: a wrong token/clue never reaches the queue."""
    code, host, players = table(app, "Bob", "Cat")
    to_board(host, code)
    host_action(host, code, "pick_clue", clue_id="j-c0-200")
    host_action(host, code, "open_buzzers")
    room = room_state(host, code)
    clue, token = room["activeClueId"], room["buzzToken"]
    assert token  # the client echoes exactly these two fields

    res = player_event(players["Bob"], code, "buzz", clue_id=clue, buzz_token="stale")
    assert res.status_code == 400 and "ring-in has passed" in res.json()["detail"]
    res = player_event(players["Bob"], code, "buzz", clue_id="j-c9-999", buzz_token=token)
    assert res.status_code == 400 and "has left the board" in res.json()["detail"]
    assert room_state(host, code)["buzzQueue"] == []

    assert player_event(players["Bob"], code, "buzz", clue_id=clue, buzz_token=token).status_code == 200
    assert player_event(players["Cat"], code, "buzz", clue_id=clue, buzz_token=token).status_code == 200
    assert room_state(host, code)["buzzQueue"] == ["Bob", "Cat"]
    assert room_state(host, code)["rev"] >= 2  # revisions carried all the way out
