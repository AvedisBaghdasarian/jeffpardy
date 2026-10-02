"""Central Game object: state installation, undo history, actors, scoring.

Separation of concerns:

* ``states.py`` — *what* the game can do next (pure proposals + capability
  discovery). This module — the mutable game record, domain rules for players
  and the host, and the only place ``state`` is ever assigned
  (:meth:`Game._host_run` glues the previous state + mutable data into undo
  history before installing each host action's proposal).
* ``main.py`` — interactions only: generic routers that authenticate the
  sender (host vs player) and forward to :meth:`Game.perform` / player event
  methods; it never touches state or scores directly.

Identity model: the first player through the door is the lobby leader — the
HOST — and does not play (not in ``players``, may not buzz/answer). Everyone
after is a player, keyed by their cookie name.
"""

from __future__ import annotations

import inspect
import random
import string
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum
from uuid import uuid4

from .errors import InvalidAction, RoomError
from .hosting import HostMode, get_host_policy
from .states import (
    Answering,
    Asking,
    BuzzersOpen,
    CanAdjustScore,
    CanBeginQuestAnswer,
    CanCloseClue,
    CanCloseFinalWagering,
    CanCloseGame,
    CanEndGame,
    CanFinishIntro,
    CanFinishRound,
    CanGradeFinal,
    CanMarkCorrect,
    CanMarkIncorrect,
    CanOpenBuzzers,
    CanOpenFinalWagering,
    CanPause,
    CanPickClue,
    CanPromoteHost,
    CanRevealAnswer,
    CanResume,
    CanSelectBuzzer,
    CanSetPicker,
    CanSetViewer,
    CanStartFinalReveal,
    CanStartGame,
    CanStartNextRound,
    FinalCategory,
    FinalClue,
    FinalReveal,
    FinalWagering,
    GameOver,
    HostAction,
    Intro,
    Lobby,
    Paused,
    QuestAnswering,
    QuestWager,
    Review,
    Round,
    RoundIntermission,
    State,
)

# Note: states.Board is deliberately not imported — ``Board`` here is the
# clue grid (content), not the game state.

ROOM_CODE_RE = r"^\d{4}$"
VALID_CODE_CHARS = string.digits
CATEGORIES = 6
CLUES_PER_CATEGORY = 5
CLUE_VALUES = (200, 400, 600, 800, 1000)
MAX_UNDO = 200

# Room lifecycle: all wall-clock policy, all environment (never in Core —
# undo only ever rewinds the game record, never presence, timers, or seats).
IDLE_CULL_SECONDS = 20 * 60  # no activity at all: close the room
MAX_GAME_SECONDS = 2 * 60 * 60  # the show runs two hours, then closes
AWAY_GRACE_SECONDS = 5 * 60  # a live (socket-open) drop only counts after
LEAVE_GRACE_SECONDS = 5 * 60  # an explicit goodbye counts after the same wait


class ClueStatus(Enum):
    BOARD = "board"
    ACTIVE = "active"
    ANSWERED = "answered"


@dataclass
class Clue:
    id: str
    category: str
    value: int
    question: str
    answer: str


@dataclass
class Board:
    """Clue grid (content). Per-clue *progress* lives on Game for undo."""

    categories: list[str]
    clues: dict[str, Clue]

    def clues_for(self, category: str) -> list[Clue]:
        return sorted(
            (c for c in self.clues.values() if c.category == category),
            key=lambda c: c.value,
        )


@dataclass(frozen=True)
class FinalData:
    category: str
    question: str
    answer: str


def build_board(grouped: dict[str, list[tuple[str, str]]], prefix: str = "") -> Board:
    """Build the clue grid from {category: [(q, a), ...]}, up to 6 x 5.

    ``prefix`` keeps clue ids unique across the Jeffpardy and Bonus Jeffpardy
    boards (both live in one game). Trivia fetching guarantees full boards;
    the builder stays permissive so small test boards work.
    """
    categories = list(grouped.keys())[:CATEGORIES]
    clues: dict[str, Clue] = {}
    for cat_index, category in enumerate(categories):
        entries = grouped[category][:CLUES_PER_CATEGORY]
        if len(entries) < CLUES_PER_CATEGORY:
            raise RoomError(f"category {category!r} needs {CLUES_PER_CATEGORY} clues")
        for clue_index, (question, answer) in enumerate(entries):
            clue_id = f"{prefix}c{cat_index}-{CLUE_VALUES[clue_index]}"
            clues[clue_id] = Clue(
                id=clue_id,
                category=category,
                value=CLUE_VALUES[clue_index],
                question=question,
                answer=answer,
            )
    return Board(categories=categories, clues=clues)


@dataclass(frozen=True, slots=True)
class Core:
    """The undoable game record: the phase plus every game fact.

    Deeply immutable by discipline: containers are never mutated after
    construction — transitions build a fresh ``Core``, and undo is a plain
    pointer rebind off the history stack. Identity keys are stable player ids;
    they are *not* required to match the current roster (the ledger outlives
    connections). Environment (roster, host, viewers, auth) lives on ``Game``
    and is never captured here.
    """

    state: State
    round: Round
    scores: dict[str, int]
    buzz_queue: list[str]
    wagers: dict[str, int]
    quest_wagers: dict[str, int]
    final_answers: dict[str, str]
    graded: set[str]
    answered_players: set[str]
    clue_status: dict[str, ClueStatus]
    clue_scored: bool
    answer_revealed: bool
    active_quest: str | None
    picker: str | None


