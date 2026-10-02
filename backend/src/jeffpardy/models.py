"""Pydantic schemas: the interaction envelopes for REST and WebSocket.

Both transports accept the same generic shapes — ``{action, ...payload}`` for
the host and ``{action, ...payload}`` for players — so adding a new host
capability requires zero schema changes (extra fields pass through).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Single source of truth for player actions: (Game method, payload keys it
# reads). The schema Literal below and the dispatch table in main.py both
# derive from this — an action can no longer exist in one and not the rest.
PLAYER_EVENTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "buzz": ("buzz", ("clue_id", "buzz_token")),
    "leave": ("leave", ()),
    "kick": ("kick", ("player_id",)),
    "wager": ("wager", ("amount",)),
    "quest_wager": ("quest_wager", ("amount",)),
    "final_answer": ("final_answer", ("text",)),
    "pick": ("pick", ("clue_id",)),
    "heartbeat": ("heartbeat", ()),
}

PlayerAction = Literal[*tuple(PLAYER_EVENTS)]


class CreateRoomRequest(BaseModel):
    use_fallback: bool = False


class JoinRequest(BaseModel):
    name: str = Field(min_length=1, max_length=24)


class HostRequest(BaseModel):
    """POST /api/rooms/{code}/host — flat: {"action": "pick_clue", ...}."""

    model_config = ConfigDict(extra="allow")

    action: str

    def payload(self) -> dict:
        return dict(self.model_extra or {})


class PlayerEventRequest(BaseModel):
    """POST /api/rooms/{code}/player — flat: {"action": "buzz", ...}.

    The acting name is *always* taken from the session cookie, never from the
    body, so a client can't buzz/leave/wager as someone else.
    """

    model_config = ConfigDict(extra="allow")

    action: PlayerAction

    def payload(self) -> dict:
        return dict(self.model_extra or {})


class HostMessage(BaseModel):
    """WS: {"type": "host", "action": ..., ...payload}."""

    model_config = ConfigDict(extra="allow")

    type: Literal["host"]
    action: str

    def payload(self) -> dict:
        return dict(self.model_extra or {})


class PlayerMessage(BaseModel):
    """WS: {"type": "player", "action": ..., ...payload}."""

    model_config = ConfigDict(extra="allow")

    type: Literal["player"]
    action: PlayerAction

    def payload(self) -> dict:
        return dict(self.model_extra or {})


def parse_ws_message(raw: object) -> HostMessage | PlayerMessage:
    if not isinstance(raw, dict):
        raise ValueError("message must be an object")
    kind = raw.get("type")
    if kind == "host":
        return HostMessage.model_validate(raw)
    if kind == "player":
        return PlayerMessage.model_validate(raw)
    raise ValueError("message type must be 'host' or 'player'")
