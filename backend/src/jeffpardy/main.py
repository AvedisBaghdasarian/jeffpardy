"""FastAPI transport: generic host/player routers + WebSocket live channel.

All game rules live in the domain (``game.py`` / ``states.py``). This module
only:

* authenticates the sender (cookie for REST, ``?name=`` for WS) and forwards
  actions by name — a new host capability needs *zero* transport changes;
* fans out viewer-aware snapshots (the host always sees answers; players see
  them per the reveal rules) to every socket in the room.

State lives per-app so each test/client gets isolated registries.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path

from fastapi import Cookie, FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .errors import InvalidAction, RoomError
from .game import FinalData, GameManager, build_board, normalize_code, snapshot, snapshots_for
from .models import (
    PLAYER_EVENTS,
    CreateRoomRequest,
    HostMessage,
    HostRequest,
    JoinRequest,
    PlayerEventRequest,
    parse_ws_message,
)
from .states import Round
from .trivia import fetch_boards, fetch_categories

# Repo layout: backend/src/jeffpardy/main.py -> repo root is parents[3]
FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"


def create_app() -> FastAPI:
    app = FastAPI(title="Jeffpardy")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    rooms = GameManager()
    sockets: dict[str, dict[WebSocket, str]] = defaultdict(dict)
    sockets_lock = asyncio.Lock()

    async def broadcast(code: str) -> None:
        code = normalize_code(code)
        try:
            game = await rooms.get(code)
        except RoomError:
            return
        async with sockets_lock:
            targets = list(sockets.get(code, {}).items())
        # One host-truth build, one mask per identity — not one rebuild per socket.
        views = snapshots_for(game, [viewer for _, viewer in targets])
        dead: list[WebSocket] = []
        for ws, viewer in targets:
            try:
                await ws.send_json({"type": "state", "state": views[viewer]})
            except Exception:
                dead.append(ws)
        if dead:
            async with sockets_lock:
                for ws in dead:
                    sockets[code].pop(ws, None)

    async def sweep_loop() -> None:
        """Background reaper: every minute, close out rooms whose time is up
        (nobody left, the two-hour show clock, twenty quiet minutes) and tell
        anyone still watching. Runs on the loop — no threads, no locks."""
        import asyncio as _asyncio

        while True:
            await _asyncio.sleep(60)
            try:
                culled = await rooms.sweep()
            except Exception:
                continue
            for code in culled:
                await broadcast(code)
            for code in culled:
                async with sockets_lock:
                    for ws in list(sockets.get(code, {})):
                        try:
                            await ws.close(code=4404)
                        except Exception:
                            pass
                        sockets[code].pop(ws, None)

    @app.on_event("startup")
    async def _start_sweeper() -> None:
        import asyncio as _asyncio

        app.state.sweeper = _asyncio.create_task(sweep_loop())

    async def run_host(code: str, sender: str | None, action: str, payload: dict):
        game = await rooms.run(code, lambda g: g.perform(sender, action, **payload))
        game.touch(name=sender)
        return game

    async def run_player(code: str, sender: str | None, action: str, payload: dict):
        return await rooms.run(code, lambda g: _apply(g, sender, action, payload))

    def _apply(game, sender: str | None, action: str, payload: dict) -> None:
        """Whitelisted player events; the acting name is always the sender.

        The whitelist *is* ``PLAYER_EVENTS`` — one table owns schema and
        dispatch, so they cannot drift. ``kick`` is the one exception: it
        rides the player channel but runs on host authority — only the host's
        ``sender`` may remove a seat.
        """
        if not sender:
            raise InvalidAction("take a seat in the game first")
        spec = PLAYER_EVENTS.get(action)
        if spec is None:
            raise InvalidAction("that's not a move in this game")
        if action == "kick":
            if not game.host or sender.strip().lower() != game.host.lower():
                raise InvalidAction("only the host runs the show")
            game.touch(name=sender)
            game.kick(payload.get("player_id"))
            return
        method_name, keys = spec
        result = getattr(game, method_name)(sender, *(payload.get(k) for k in keys))
        # Heartbeats keep presence alive without publishing a new revision.
        if action != "heartbeat":
            game.touch(name=sender)
        return result

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok", "game": "jeffpardy"}

    @app.get("/api/categories")
    async def categories() -> dict:
        return {"categories": await fetch_categories()}

    @app.post("/api/rooms")
    async def create_room(payload: CreateRoomRequest | None = None) -> dict:
        use_fallback = bool(payload and payload.use_fallback)
        first, second, final = await fetch_boards(use_fallback=use_fallback)
        game = await rooms.create(
            boards={
                Round.MAIN: build_board(first, prefix="j-"),
                Round.BONUS: build_board(second, prefix="dj-"),
            },
            final=FinalData(*final),
        )
        return snapshot(game)

    @app.get("/api/me")
    async def me(jeffpardy_name: str | None = Cookie(default=None)) -> dict:
        return {"name": jeffpardy_name}

    @app.get("/api/rooms/{code}")
    async def get_room(code: str, jeffpardy_name: str | None = Cookie(default=None)) -> dict:
        try:
            game = await rooms.get(code)
        except RoomError as exc:
            return _error(str(exc), 404)
        return snapshot(game, jeffpardy_name)

    @app.post("/api/rooms/{code}/join")
    async def join_room(
        code: str,
        payload: JoinRequest,
        jeffpardy_name: str | None = Cookie(default=None),
    ) -> JSONResponse:
        try:
            game = await rooms.run(code, lambda g: g.join(payload.name))
        except RoomError as exc:
            return _error(str(exc), 404)
        except InvalidAction as exc:
            return _error(str(exc), 400)
        chosen = payload.name.strip()
        response = JSONResponse(snapshot(game, chosen))
        existing = (jeffpardy_name or "").strip()
        if not existing or existing.lower() != chosen.lower():
            response.set_cookie("jeffpardy_name", chosen, max_age=60 * 60 * 24 * 365)
        # Players already at the table must see the roster change live.
        await broadcast(game.code)
        return response

    @app.post("/api/rooms/{code}/host")
    async def host_action(
        code: str,
        payload: HostRequest,
        jeffpardy_name: str | None = Cookie(default=None),
    ) -> JSONResponse:
        try:
            game = await run_host(code, jeffpardy_name, payload.action, payload.payload())
        except RoomError as exc:
            return _error(str(exc), 404)
        except InvalidAction as exc:
            return _error(str(exc), 400)
        await broadcast(game.code)
        return JSONResponse(snapshot(game, jeffpardy_name))

    @app.post("/api/rooms/{code}/player")
    async def player_event(
        code: str,
        payload: PlayerEventRequest,
        jeffpardy_name: str | None = Cookie(default=None),
    ) -> JSONResponse:
        try:
            game = await run_player(code, jeffpardy_name, payload.action, payload.payload())
        except RoomError as exc:
            return _error(str(exc), 404)
        except InvalidAction as exc:
            return _error(str(exc), 400)
        await broadcast(game.code)
        return JSONResponse(snapshot(game, jeffpardy_name))

    @app.websocket("/ws/rooms/{code}")
    async def ws_room(ws: WebSocket, code: str, name: str = Query(default="")) -> None:
        code = normalize_code(code)
        sender = name.strip()
        try:
            game = await rooms.get(code)
        except RoomError:
            await ws.close(code=4404)
            return
        await ws.accept()
        async with sockets_lock:
            sockets[code][ws] = sender
        # Reconnecting lands them back inside the grace: forgiven, not re-seated.
        try:
            await rooms.run(code, lambda g: g.heartbeat(sender))
        except (InvalidAction, RoomError):
            pass
        try:
            game = await rooms.get(code)
        except RoomError:
            await ws.close(code=4404)
            return
        await ws.send_json({"type": "state", "state": snapshot(game, sender)})
        try:
            while True:
                raw = await ws.receive_json()
                applied = False
                try:
                    msg = parse_ws_message(raw)
                    if isinstance(msg, HostMessage):
                        game = await run_host(code, sender, msg.action, msg.payload())
                    else:
                        game = await run_player(code, sender, msg.action, msg.payload())
                    applied = True
                except (InvalidAction, RoomError, ValueError) as exc:
                    detail = str(exc) or "invalid message"
                    await ws.send_json({"type": "error", "message": detail})
                # Failed actions change nothing; skipping the broadcast keeps
                # the sender's error visible instead of clearing it with state.
                if applied:
                    await broadcast(code)
        except WebSocketDisconnect:
            pass
        finally:
            async with sockets_lock:
                sockets.get(code, {}).pop(ws, None)

    # Serve the built frontend (npm run build) when present, so the whole
    # app is one origin: /api, /ws, and static assets all on this server.
    if FRONTEND_DIST.is_dir():
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")

    return app


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": message})


app = create_app()