@dataclass(slots=True)
class Game:
    """Mutable environment + auth around an immutable ``Core``.

    ``Game`` owns who is here (roster), who runs it (host), who only watches
    (viewers), and identity resolution — all read by the transport to decide
    *whether* an action may run. The state machine only ever reads env; it
    never mutates it. All game facts live in ``self._core`` (see ``Core``).
    """

    code: str
    boards: dict[Round, Board]
    final: FinalData

    host: str | None = None
    viewers: set[str] = field(default_factory=set, repr=False)
    host_mode: HostMode = HostMode.MANUAL  # hosting posture (env, not a game fact)
    rev: int = 0  # transport clock: bumped per commit, never restored (undo publishes as newer)
    race_token: str | None = None  # which pass of the open buzz race this is — never in Core

    # Presence (env, not a game fact). `_seen` tracks the last-seen wall clock
    # of every identity that has joined at all (host included); `_gone`
    # records when someone explicitly said goodbye (or was removed by the
    # host). A seat only counts as empty once its grace has fully passed — a
    # refresh or a reconnect lands them back before that without losing a thing.
    created_at: float = field(default_factory=time.time, repr=False)
    last_activity: float = field(default_factory=time.time, repr=False)
    _seen: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _gone: dict[str, float] = field(default_factory=dict, init=False, repr=False)

    _seated: dict[str, None] = field(default_factory=dict, init=False, repr=False)
    _identities: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _history: list[Core] = field(default_factory=list, init=False, repr=False)
    _core: Core = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._core = Core(
            state=Lobby(),
            round=Round.MAIN,
            scores={},
            buzz_queue=[],
            wagers={},
            quest_wagers={},
            final_answers={},
            graded=set(),
            answered_players=set(),
            clue_status={
                clue.id: ClueStatus.BOARD
                for board in self.boards.values()
                for clue in board.clues.values()
            },
            clue_scored=False,
            answer_revealed=False,
            active_quest=None,
            picker=None,
        )
        self.touch(time.time())

    # -- presence + lifecycle ----------------------------------------------

    def touch(self, now: float | None = None, name: str | None = None) -> None:
        """Record wall-clock activity: the room is alive, and (if named) so is
        that identity. Never a game fact — presence is pure environment."""
        stamp = time.time() if now is None else now
        self.last_activity = stamp
        if name is not None:
            key = self.player_key(name)
            if key is None and self.host and name.strip().lower() == self.host.lower():
                key = self.host
            if key is not None:
                self._seen[key] = stamp
                self._gone.pop(key, None)

    def heartbeat(self, name: str, now: float | None = None) -> None:
        """A live socket saying "I'm still here" — counts exactly like an
        action for presence, and forgives an explicit goodbye in flight."""
        self.touch(now, name)

    def is_away(self, key: str, now: float | None = None) -> bool:
        """A socket-open seat whose last sighting is older than the grace."""
        stamp = time.time() if now is None else now
        return stamp - self._seen.get(key, self.created_at) >= AWAY_GRACE_SECONDS

    def is_gone(self, key: str, now: float | None = None) -> bool:
        """An explicit goodbye whose grace has fully passed."""
        stamp = time.time() if now is None else now
        left_at = self._gone.get(key)
        return left_at is not None and stamp - left_at >= LEAVE_GRACE_SECONDS

    def has_left(self, key: str, now: float | None = None) -> bool:
        """Counted as left, either way it happened: an old enough goodbye, or
        a live seat gone quiet past the same wait. One rule, both doors."""
        return self.is_gone(key, now) or (
            key not in self._gone and self.is_away(key, now)
        )

    def departed(self, now: float | None = None) -> list[str]:
        """Every identity whose grace has passed (host included)."""
        names = set(self._seated) | set(self._gone)
        if self.host:
            names.add(self.host)
        return sorted(k for k in names if self.has_left(k, now))

    def present_players(self, now: float | None = None) -> list[str]:
        """Seated, playing identities still with us (host/viewers excluded)."""
        return [
            p for p in self._seated
            if p not in self.viewers
            and not (self.host and p.lower() == self.host.lower())
            and not self.has_left(p, now)
        ]

    def should_cull_idle(self, now: float | None = None) -> bool:
        stamp = time.time() if now is None else now
        return stamp - self.last_activity >= IDLE_CULL_SECONDS

    def should_cull_overtime(self, now: float | None = None) -> bool:
        stamp = time.time() if now is None else now
        return stamp - self.created_at >= MAX_GAME_SECONDS

    def should_cull_empty(self, now: float | None = None) -> bool:
        """Nobody left in the seats: every identity ever seated has grace-out."""
        stamp = time.time() if now is None else now
        seated = set(self._seated) | set(self._gone)
        if self.host:
            seated.discard(self.host)
        if not seated and not self._seated and not self._gone:
            return False  # nobody ever arrived — not empty, just new
        return bool(seated) and all(self.has_left(k, stamp) for k in seated)

    def cull_reason(self, now: float | None = None) -> str | None:
        """Why this room should close, if it should: the first true rule wins
        (empty seats, then the two-hour show clock, then a quiet room)."""
        if self.closed:
            return None
        if self.should_cull_empty(now):
            return "empty"
        if self.should_cull_overtime(now):
            return "overtime"
        if self.should_cull_idle(now):
            return "idle"
        return None

    # -- the Core, as read-only views ----------------------------------------

    @property
    def state(self) -> State:
        return self._core.state

    @property
    def round(self) -> Round:
        return self._core.round

    @property
    def players(self) -> dict[str, int]:
        """Seated players and their ledger scores — a projection, not storage."""
        core = self._core
        return {p: core.scores.get(p, 0) for p in self._seated}

    @property
    def buzz_queue(self) -> list[str]:
        return self._core.buzz_queue

    @property
    def wagers(self) -> dict[str, int]:
        return self._core.wagers

    @property
    def quest_wagers(self) -> dict[str, int]:
        return self._core.quest_wagers

    @property
    def final_answers(self) -> dict[str, str]:
        return self._core.final_answers

    @property
    def graded(self) -> set[str]:
        return self._core.graded

    @property
    def answered_players(self) -> set[str]:
        return self._core.answered_players

    @property
    def clue_status(self) -> dict[str, ClueStatus]:
        return self._core.clue_status

    @property
    def clue_scored(self) -> bool:
        return self._core.clue_scored

    @property
    def answer_revealed(self) -> bool:
        return self._core.answer_revealed

    @property
    def active_quest(self) -> str | None:
        return self._core.active_quest

    @property
    def picker(self) -> str | None:
        return self._core.picker

    @property
    def core(self) -> Core:
        """Read access for state methods, which thread it through pure ops."""
        return self._core

    # -- plumbing -------------------------------------------------------------

    @property
    def host_policy(self):
        """The middleware for this room's hosting posture."""
        return get_host_policy(self.host_mode)

    @property
    def available_host_actions(self) -> tuple[HostAction, ...]:
        return self.host_policy.advertise(self, self.state.available_host_actions)

    @property
    def board(self) -> Board | None:
        """The clue grid for the round in progress (None during Final)."""
        return self.boards.get(self.round)

    def arm_quest(self, core: Core) -> Core:
        """Park the Gamely Jeff Quest on the board's home-square clue.

        Deterministic (the board's last clue on standard grids) so tests and
        the UI can reason about placement without a seed. The square is host
        truth — ``mask_snapshot`` scrubs ``activeQuest`` from every other
        viewer, so the room finds the jackpot by playing, not by reading the
        board.
        """
        board = self.boards.get(core.round)
        quest = list(board.clues)[-1] if board and board.clues else None
        return replace(core, active_quest=quest)

    def is_quest_clue(self, clue_id: str) -> bool:
        return self.active_quest is not None and clue_id == self.active_quest

    def quest_limit(self, player_id: str) -> int:
        """Show rules: bet what you have; broke players may match the table."""
        score = self.players.get(player_id, 0)
        if score > 0:
            return score
        return max(max(self.players.values(), default=0), 0)

    def everyone_answered(self, clue_id: str) -> bool:
        """No present, playing identity left who could take this clue.

        The host is never a contestant; viewers are benched; seats whose
        grace has passed are gone too. The buzz queue still holding names
        means a pass is live — callers check the queue themselves; this
        answers only "is there anyone unanswered left to pass to".
        """
        del clue_id  # the per-clue record is who-answered, not what-clue
        seated = set(self.present_players())
        return bool(seated) and seated <= set(self.answered_players)

    def dead_clue(self, clue_id: str) -> bool:
        """The clue can't go anywhere: nobody is left who could take it and
        nobody is still queued to be called on.

        Every state that can close a clue reads this one rule — the host's
        manual "Toss Up the Clue" (via ``canAnyoneAnswer``) and the shared
        hosting's auto-skip — so a buzzer queue holding a name that already
        answered can't keep a spent clue alive.
        """
        return not self.buzz_queue and self.everyone_answered(clue_id)

    @property
    def closed(self) -> bool:
        return isinstance(self.state, GameOver)

    @property
    def close_reason(self) -> str | None:
        """Why the room shut (None while the show is live)."""
        st = self.state
        return st.close_reason if isinstance(st, GameOver) else None

    @property
    def can_undo(self) -> bool:
        return bool(self._history)

    @property
    def active_state(self) -> State:
        """The flow state, unwrapping pause (for event guards)."""
        return self.state.previous if isinstance(self.state, Paused) else self.state

    def _friendly_state(self) -> str:
        """Where the show is right now — never a class name."""
        state = self.state
        if isinstance(state, Paused):
            return "while the show is paused"
        name = type(state).__name__
        names = {
            "Lobby": "in the green room",
            "Intro": "during the contestant introductions",
            "Board": "on the board",
            "Asking": "while the clue is on the board",
            "BuzzersOpen": "while the buzzers are live",
            "Answering": "while a contender is answering",
            "QuestWager": "during the quest wager",
            "QuestAnswering": "while the quest answer is playing",
            "RoundIntermission": "during the commercial break",
            "FinalCategory": "during the Ultimate category reveal",
            "FinalWagering": "during Ultimate wagering",
            "FinalClue": "while the Ultimate clue is up",
            "FinalReveal": "during the Ultimate reveal",
            "Review": "at tonight's final scores",
            "GameOver": "\u2014 the show has ended",
        }
        return names.get(name, "at this point in the show")

    def _require(self, capability: type) -> State:
        state = self.state
        if not isinstance(state, capability):
            label = capability.action.label
            raise InvalidAction(
                f"'{label}' isn't on tonight — {self._friendly_state()}, so the host can't play it here"
            )
        return state

    def _commit(self, new_core: Core) -> None:
        """Install a host action's result: history checkpoint + fresh Core.

        The only ``_core`` write for host actions. Atomicity comes free —
        a method that raises simply never returns a Core, and nothing it
        touched could have been mutated (Core is immutable).
        """
        self._history.append(self._core)
        if len(self._history) > MAX_UNDO:
            self._history.pop(0)
        self._core = new_core
        self.rev += 1

    def _event(self, new_core: Core) -> None:
        """Install a player event's result: fresh Core, no history checkpoint.

        Events (buzz/wager/answer) aren't host actions — undo doesn't know
        or care about them; it just rewinds the whole record wholesale.
        In assisted mode the event may complete a set the automation was
        waiting on (last wager, last answer, locked risk), so settling here
        lets one player move run the machine to rest.
        """
        self._core = new_core
        self.rev += 1
        self._settle_assist()

    def _host_run(self, method: Callable[..., Core], *args: object) -> None:
        """Run a host action; commit its returned Core (or change nothing).

        The core is immutable, so failure is trivially atomic: the old Core
        object is still installed. ``rev`` advances either way — the next
        publish is newer even if the action was refused.
        """
        try:
            new_core = method(self, *args)
        except Exception:
            self.rev += 1
            raise
        self._commit(new_core)
        self._settle_assist()

    def _settle_assist(self) -> None:
        """Run the assist chain to rest (assisted mode only).

        Applied after every install — commits, player events, mode flips —
        so one move in runs the machine to rest. Each auto step is a full
        host action (its own history checkpoint + its own rev bump).
        Settlement never runs on undo: an undo lands on the *in-between*
        state — the auto steps that fired after the checkpoint do not replay
        — and the host holds that rest state with full manual control (their
        next move settles again from there).
        """
        if self.host_mode is not HostMode.COHOSTED:
            return
        while True:
            self.reconcile_pointers()  # a goodbye may have voided the clock
            nxt = self.host_policy.auto_step(self)
            if nxt is None:
                return
            action, kwargs = nxt
            method = getattr(self, action, None)
            if method is None or not callable(method):
                return
            try:
                method(**kwargs)
            except InvalidAction:
                return

    # -- hosting mode (environment, like host/viewers) -----------------------

    def set_host_mode(self, mode: HostMode | str) -> None:
        """Switch the room's hosting posture. Checkpoints like a host action —
        undo never rewinds it (environment), but a mode switch still settles:
        flipping to assisted in the middle of a mechanical chain runs it."""
        if isinstance(mode, str):
            try:
                mode = HostMode(mode.strip().lower())
            except ValueError:
                normalized = mode.strip().lower().replace("_", "-") if isinstance(mode, str) else ""
                if normalized in ("assisted", "cohosted", "co-hosted", "shared", "shared-hosting"):
                    mode = HostMode.COHOSTED
                else:
                    raise InvalidAction(
                        "that hosting style isn't on the menu — solo or shared"
                    ) from None
        if not isinstance(mode, HostMode):
            raise InvalidAction("that hosting style isn't on the menu — solo or shared")
        get_host_policy(mode)  # reserved names refuse here, before anything changes
        self._history.append(self._core)
        if len(self._history) > MAX_UNDO:
            self._history.pop(0)
        self.host_mode = mode
        self.rev += 1
        self._settle_assist()

    def undo(self) -> None:
        if self.closed:
            raise InvalidAction("the show has ended")  # closing is final
        if not self._history:
            raise InvalidAction("nothing to take back yet — the show hasn't made a move")
        self._core = self._history.pop()  # pointer rebind — the whole record, events included
        self.rev += 1  # domain rewinds; the transport clock never does
        if isinstance(self.active_state, BuzzersOpen):
            self.start_race()  # re-entered race = a fresh pass; first-pass buzzes are dead
        else:
            self.end_race()  # left the race behind: the old token dies with it

    # -- generic host entry (used by both REST and WS routers) ---------------

    def perform(self, sender: str | None, action: str, /, **payload: object) -> None:
        """Authenticate the sender and run a capability action by name.

        ``sender``/``action`` are positional-only on purpose: a hostile
        payload carrying its own ``sender`` or ``action`` key then lands in
        ``**payload`` and dies inside the method as an invalid argument,
        instead of colliding with this signature at the call site.
        """
        if not sender or not self.host or sender.strip().lower() != self.host.lower():
            raise InvalidAction("only the host runs the show")
        # The policy sees every host action — including "undo" and
        # "set_host_mode", which are environment ops beside the capabilities.
        self.host_policy.authorize(self, sender, action, dict(payload))
        if action == "undo":
            self.undo()
            return
        if action == "set_host_mode":
            try:
                self.set_host_mode(**payload)
            except (TypeError, AttributeError, ValueError) as exc:
                raise InvalidAction("that hosting choice fizzled — give it another go") from exc
            return
        method = getattr(self, action, None)
        if method is None or not callable(method):
            raise InvalidAction("that isn't a move on this show — the host can't play it here")
        allowed = {a.name for a in self.available_host_actions}
        if action not in allowed:
            raise InvalidAction(
                f"that move isn't on tonight — {self._friendly_state()}, so the host can't play it here"
            )
        try:
            method(**payload)
        except (TypeError, AttributeError, ValueError) as exc:
            # Hostile payloads surface as builtin exceptions (None.strip(),
            # unhashable ids, …) — map them all to a domain error.
            raise InvalidAction("that move fizzled — check the details and try again") from exc

    # -- host API (capability gate + install) ---------------------------------

    def start_game(self) -> None:
        self._host_run(self._require(CanStartGame).start_game)

    def finish_intro(self) -> None:
        self._host_run(self._require(CanFinishIntro).finish_intro)

    def pick_clue(self, clue_id: str) -> None:
        self._host_run(self._require(CanPickClue).pick_clue, clue_id)

    def open_buzzers(self) -> None:
        self._host_run(self._require(CanOpenBuzzers).open_buzzers)

    def reveal_answer(self) -> None:
        self._host_run(self._require(CanRevealAnswer).reveal_answer)

    def select_buzzer(self, player_id: str) -> None:
        self._host_run(self._require(CanSelectBuzzer).select_buzzer, player_id)

    def correct(self) -> None:
        self._host_run(self._require(CanMarkCorrect).correct)

    def begin_quest_answer(self) -> None:
        self._host_run(self._require(CanBeginQuestAnswer).begin_quest_answer)

    def incorrect(self) -> None:
        self._host_run(self._require(CanMarkIncorrect).incorrect)

    def close_clue(self) -> None:
        self._host_run(self._require(CanCloseClue).close_clue)

    def finish_round(self) -> None:
        self._host_run(self._require(CanFinishRound).finish_round)

    def start_next_round(self) -> None:
        self._host_run(self._require(CanStartNextRound).start_next_round)

    def open_final_wagering(self) -> None:
        self._host_run(self._require(CanOpenFinalWagering).open_final_wagering)

    def close_final_wagering(self) -> None:
        self._host_run(self._require(CanCloseFinalWagering).close_final_wagering)

    def start_final_reveal(self) -> None:
        self._host_run(self._require(CanStartFinalReveal).start_final_reveal)

    def grade_final(self, player_id: str, correct: bool) -> None:
        self._host_run(self._require(CanGradeFinal).grade_final, player_id, correct)

    def end_game(self) -> None:
        self._host_run(self._require(CanEndGame).end_game)

    def close_game(self) -> None:
        self._host_run(self._require(CanCloseGame).close_game)

    def close_room(self, reason: str = "closed") -> None:
        """Lifecycle close: the room's time is up (nobody left, the two-hour
        show clock ran out, or twenty quiet minutes). Not a host move and not
        undoable — closing is final. Puts up the Game Over board directly so
        the sweeper never needs a capability the state doesn't have."""
        if self.closed:
            return
        self._history.append(self._core)
        if len(self._history) > MAX_UNDO:
            self._history.pop(0)
        self._core = replace(self._core, state=GameOver(close_reason=reason))
        self.rev += 1

    def adjust_score(self, player_id: str, value: int) -> None:
        self._host_run(self._require(CanAdjustScore).adjust_score, player_id, value)

    def set_viewer(self, player_id: str, viewer: bool) -> None:
        """Environment op — benches never checkpoint undo history."""
        self.touch(name=self.host)
        self._require(CanSetViewer)
        core = None
        try:
            core = self.mark_viewer(player_id, viewer)
        finally:
            if core is None:
                self.rev += 1  # refusal or pure-env change: exactly one bump
            else:
                self._event(core)  # bench also cleared the game clock

    def kick(self, player_id: str, now: float | None = None) -> None:
        """Remove a player from the room (env, like a goodbye with teeth).

        The ledger stays — scores, wagers, and queued buzzes are game facts,
        and the identity resolves if they rejoin. Never checkpoints undo
        history; the clock (picker) and the finals sets are reconciled on the
        way out so no chain ever waits on a missing seat. The host can't be
        kicked and the answerer can't be kicked mid-answer.
        """
        stamp = time.time() if now is None else now
        key = self.player_key(player_id)
        if key is None or key not in self._seated:
            raise InvalidAction("they aren't a contender on this show")
        if self.host and key.lower() == self.host.lower():
            raise InvalidAction("the host emcees the whole night — hand over the show first")
        active = self.active_state
        if isinstance(active, Answering) and active.player_id.lower() == key.lower():
            raise InvalidAction("let them finish answering first")
        if isinstance(active, QuestWager) and active.player_id.lower() == key.lower():
            raise InvalidAction("let them finish the quest wager first")
        if isinstance(active, QuestAnswering) and active.player_id.lower() == key.lower():
            raise InvalidAction("let them finish answering the quest first")
        self.remove_seat(key, stamp)
        self.rev += 1
        self._settle_assist()

    def remove_seat(self, key: str, now: float | None = None) -> bool:
        """Share one exit path for goodbyes, boots, and the sweeper: unseat,
        stamp the goodbye, and reconcile the clock + finals sets.

        Returns True when the clock moved (the caller then publishes through
        ``_event`` so the Core write is real); False when only env changed.
        """
        stamp = time.time() if now is None else now
        if key not in self._seated:
            return False
        del self._seated[key]
        self.viewers.discard(key)
        self._gone[key] = stamp
        return self.reconcile_pointers()

    def reconcile_pointers(self) -> bool:
        """Re-resolve every Core pointer against the live roster.

        The picker must be someone who could actually choose (seated, playing,
        present). Wagers, answers, buzz records, and grades stay untouched —
        the ledger is history, and departing mid-finals never erases a move
        already made; the co-hosted chain simply stops *waiting* on present
        seats only (see ``present_players``), so a goodbye never strands it.
        Returns True when the Core changed (the clock moved).
        """
        core = self._core
        picker = core.picker
        if picker is not None and (
            picker not in self._seated
            or picker in self.viewers
            or self.has_left(picker)
        ):
            self._core = replace(core, picker=None)
            return True
        return False

    def sweep_departed(self, now: float | None = None) -> list[str]:
        """Unseat grace-out identities whose goodbye was recorded earlier.

        Presence heartbeats keep landing in ``_seen``; this only reaps seats
        whose grace has fully passed. Reconciles pointers on the way out so
        the co-hosted chain never waits on a ghost.
        """
        stamp = time.time() if now is None else now
        reaped = [k for k in list(self._seated) if self.has_left(k, stamp)]
        moved = False
        for key in reaped:
            del self._seated[key]
            self.viewers.discard(key)
        if reaped:
            moved = self.reconcile_pointers()
        if reaped or moved:
            self.rev += 1
            self._settle_assist()
        return reaped

    def promote_host(self, player_id: str) -> None:
        """Environment op — the crown lives outside the game record."""
        self.touch(name=self.host)
        self._require(CanPromoteHost)
        try:
            self.transfer_host(player_id)
        finally:
            self.rev += 1

    def set_picker(self, player_id: str | None) -> None:
        self._host_run(self._require(CanSetPicker).set_picker, player_id)

    def pause(self) -> None:
        self._host_run(self._require(CanPause).pause)

    def resume(self) -> None:
        self._host_run(self._require(CanResume).resume)

    # -- player events (no transitions — pure data) --------------------------

    def join(self, name: str, now: float | None = None) -> None:
        name = name.strip() if isinstance(name, str) else ""
        stamp = time.time() if now is None else now
        if not name:
            raise InvalidAction("tell the room your name first")
        if len(name) > 24:
            raise InvalidAction("keep stage names to 24 characters")
        if self.closed:
            raise InvalidAction("the show has ended")
        if self.host is None:
            self.host = name  # lobby leader leads — and does not play
            self._identities.setdefault(name.lower(), name)
            self.touch(stamp, name)
            self.rev += 1
            return
        if name.lower() == self.host.lower():
            self.touch(stamp, name)  # host (re)connecting: identity is the cookie
            return
        key = self._identities.setdefault(name.lower(), name)
        if key in self._seated:
            # An explicit goodbye is forgiven by *acting* (a game move) or by a
            # heartbeat — not by merely opening the page. A plain rejoin while
            # still marked gone would sit the player out with no way back.
            if self.has_left(key):
                raise InvalidAction(
                    "you stepped out of this room — open it again from the couch to slide back in"
                )
            self.touch(stamp, name)  # player reconnecting: forgiveness, not a seat
            return
        self._seated[key] = None
        self.touch(stamp, name)
        self.rev += 1

    def leave(self, name: str, now: float | None = None) -> None:
        """Environment exit: the seat frees up now, the goodbye counts after
        the grace — rejoin (or reconnect) inside five minutes and nothing was
        ever lost. The ledger is untouched: scores, wagers and queue entries
        stay game facts; the identity resolves if they return."""
        if self.closed:
            raise InvalidAction("the show has ended")
        if not isinstance(name, str):
            raise InvalidAction("take a seat in the game first")
        stamp = time.time() if now is None else now
        if self.host and name.strip().lower() == self.host.lower():
            raise InvalidAction("the host emcees till the end — sign off the room instead")
        key = self.player_key(name)
        if key is None or key not in self._seated:
            raise InvalidAction("take a seat in the game first")
        active = self.active_state
        if isinstance(active, Answering) and active.player_id.lower() == key.lower():
            raise InvalidAction("let them finish answering first")
        if isinstance(active, QuestWager) and active.player_id.lower() == key.lower():
            raise InvalidAction("let them finish the quest wager first")
        if isinstance(active, QuestAnswering) and active.player_id.lower() == key.lower():
            raise InvalidAction("let them finish answering the quest first")
        moved = self.remove_seat(key, stamp)
        self.touch(stamp)
        if isinstance(active, BuzzersOpen) and self.dead_clue(active.clue_id):
            # Whoever just left was the last contender who could take this
            # clue — while sharing the hosting the show skips it rather than
            # waiting on a buzz that can never come.
            self.end_race()
            reaped = self.retire_clue(active.clue_id, self._core)
            self._event(replace(reaped, state=Board()))
        elif moved:
            self._event(self._core)  # they took the clock with them
        else:
            self.rev += 1
        self._settle_assist()

    def start_race(self) -> None:
        """Mint the token for one pass of an open buzz race: once when the
        buzzers open, again whenever undo drops us back into an earlier race —
        transport metadata never rides a snapshot, so first-pass buzzes die."""
        self.race_token = uuid4().hex[:16]

    def end_race(self) -> None:
        self.race_token = None

    def buzz(self, name: str, clue_id: object = None, buzz_token: object = None) -> None:
        if not isinstance(self.state, BuzzersOpen):
            if isinstance(self.state, Paused):
                raise InvalidAction("buzzers are closed while paused — they reopen when the host resumes")
            raise InvalidAction("buzzers aren't open yet — wait for the host's call")
        if not isinstance(name, str):
            raise InvalidAction("take a seat in the game first")
        if self.host and name.strip().lower() == self.host.lower():
            raise InvalidAction("the host reads the clues — contenders ring in")
        key = self.player_key(name)
        if key is None or key not in self._seated:
            raise InvalidAction("take a seat in the game first")
        if self.has_left(key):
            raise InvalidAction("you stepped out — slip back in and ring back in")
        if key in self.viewers:
            raise InvalidAction("the audience watches this one — rejoin the game to ring in")
        if key in self.answered_players:
            # A wrong answer locks you out of that clue on the real show — and
            # it keeps the queue honest: every listed buzzer can still be called on.
            raise InvalidAction("you already had this clue — it's someone else's shot")
        if key in self.buzz_queue:
            return  # mashing is allowed — already queued is a silent no-op
        # The client echoes the race it saw; a straggler from another clue or
        # an earlier pass dies here. Omitted fields skip the check (direct callers).
        if clue_id is not None and (not isinstance(clue_id, str) or clue_id != self.state.clue_id):
            raise InvalidAction("too late — that clue has left the board")
        if buzz_token is not None and buzz_token != self.race_token:
            raise InvalidAction("that ring-in has passed — hit the buzzer again for this clue")
        core = self._core
        self._event(replace(core, buzz_queue=[*core.buzz_queue, key]))

    def wager(self, name: str, amount: object) -> None:
        if not isinstance(self.state, FinalWagering):
            raise InvalidAction("wagering isn't open yet — wait for the Ultimate category")
        if not isinstance(name, str):
            raise InvalidAction("take a seat in the game first")
        if self.host and name.strip().lower() == self.host.lower():
            raise InvalidAction("the host reads the clues — contenders ring in")
        key = self.player_key(name)
        if key is None or key not in self._seated:
            raise InvalidAction("take a seat in the game first")
        if self.has_left(key):
            raise InvalidAction("you stepped out — slip back in and the wager is yours")
        if key in self.viewers:
            raise InvalidAction("the audience watches this one — rejoin the game to wager")
        if key in self.wagers:
            raise InvalidAction("that wager is already on the table")
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise InvalidAction("wagers are whole dollars — no cents")
        limit = max(self.players[key], 0)
        if amount < 0 or amount > limit:
            raise InvalidAction(f"your wager must be between 0 and {limit}")
        core = self._core
        self._event(replace(core, wagers={**core.wagers, key: amount}))

    def final_answer(self, name: str, text: object) -> None:
        if not isinstance(self.state, FinalClue):
            raise InvalidAction("the Ultimate clue isn't up yet")
        if not isinstance(name, str):
            raise InvalidAction("take a seat in the game first")
        if self.host and name.strip().lower() == self.host.lower():
            raise InvalidAction("the host reads the clues — contenders ring in")
        key = self.player_key(name)
        if key is None or key not in self._seated:
            raise InvalidAction("take a seat in the game first")
        if self.has_left(key):
            raise InvalidAction("you stepped out — slip back in and answer again")
        if key in self.viewers:
            raise InvalidAction("the audience watches this one — rejoin the game to answer")
        if not isinstance(text, str) or not text.strip():
            raise InvalidAction("write your response first — in the form of a question")
        core = self._core
        # last write wins while open
        self._event(replace(core, final_answers={**core.final_answers, key: text.strip()[:80]}))

    def quest_wager(self, name: str, amount: object) -> None:
        """The Gamely Jeff Quest claimant names their own wager (host waits)."""
        if not isinstance(self.state, QuestWager):
            raise InvalidAction("the quest wager isn't on the table yet")
        if not isinstance(name, str):
            raise InvalidAction("take a seat in the game first")
        if self.host and name.strip().lower() == self.host.lower():
            raise InvalidAction("the host reads the clues — contenders ring in")
        key = self.player_key(name)
        if key is None:
            raise InvalidAction("take a seat in the game first")
        if key != self.state.player_id:
            raise InvalidAction(f"only {self.state.player_id} plays this quest — it's their find")
        if self.has_left(key):
            raise InvalidAction("you stepped out — slip back in and the wager is yours")
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise InvalidAction("wagers are whole dollars — no cents")
        limit = self.quest_limit(self.state.player_id)
        if amount < 0 or amount > limit:
            raise InvalidAction(f"your wager must be between 0 and {limit}")
        core = self._core
        self._event(replace(core, quest_wagers={**core.quest_wagers, key: amount}))

    def pick(self, name: str, clue_id: object = None) -> None:
        """While sharing the hosting, whoever has the board names the clue.

        Runs the *same* step as the host's pick-a-clue — including the
        quest-claim rule — but from the player's seat, then lets the shared
        hosting run on (so a plain choice opens the buzzers itself). Only
        while shared, only while the board is open, and only the board-holder
        (or anyone, while nobody has it): holding the board is the deal.
        """
        if self.host_mode is not HostMode.COHOSTED:
            raise InvalidAction("the host is calling the clues tonight — shared hosting is off")
        if self.closed:
            raise InvalidAction("the show has ended")
        if isinstance(self.state, Paused):
            raise InvalidAction("the show is paused")
        from .states import Board as BoardState

        if not isinstance(self.active_state, BoardState):
            raise InvalidAction("the board isn't taking calls right now")
        if not isinstance(name, str):
            raise InvalidAction("take a seat in the game first")
        if self.host and name.strip().lower() == self.host.lower():
            raise InvalidAction("the host reads the clues — contenders ring in")
        key = self.player_key(name)
        if key is None or key not in self._seated:
            raise InvalidAction("take a seat in the game first")
        if self.has_left(key):
            raise InvalidAction("you stepped out — slip back in and call back in")
        if key in self.viewers:
            raise InvalidAction("the audience watches this one — rejoin the game to call the board")
        if self.picker is not None and key != self.picker:
            raise InvalidAction(f"{self.picker} calls it — leave the board to them")
        board = self.boards.get(self.round)
        if board is None or not isinstance(clue_id, str) or clue_id not in board.clues:
            raise InvalidAction("that clue isn't on this board")
        self.pick_clue(clue_id=clue_id)  # same transition, settles the chain

    # -- domain helpers (called by states) ------------------------------------

    def player_key(self, name: str | None) -> str | None:
        """Canonical identity for a name (case-insensitive), or None.

        Resolves against the append-only identity registry: a player who left
        still resolves (the ledger is theirs); a stranger never does.
        """
        if not isinstance(name, str) or not name:
            return None
        return self._identities.get(name.strip().lower())

    @staticmethod
    def _reset_per_clue(core: Core) -> Core:
        """Fresh race, fresh grading scratch — every clue change starts clean."""
        return replace(
            core,
            buzz_queue=[],
            answered_players=set(),
            clue_scored=False,
            answer_revealed=False,
        )

    def prepare_clue(self, clue_id: str, core: Core) -> Core:
        board = self.boards.get(core.round)
        if board is None or clue_id not in board.clues:
            raise InvalidAction("that clue isn't on this board")
        if core.clue_status.get(clue_id) is ClueStatus.ANSWERED:
            raise InvalidAction("that clue is already played")
        status = {
            cid: (
                ClueStatus.ACTIVE
                if cid == clue_id
                else ClueStatus.BOARD
                if s is ClueStatus.ACTIVE
                else s
            )
            for cid, s in core.clue_status.items()
        }
        return self._reset_per_clue(replace(core, clue_status=status))

    def retire_clue(self, clue_id: str, core: Core) -> Core:
        """Host closed the clue: scored clues are spent, skips go back."""
        status = dict(core.clue_status)
        status[clue_id] = ClueStatus.ANSWERED if core.clue_scored else ClueStatus.BOARD
        return self._reset_per_clue(replace(core, clue_status=status))

    def mark_answered(self, clue_id: str, core: Core) -> Core:
        status = dict(core.clue_status)
        status[clue_id] = ClueStatus.ANSWERED
        return self._reset_per_clue(replace(core, clue_status=status))

    def require_selectable(self, player_id: str) -> str:
        if self.host and player_id.strip().lower() == self.host.lower():
            raise InvalidAction("the host reads the clues — contenders ring in")
        key = self.player_key(player_id)
        if key is None:
            raise InvalidAction("they aren't a contender on this show")
        if key in self.viewers:
            raise InvalidAction("they're sitting this one out with the audience")
        if key in self.answered_players:
            raise InvalidAction(
                f"{key} already had this clue — call on someone else, or toss up the rest"
            )
        return key

    def mark_viewer(self, player_id: str, viewer: object) -> Core | None:
        """Bench/unbench — environment. Returns a fresh Core only when the
        change invalidates a game pointer (the picker), else None."""
        key = self.player_key(player_id)
        if key is None or key not in self._seated:
            raise InvalidAction("they aren't a contender on this show")
        if not isinstance(viewer, bool):
            raise InvalidAction("choose audience or contender — on or off")
        active = self.active_state
        if isinstance(active, Answering) and active.player_id.lower() == key.lower():
            raise InvalidAction("let them finish answering first")  # can't bench the answerer
        if viewer:
            if key in self.viewers:
                raise InvalidAction("they're already out with the audience")
            self.viewers.add(key)
        else:
            if key not in self.viewers:
                raise InvalidAction("they're already at the podium")
            self.viewers.discard(key)
        if self.picker == key:
            return replace(self._core, picker=None)  # benched the player on the clock
        return None

    def transfer_host(self, player_id: str) -> None:
        key = self.player_key(player_id)
        if key is None or key not in self._seated:
            raise InvalidAction("they aren't a contender on this show")
        if self.has_left(key):
            raise InvalidAction("they stepped out — they slip back in first")
        previous = self.host
        self.host = key
        self.viewers.discard(key)  # the host runs the game, they don't watch
        if previous:
            self._identities.setdefault(previous.lower(), previous)
            self._seated.setdefault(previous, None)  # old host takes a seat

    def assign_picker(self, player_id: object) -> str | None:
        """Who picks the next clue — validates and returns the new value; the
        caller installs it into a fresh Core (one hook for the host's button
        and the automatic "correct answer picks next" rule). ``None`` clears."""
        if player_id is None:
            return None
        if not isinstance(player_id, str):
            raise InvalidAction("name a contender to give the pick to — or open the board to everyone")
        if self.host and player_id.strip().lower() == self.host.lower():
            raise InvalidAction("the host reads the clues — contenders ring in")
        key = self.player_key(player_id)
        if key is None:
            raise InvalidAction("they aren't a contender on this show")
        if key not in self._seated:
            return None  # known identity, but they left: nobody gets the clock
        if key in self.viewers:
            raise InvalidAction("the audience can't hold the pick — bring them back in first")
        if self.has_left(key):
            raise InvalidAction("they stepped out — they slip back in first")
        return key

    def quest_owner(self) -> str:
        """Show rule: you pick the marked card, you own it — the host decides
        who has the board before the flip."""
        if not self.picker or self.picker not in self.players or self.picker in self.viewers:
            raise InvalidAction("give someone the pick first — then flip the quest card")
        return self.picker

    def _clue(self, clue_id: str) -> Clue:
        board = self.board
        if board is None or not isinstance(clue_id, str) or clue_id not in board.clues:
            raise InvalidAction("that clue isn't on this board")
        return board.clues[clue_id]

    def require_scored(self, player_id: str) -> str:
        """Any known identity may hold a score — the ledger outlives the roster."""
        key = self.player_key(player_id)
        if key is None:
            raise InvalidAction("they aren't a contender on this show")
        return key

    def score_correct(self, player_id: str, clue_id: str, core: Core) -> Core:
        key = self.require_scored(player_id)
        scores = dict(core.scores)
        scores[key] = scores.get(key, 0) + self._clue(clue_id).value
        return replace(core, scores=scores, clue_scored=True)

    def score_incorrect(self, player_id: str, clue_id: str, core: Core) -> Core:
        key = self.require_scored(player_id)
        scores = dict(core.scores)
        scores[key] = scores.get(key, 0) - self._clue(clue_id).value
        answered = set(core.answered_players) | {key}
        return replace(core, scores=scores, answered_players=answered, clue_scored=True)

    def apply_adjust(self, player_id: str, value: int, core: Core) -> Core:
        key = self.require_scored(player_id)
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidAction("score fixes are whole dollars — no cents")
        scores = dict(core.scores)
        scores[key] = scores.get(key, 0) + value
        return replace(core, scores=scores)

    def apply_final_grade(self, player_id: str, correct: bool, core: Core) -> Core:
        if not isinstance(correct, bool):
            raise InvalidAction("call it right or wrong")
        key = self.require_scored(player_id)
        if key in core.graded:
            raise InvalidAction("already called — the ruling stands")
        wager = core.wagers.get(key, 0)
        scores = dict(core.scores)
        scores[key] = scores.get(key, 0) + (wager if correct else -wager)
        graded = set(core.graded) | {key}
        return replace(core, scores=scores, graded=graded)

    def apply_quest_grade(self, player_id: str, correct: bool, core: Core) -> Core:
        key = self.require_scored(player_id)
        amount = core.quest_wagers.get(key, 0)
        scores = dict(core.scores)
        scores[key] = scores.get(key, 0) + (amount if correct else -amount)
        answered = set(core.answered_players)
        if not correct:
            answered.add(key)  # struck out — others may still try
        return replace(core, scores=scores, answered_players=answered, clue_scored=True)


