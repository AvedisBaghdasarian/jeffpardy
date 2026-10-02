import { useState } from "react";
import { useRoom } from "../hooks/useRoom.js";
import { Board } from "../components/Board.jsx";
import { Buzzer } from "../components/Buzzer.jsx";
import { ClueModal } from "../components/ClueModal.jsx";
import { FinalPanel } from "../components/FinalPanel.jsx";
import { HostControls } from "../components/HostControls.jsx";
import { Scoreboard } from "../components/Scoreboard.jsx";

const FINAL_STAGES = ["final_category", "final_wagering", "final_clue", "final_reveal"];
const PLAY_STAGES = ["board", "asking", "buzzers_open", "answering", "round_intermission", "quest_answering"];

export function RoomPage({ code, name, onLeave }) {
  const { state, connected, error, send } = useRoom(code, name);
  const [gaveUp, setGaveUp] = useState(false);
  const [questWager, setQuestWager] = useState("");

  const isHost = !!state?.host && !!name && state.host.toLowerCase() === name.toLowerCase();
  const actions = isHost && state ? state.availableHostActions || [] : [];
  const has = (action) => actions.some((a) => a.name === action);
  const hostAct = (action, payload = {}) => send({ type: "host", action, ...payload });
  const playerAct = (action, payload = {}) => send({ type: "player", action, ...payload });

  function leaveRoom() {
    playerAct("leave");
    setGaveUp(true);
    onLeave();
  }

  if (gaveUp) return null;
  if (!state) {
    return (
      <main className="room">
        <p data-testid="room-loading">Taking your seat in room {code}…</p>
        {error && (
          <p className="error" role="alert">
            {error} <button className="secondary" onClick={onLeave}>Back to the couch</button>
          </p>
        )}
      </main>
    );
  }

  const paused = state.state === "paused";
  const flow = paused ? state.pausedIn : state.state;
  const viewers = state.viewers || [];
  const departed = state.departed || [];
  const departedLower = new Set(departed.map((v) => v.toLowerCase()));
  const viewersLower = new Set(viewers.map((v) => v.toLowerCase()));
  const iAmViewer = viewersLower.has((name || "").toLowerCase());
  const iSteppedOut = departedLower.has((name || "").toLowerCase());
  const closeReason = state.closeReason || null;
  const closeMessage =
    closeReason === "empty"
      ? `Everyone stepped out of room ${state.code}, so the curtains closed. Final scores are on the right — settle in on the couch to play again.`
      : closeReason === "overtime"
        ? `Room ${state.code} played its two-hour show, so the curtains closed. Final scores are on the right — settle in on the couch to play again.`
        : closeReason === "idle"
          ? `Room ${state.code} sat quiet through the commercial break, so the curtains closed. Final scores are on the right — settle in on the couch to play again.`
          : `The host signed off room ${state.code}. Final scores are on the right — settle in on the couch to play again.`;

  // The holder names the clue — they tap the board, otherwise the host runs it.
  // Wire says "co-hosted"; old rooms may still say "assisted".
  const assisted = state.hostMode === "co-hosted" || state.hostMode === "assisted";
  const hasTheBoard =
    !!state.picker && !!name && state.picker.toLowerCase() === name.toLowerCase();
  const iAmPicker = assisted && !isHost && !iAmViewer && (hasTheBoard || !state.picker);
  const canPlayerPick = iAmPicker && flow === "board" && !state.closed && !paused;
  const hostCanPick = isHost && has("pick_clue");

  const buzzQueue = state.buzzQueue || [];
  const queuedLower = new Set(buzzQueue.map((n) => n.toLowerCase()));
  const others = Object.keys(state.players || {}).filter(
    (p) =>
      p.toLowerCase() !== (state.answerer || "").toLowerCase() &&
      !queuedLower.has(p.toLowerCase()) &&
      !viewersLower.has(p.toLowerCase()) // the host can't call on the audience
  );

  let stageContent = null;
  if (state.closed) {
    stageContent = (
      <div className="game-over" data-testid="game-over">
        <h2>That's the game!</h2>
        <p>{closeMessage}</p>
        <button onClick={onLeave} data-testid="back-home">Back to the couch</button>
      </div>
    );
  } else if (flow === "lobby") {
    stageContent = (
      <div className="lobby-screen" data-testid="lobby-screen">
        <h2>Green Room</h2>
        <p>
          {isHost
            ? "You're tonight's host: you read the clues and call the shots. Roll the show once everyone's in."
            : `Waiting for ${state.host} to roll the show…`}
        </p>
        <ul className="roster" data-testid="roster">
          {Object.keys(state.players).map((p) => (
            <li key={p} data-testid={`roster-${p}`}>
              {p}
              {viewers.includes(p) && " 👁"}
              {isHost && has("promote_host") && (
                <button
                  className="secondary"
                  data-testid={`promote-${p}`}
                  onClick={() => hostAct("promote_host", { player_id: p })}
                >
                  Make host
                </button>
              )}
            </li>
          ))}
          {Object.keys(state.players).length === 0 && (
            <li className="muted">No contenders yet — call out code {state.code}.</li>
          )}
        </ul>
      </div>
    );
  } else if (flow === "intro") {
    stageContent = (
      <div className="intro-screen" data-testid="intro-screen">
        <h2>{state.roundLabel} is next!</h2>
        <p>Tonight's contenders: {Object.keys(state.players).join(", ") || "—"}</p>
        <p className="muted">
          {isHost ? "Roll the board when the room is ready." : "Hang tight — the host rolls the board…"}
        </p>
      </div>
    );
  } else if (FINAL_STAGES.includes(flow)) {
    stageContent = (
      <FinalPanel
        state={state}
        name={name}
        isHost={isHost}
        onPlayerAction={playerAct}
        onHostAction={hostAct}
      />
    );
  } else if (flow === "review") {
    stageContent = (
      <div className="review-screen" data-testid="review-screen">
        <h2>Tonight's Final Scores</h2>
        <p className="muted">
          What a game! The host can still fix a score before signing off.
        </p>
      </div>
    );
  } else if (flow === "quest_wager") {
    const claimant = state.answerer;
    const isClaimant = !!claimant && !!name && claimant.toLowerCase() === name.toLowerCase();
    const risk = state.questWager;
    const limit = state.questLimit ?? 0;
    stageContent = (
      <div className="final-card" data-testid="quest-wager">
        <div className="big">🎯 GAMELY JEFF QUEST</div>
        <p data-testid="quest-claimant">{claimant} found the quest card!</p>
        {state.question && (
          <p className="clue-question" data-testid="quest-question">{state.question}</p>
        )}
        {risk != null ? (
          <p data-testid="quest-wager-status">Wager on the table: ${risk}</p>
        ) : isClaimant ? (
          <div className="final-form">
            <input
              type="number"
              min={0}
              max={limit}
              value={questWager}
              placeholder={`0–${limit}`}
              onChange={(e) => setQuestWager(e.target.value)}
              data-testid="quest-wager-input"
            />
            <button
              data-testid="submit-quest-wager"
              disabled={questWager === "" || Number.isNaN(Number(questWager))}
              onClick={() => playerAct("quest_wager", { amount: Number(questWager) })}
            >
              Put it on the table
            </button>
          </div>
        ) : (
          <p className="muted" data-testid="quest-waiting">
            Waiting on {claimant}'s wager…
          </p>
        )}
        <p className="muted">
          {isHost
            ? assisted
              ? "The clue plays as soon as their wager lands."
              : "Play Hear Their Answer once the wager lands."
            : assisted
              ? "The clue plays as soon as the wager lands."
              : "The host plays the clue when the room is ready."}
        </p>
      </div>
    );
  } else if (PLAY_STAGES.includes(flow)) {
    stageContent = (
      <>
        <div className="round-banner" data-testid="round-banner">{state.roundLabel}</div>
        <div className="picker-line" data-testid="picker-line">
          <span data-testid="picker-badge">
            {state.picker ? `🎯 ${state.picker} picks the next clue` : "🎯 Nobody has the board yet"}
          </span>
          {has("set_picker") && (
            <span className="picker-controls">
              <span className="muted">Who picks next?</span>
              {Object.keys(state.players || {})
                .filter(
                  (p) =>
                    !viewersLower.has(p.toLowerCase()) &&
                    p.toLowerCase() !== (state.host || "").toLowerCase(),
                )
                .map((p) => (
                  <button
                    key={p}
                    data-testid={`set-picker-${p}`}
                    onClick={() => hostAct("set_picker", { player_id: p })}
                  >
                    {p}
                  </button>
                ))}
            </span>
          )}
          {/* The holder names the clue, the host watches. The badge says whose
              choice counts this turn. */}
          {assisted && flow === "board" && (
            <span className="muted" data-testid="pick-hint">
              {iAmPicker
                ? state.picker
                  ? "Your pick — call a clue."
                  : "Nobody has the board — your call claims it."
                : state.picker
                  ? `${state.picker} calls it — leave the board to them.`
                  : "Waiting for someone to claim the board."}
            </span>
          )}
        </div>
        {flow === "round_intermission" && (
          <p className="intermission" data-testid="intermission">
            That's the round — {state.round === "main" ? "Bonus Jeffpardy" : "Ultimate Jeffpardy"} coming up after the break!
          </p>
        )}
        <ClueModal state={state} />
        <Board
          state={state}
          canPick={hostCanPick}
          canPlayerPick={canPlayerPick}
          pickerName={state.picker}
          onPick={(id) =>
            canPlayerPick
              ? playerAct("pick", { clue_id: id })
              : hostAct("pick_clue", { clue_id: id })
          }
        />
        {(flow === "answering" || flow === "quest_answering") && (
          <div className="answer-panel" data-testid="answer-panel">
            <p>
              <strong data-testid="answerer-name">{state.answerer}</strong> has the floor…
            </p>
            {flow === "quest_answering" && (
              <p data-testid="quest-risk">
                🎯 playing for <strong>${state.questWager ?? 0}</strong>
              </p>
            )}
            {isHost && has("correct") && (
              <button data-testid="host-correct" onClick={() => hostAct("correct")}>
                Right
              </button>
            )}
            {isHost && has("incorrect") && (
              <button className="secondary" data-testid="host-incorrect" onClick={() => hostAct("incorrect")}>
                Wrong
              </button>
            )}
          </div>
        )}
        {(buzzQueue.length > 0 || flow === "buzzers_open") && (
          <div className="queue-panel" data-testid="buzz-queue">
            <h3>Buzz order</h3>
            {buzzQueue.length === 0 && (
              <p className="muted">
                {flow === "buzzers_open" ? "No buzzes yet — the first one goes to the front." : "Nobody rang in."}
              </p>
            )}
            <ol>
              {buzzQueue.map((p, i) => (
                <li key={p} data-testid={`queue-${p}`}>
                  <span>#{i + 1} {p}</span>
                  {isHost && has("select_buzzer") && (
                    <button
                      data-testid={`select-buzzer-${p}`}
                      onClick={() => hostAct("select_buzzer", { player_id: p })}
                    >
                      Call on
                    </button>
                  )}
                </li>
              ))}
            </ol>
            {isHost && has("select_buzzer") && others.length > 0 && (
              <div className="queue-others">
                <span className="muted">Or give the floor to:</span>
                {others.map((p) => (
                  <button
                    key={p}
                    className="secondary"
                    data-testid={`select-buzzer-${p}`}
                    onClick={() => hostAct("select_buzzer", { player_id: p })}
                  >
                    {p}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </>
    );
  }

  return (
    <main className="room">
      <header className="topbar">
        <span className="logo-mini">JEFFPARDY!</span>
        <span className="room-code" data-testid="room-code">Room {state.code}</span>
        <span className={`conn ${connected ? "on" : "off"}`} data-testid="connection-status">
          {connected ? "on the air" : "reconnecting…"}
        </span>
        {isHost ? (
          <span className="host-pill" data-testid="you-are-host">You're hosting tonight</span>
        ) : (
          <button className="secondary" data-testid="leave-room" onClick={leaveRoom}>
            Step out
          </button>
        )}
      </header>

      {error && (
        <p className="error" role="alert" data-testid="room-error">
          {error}
        </p>
      )}

      <div className="room-grid">
        <section className="stage">
          {stageContent}
          {/* The host flips this any time. Shared hosting: the contender with the
              pick calls the clues and the show keeps the pace — extra host
              buttons stay tucked away, so nothing fights the flow of the game. */}
          {isHost && !state.closed && (
            <div className="host-mode" data-testid="host-mode">
              <span className="muted" data-testid="host-mode-badge">
                {assisted ? "🤝 Shared hosting" : "🎙️ Hosting it solo"}
              </span>
              <button
                className="secondary"
                data-testid={assisted ? "host-mode-manual" : "host-mode-assisted"}
                onClick={() =>
                  hostAct("set_host_mode", { mode: assisted ? "manual" : "co-hosted" })
                }
              >
                {assisted ? "Take back the whole show" : "Share hosting with the room"}
              </button>
            </div>
          )}
          {isHost && assisted && !state.closed && (
            <p className="muted" data-testid="assist-note">
              Shared hosting: the contender with the pick calls the clues and the
              show keeps the pace. You rule on the responses and keep the night moving.
            </p>
          )}
          {isHost && !state.closed && <HostControls actions={actions} onAction={hostAct} />}
        </section>

        <Scoreboard
          state={state}
          isHost={isHost}
          canAdjust={has("adjust_score")}
          onAdjust={(player, value) => hostAct("adjust_score", { player_id: player, value })}
          canManageViewers={isHost && has("set_viewer")}
          onToggleViewer={(player, viewer) => hostAct("set_viewer", { player_id: player, viewer })}
          onKick={isHost && !state.closed ? (player) => playerAct("kick", { player_id: player }) : null}
        />
      </div>

      {!state.closed && (
        <Buzzer
          state={state}
          name={name}
          isHost={isHost}
          isViewer={iAmViewer}
          onBuzz={() =>
            playerAct("buzz", { clue_id: state.activeClueId, buzz_token: state.buzzToken })
          }
        />
      )}

      {paused && (
        <div className="pause-overlay" data-testid="pause-overlay">
          <div className="pause-card">
            <h2>Intermission</h2>
            <p>The host called a break — hang tight.</p>
            {isHost && has("resume") && (
              <button data-testid="host-resume" onClick={() => hostAct("resume")}>
                Back to the Game
              </button>
            )}
          </div>
        </div>
      )}

      {iSteppedOut && !state.closed && (
        <p className="muted" data-testid="stepped-out-note">
          You stepped out — slip back within five minutes and your score is right where you left it.
        </p>
      )}
    </main>
  );
}
