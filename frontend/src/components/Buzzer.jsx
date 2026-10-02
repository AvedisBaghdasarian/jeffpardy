export function Buzzer({ state, name, isHost, isViewer, onBuzz }) {
  const queued = (state.buzzQueue || []).some((n) => n.toLowerCase() === (name || "").toLowerCase());
  const queueIndex = (state.buzzQueue || []).findIndex((n) => n.toLowerCase() === (name || "").toLowerCase());
  const answering = state.answerer && state.answerer.toLowerCase() === (name || "").toLowerCase();
  // Missed this clue already? The show locks you out — say so instead of
  // letting the tap bounce off the server as an error.
  const lockedOut = (state.answeredPlayers || []).some(
    (n) => n.toLowerCase() === (name || "").toLowerCase(),
  );
  const open = state.state === "buzzers_open" && !state.closed;

  if (isHost) {
    return (
      <p className="muted host-note" data-testid="host-note">
        You're hosting — call on someone when they buzz in.
      </p>
    );
  }

  if (isViewer) {
    return (
      <p className="muted viewer-note" data-testid="viewer-note">
        👁 You're in the audience — the host can bring you back in any time.
      </p>
    );
  }

  // Leave it live so players can buzz the instant it opens. The server
  // rules out mistimed buzzes; a buzz that's already in line is a no-op.
  let label = open ? "BUZZ!" : "Buzzers closed";
  if (lockedOut) label = "You already had this clue";
  if (answering) label = "You're answering!";
  return (
    <div className="buzzer-wrap">
      <button className="buzzer" data-testid="buzzer" onClick={onBuzz} disabled={lockedOut}>
        {label}
      </button>
      {lockedOut && !answering && (
        <span className="queued-status" data-testid="locked-out-status">
          Someone else gets a shot at this one.
        </span>
      )}
      {queued && !answering && (
        <span className="queued-status" data-testid="queued-status">
          Buzzed in — #{queueIndex + 1}
        </span>
      )}
    </div>
  );
}
