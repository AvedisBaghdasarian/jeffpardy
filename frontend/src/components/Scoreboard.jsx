export function Scoreboard({ state, isHost, canAdjust, onAdjust, canManageViewers, onToggleViewer, onKick }) {
  const entries = Object.entries(state.players || {}).sort((a, b) => b[1] - a[1]);
  const viewers = state.viewers || [];
  const departed = state.departed || [];
  const away = state.away || [];
  const isViewer = (n) => viewers.includes(n);
  const isDeparted = (n) => departed.includes(n);
  const isAway = (n) => away.includes(n);
  const showAdjust = isHost && canAdjust;
  return (
    <aside className="scoreboard" data-testid="scoreboard">
      <h2>Scores</h2>
      {state.host && (
        <div className="host-badge" data-testid="host-badge">
          👑 Host: {state.host}
        </div>
      )}
      {entries.length === 0 && <p className="muted">No players yet.</p>}
      {entries.map(([name, score]) => (
        <div key={name} className="score-row" data-testid={`score-${name}`}>
          <span>
            {name}
            {isViewer(name) && (
              <span className="viewer-badge" data-testid={`viewer-badge-${name}`}> 👁 in the audience</span>
            )}
            {isDeparted(name) && (
              <span className="away-badge" data-testid={`away-badge-${name}`}> 🚪 stepped out</span>
            )}
            {!isDeparted(name) && isAway(name) && (
              <span className="away-badge" data-testid={`away-badge-${name}`}> 📶 connection lost</span>
            )}
          </span>
          <strong>${score}</strong>
          {canManageViewers && (
            <button
              className="secondary adjust-btn"
              data-testid={`viewer-toggle-${name}`}
              onClick={() => onToggleViewer(name, !isViewer(name))}
            >
              {isViewer(name) ? "Bring back in" : "Sit out"}
            </button>
          )}
          {isHost && onKick && !isDeparted(name) && (
            <button
              className="secondary adjust-btn"
              data-testid={`kick-${name}`}
              aria-label={`Remove ${name} from the game`}
              title={`${name} keeps their score if they rejoin`}
              onClick={() => onKick(name)}
            >
              Remove
            </button>
          )}
          {showAdjust && (
            <span className="adjust-group">
              <button
                className="secondary adjust-btn"
                data-testid={`adjust-${name}-down`}
                aria-label={`Subtract 200 from ${name}`}
                onClick={() => onAdjust(name, -200)}
              >
                −$200
              </button>
              <button
                className="secondary adjust-btn"
                data-testid={`adjust-${name}-up`}
                aria-label={`Add 200 to ${name}`}
                onClick={() => onAdjust(name, 200)}
              >
                +$200
              </button>
            </span>
          )}
        </div>
      ))}
      {Object.keys(state.players || {}).length === 0 && state.host && (
        <p className="muted" data-testid="host-only-note">
          Just you so far — share the code to invite players.
        </p>
      )}
    </aside>
  );
}
