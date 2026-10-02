# Jeffpardy — Architecture Review

*Point-in-time review written after the Gamely Jeff Quest work. Every claim was grounded in
the code at that revision — file:line references included so it can be checked, not trusted.*

*Revised through two follow-up rounds — **concurrency v2** (single action registry, lock
removal, revision clock + race epochs) and the **Core refactor** (immutable game record,
environment split, snapshot machinery deleted). §1–2 and §4–5 describe the system as it
stands; §3 preserves each original finding, tagged **RESOLVED / PARTIAL / OPEN**, with what
was done and why. Where a finding's prose and its status disagree, the status is
authoritative. Current suite: **205 pytest / 16 vitest / 5 e2e**, all green.*

## TL;DR

| Question | Answer |
|---|---|
| Multiprocessing or multithreading? | **Neither.** One uvicorn process, one asyncio event loop, all handlers `async def`. |
| Enough for ~1000 simultaneous games? | Idle: trivially yes. Busy (action storms): **on the edge** — broadcast fan-out saturates at roughly **300–600 aggregate actions/sec across all rooms** (estimate, §2 — still unbenchmarked). |
| Worst correctness bug hiding in plain sight? | **Was** out-of-order broadcasts: a slow client could sit on a stale state (§3.3b). **Resolved** — monotonic `rev` + client-side guard. The remaining §3.3 concern is *CPU* (per-viewer rebuilds), not correctness. |
| Biggest maintainability tax? | **Was** player actions registered in three drifting places (§3.1). **Resolved** — one registry derives schema and dispatch. Next up: host-action payload contracts (§3.2) and the UI's string-mirrored state machine (§3.6). |
| Biggest scaling blockers? | In-memory rooms → **can't add worker processes**; rooms never evicted (§3.4, §4). Undo history is now capped (`MAX_UNDO = 200`). |
| Work status | Done: registry, rev guard, lock removal, history cap, Core refactor. Next: broadcast grouping, idle-room eviction, benchmark §2 (§6). |

---

## 1. How it runs today

```text
One uvicorn process  (AGENTS.md — run command has no --workers)
└── One asyncio event loop — every endpoint is `async def`, so nothing ever
    leaves this thread (FastAPI only offloads plain `def` endpoints to a
    threadpool; we have none)
    │
    ├── GameManager (game.py:952)   NO lock — mutations are synchronous by
    │                               contract: run() (game.py:987) rejects
    │                               awaitables with TypeError, so no handler
    │                               can ever park a room mid-mutation.
    │                               A commit = append previous Core, rebind
    │                               self._core, bump rev. µs of pointer and
    │                               dict work; the loop itself serializes.
    │
    ├── sockets_lock (main.py:56)   ONE global lock for the socket registry
    │                               — held only to copy/prune dicts
    │
    └── broadcast() (main.py:58)    runs AFTER the commit, outside any lock,
                                    on the same loop: per-socket snapshot
                                    build + JSON encode + `await send_json`,
                                    serially. Every publish carries `rev`.
```

### The request path, with a real example — *Bob buzzes*

```text
POST /api/rooms/ABCD/player   {"action":"buzz"}     (async def — stays on the loop)
  └─ rooms.run(code, fn)                             no lock to acquire
       └─ _apply() registry dispatch → game.buzz("Bob")
            sync: validate → build fresh Core → rebind pointer → rev++  (~µs)
  └─ await broadcast(code)                           after the commit — good
       └─ for each socket: snapshot(game, viewer)    ~50–200µs PER VIEWER, then
          + ws.send_json(...)                        JSON + TCP, sequentially
```

**Not present anywhere:** `threading`, `multiprocessing`, `to_thread`,
`--workers`. The only I/O is `httpx.AsyncClient` + `asyncio.gather` in
`trivia.py` — genuinely non-blocking ✓.

### Why this shape is a *good* starting point

- The game domain is pure sync code → **205 tests run in ~1.5s with zero
  mocking**, no event-loop gymnastics in tests.
- Capability-driven UI: buttons render from `availableHostActions`, so
  `has("pick_clue")` can't lie to the frontend.
- **Atomicity by construction**: the original review credited "snapshot-before-
  action, restore-on-exception." The Core refactor kept the guarantee and
  deleted the machinery — `Core` is immutable, so a raising method never
  returns one; the previous object stays installed with no history checkpoint.
  There is no restore path left to get wrong
  (`test_failed_host_action_leaves_the_core_object_untouched`).
- **The invariant is enforced, not documented.** The review flagged the global
  lock as a trap (§3.5); the resolution removed it and made the underlying rule
  checkable — `GameManager.run` rejects awaitables outright. A future `await`
  inside a mutation is a TypeError at the call site, not a silent latency cliff
  for all 1000 rooms.

