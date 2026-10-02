import { useCallback, useEffect, useRef, useState } from "react";

function socketUrl(code, name) {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/ws/rooms/${code}?name=${encodeURIComponent(name)}`;
}

// Show each game update once: late arrivals (a page load racing the live
// feed, a reconnect replay) never rewind what's on screen. The same update
// arriving twice still applies.
export function applyRemoteState(shown, incoming) {
  if (!incoming || !shown) return incoming;
  if (Number.isInteger(incoming.rev) && Number.isInteger(shown.rev) && incoming.rev < shown.rev) {
    return shown;
  }
  return incoming;
}

export function useRoom(code, name) {
  const [state, setState] = useState(null);
  const [error, setError] = useState(null);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef(null);

  useEffect(() => {
    if (!code) return;
    let closed = false;
    let attempt = 0;
    let timer = null;

    function connect() {
      if (closed) return;
      let ws;
      try {
        ws = new WebSocket(socketUrl(code, name));
      } catch {
        schedule();
        return;
      }
      wsRef.current = ws;
      // Only the live connection may write: page setup can briefly hold two
      // open at once, and the older one's goodbye must not end the newer one.
      ws.onopen = () => {
        if (wsRef.current !== ws) return;
        attempt = 0;
        setConnected(true);
      };
      ws.onmessage = (event) => {
        if (wsRef.current !== ws) return;
        let msg;
        try {
          msg = JSON.parse(event.data);
        } catch {
          return;
        }
        if (msg.type === "state") {
          setState((prev) => applyRemoteState(prev, msg.state)); // keep the newest update
          setError(null);
        } else if (msg.type === "error") {
          setError(msg.message);
        }
      };
      ws.onerror = () => {
        if (wsRef.current !== ws) return;
        setConnected(false);
      };
      ws.onclose = (event) => {
        if (wsRef.current === ws) {
          setConnected(false);
          wsRef.current = null;
        }
        if (!closed && event.code !== 4404) schedule();
        else if (event.code === 4404) setError("That room is gone. Play again from the menu.");
      };
    }

    function schedule() {
      if (closed) return;
      attempt += 1;
      setError((prev) => prev ?? "Connection lost. Reconnecting…");
      const delay = Math.min(1000 * 2 ** Math.min(attempt, 4), 8000);
      timer = setTimeout(connect, delay);
    }

    connect();

    // Lobby heartbeats: every 45s the tab says "still here", so a quiet room
    // reads as occupied and only a truly silent one gets cleaned up.
    const ping = setInterval(() => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "player", action: "heartbeat" }));
      }
    }, 45000);
    return () => {
      closed = true;
      if (timer) clearTimeout(timer);
      clearInterval(ping);
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [code, name]);

  const send = useCallback((message) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(message));
      return true;
    }
    return false;
  }, []);

  return { state, error, send, connected };
}