# ---------------------------------------------------------------------------
# Snapshots: one host-truth view, per-viewer masks (formatting only — no
# game decisions). The host sees everything (they run the game). Players and
# viewers (spectators are identity-less players for redaction: they hold no
# secrets of their own) see the host truth masked: answers until the host
# reveals them, and only their own Final wagers/answers until the reveal.
# The quest's home square rides the same rule: host truth, scrubbed for the
# room until play uncovers it.
# Host controls ride through unmasked — ``perform`` refuses non-hosts and the
# UI gates on host identity, so they are capability metadata, never secrets.
# ---------------------------------------------------------------------------


def is_host_viewer(game: Game, viewer: str | None) -> bool:
    """The host always sees the unmasked truth."""
    return bool(viewer and game.host and viewer.strip().lower() == game.host.lower())


def host_snapshot(game: Game) -> dict:
    """The full room view: every clue answer, every Final secret, host controls.

    Built once per publish; per-socket masks (``mask_snapshot``) redact it
    cheaply instead of rebuilding the board per viewer.
    """
    st = game.state
    paused = isinstance(st, Paused)
    eff = st.previous if paused else st
    active_clue_id = (
        eff.clue_id
        if isinstance(eff, (Asking, BuzzersOpen, Answering, QuestWager, QuestAnswering))
        else None
    )
    answerer = (
        eff.player_id if isinstance(eff, (Answering, QuestWager, QuestAnswering)) else None
    )
    quest_state = eff if isinstance(eff, (QuestWager, QuestAnswering)) else None
    quest_wager = (
        game.quest_wagers.get(quest_state.player_id) if quest_state is not None else None
    )
    quest_limit = (
        game.quest_limit(quest_state.player_id) if quest_state is not None else None
    )

    categories: list[str] = []
    clues: list[dict] = []
    board = game.board
    if board is not None:
        categories = list(board.categories)
        for clue in board.clues.values():
            status = game.clue_status.get(clue.id, ClueStatus.BOARD)
            clues.append(
                {
                    "id": clue.id,
                    "category": clue.category,
                    "value": clue.value,
                    "state": status.value,
                    "question": clue.question,
                    "answer": clue.answer,
                }
            )

    active = next((c for c in clues if c["id"] == active_clue_id), None)
    final_states = (FinalCategory, FinalWagering, FinalClue, FinalReveal, Review, GameOver)

    undoable = game.can_undo and not game.closed
    actions = [{"name": a.name, "label": a.label} for a in game.available_host_actions]
    if undoable:
        actions.append({"name": "undo", "label": "Take It Back"})
    now = time.time()
    departed = game.departed(now)
    away = sorted(
        k for k in set(game._seated) | ({game.host} if game.host else set())
        if k not in departed
        and now - game._seen.get(k, game.created_at) >= AWAY_GRACE_SECONDS / 2
    )

    return {
        "code": game.code,
        "host": game.host,
        "hostMode": game.host_mode.value,
        "state": st.name,
        "pausedIn": st.previous.name if paused else None,
        "closed": game.closed,
        "closeReason": game.close_reason,
        "round": game.round.name.lower(),
        "roundLabel": game.round.label,
        "players": dict(game.players),
        "viewers": sorted(game.viewers),
        "departed": departed,
        "away": away,
        "categories": categories,
        "clues": clues,
        "activeClueId": active_clue_id,
        "question": active["question"] if active else None,
        "answer": active["answer"] if active else None,
        "answerRevealed": game.answer_revealed,
        "answerer": answerer,
        "rev": game.rev,
        "activeQuest": game.active_quest,
        "picker": game.picker,
        "buzzToken": game.race_token,
        "questWager": quest_wager,
        "questLimit": quest_limit,
        "buzzQueue": list(game.buzz_queue),
        "answeredPlayers": list(game.answered_players),
        "final": {
            "category": game.final.category,
            "question": game.final.question,
            "wagers": dict(game.wagers),
            "answers": dict(game.final_answers),
            "graded": sorted(game.graded),
            "wagerOpen": isinstance(game.state, FinalWagering),
            "answerOpen": isinstance(game.state, FinalClue),
        },
        "availableHostActions": actions,
        "canUndo": undoable,
        "showFinal": isinstance(eff, final_states),
    }


