# AGENTS.md — Jeffpardy

Online Jeffpardy-style buzzer game. FastAPI + WebSocket backend, React/Vite frontend.
Concurrency model, scale ceiling (~300–600 agg actions/sec, single event loop), and known
pain points are written up in `ARCHITECTURE_REVIEW.md` — read it before touching transport,
broadcast, or the action registries.

## Commands

```bash
# Backend tests (domain unit + full-game/shenanigan integration + REST/WS + trivia) — from repo root
.venv/bin/python -m pytest -q

# Frontend unit tests (vitest) — from frontend/
npm run test:unit

# E2E tests (Playwright; auto-starts backend :8123 and vite :5173) — from frontend/
# The last spec, "a full game runs from the green room to the final scores", is the
# wire-to-wire one: lobby -> Jeffpardy -> Bonus -> Ultimate wagering/clue/reveal ->
# final scores, three live contexts, every snapshot hop over the socket.
npm run test:e2e

# Dev servers
PYTHONPATH=backend/src .venv/bin/python -m uvicorn jeffpardy.main:app --host 127.0.0.1 --port 8123
cd frontend && npm run dev   # proxies /api and /ws to 8123

# Public deployment via tunnel (single origin on 8123)
npm run build                                  # produces frontend/dist, served by uvicorn at /
TUNNEL_URL=https://<public-url> node frontend/tunnel-check.mjs   # 2-player game + undo over the tunnel

# Cloudflare quick tunnel (preferred: no interstitial, no per-IP reminder page).
# cloudflared has no localtunnel-style visitor reminder, so no bypass header is needed.
# Use an absolute path if /tmp gets cleared; arm64 build shown, swap for amd64 as needed.
curl -sL -o /tmp/cf/cloudflared \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64
chmod +x /tmp/cf/cloudflared
nohup bash -c 'while true; do /tmp/cf/cloudflared tunnel --no-autoupdate \
  --url http://127.0.0.1:8123 --protocol http2 >> /tmp/cf/tunnel.log 2>&1; \
  echo "[supervisor] cloudflared exited $(date), respawning" >> /tmp/cf/tunnel.log; sleep 3; done' &
grep -Eo "https://[a-z0-9-]+\.trycloudflare\.com" /tmp/cf/tunnel.log | head -1   # current public URL

# localtunnel (shows a visitor interstitial; keep a subdomain)
npx -y localtunnel --port 8123 --subdomain <name>
```

## Architecture: domain-first, capability-mixin state machine

The design follows `ref.txt` (the project vision): game logic and interactions are separate, the
host has full control, and capability mixins make the legal-action surface self-describing.

- **`states.py` — what the game may do next (pure proposals).**
  - `State.available_host_actions` walks the MRO for `action = HostAction(name, label)` declared on
    capability base classes (`CanStartGame`, `CanPickClue`, `CanSelectBuzzer`, `CanMarkCorrect`, …).
    **Inheritance is the capability declaration — there is no transition table to keep in sync.**
  - Concrete states are frozen dataclasses (`Lobby`, `Intro`, `Board`, `Asking`, `BuzzersOpen`,
    `Answering`, `RoundIntermission`, `FinalCategory/Wagering/Clue/Reveal`, `Review`, `GameOver`).
    A state method receives `game`, threads the immutable `Core` through pure domain
    ops, and *returns* the next `Core`; only `Game` ever installs one.
  - `PausableState`/`Paused(previous=…)` wrap mid-flow states; `Paused` exposes only `resume`.
  - Flow: Lobby → Intro → Board ↔ (Asking → BuzzersOpen → Answering) → RoundIntermission →
    Bonus Jeffpardy board → FinalCategory → FinalWagering → FinalClue → FinalReveal → Review → GameOver.
