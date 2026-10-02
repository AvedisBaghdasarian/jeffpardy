export function ClueModal({ state }) {
  if (!state.activeClueId || !state.question) return null;
  const active = (state.clues || []).find((c) => c.id === state.activeClueId);
  return (
    <div className="clue-modal" data-testid="clue-modal">
      <div className="clue-category">
        {active?.category} — ${active?.value}
      </div>
      <div className="clue-question" data-testid="clue-question">
        {state.question}
      </div>
      {state.answer && (
        <div className="clue-answer" data-testid="clue-answer">
          {state.answer}
        </div>
      )}
      {state.answerer && (
        <div className="answerer" data-testid="modal-answerer">
          {state.answerer} is answering…
        </div>
      )}
      {state.answerRevealed && !state.answerer && (
        <div className="muted" data-testid="reveal-note">
          Answer revealed by the host.
        </div>
      )}
    </div>
  );
}
