"""Host modes: how the show gets run — by the host alone, or shared with the room.

``MANUAL`` is the classic night: the host picks every clue and runs every
beat. ``ASSISTED`` shares the hosting with the player holding the board —
they choose the clues while the show keeps the pace (one move in, the show
carries it to a natural pause). The layer exists so new hosting styles plug
in without touching ``Game.perform``, the routers, or the snapshot call sites:

* ``ASSISTED``: the board-holder taps tiles (player event ``pick``), and the show
  auto-fires what needs no judgment — opening the buzzers after a read,
  taking the quest answer once the wager is locked, skipping clues nobody
  can answer any more, and marching the finals forward once every player
  has moved. Co-hosted beats are both refused from and hidden in the
  host API (``authorize`` + ``advertise``), so the host UI never races the
  show. Calling right/wrong (clues, quest answers, finals) stays manual.
* ``AUTO`` (reserved): no human host required — a driver calls
  ``Game.perform`` with host authority on timers/events. Needs: an async
  driver *outside* ``GameManager.run`` (mutations stay synchronous) plus a
  policy that authorizes the driver as sender.

The split of duties:

* ``Game.perform`` authenticates (host-only — unchanged) and executes.
* ``HostPolicy.authorize`` adds per-mode rules; raising ``InvalidAction``
  refuses the action with the policy's message.
* ``HostPolicy.advertise`` filters the state's capability list for the mode.
  Both consumers — perform's allow-list and the snapshot's
  ``availableHostActions`` — go through it, so the UI can never offer what
  the mode refuses.
* ``HostPolicy.auto_step`` proposes the next beat after a commit,
  or ``None`` when the show is at rest; ``Game._settle_assist`` runs the
  show on. Each show beat is a full host action (own history checkpoint,
  own rev bump): taking it back lands on the in-between beat and does
  not replay — the host holds the rest beat with manual control,
  taking back again as usual.

Policies run inside ``GameManager.run``: synchronous, no await, no network,
no disk — same contract as every other mutation.

``Game.host_mode`` is environment (like the host seat and viewer flags), not
a game fact: switching modes bumps ``rev`` and still settles, checkpoints
undo history so the transition itself can be rewound, and survives undo
(it is never the thing rewound). The route is ``"set_host_mode"`` in
``Game.perform`` beside ``"undo"`` (it is not a state capability).
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from .errors import InvalidAction
from .states import HostAction

if TYPE_CHECKING:
    from .game import Game  # noqa: F401 (typing only; avoids import cycle)


class HostMode(Enum):
    """Hosting posture vocabulary. Only MANUAL is registered (see below);
    the others are reserved names so rooms can name a mode before it exists —
    selecting one is refused until a policy is registered."""

    MANUAL = "manual"
    COHOSTED = "co-hosted"
    # Deprecated alias: the wire used to say "assisted". Still accepted on
    # input so old clients keep working; new code should use COHOSTED.
    ASSISTED = "co-hosted"
    AUTO = "auto"

    @classmethod
    def _missing_(cls, value: object) -> HostMode | None:
        if isinstance(value, str):
            v = value.strip().lower().replace("_", "-")
            if v in ("assisted", "cohosted", "co-hosted", "shared", "shared-hosting"):
                return cls.COHOSTED
        return None


class HostPolicy:
    """Middleware around host actions for one mode (see module docstring)."""

    mode: HostMode

    def authorize(
        self, game: Game, sender: str | None, action: str, payload: dict
    ) -> None:
        """Raise ``InvalidAction`` to refuse a host action; pass to allow it."""
        raise NotImplementedError

    def advertise(
        self, game: Game, actions: tuple[HostAction, ...]
    ) -> tuple[HostAction, ...]:
        """Filter the state's capabilities down to what this mode offers."""
        raise NotImplementedError

    def auto_step(self, game: Game) -> tuple[str, dict] | None:
        """Next mechanical step after a commit, or ``None`` at rest."""
        return None


class ManualHostPolicy(HostPolicy):
    """Full manual: the human host runs everything. Authentication lives in
    ``Game.perform``; manual adds no rules and hides no capabilities."""

    mode = HostMode.MANUAL

    def authorize(
        self, game: Game, sender: str | None, action: str, payload: dict
    ) -> None:
        return None

    def advertise(
        self, game: Game, actions: tuple[HostAction, ...]
    ) -> tuple[HostAction, ...]:
        return actions