- **`game.py` — the environment around an immutable `Core`.** All game facts (phase, round,
  score ledger keyed by identity, buzz queue, wagers, quest, picker, per-clue bookkeeping) live
  in one frozen record, `Core`; `Game` holds only environment: roster (`_seated`), identity
  registry (`_identities`), host, viewers, sockets, `rev`, `race_token`. Host actions run through
  `_host_run` → state method returns a fresh `Core` → `_commit` appends the *previous* Core to
  history and installs the new one — that history stack **is** the undo button (`can_undo`,
  `perform("undo")` = `self._core = history.pop()`, a pointer rebind; events landed after the
  checkpoint are rewound with everything else). Player events install via `_event` (no history);
  env ops (join/leave/promote/bench) never touch the Core. `game.state`/`players`/`wagers`/…
  are read-only views onto `_core` — never assign them.
  - `Game.perform(sender, action, /, **payload)` is the generic host entry: checks the sender is the
    host, runs the room's `HostPolicy.authorize` middleware (`hosting.py`: `MANUAL`
    passes everything through, `ASSISTED` locks delegated/auto steps, `AUTO` is still a
    reserved name refused until a policy registers), checks the action name is in
    `available_host_actions` (which is `policy.advertise(state.available_host_actions)`,
    so the UI can never offer what the mode refuses), dispatches by name.
    `sender`/`action` are positional-only so hostile payload keys named `sender`/`action` fall into
    `**payload` and die as `invalid arguments` instead of colliding at the call site.
    Unknown vs unavailable vs bad-kwargs produce distinct `InvalidAction` messages (tests assert on them).
    `Game.host_mode` (`HostMode.MANUAL`, default) is environment like host/viewers — never
    rewound by undo; the `set_host_mode` route in `perform` (beside `undo`) checkpoints like
    a host action and settles the assist chain when flipping to assisted. A custom policy
    plugs in via `register_host_policy` without touching `perform` (test asserts this).
    `ASSISTED` delegates board control to the picker via the `pick` player event (same
    transition as `pick_clue`, picker-rule enforced) and auto-advances mechanical steps
    through `policy.auto_step` + `Game._settle_assist` (plain flips open the race, locked
    quest risks open the answer, dead clues close themselves, the finals chain marches once
    every seat has moved). Delegated/auto actions lock via `authorize` + `advertise`, so the
    host API refuses and the host UI hides exactly what would race the chain; pause holds
    the chain and grading/seating/pause/undo/mode-switches stay manual. Each auto step is a
    full host action (own checkpoint + rev bump): undo lands on the in-between with no
    replay, and `set_host_mode` is what the `host-mode-*` toggle calls. Future `AUTO`
    needs an async driver *outside* `GameManager.run` calling `perform` with host
    authority (mutations stay synchronous).
  - **Round enum is MAIN/BONUS/ULTIMATE** with labels "Jeffpardy"/"Bonus Jeffpardy"/"Ultimate
    Jeffpardy" (trademark rule: the bare word "Jeopardy" appears nowhere in code or copy).
  - **Gamely Jeff Quest** (the jackpot clue): each board round arms one special clue — the
    board's last position (`Game.arm_quest`), **host truth**: the gold chip (`data-quest`)
    marks the square for the host alone — `mask_snapshot` scrubs `activeQuest` from every other
    viewer, so the room finds the jackpot by playing rather than reading the board.
    **The picker owns it**: `game.picker` is a Core field (`None` allowed, rewinds with undo) meaning "who's on the clock"; the Board-only host action `set_picker` and the
    automatic "correct answer picks next" rule share one hook, `Game.assign_picker`. Flipping
    the marked card goes straight to `QuestWager` with `player_id = picker` — no buzz race —
    and flipping with nobody on the clock errors ("set a picker first"), so the host's free
    reign is: set the picker, flip. The claimant wagers alone (capped at their score; broke
    players match the table), the host runs `begin_quest_answer` → `QuestAnswering`, graded on
    **the wager**: a hit spends the clue and puts the holder on the clock; a miss spends it
    for everyone (picker unchanged). Leaving/benching clears the picker (bench/leave are env
    ops that clear it with a Core write but **no** history checkpoint). `active_quest`,
    `quest_wagers`, and `picker` all live in the Core; no quest during Ultimate.
  - **View-only + host promotion**: `set_viewer` (capability `CanSetViewer`, rides every pausable
    state and Review; host is never a valid target) and lobby-only `promote_host`
    (`CanPromoteHost`, old host keeps their seat). Both are **environment** on `Game` — undo
    never touches them (promote/bench are not game facts). Viewers can't buzz/wager/answer/select.
  - **Undo rewinds the whole record, roster excluded**: the popped Core restores phase, scores,
    queue, wagers and bookkeeping wholesale — buzzes that landed after the checkpoint are rewound
    too. Membership/host/viewers/`rev`/`race_token` are environment or transport and never rewind.
    Scores follow identity: `leave` touches only the roster, so a rejoin (same cookie) keeps the
    ledger, and departed identities stay gradeable (queue entries, quest holders).
  - A failed action is atomic *by construction*: Cores are immutable, so a raising method simply
    never returns one — the old object stays installed, no history checkpoint, `rev` still advances.
- **Host = first joiner, and does not play**: `Game.join` sets `host` on the first arrival (not
  added to `players`). Rejoin with the same name (case-insensitive) is idempotent. The host may not
  buzz/select themselves, may not leave ("end the game instead"), and only the host may `perform`.
  There is no client-side host toggle anymore — the server decides from the cookie/WS name.
