import { useCallback, useState } from "react";
import { addRecentRoom, loadIdentity } from "./lib/identity.js";
import { HomePage } from "./pages/HomePage.jsx";
import { RoomPage } from "./pages/RoomPage.jsx";

const SESSION_KEY = "jeffpardy_session";

function loadSession() {
  try {
    const raw = window.sessionStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (parsed && /^\d{4}$/.test(parsed.code) && parsed.name) return parsed;
    return null;
  } catch {
    return null;
  }
}

export function App() {
  const [session, setSession] = useState(() => loadSession());

  const enter = useCallback((next) => {
    try {
      window.sessionStorage.setItem(SESSION_KEY, JSON.stringify(next));
    } catch {
      /* noop */
    }
    addRecentRoom(next.code, next.name);
    setSession(next);
  }, []);

  const leave = useCallback(() => {
    try {
      window.sessionStorage.removeItem(SESSION_KEY);
    } catch {
      /* noop */
    }
    setSession(null);
  }, []);

  if (!session) {
    return <HomePage onEnter={enter} initialName={loadIdentity()} />;
  }
  return <RoomPage code={session.code} name={session.name} onLeave={leave} />;
}