---

## 2. Will it hold 1000 simultaneous games?

Back-of-envelope numbers — treat as order-of-magnitude, not benchmarks.
**These remain estimates: no load harness exists. Benchmark before trusting
them or optimizing for them (§6).**

| Load axis | Per-unit cost | At 1000 games |
|---|---|---|
| Room exists (idle) | memory only | Fine — until §3.4 (eviction) |
| Player/host **action** | ~µs mutation + Core rebind, no lock | 1000 act/s ≈ **well under 5% of one loop** — fine |
| **Broadcast fan-out** | per action: M × (snapshot 50–200µs + JSON + send), serial | **The ceiling.** 6–10 players → ~0.6–3ms loop-work per action → saturation around **300–600 agg actions/sec** (unbenchmarked estimate) |
| **Client state render** | full-state payload ~5–15KB, small React tree, no deltas | Negligible — nobody waits on a *render*; the wait they'd feel is the **loop queueing** behind other rooms' serialization |

**Worked scenario:** 1000 rooms × avg 6 players, each doing 1 action/sec, with a
3× buzz-storm during reveals. Steady state ≈ 600 act/s → ~0.6–1.1s of loop work
per second → already at the edge. The storm pushes it over: every room's
latency rises, not just the busy ones'. Classic **noisy neighbor**: all rooms
share one loop's fate.

**Answer to the literal question:** the router is *not* doing multiprocessing or
multithreading, and single-loop asyncio is enough at human pacing — but it is
**not** enough headroom for 1000 *busy* games. Broadcast serialization (not the
lock — which is gone — not React renders) is what saturates first.

---

## 3. Pain points, ranked — with examples

*Findings as written on the review date; each closes with its outcome.*

### 3.1 Player actions are registered in three places that can drift — **RESOLVED**

*(as found)* The same action had to be listed in **three independent structures**:

```python
# 1) models.py:14 — the schema gate (rejects before dispatch)
PlayerAction = Literal["buzz", "leave", "wager", "final_answer", "quest_wager"]

# 2) main.py:_apply — a hand-written if-chain (dispatch whitelist)
elif action == "quest_wager":
    game.quest_wager(sender, payload.get("amount"))

# 3) game.py — a hand-written wrapper per action (tests + perform ergonomics)
def set_picker(self, player_id: str | None) -> None:
    self._host_run(self._require(CanSetPicker).set_picker, player_id)
```

**This wasn't theoretical.** When the quest wager shipped:

1. Domain + pytest were written first → **178/178 green**.
2. The transport layers were separate edits and lagged behind.
3. First e2e run failed: `1 validation error for PlayerMessage action Input
   should be 'buzz', 'leave', 'wager' or 'final_answer'` — the Literal said no.
4. Fixed the Literal → e2e failed *again* — `_apply` still said no.

Two failures, two fixes, one feature — because one concept lived in three files.
Domain tests structurally cannot catch this: they call `game.quest_wager()`
directly and bypass transport.

**Outcome (concurrency v2):** `models.PLAYER_EVENTS` is the single table —
name → Game method + payload keys it reads. The schema `Literal` and
`main._apply` dispatch both derive from it, and
`test_player_action_schema_and_dispatch_share_one_table` fails the build if
they ever disagree. Adding a player event is one row plus the method; drift is
unrepresentable. (Host actions keep their two-layer shape — capability mixin
+ wrapper — but are gated by capability discovery, where inheritance *is* the
declaration and cannot drift.)

---

### 3.2 Parameterized host actions have no payload contract — **OPEN**

`HostRequest`/`PlayerEventRequest` are `extra="allow"` — **zero schema**. The
frontend must know payload key names by folklore:

```jsx
// RoomPage.jsx — hand-wired because there's no generic contract
onClick={() => hostAct("set_picker", { player_id: p })}
```

If the backend renames `player_id` → `picker_id`, nothing fails at build time.
It surfaces at runtime as `invalid arguments` (the domain's kwargs check — a
good error, but late). Worse: every new parameterized action needs bespoke UI
(`set_picker` needed its own buttons + a `CONTEXTUAL` entry + a RoomPage
branch), where a parameterless action renders itself for free from server
metadata.

**Same function, no loss:** emit parameter metadata inside
`availableHostActions` (name, label, param, allowed values — players/null are
*already known to the server*) and let HostControls render generic pickers.
`set_picker` would have been ~zero frontend code.

---

### 3.3 Broadcast: O(players) work per action, and no ordering guarantee — **PARTIAL**

Two separate issues were found in the broadcast path:

