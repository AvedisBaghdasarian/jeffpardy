export function Board({ state, canPick, canPlayerPick, pickerName, onPick }) {
  const { categories, clues } = state;
  if (!categories?.length) return null;
  return (
    <div className="board" data-testid="board" aria-label="Jeffpardy board">
      {categories.map((cat) => (
        <div className="board-column" key={cat}>
          <div className="board-header" data-testid={`category-${cat}`}>
            {cat}
          </div>
          {[200, 400, 600, 800, 1000].map((value) => {
            const clue = clues.find((c) => c.category === cat && c.value === value);
            if (!clue) {
              return <div className="clue" key={value} style={{ visibility: "hidden" }} />;
            }
            const answered = clue.state === "answered";
            const active = clue.state === "active";
            // Either side chooses its own way: the host names the clue, or the
            // player holding the board names it while sharing the hosting.
            // Only one side chooses at a time, so the board never double-books.
            const playable = !answered && !active && (canPick || canPlayerPick);
            const quest = state.activeQuest === clue.id && !answered;
            return (
              <button
                key={value}
                className={`clue${answered ? " answered" : ""}${active ? " active" : ""}${quest ? " quest" : ""}`}
                data-testid={`clue-${clue.id}`}
                data-quest={quest ? "1" : undefined}
                disabled={!playable}
                onClick={() => onPick(clue.id)}
                aria-label={`${cat} for $${value}${quest ? " — Gamely Jeff Quest" : ""}${canPlayerPick && !answered && !active ? ` — ${pickerName || "your"} pick` : ""}`}
              >
                {answered ? "" : active ? "?" : `$${value}`}
                {quest && <span className="quest-chip" data-testid="quest-chip">🎯</span>}
              </button>
            );
          })}
        </div>
      ))}
    </div>
  );
}
