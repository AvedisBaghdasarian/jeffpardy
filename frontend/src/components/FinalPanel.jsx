import { useState } from "react";

// Ultimate Jeffpardy: category -> wagering -> clue -> reveal.
// Players only ever see their own wager/answer until the reveal;
// the host sees everything.
export function FinalPanel({ state, name, isHost, onPlayerAction, onHostAction }) {
  const stage = state.state;
  const final = state.final || {};
  const myScore = Math.max(state.players?.[name] ?? 0, 0);
  const myWager = Object.prototype.hasOwnProperty.call(final.wagers || {}, name)
    ? final.wagers[name]
    : undefined;
  const myAnswer = final.answers?.[name];
  const [wager, setWager] = useState("");
  const [answer, setAnswer] = useState("");
  const graded = final.graded || [];
  const roster = Object.keys(state.players || {});

  if (stage === "final_category") {
    return (
      <div className="final-card" data-testid="final-category">
        <div className="big">ULTIMATE JEFFPARDY</div>
        <p className="clue-category">{final.category}</p>
        {isHost && final.question && (
          <p className="muted">Your next read: {final.question}</p>
        )}
        <p className="muted">
          {isHost ? "Take wagers when everyone's ready." : "Waiting for the host to take wagers…"}
        </p>
      </div>
    );
  }

  if (stage === "final_wagering") {
    return (
      <div className="final-card" data-testid="final-wagering">
        <div className="big">{final.category}</div>
        {!isHost && (
          <>
            <p>Wager up to <strong>${myScore}</strong> before the clue.</p>
            {myWager === undefined ? (
              <div className="final-form">
                <input
                  type="number"
                  min={0}
                  max={myScore}
                  value={wager}
                  placeholder={`0–${myScore}`}
                  onChange={(e) => setWager(e.target.value)}
                  data-testid="wager-input"
                />
                <button
                  data-testid="submit-wager"
                  disabled={wager === "" || Number.isNaN(Number(wager))}
                  onClick={() => onPlayerAction("wager", { amount: Number(wager) })}
                >
                  Submit wager
                </button>
              </div>
            ) : (
              <p data-testid="wager-status">Wager locked in: ${myWager}</p>
            )}
          </>
        )}
        {isHost && (
          <div className="final-list" data-testid="wagers-placed">
            <span className="muted">Wagers in:</span>
            {roster.map((p) => (
              <span key={p} data-testid={`wager-placed-${p}`}>
                {p}: {Object.prototype.hasOwnProperty.call(final.wagers || {}, p) ? `$${final.wagers[p]}` : "…"}
              </span>
            ))}
          </div>
        )}
      </div>
    );
  }

  if (stage === "final_clue") {
    return (
      <div className="final-card" data-testid="final-clue">
        <div className="big">{final.category}</div>
        <p className="clue-question" data-testid="final-question">{final.question}</p>
        {!isHost && (
          <div className="final-form">
            <input
              value={answer}
              placeholder="What is…"
              onChange={(e) => setAnswer(e.target.value)}
              data-testid="final-answer-input"
            />
            <button
              data-testid="submit-final-answer"
              disabled={!answer.trim()}
              onClick={() => onPlayerAction("final_answer", { text: answer })}
            >
              {myAnswer === undefined ? "Submit answer" : "Update answer"}
            </button>
          </div>
        )}
        {/* Read the answer back once it's locked in: a bare text box would look
            like the answer was never received. */}
        {!isHost && myAnswer !== undefined && (
          <p data-testid="final-answer-status">
            Answer locked in: “{myAnswer}” — you can still change it until the host reveals.
          </p>
        )}
        {isHost && (
          <div className="final-list" data-testid="final-answers-list">
            <span className="muted">Answers in:</span>
            {roster.map((p) => (
              <span key={p} data-testid={`final-answer-${p}`}>
                {p}: {final.answers?.[p] ?? "…"}
              </span>
            ))}
          </div>
        )}
      </div>
    );
  }

  // final_reveal
  return (
    <div className="final-card" data-testid="final-reveal">
      <div className="big">The reveal</div>
      <div className="final-list">
        {roster.map((p) => (
          <div key={p} className="grade-row" data-testid={`grade-row-${p}`}>
            <strong>{p}</strong>
            <span>wagered ${final.wagers?.[p] ?? 0}</span>
            <span>“{final.answers?.[p] ?? "no answer"}”</span>
            {graded.includes(p) && <span data-testid={`graded-${p}`}>✓ ruled</span>}
            {isHost && !graded.includes(p) && state.availableHostActions?.some((a) => a.name === "grade_final") && (
              <>
                <button data-testid={`grade-${p}-correct`} onClick={() => onHostAction("grade_final", { player_id: p, correct: true })}>Right</button>
                <button className="secondary" data-testid={`grade-${p}-incorrect`} onClick={() => onHostAction("grade_final", { player_id: p, correct: false })}>Wrong</button>
              </>
            )}
          </div>
        ))}
      </div>
      <p className="muted">
        {isHost ? "Judge each response, then read the Final Scores." : "Waiting for the host to judge…"}
      </p>
    </div>
  );
}
