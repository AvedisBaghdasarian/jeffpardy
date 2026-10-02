"""Capability-mixin state machine.

Design (per the project vision):

* ``State`` discovers ``available_host_actions`` by walking the MRO for
  ``action = HostAction(...)`` declared on capability base classes — there is
  no transition table and no allowed-actions list to keep in sync. Inheritance
  *is* the capability declaration.
* Concrete states are frozen dataclasses: the phase only. A state method
  receives the ``Game``, threads its immutable ``Core`` through pure domain
  ops, and *returns* the next ``Core`` — only ``Game._commit`` installs it
  (with an undo checkpoint); player events use ``Game._event`` (no history)
  and environment ops never touch the core.
* ``PausableState`` / ``Paused(previous=...)`` wrap any mid-flow state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import TYPE_CHECKING

from .errors import InvalidAction

if TYPE_CHECKING:
    from .game import Core, Game  # noqa: F401  (typing only; avoids import cycle)

ClueId = str
PlayerId = str


class Round(Enum):
    MAIN = auto()
    BONUS = auto()
    ULTIMATE = auto()

    @property
    def label(self) -> str:
        return {
            Round.MAIN: "Jeffpardy",
            Round.BONUS: "Bonus Jeffpardy",
            Round.ULTIMATE: "Ultimate Jeffpardy",
        }[self]


# ---------------------------------------------------------------------------
# Host action metadata + base state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HostAction:
    name: str
    label: str


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


class State:
    @property
    def name(self) -> str:
        return _snake(type(self).__name__)

    @property
    def available_host_actions(self) -> tuple[HostAction, ...]:
        """Discover actions from capability base classes via the MRO.

        No separate allowed-actions table is required.
        """
        actions: list[HostAction] = []
        seen: set[str] = set()
        for cls in type(self).__mro__:
            action = cls.__dict__.get("action")
            if isinstance(action, HostAction) and action.name not in seen:
                actions.append(action)
                seen.add(action.name)
        return tuple(actions)


# ---------------------------------------------------------------------------
# Host capabilities
# ---------------------------------------------------------------------------


class CanStartGame:
    action = HostAction("start_game", "Start the Show")

    def start_game(self, game: Game) -> Core:
        raise NotImplementedError


class CanFinishIntro:
    action = HostAction("finish_intro", "Roll the Board")

    def finish_intro(self, game: Game) -> Core:
        raise NotImplementedError


class CanPickClue:
    action = HostAction("pick_clue", "Play a Clue")  # tiles + quest card render per-clue instead

    def pick_clue(self, game: Game, clue_id: ClueId) -> Core:
        raise NotImplementedError


class CanOpenBuzzers:
    action = HostAction("open_buzzers", "Open the Buzzers")

    def open_buzzers(self, game: Game) -> Core:
        raise NotImplementedError


class CanRevealAnswer:
    action = HostAction("reveal_answer", "Reveal the Answer")

    def reveal_answer(self, game: Game) -> Core:
        raise NotImplementedError


class CanSelectBuzzer:
    action = HostAction("select_buzzer", "Call on a Player")  # buzz list buttons render per-player instead

    def select_buzzer(self, game: Game, player_id: PlayerId) -> Core:
        raise NotImplementedError


class CanMarkCorrect:
    action = HostAction("correct", "Right")  # rendered as "Right" next to the answerer

    def correct(self, game: Game) -> Core:
        raise NotImplementedError


class CanMarkIncorrect:
    action = HostAction("incorrect", "Wrong")  # rendered as "Wrong" next to the answerer

    def incorrect(self, game: Game) -> Core:
        raise NotImplementedError


class CanBeginQuestAnswer:
    action = HostAction("begin_quest_answer", "Hear Their Answer")

    def begin_quest_answer(self, game: Game) -> Core:
        raise NotImplementedError


class CanCloseClue:
    action = HostAction("close_clue", "Back to the Board")

    def close_clue(self, game: Game) -> Core:
        raise NotImplementedError


class CanFinishRound:
    action = HostAction("finish_round", "Wrap the Round")

    def finish_round(self, game: Game) -> Core:
        raise NotImplementedError


class CanStartNextRound:
    action = HostAction("start_next_round", "Start the Next Round")

    def start_next_round(self, game: Game) -> Core:
        raise NotImplementedError


class CanOpenFinalWagering:
    action = HostAction("open_final_wagering", "Open Ultimate Wagers")

    def open_final_wagering(self, game: Game) -> Core:
        raise NotImplementedError


class CanCloseFinalWagering:
    action = HostAction("close_final_wagering", "Read the Ultimate Clue")

    def close_final_wagering(self, game: Game) -> Core:
        raise NotImplementedError


class CanStartFinalReveal:
    action = HostAction("start_final_reveal", "Reveal the Responses")

    def start_final_reveal(self, game: Game) -> Core:
        raise NotImplementedError


class CanGradeFinal:
    action = HostAction("grade_final", "Judge")  # finals panel renders per-player Right/Wrong instead

    def grade_final(self, game: Game, player_id: PlayerId, correct: bool) -> Core:
        raise NotImplementedError


class CanEndGame:
    action = HostAction("end_game", "Show Final Scores")

    def end_game(self, game: Game) -> Core:
        raise NotImplementedError


class CanCloseGame:
    action = HostAction("close_game", "Sign Off the Room")

    def close_game(self, game: Game) -> Core:
        raise NotImplementedError


class CanAdjustScore:
    action = HostAction("adjust_score", "Fix a Score")

    def adjust_score(self, game: Game, player_id: PlayerId, value: int) -> Core:
        raise NotImplementedError


class CanPause:
    action = HostAction("pause", "Take a Break")

    def pause(self, game: Game) -> Core:
        raise NotImplementedError


class CanResume:
    action = HostAction("resume", "Back to the Game")

    def resume(self, game: Game) -> Core:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Pausable wrapper
# ---------------------------------------------------------------------------


class CanSetViewer:
    action = HostAction("set_viewer", "Sit Out / Back In")

    # Environment gate only: sitting out is decided by Game (roster/auth) and
    # never checkpoints undo history. The capability declares WHERE sitting
    # out is allowed; Game does HOW.


class CanPromoteHost:
    action = HostAction("promote_host", "Hand Over Hosting")

    # Environment gate only — promotion is Game's business (see above).


class CanSetPicker:
    action = HostAction("set_picker", "Give the Board to…")

    # The board-holder is a field, not a transition: the host's button and the
    # automatic "right answer chooses next" rule funnel through one hook
    # (game.assign_picker), and the pick round simply re-issues with a new
    # parameter — Board → Board. The generic label is never rendered: the
    # board shows one "give it to X" button per contender (see HostControls's
    # CONTEXTUAL set), because a bare button has no player to name.
    def set_picker(self, game: Game, player_id: PlayerId | None) -> Core:
        return replace(game.core, picker=game.assign_picker(player_id))


class PausableState(State, CanPause, CanSetViewer):
    def pause(self, game: Game) -> Core:
        return replace(game.core, state=Paused(previous=self))


# ---------------------------------------------------------------------------
# Main game states
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Lobby(State, CanStartGame, CanAdjustScore, CanSetViewer, CanPromoteHost):
    def start_game(self, game: Game) -> Core:
        return replace(game.core, state=Intro())

    def adjust_score(self, game: Game, player_id: PlayerId, value: int) -> Core:
        return game.apply_adjust(player_id, value, game.core)


@dataclass(frozen=True, slots=True)
class Intro(PausableState, CanFinishIntro):
    def finish_intro(self, game: Game) -> Core:
        core = replace(game.core, round=Round.MAIN)
        return replace(game.arm_quest(core), state=Board())


@dataclass(frozen=True, slots=True)
class Board(PausableState, CanPickClue, CanFinishRound, CanAdjustScore, CanSetPicker):
    def pick_clue(self, game: Game, clue_id: ClueId) -> Core:
        core = game.prepare_clue(clue_id, game.core)  # validates + resets per-clue
        if game.is_quest_clue(clue_id):
            # The show's rule: the flip IS the claim — whoever holds the board
            # owns the quest, and the host just pressed the button.
            return replace(core, state=QuestWager(clue_id=clue_id, player_id=game.quest_owner()))
        return replace(core, state=Asking(clue_id=clue_id))

    def finish_round(self, game: Game) -> Core:
        if game.round is Round.MAIN:
            return replace(game.core, state=RoundIntermission(next_round=Round.BONUS))
        if game.round is Round.BONUS:
            core = replace(game.core, round=Round.ULTIMATE, active_quest=None)  # no board
            return replace(core, state=FinalCategory())
        raise InvalidAction("there's no board round in Ultimate Jeffpardy")

    def adjust_score(self, game: Game, player_id: PlayerId, value: int) -> Core:
        return game.apply_adjust(player_id, value, game.core)


@dataclass(frozen=True, slots=True)
class Asking(PausableState, CanOpenBuzzers, CanRevealAnswer, CanCloseClue):
    clue_id: ClueId

    def open_buzzers(self, game: Game) -> Core:
        game.start_race()  # this pass of the race starts now
        return replace(game.core, state=BuzzersOpen(clue_id=self.clue_id))

    def reveal_answer(self, game: Game) -> Core:
        return replace(game.core, answer_revealed=True)

    def close_clue(self, game: Game) -> Core:
        core = game.retire_clue(self.clue_id, game.core)
        return replace(core, state=Board())


@dataclass(frozen=True, slots=True)
class BuzzersOpen(
    PausableState,
    CanSelectBuzzer,
    CanCloseClue,
    CanRevealAnswer,
):
    clue_id: ClueId

    def select_buzzer(self, game: Game, player_id: PlayerId) -> Core:
        player_id = game.require_selectable(player_id)
        game.end_race()  # first in has answered; this pass of the race is over
        core = replace(game.core, buzz_queue=[])  # leaving buzzers_open flushes
        return replace(core, state=Answering(clue_id=self.clue_id, player_id=player_id))

    def close_clue(self, game: Game) -> Core:
        core = game.retire_clue(self.clue_id, game.core)
        game.end_race()
        return replace(core, state=Board())

    def reveal_answer(self, game: Game) -> Core:
        return replace(game.core, answer_revealed=True)


@dataclass(frozen=True, slots=True)
class Answering(PausableState, CanMarkCorrect, CanMarkIncorrect):
    clue_id: ClueId
    player_id: PlayerId

    def correct(self, game: Game) -> Core:
        core = game.score_correct(self.player_id, self.clue_id, game.core)
        core = game.mark_answered(self.clue_id, core)
        core = replace(core, picker=game.assign_picker(self.player_id))  # right answer chooses
        return replace(core, state=Board())

    def incorrect(self, game: Game) -> Core:
        core = game.score_incorrect(self.player_id, self.clue_id, game.core)
        game.start_race()  # re-opening the floor = a fresh pass of the race
        return replace(core, state=BuzzersOpen(clue_id=self.clue_id))


# ---------------------------------------------------------------------------
# Gamely Jeff Quest (the jackpot clue: the host flips the marked card to
# whoever holds the board — they wager alone, ruled on the wager)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuestWager(PausableState, CanBeginQuestAnswer):
    clue_id: ClueId
    player_id: PlayerId

    def begin_quest_answer(self, game: Game) -> Core:
        if self.player_id not in game.quest_wagers:
            raise InvalidAction(f"waiting on {self.player_id}'s wager before the clue comes up")
        return replace(game.core, state=QuestAnswering(clue_id=self.clue_id, player_id=self.player_id))


@dataclass(frozen=True, slots=True)
class QuestAnswering(PausableState, CanMarkCorrect, CanMarkIncorrect):
    clue_id: ClueId
    player_id: PlayerId

    def correct(self, game: Game) -> Core:
        core = game.apply_quest_grade(self.player_id, True, game.core)
        core = game.mark_answered(self.clue_id, core)
        core = replace(core, picker=game.assign_picker(self.player_id))  # holder chooses next
        return replace(core, state=Board())

    def incorrect(self, game: Game) -> Core:
        core = game.apply_quest_grade(self.player_id, False, game.core)
        core = game.retire_clue(self.clue_id, core)  # a miss spends the quest for everyone
        return replace(core, state=Board())


# ---------------------------------------------------------------------------
# Between rounds
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoundIntermission(PausableState, CanStartNextRound, CanAdjustScore):
    next_round: Round

    def start_next_round(self, game: Game) -> Core:
        core = game.arm_quest(replace(game.core, round=self.next_round))
        return replace(core, state=Board())

    def adjust_score(self, game: Game, player_id: PlayerId, value: int) -> Core:
        return game.apply_adjust(player_id, value, game.core)


# ---------------------------------------------------------------------------
# Ultimate Jeffpardy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FinalCategory(PausableState, CanOpenFinalWagering):
    def open_final_wagering(self, game: Game) -> Core:
        return replace(game.core, state=FinalWagering())


@dataclass(frozen=True, slots=True)
class FinalWagering(PausableState, CanCloseFinalWagering):
    def close_final_wagering(self, game: Game) -> Core:
        return replace(game.core, state=FinalClue())


@dataclass(frozen=True, slots=True)
class FinalClue(PausableState, CanStartFinalReveal):
    def start_final_reveal(self, game: Game) -> Core:
        return replace(game.core, state=FinalReveal())


@dataclass(frozen=True, slots=True)
class FinalReveal(PausableState, CanGradeFinal, CanEndGame):
    def grade_final(self, game: Game, player_id: PlayerId, correct: bool) -> Core:
        return game.apply_final_grade(player_id, correct, game.core)

    def end_game(self, game: Game) -> Core:
        return replace(game.core, state=Review())


# ---------------------------------------------------------------------------
# Review + end
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Review(State, CanAdjustScore, CanCloseGame, CanSetViewer):
    """Post-game standings; the host reviews (and may fix scores) before
    closing the room for good."""

    def adjust_score(self, game: Game, player_id: PlayerId, value: int) -> Core:
        return game.apply_adjust(player_id, value, game.core)

    def close_game(self, game: Game) -> Core:
        return replace(game.core, state=GameOver())


@dataclass(frozen=True, slots=True)
class GameOver(State):
    """Terminal: the room is closed. Joins and every action are refused."""

    close_reason: str = "closed"  # empty | overtime | idle — why the room shut


# ---------------------------------------------------------------------------
# Pause
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Paused(State, CanResume):
    previous: State

    def resume(self, game: Game) -> Core:
        return replace(game.core, state=self.previous)
