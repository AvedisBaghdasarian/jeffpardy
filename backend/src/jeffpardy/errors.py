"""Jeffpardy error types.

RoomError   — the room itself is the problem (not found).
InvalidAction — the action is illegal for this actor or this state.
"""

from __future__ import annotations


class RoomError(Exception):
    """Room-level failure: unknown 4-digit code."""


class InvalidAction(RuntimeError):
    """Action refused by the state machine or by actor rules (host/player)."""
