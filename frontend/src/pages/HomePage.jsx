import { useEffect, useState } from "react";
import { api } from "../lib/api.js";
import {
  addRecentRoom,
  getRecentRooms,
  isValidCode,
  isValidName,
  loadIdentity,
  normalizeCode,
  saveIdentity,
} from "../lib/identity.js";

export function HomePage({ onEnter }) {
  const [name, setName] = useState(() => loadIdentity());
  const [code, setCode] = useState("");
  const [quickPlay, setQuickPlay] = useState(false);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [recent, setRecent] = useState(() => getRecentRooms());
  const [showHelp, setShowHelp] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .me()
      .then((me) => {
        if (!cancelled && me?.name && !loadIdentity()) {
          setName(me.name);
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  function nameError() {
    if (!name.trim()) return "Enter your display name first — it's saved on this device so you never log in.";
    if (!isValidName(name)) return "Name must be 1–24 characters.";
    return null;
  }

  async function enter(roomCode) {
    const badName = nameError();
    if (badName) {
      setError(badName);
      return;
    }
    const cleanCode = normalizeCode(roomCode);
    if (!isValidCode(cleanCode)) {
      setError("Room codes are 4 digits, e.g. 1234.");
      return;
    }
    const trimmed = saveIdentity(name);
    setBusy(true);
    setError(null);
    try {
      await api.getRoom(cleanCode);
      await api.joinRoom(cleanCode, trimmed);
      setRecent(addRecentRoom(cleanCode, trimmed));
      onEnter({ code: cleanCode, name: trimmed });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function create() {
    const badName = nameError();
    if (badName) {
      setError(badName);
      return;
    }
    const trimmed = saveIdentity(name);
    setBusy(true);
    setError(null);
    try {
      const room = await api.createRoom(quickPlay);
      await api.joinRoom(room.code, trimmed);
      setRecent(addRecentRoom(room.code, trimmed));
      onEnter({ code: room.code, name: trimmed });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="home">
      <h1 className="logo">JEFFPARDY!</h1>
      <p className="tagline">The online buzzer quiz game for the whole room. Fresh clues every game — no login, just pick a name.</p>

      <section className="card" aria-label="Your identity">
        <label className="field">
          Display name <span className="muted">(saved on this device)</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={24}
            placeholder="e.g. Jeff"
            autoComplete="nickname"
            data-testid="name-input"
          />
        </label>
        {name.trim() && (
          <p className="muted" data-testid="identity-hint">
            Playing as <strong>{name.trim()}</strong> on this device.
          </p>
        )}
      </section>

      <section className="card" aria-label="Create a game">
        <h2>Create a room</h2>
        <label className="check">
          <input
            type="checkbox"
            checked={quickPlay}
            onChange={(e) => setQuickPlay(e.target.checked)}
            data-testid="quick-play"
          />
          Quick game (ready-made clues, starts instantly)
        </label>
        <div className="button-row">
          <button onClick={create} disabled={busy} data-testid="create-room">
            {busy ? "Creating…" : "Create room"}
          </button>
          <button className="secondary" onClick={() => setShowHelp((v) => !v)} data-testid="how-to">
            {showHelp ? "Hide how to play" : "How to play"}
          </button>
        </div>
        {showHelp && (
          <ol className="help" data-testid="how-to-text">
            <li>The first player in hosts the show and reads the clues — hosting means no score for them.</li>
            <li>Everyone else plays with the 4-digit code — no login, ever.</li>
            <li>The host reveals a clue and opens the buzzers; players hit BUZZ. The host calls on whoever buzzed first.</li>
            <li>The host rules each response right or wrong, through Jeffpardy and Bonus Jeffpardy, then the Ultimate Jeffpardy wagers.</li>
            <li>Wrong button? The host can always take it back.</li>
          </ol>
        )}
      </section>

      <form
        className="card"
        aria-label="Join a game"
        onSubmit={(e) => {
          e.preventDefault();
          enter(code);
        }}
      >
        <h2>Join with a 4-digit code</h2>
        <label className="field">
          Room code
          <input
            value={code}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 4))}
            maxLength={4}
            inputMode="numeric"
            placeholder="1234"
            data-testid="code-input"
          />
        </label>
        <div className="button-row">
          <button type="submit" disabled={busy || !code.trim()} data-testid="join-room">
            Join room
          </button>
        </div>
      </form>

      {recent.length > 0 && (
        <section className="card" aria-label="Recent rooms">
          <h2>Recent rooms</h2>
          <div className="button-row">
            {recent.map((r) => (
              <button
                key={r.code}
                className="secondary"
                disabled={busy}
                onClick={() => {
                  setCode(r.code);
                  enter(r.code);
                }}
                data-testid={`recent-${r.code}`}
              >
                {r.code}
              </button>
            ))}
          </div>
        </section>
      )}

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </main>
  );
}