def mask_snapshot(game: Game, base: dict, viewer: str | None) -> dict:
    """Redact a host-truth view for one non-host viewer (shape is stable —
    secrets become ``None``/``{}``/``[]``).

    Players additionally keep their *own* Final wagers/answers until the
    reveal; spectators (viewers, anonymous) hold none, so the same filter
    covers both — no separate spectator path. Host controls
    (``availableHostActions``/``canUndo``) ride through unmasked: the frontend
    gates them on host identity and ``perform`` authenticates, so advertising
    them leaks nothing.
    """
    viewer_key = game.player_key(viewer)

    st = game.state
    paused = isinstance(st, Paused)
    eff = st.previous if paused else st
    reveal_states = (FinalReveal, Review, GameOver)
    can_see_all_final = isinstance(eff, reveal_states)
    sees_final_question = isinstance(eff, (FinalClue, *reveal_states))

    revealed = game.answer_revealed

    clues = []
    for clue in base["clues"]:
        status = clue["state"]
        entry = dict(clue)
        if status != ClueStatus.ANSWERED.value:
            if status != ClueStatus.ACTIVE.value:
                # Board tiles never carry text — only the modal's active clue does.
                entry["question"] = None
                entry["answer"] = None
            elif not revealed:
                # The answer stays off every player's screen until the host
                # reveals it — a contender who has the floor (and the rest of
                # the room, who can still buzz after a miss) must not read it.
                entry["answer"] = None
        clues.append(entry)
    active = next((c for c in clues if c["id"] == base["activeClueId"]), None)

    final = dict(base["final"])
    final["question"] = base["final"]["question"] if sees_final_question else None
    final["wagers"] = {
        name: amount
        for name, amount in base["final"]["wagers"].items()
        if can_see_all_final or name == viewer_key
    }
    final["answers"] = {
        name: text
        for name, text in base["final"]["answers"].items()
        if can_see_all_final or name == viewer_key
    }
    final["graded"] = base["final"]["graded"] if can_see_all_final else []

    view = dict(base)
    view["clues"] = clues
    view["question"] = active["question"] if active else None
    view["answer"] = active["answer"] if active else None
    # The quest's home square is host truth: the marker (gold chip, glow) is
    # this game's Daily Double, so the room discovers it by playing a clue, not
    # by reading the board. The host keeps it — they run the flip.
    view["activeQuest"] = None
    view["final"] = final
    return view