**(a) Rebuild per viewer, every time — OPEN.** `snapshot(game, viewer)` is
viewer-aware (host sees answers; the answerer sees theirs) — so a 10-player
room pays **10 snapshot builds + 10 JSON encodes per action**. Realistically
there are ≤3 distinct payload variants (host / answerer / bystander) per state
— group by role and build 3, not M. This is §2's CPU ceiling; per plan it is
deferred until a benchmark says it matters.

**(b) Concurrent broadcasts could deliver stale state — RESOLVED.** The
original failure mode, for context:

```text
t0  Action A → broadcast1: builds state(A), sends to Alice ✓,
    now awaiting the send to Bob (slow laptop, TCP backpressure)
t1  Action B → broadcast2: builds state(B), sends state(B) to Alice AND Bob ✓
t2  broadcast1's queued frame finally flushes to Bob → Bob's screen now
    shows state(A)  ← stale state, delivered ON TOP of the newer one
t3  Nothing else happens. Bob sits on state(A) until the next action in
    this room — which could be minutes away.
```

The prescription was "serialize per-room broadcasts." What shipped instead was
a **revision clock at the consumption edge**: every snapshot carries a
monotonic `rev` (bumped on every commit, every refusal, and every undo — the
clock never rewinds), and the client's `applyRemoteState` drops strictly older
states (equal revs apply — REST and WS can deliver the *same* state twice).
Context for choosing this over transport serialization: ordering can be lost on
*any* delivery path — WS frames, REST responses racing broadcasts, a tab
hibernating through two actions — and one guard at the apply-site covers all of
them, while a send-queue covers only WS interleaving. It also absorbed the
second stale-state bug: buzzes racing across clue/race epochs, handled by
`buzz` carrying `{clue_id, buzz_token}` (epochs only where the phase actually
repeats; buzz/wager deliberately do *not* gate on `rev` — races are fought on
identical base state by design).

*Blast radius note: the bug was one room, but any slow client could trigger it,
and the symptom ("UI randomly out of date until someone else moves") was near-
impossible to reproduce on a fast localhost run — which is why a durable guard,
not a narrowed race window, was the fix.*

---

### 3.4 Memory grows monotonically — **PARTIAL**

1. **Rooms are never evicted — OPEN.** `GameManager._games` has no TTL, no
   `del` — after everyone leaves, the `Game` stays forever. A server that
   serves 1000 rooms/day for a week holds 7000 rooms. This also answers the
   "host vanishes" question from the follow-up discussion: a room whose host
   disconnected can't be run by anyone else (promotion is lobby-only), so
   eviction is the agreed cleanup for both abandoned and orphaned rooms.

2. **Undo history — RESOLVED, with two corrections.** The original finding
   said "unbounded, a full `_Snapshot` deep-copy per action, 1–3GB at 1000
   rooms." Reality check one: the cap already existed (`MAX_UNDO = 200`, a
   ring) — the finding overstated it. Reality check two (Core refactor): the
   history stack now holds the *previous immutable `Core`* — no deepcopy, no
   restore copy; entries cost only the containers they own (a few KB each,
   ring-bounded). Worst case is a few MB per room, not gigabytes.

**Same function, no loss:** cap the history ring (humans undo a handful of
steps — last-N is behaviorally identical) + evict idle rooms. The frontend
`sockets` defaultdict also lingers per code forever — trivial, but same smell.

---

### 3.5 The single global lock: right today, a trap tomorrow — **RESOLVED**

*(as found)* The docstring said it plainly:

> *"Async registry of games; serializes mutations on the event loop… this layer
> adds the `asyncio.Lock` so transport callers interleave safely."*

Today's critical section was microseconds of dict work — correct, but the trap:
the day anyone added I/O *inside* `rooms.run()` — persistence, a rate-limiter,
a metrics call — **all 1000 rooms suddenly serialize behind one disk/network
round-trip**. Nothing in the structure warned you.

**Outcome:** the lock is gone and the trap is closed harder than the original
prescription (per-room locks) would have closed it. The enforced invariant is
now "mutations are synchronous" — `GameManager.run` raises `TypeError` on any
awaitable — so the risky code cannot be written silently at all; per-room locks
would still have allowed it, just one room at a time. The loop itself is the
serializer; commit sections are µs of pointer/dict work.

---

### 3.6 The UI mirrors the state machine by string — **OPEN**

`RoomPage.jsx` (347 lines, still fine) knows backend states as raw strings in
up to three spots:

```js
const PLAY_STAGES = ["board", "asking", ..., "quest_answering"];  // stage gate
// plus: flow === "quest_answering" branches, HostControls' CONTEXTUAL set,
// and snapshot()'s hand-rolled camelCase field mapping
```

