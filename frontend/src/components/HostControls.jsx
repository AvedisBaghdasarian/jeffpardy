// The host's buttons, straight from the server.
// Steps with their own spot on screen (board choices, call-on buttons,
// right/wrong rulings, audience toggles, the pause overlay) appear next to
// that spot instead of here — everything else is a plain labeled button,
// so new steps appear without touching this file.
const CONTEXTUAL = new Set([
  "pick_clue",
  "select_buzzer",
  "correct",
  "incorrect",
  "grade_final",
  "adjust_score",
  "set_viewer", // per-player toggles live in the scoreboard/roster
  "set_picker", // the board renders one "give it to X" button per contender
  "promote_host", // lobby roster button
  "resume", // the pause overlay owns Resume
]);

const SECONDARY = new Set(["undo", "pause", "reveal_answer"]);

export function HostControls({ actions, onAction }) {
  const buttons = actions.filter((a) => !CONTEXTUAL.has(a.name));
  if (buttons.length === 0) return null;
  return (
    <div className="host-controls" data-testid="host-controls" aria-label="Host controls">
      {buttons.map((a) => (
        <button
          key={a.name}
          className={SECONDARY.has(a.name) ? "secondary" : undefined}
          data-testid={`host-${a.name}`}
          onClick={() => onAction(a.name)}
        >
          {a.label}
        </button>
      ))}
    </div>
  );
}