def snapshot(game: Game, viewer: str | None = None) -> dict:
    """Serialize the game for one viewer: host truth, or the masked view."""
    base = host_snapshot(game)
    if is_host_viewer(game, viewer):
        return base
    return mask_snapshot(game, base, viewer)


def snapshots_for(game: Game, viewers: list[str | None]) -> dict[str | None, dict]:
    """Batch one publish: build the host truth once, mask per viewer.

    The host and every distinct player identity get their own payload;
    repeat identities (two sockets, same player) share one dict. Spectators
    (``None``, unknown names, viewers) share a single mask — the own-secrets
    filter matches nothing for them.
    """
    base = host_snapshot(game)
    out: dict[str | None, dict] = {}
    by_key: dict[str | None, dict] = {}
    for viewer in viewers:
        if is_host_viewer(game, viewer):
            if "host" not in by_key:
                by_key["host"] = base
            out[viewer] = by_key["host"]
            continue
        key = game.player_key(viewer)
        if key not in by_key:
            by_key[key] = mask_snapshot(game, base, viewer)
        out[viewer] = by_key[key]
    return out


# ---------------------------------------------------------------------------
# Async registry (transport-facing)
# ---------------------------------------------------------------------------


def normalize_code(code: str) -> str:
    return (code or "").strip()