- **Player events are data-only** (no transitions) — except `pick`: `buzz`, `leave`, `wager`,
  `quest_wager`, `final_answer`, guarded by state (pause blocks them; `buzz` only in
  `buzzers_open`, and a wrong ruling locks that contender out of the clue for the rest of the
  race so the queue only ever lists someone the host can still call on; the active answerer may
  not leave; wagers ≤ max(score, 0)). `pick`
  (assisted-only, Board-only, clock-holder-or-open-clock) is the one event that runs a
  transition — it calls through to the same `pick_clue` domain path and settles the assist
  chain, so the plain flip auto-opens the race.
- **One action registry**: player actions live in one table — `models.PLAYER_EVENTS`
  (name → Game method + payload keys it reads). The schema `Literal` and `main._apply` dispatch
  both derive from it; `test_player_action_schema_and_dispatch_share_one_table` is the drift
  tripwire. Add a player event by editing that table (plus the Game method) — never the Literal.
- **Mutations are synchronous — there is no lock**: `GameManager.run` rejects anything awaitable
  (TypeError) and the single event loop serializes whole mutations. Domain rule: no await/network/
  disk inside mutation logic. If serialization ever becomes needed, go per-room — never global.
- **`rev` (transport clock) + `buzzToken` (race epoch)**: `game.rev` increments on every commit —
  including refused actions (the old Core stays installed; the clock still advances), and
  **undo publishes the rewound state as *newer*** (the
  snapshot-equality tests pop `rev` (+ env fields) before comparing domain content). `race_token`
  is minted on `open_buzzers`, re-open after a miss, and undo-into-`buzzers_open`; it never rides `Core`
  and a failed action never rotates it. `buzz` carries `{clue_id, buzz_token}` (checked when
  provided; the frontend always sends both). Clients drop only *strictly older* states
  (`applyRemoteState` in `useRoom.js`; equal rev = same action delivered twice → apply). **No
  rev-gating of writes**: `adjust_score` is a delta (`players[key] += value`), so stale-render
  double-clicks are safe by construction — the first true absolute value-write must gate on `rev`.
- **`main.py` — interactions only**: generic routers `POST /api/rooms/{code}/host` (body
  `{action, ...payload}`, sender = cookie) and `POST …/player` (whitelist, sender = cookie, name
  never taken from the body). WS mirrors them: `{"type":"host"|"player", "action", ...payload}`.
  Adding a host capability requires **zero** transport or schema changes.
- **Snapshots are host-truth + per-viewer masks**: `host_snapshot(game)` builds the full room view once per publish (every clue answer, every Final secret, host controls, `hostMode`); `mask_snapshot(game, base, viewer)` redacts it per identity: the clue answer stays hidden until the host explicitly reveals it (no "the room is answering" exemption � a contender reading the card, or the room waiting to re-buzz after a miss, gets no free look), only *own* Final wagers/answers until the reveal. `snapshot(game, viewer)` is truth-or-mask by role; `snapshots_for(game, viewers)` batches one publish (host truth + one mask per distinct identity; spectators share the anonymous mask: viewers hold no secrets of their own, so the player filter covers them with no separate path). Same shape for everyone, redacted values only; host controls ride through unmasked (capability metadata, never secrets: `perform` authenticates, the UI gates on host identity). `main.broadcast` builds once and masks per identity instead of rebuilding the board per socket.
- **Scoring/rounds**: two boards (`boards[Round]`, clue ids prefixed `j-`/`dj-`), per-clue progress
  lives on `game.clue_status` (not on the Clue objects) so the Core rewinds it wholesale. Skipping a
  clue with no score returns it to the board; a scored clue is spent on `close_clue`.
  `trivia.fetch_boards(use_fallback)` deals both boards + the Final clue (3 parallel OpenTDB calls;
  offline fallback copies + `FINAL_FALLBACK`).
- **Room codes are strictly 4 digits** (`game.ROOM_CODE_RE`). Identity = `jeffpardy_name` cookie,
  no login (`frontend/src/lib/identity.js` mirrors it; `/api/me` reads it).
- **Gameplay actions ride the WebSocket**; REST is for page-load lifecycle (create/join/get/me),
  the symmetric host/player routers (used by integration tests), and error reporting. Reason:
  tunnels multiplex WS+HTTP unreliably — new HTTP POSTs while WebSockets are live get 502'd.
- **UI is server-driven**: snapshots carry `availableHostActions` + `canUndo` + `hostMode`; `HostControls`
  renders `{action}` buttons generically as `data-testid="host-{action}"` (contextual actions —
  pick/select/grade/correct/adjust — render next to their context instead). `RoomPage` switches
  screens on `state`/`pausedIn` (lobby, intro, board plays, final stages, review, game over, pause
  overlay). Zero client-side flow knowledge beyond screen mapping. In assisted mode the tiles
  belong to the picker (`Board` takes `canPlayerPick`, taps send the `pick` player event; the
  `pick-hint` badge names whose tap the server will accept), the host flips posture with the
  `host-mode-*` toggle (`set_host_mode`), and the `assist-note` explains the split (machine runs
  mechanics, host grades and holds rest) — hidden/auto actions never render because the policy's
  `advertise` already removed them.