class CohostedHostPolicy(HostPolicy):
    """Co-hosted: the board-holder chooses, the show runs the mechanics.

    Delegation: ``pick_clue`` belongs to the holder (player event ``pick``)
    — the host API refuses it and the host UI hides it. After a
    read the buzzers open themselves, the quest answer comes up once the
    wager is locked, dead clues skip themselves, and the finals march
    once every player has moved. The same co-hosted set is hidden from
    ``availableHostActions`` so one action in runs the show to rest with
    no host race. Everything needing judgment — calling answers right or
    wrong, fixing scores, seating, pause, take-backs, hosting-style switches
    — stays manual and stays advertised.
    """

    mode = HostMode.COHOSTED

    def authorize(
        self, game: Game, sender: str | None, action: str, payload: dict
    ) -> None:
        if action in self._auto_actions(game):
            raise InvalidAction(
                "the show runs that one while co-hosted — it stays off your buttons"
            )

    def advertise(
        self, game: Game, actions: tuple[HostAction, ...]
    ) -> tuple[HostAction, ...]:
        hidden = self._auto_actions(game)
        return tuple(a for a in actions if a.name not in hidden)

    def auto_step(self, game: Game) -> tuple[str, dict] | None:
        from .states import (
            Answering,
            Asking,
            BuzzersOpen,
            FinalCategory,
            FinalClue,
            FinalWagering,
            Paused,
            QuestWager,
        )

        state = game.state
        if isinstance(state, Paused) or game.closed:
            return None
        if isinstance(state, QuestWager):
            if state.player_id in game.quest_wagers:
                return ("begin_quest_answer", {})
            return None
        # Asking and BuzzersOpen are siblings (both Pausable): quest reads
        # hold for the wager (a player move, not a host move); plain reads
        # open the buzzers; a quiet round nobody can answer skips itself.
        if isinstance(state, Asking):
            if state.clue_id == game.active_quest:
                return None
            return ("open_buzzers", {})
        if isinstance(state, BuzzersOpen):
            if game.dead_clue(state.clue_id):
                return ("close_clue", {})
            return None
        if isinstance(state, Answering):
            if game.dead_clue(state.clue_id):
                return ("close_clue", {})
            return None
        if isinstance(state, FinalCategory):
            return ("open_final_wagering", {})
        if isinstance(state, FinalWagering):
            seated = set(game.present_players())
            if seated and seated <= set(game.wagers):
                return ("close_final_wagering", {})
            return None
        if isinstance(state, FinalClue):
            seated = set(game.present_players())
            if seated and seated <= set(game.final_answers):
                return ("start_final_reveal", {})
            return None
        return None  # Board / intermission / reveal / review: the host holds the show

    @staticmethod
    def _auto_actions(game: Game) -> set[str]:
        """What the show takes over while co-hosted, given the current beat.

        Holder choices are always host-locked; the show's beats lock exactly
        when they would fire (a fresh read locks ``open_buzzers`` so the host
        can't beat the show to it, a full book locks the finals advance so
        the show marches it). Calling right/wrong and seating never lock:
        the show locks only what it runs, so the host keeps every judgment call.
        """
        from .states import (
            Answering,
            Asking,
            BuzzersOpen,
            FinalCategory,
            FinalClue,
            FinalWagering,
        )

        locked = {"pick_clue"}
        state = game.state
        if isinstance(state, Asking):
            if state.clue_id != game.active_quest:
                locked.add("open_buzzers")
            if game.everyone_answered(state.clue_id):
                locked.add("close_clue")
        if isinstance(state, BuzzersOpen):
            if game.dead_clue(state.clue_id):
                locked.add("close_clue")
        if isinstance(state, Answering):
            if game.dead_clue(state.clue_id):
                locked.add("close_clue")
        if isinstance(state, FinalCategory):
            locked.add("open_final_wagering")
        if isinstance(state, FinalWagering):
            # Wagering open: the host could only cut the wagers short — co-hosted
            # holds the reveal for every player, then the show marches it.
            locked.add("close_final_wagering")
        if isinstance(state, FinalClue):
            locked.add("start_final_reveal")
        return locked


_POLICIES: dict[HostMode, HostPolicy] = {}


def register_host_policy(policy: HostPolicy) -> None:
    """Install the policy for its mode (future modes and tests)."""
    _POLICIES[policy.mode] = policy


def get_host_policy(mode: HostMode) -> HostPolicy:
    """Resolve a mode to its policy — reserved modes refuse until implemented."""
    try:
        return _POLICIES[mode]
    except KeyError:
        raise InvalidAction(
            "that hosting style isn't on the menu yet"
        ) from None


register_host_policy(ManualHostPolicy())
register_host_policy(CohostedHostPolicy())
# Back-compat: old code imports AssistedHostPolicy by name.
AssistedHostPolicy = CohostedHostPolicy


__all__ = [
    "AssistedHostPolicy",
    "CohostedHostPolicy",
    "HostMode",
    "HostPolicy",
    "ManualHostPolicy",
    "get_host_policy",
    "register_host_policy",
]