class GameManager:
    """Async registry of games.

    Mutations run synchronously on the event loop — ``run`` rejects anything
    awaitable — so the single thread serializes them and no lock is needed.
    The game domain itself stays synchronous (trivially unit-testable).
    """

    def __init__(self, code_factory: Callable[[], str] | None = None) -> None:
        self._games: dict[str, Game] = {}
        self._code_factory = code_factory or self._random_code

    @staticmethod
    def _random_code() -> str:
        return "".join(random.choices(VALID_CODE_CHARS, k=4))

    def _must_get(self, code: str) -> Game:
        game = self._games.get(normalize_code(code))
        if game is None:
            raise RoomError(f"room {code} not found")
        return game

    async def create(self, boards: dict[Round, Board], final: FinalData) -> Game:
        # Single-threaded loop: check-then-insert can't interleave, no lock.
        for _ in range(50):
            code = normalize_code(self._code_factory())
            if code not in self._games:
                game = Game(code=code, boards=boards, final=final)
                self._games[code] = game
                return game
        raise RoomError("could not allocate room code")

    async def get(self, code: str) -> Game:
        return self._must_get(code)

    async def run(self, code: str, action: Callable[[Game], object]) -> Game:
        """Apply a synchronous domain action — the mutation happens whole or not at all."""
        game = self._must_get(code)
        result = action(game)
        if inspect.isawaitable(result):
            raise TypeError(
                "mutations must be synchronous — no await, network, or disk inside run()"
            )
        return game

    async def sweep(self, now: float | None = None) -> list[str]:
        """Reap lifecycle-expired rooms: emptied seats close the room, and the
        two-hour show clock and the twenty-minute quiet room do too. Sweeping
        never touches a live game — it only closes, and closing is final."""
        stamp = time.time() if now is None else now
        culled: list[str] = []
        for code, game in list(self._games.items()):
            game.sweep_departed(stamp)
            reason = game.cull_reason(stamp)
            if reason is None:
                continue
            game.close_room(reason)
            culled.append(code)
        for code in culled:
            self._games.pop(code, None)
        return culled


__all__ = [
    "Board",
    "Clue",
    "ClueStatus",
    "FinalData",
    "Game",
    "GameManager",
    "InvalidAction",
    "RoomError",
    "build_board",
    "host_snapshot",
    "is_host_viewer",
    "mask_snapshot",
    "normalize_code",
    "snapshot",
    "snapshots_for",
]