- When `frontend/dist` exists, uvicorn mounts it at `/` → app + API + WS on one origin (8123).
  Rebuild for tunnel users to see changes.
- `frontend/src/lib/api.js` retries GETs and `/join` on 502/503/504 (8s abort); host/player actions
  are never retried (a lost response must not double-apply points or push extra undo snapshots).
- **localtunnel visitor interstitial**: browsers see it once per IP / 7 days; bypass with header
  `bypass-tunnel-reminder: <any value>`.

## Pitfalls (cost real debugging time — do not regress)

1. **Stale server on port 8123**: Playwright `webServer` has `reuseExistingServer: true` — an old
   uvicorn silently serves outdated code. Before e2e: `ps aux | grep uvicorn`, kill stale PIDs,
   `curl :8123/api/me` → 200. Also: bare uvicorn needs `PYTHONPATH=backend/src` or it dies with
   `ModuleNotFoundError: No module named 'jeffpardy'`.
2. **Never animate `transform` on clickable elements**: Playwright clicks fail with "element is not
   stable". The buzzer pulse uses `box-shadow` (`@keyframes pulse`).
3. Two vite instances fight over 5173 (the second falls back to 5174, breaking the proxy). Keep one.
4. **localtunnel dies silently (twice in one day)**: client exits with no error; public URL returns
   `503 - Tunnel Unavailable` while local `:8123` stays healthy. **Always run it under a supervisor**
   (respawns on exit) rather than bare `nohup`:
   `nohup bash -c 'while true; do npx localtunnel --port 8123 --subdomain <name> >> /tmp/jeffpardy-tunnel.log 2>&1; sleep 3; done' &`
   The log records `[supervisor]` respawns and the latest `your url is:` line. Health probe:
   `curl -H "bypass-tunnel-reminder: 1" https://<name>.loca.lt/api/health`.
5. **`useRoom` socket handlers guard `wsRef` ownership** — StrictMode double-mounts effects; stale
   `onclose` used to null the live ref. Keep the `wsRef.current !== ws` guards.
6. **`join` must broadcast** — only a late joiner (host already connected) exercises roster sync.
   Regression: `test_late_join_broadcast_reaches_connected_players` + tunnel check (host first,
   guest later). Keep `await broadcast(code)` in the join endpoint.
7. **Duplicate `data-testid`s break Playwright strict mode** (two elements resolve): every testid
   must be unique per screen — e.g. the modal uses `modal-answerer`, the panel `answerer-name`.
8. **Big heredoc writes can silently no-op or garble** in this environment: after writing a large
   file with `cat > … <<'EOF'`, always verify (`grep` a marker / `py_compile`) before moving on;
   if a test file ends mid-function, tests hang rather than fail (a WS `receive_json` with no
   partner blocks forever — prefer asserting server refusals before waiting on broadcasts).
9. **State-machine refactors must keep tests green as one unit**: pytest + vitest + e2e.
   A count drop usually means a skipped test or the stale-server issue in #1.
10. **Assisted auto-steps never take a payload** — each is a full host action (own checkpoint +
    rev bump), so the pick-hint badge is the tap contract and undo lands on the in-between
    with no replay. The host tiles and the picker tiles are mutually exclusive by
    construction: `advertise` hides `pick_clue` from the host exactly when `Game.pick`
    would accept the tap (assisted Board with the clock held or open); never gate one side
    on the other in the UI.
11. **`close_clue` is *not* a no-op after everyone has answered** — it spends the clue
    (`retire_clue` → `answered`) instead of returning it to the board. Only an *unanswered*
    clue goes back (which is why a whole board can't be cleared by closing alone: the same
    tile is live again). Never hide that button on `dead_clue`/`everyone_answered`; those
    describe "could anyone still take it", which is the shared-hosting auto-skip's question,
    not the host's.
12. **Don't `include_screenshot: true` on a large conversation** — the base64 image lands in
    history and can blow the request past the model's limit, so the run dies with
    `LLMBadRequestError: Error code: 400` right after the screenshot (looks like the
    screenshot crashed the agent). Assert on DOM/text (`data-testid`) instead; if pixels are
    truly needed, use `inspect_image_with_vision`.
13. **`finish_round` needs no cleared board** — a host can wrap a round with clues still on
    it; the quest card included. E2E tests should play a couple of clues and advance, not
    try to mop a whole board up.
14. **A player who is still marked gone must *act* to come back** — `Game.join` refuses a
    plain rejoin while `has_left(key)` (it would sit them out with no path back); the grace
    is cleared by a game move or heartbeat. E2E must have every seat in the lobby *before*
    `start_game`, or the late joiner never lands on the roster.