Adding a backend state means touching RoomPage in ~3 places, and a typo in a
stage name (`"quest_answerign"`) **fails silently** — the branch just never
renders. Similarly `snapshot()` maps fields by hand with no typed output model:
backend and frontend agree only by convention; a renamed field breaks at
runtime, not at build time.

**Same function, no loss:** stage→component table (lookup, not if/else chain),
and a Pydantic snapshot model so there's one schema both sides consume.

---

### 3.7 Small stuff — **OPEN (nits)**

- **Raw WS frame kills its client:** `raw = await ws.receive_json()`
  (main.py:196) sits inside a `try` whose handler only catches
  `WebSocketDisconnect` — a malformed JSON frame raises `JSONDecodeError` and
  drops the connection. Blast radius = that one client (the `finally` cleans
  the registry), so it's a nit, not a wound.
- **Double serialization on REST:** the endpoint returns `snapshot(game, me)`
  *and* the broadcast sends it over WS to the same caller — one extra build per
  REST action.
- **No persistence** (§4.1) is a posture, not an accident — but nobody has
  written it down as one.

---

## 4. Situations it is *not* robust to

| # | Scenario | What happens |
|---|---|---|
| 4.1 | **Server restart** | Every live game silently ceases to exist — in-memory only. Fine for the genre; must be a *stated* posture. |
| 4.2 | **Long-running process** | Rooms accumulate forever (eviction open, §3.4). Undo history is capped (`MAX_UNDO = 200`) — no longer part of the leak. |
| 4.3 | **`uvicorn --workers 4`** | **Silent correctness break, not speedup.** State is process-local: a REST action or WS message for room `ABCD` can land on a worker that has never seen `ABCD` → `room ABCD not found`. Scaling out requires sticky-routing by room code or an external store. |
| 4.4 | **Buzz-storm at ~1000 busy games** | Shared-loop saturation → p99 latency rises for *all* rooms, not just the busy one (§2, estimate). |
| 4.5 | **A slow WebSocket client** | Stalls that room's broadcast (sends are serial per room; grouping open, §3.3a). Stale *reordering* is covered — the rev guard drops late frames (§3.3b). |
| 4.6 | **Cookie-only identity** | Name-squatting: anyone who knows a name can join as them (documented tradeoff — fine among friends, not on a public tunnel). Related by design: identity deliberately *outlives presence* — the score ledger is keyed by cookie identity, so leaving and rejoining preserves scores, and departed identities stay gradeable. |
| 4.7 | **Host disconnects mid-game** | The room stalls: only the host may `perform`, and promotion is lobby-only (agreed policy). Rejoin-by-cookie heals an absent host instantly; a *gone* host waits for eviction (§3.4). |

---

## 5. What's good — protect this

- **Sync domain / async shell split.** 205 domain tests, ~1.5s, zero mocks.
- **Immutable `Core` + environment split.** Undo is a pointer rebind;
  failure-safety is construction, not rollback; and because the roster never
  lived in the record, the whole family of leave/undo edge cases (rejoin score
  resurrection, ghost wagers, restored viewer flags) became *unrepresentable*
  rather than handled by merge rules.
- **Revision clock at the consumption edge** — ordering survives every delivery
  path (WS, REST, hibernating tabs), and the clock never rewinds even when the
  domain does.
- **Capability system driving the UI** — buttons come from server metadata;
  the client cannot offer an action the state forbids.
- **Distinct error taxonomy** (`unknown` vs `unavailable` vs `bad-kwargs`)
  with tests asserting on the exact messages.
- **I/O is async end-to-end** (httpx AsyncClient) — the loop never blocks on
  the network — and broadcast happens *after* the commit: mutations are never
  held hostage by slow sockets.

---

## 6. Recommended order of work

1. ~~Single action registry~~ **DONE** (§3.1) — one table derives schema and
   dispatch; drift test in place.
2. **Broadcast: role-group snapshots** (§3.3a) — the ordering half shipped
   differently (rev guard, §3.3b) and is done; the CPU half remains. *Benchmark
   first — do not optimize against the §2 estimate while it is unmeasured.*
3. **Idle-room eviction** (§3.4) — the history cap already exists; eviction
   turns memory into a flat line and reaps orphaned rooms (§4.7). Decide the
   host-vanish policy as part of it: eviction only, or eviction + mid-game
   promotion.
4. **Benchmark §2** — replace the estimate with a number before any scale work.

Then, only if 1000 busy games becomes a real target: sticky-session
multi-worker or an external room store (§4.3), and delta payloads. The next
maintainability tier — payload contracts (§3.2) and the typed UI state mirror
(§3.6) — is worth doing at any scale but is taste, not risk. Per-room locks
(§3.5) dropped off the list: the lock itself is gone.
