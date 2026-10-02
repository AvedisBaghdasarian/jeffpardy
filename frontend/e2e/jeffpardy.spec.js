import { expect, test } from "@playwright/test";

const API = "http://127.0.0.1:8123";

async function createRoom(request) {
  const res = await request.post(`${API}/api/rooms`, { data: { use_fallback: true } });
  expect(res.status()).toBe(200);
  return (await res.json()).code;
}

// Identity is a cookie: each UI context joins through the form so its cookie
// jar carries the name. Whoever enters first is the host.
async function enterViaUi(page, code, name) {
  await page.goto("/");
  await page.getByTestId("name-input").fill(name);
  await page.getByTestId("code-input").fill(code);
  await page.getByTestId("join-room").click();
  await expect(page.getByTestId("room-code")).toContainText(code);
}

test("host creates a room and drives lobby through to the board", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("name-input").fill("QueenJeff");
  await page.getByTestId("quick-play").check(); // offline fallback board
  await page.getByTestId("create-room").click();

  await expect(page.getByTestId("room-code")).toHaveText(/Room \d{4}/);
  await expect(page.getByTestId("lobby-screen")).toBeVisible();
  await expect(page.getByTestId("you-are-host")).toBeVisible();
  await expect(page.getByTestId("host-badge")).toContainText("QueenJeff");
  await expect(page.getByTestId("score-QueenJeff")).toHaveCount(0); // host doesn't play
  await expect(page.getByTestId("connection-status")).toHaveText("on the air");

  await page.getByTestId("host-start_game").click();
  await expect(page.getByTestId("intro-screen")).toBeVisible();

  await page.getByTestId("host-finish_intro").click();
  await expect(page.getByTestId("board")).toBeVisible();
  await expect(page.getByTestId("round-banner")).toHaveText("Jeffpardy");
  await expect(page.getByTestId("host-note")).toBeVisible(); // hosting, not buzzing
  await expect(page.getByTestId("lobby-screen")).toHaveCount(0);
});

test("host runs a clue end to end with a live player, answer stays hidden until revealed", async ({ browser, request }) => {
  const code = await createRoom(request);
  const hostPage = await (await browser.newContext()).newPage();
  const bobPage = await (await browser.newContext()).newPage();

  await enterViaUi(hostPage, code, "Ann"); // first in: the host
  await enterViaUi(bobPage, code, "Bob");
  await expect(hostPage.getByTestId("roster-Bob")).toBeVisible(); // live roster
  await expect(bobPage.getByTestId("host-badge")).toContainText("Ann");

  await hostPage.getByTestId("host-start_game").click();
  await hostPage.getByTestId("host-finish_intro").click();
  await expect(bobPage.getByTestId("board")).toBeVisible();

  // Host picks the top-left clue
  await hostPage.locator('[data-testid^="clue-"]').first().click();
  await expect(hostPage.getByTestId("clue-modal")).toBeVisible();
  await expect(bobPage.getByTestId("clue-modal")).toBeVisible();
  await expect(bobPage.getByTestId("clue-question")).toBeVisible();

  // Viewer separation: the host always sees the answer; players don't yet.
  await expect(hostPage.getByTestId("clue-answer")).toBeVisible();
  await expect(bobPage.getByTestId("clue-answer")).toHaveCount(0);

  await hostPage.getByTestId("host-open_buzzers").click();
  await expect(bobPage.getByTestId("buzzer")).toBeEnabled();
  await bobPage.getByTestId("buzzer").click();

  // Queue is live everywhere; the HOST decides who answered.
  await expect(bobPage.getByTestId("queued-status")).toBeVisible();
  await expect(hostPage.getByTestId("queue-Bob")).toBeVisible();
  await hostPage.getByTestId("select-buzzer-Bob").click();

  await expect(hostPage.getByTestId("answer-panel")).toBeVisible();
  await expect(hostPage.getByTestId("answerer-name")).toHaveText("Bob");
  await hostPage.getByTestId("host-correct").click();

  await expect(hostPage.getByTestId("score-Bob")).toContainText("$200");
  await expect(bobPage.getByTestId("score-Bob")).toContainText("$200");
  await expect(bobPage.getByTestId("clue-modal")).toHaveCount(0); // back to the board
});

test("multiplayer: queue order syncs to everyone and host undo rewinds the grade", async ({ browser, request }) => {
  const code = await createRoom(request);
  const annPage = await (await browser.newContext()).newPage();
  const bobPage = await (await browser.newContext()).newPage();
  const catPage = await (await browser.newContext()).newPage();

  await enterViaUi(annPage, code, "Ann");
  await enterViaUi(bobPage, code, "Bob");
  await enterViaUi(catPage, code, "Cat");
  await expect(annPage.getByTestId("roster-Cat")).toBeVisible();

  await annPage.getByTestId("host-start_game").click();
  await annPage.getByTestId("host-finish_intro").click();
  await annPage.locator('[data-testid^="clue-"]').first().click();
  await annPage.getByTestId("host-open_buzzers").click();

  await bobPage.getByTestId("buzzer").click();
  await catPage.getByTestId("buzzer").click();

  // Buzz order reaches every screen: Bob first, Cat second.
  await expect(catPage.getByTestId("queue-Bob")).toContainText("#1");
  await expect(catPage.getByTestId("queue-Cat")).toContainText("#2");
  await expect(annPage.getByTestId("queue-Bob")).toBeVisible();

  await annPage.getByTestId("select-buzzer-Bob").click();
  await annPage.getByTestId("host-correct").click();
  await expect(bobPage.getByTestId("score-Bob")).toContainText("$200");
  await expect(catPage.getByTestId("score-Bob")).toContainText("$200");

  // Host misgraded — undo snaps state and score back on every screen.
  await annPage.getByTestId("host-undo").click();
  await expect(annPage.getByTestId("score-Bob")).toContainText("$0");
  await expect(bobPage.getByTestId("score-Bob")).toContainText("$0");
  await expect(catPage.getByTestId("score-Bob")).toContainText("$0");
  await expect(annPage.getByTestId("answer-panel")).toBeVisible(); // back to answering

  // ...and re-grade it correctly this time.
  await annPage.getByTestId("host-correct").click();
  await expect(catPage.getByTestId("score-Bob")).toContainText("$200");
});

test("host benches a player to view-only and hands off host in the lobby", async ({ browser, request }) => {
  const code = await createRoom(request);
  const hostPage = await (await browser.newContext()).newPage();
  const bobPage = await (await browser.newContext()).newPage();

  await enterViaUi(hostPage, code, "Marge"); // first in: the host
  await enterViaUi(bobPage, code, "Bob");
  await expect(hostPage.getByTestId("viewer-toggle-Bob")).toBeVisible(); // lobby manages seats
  await expect(hostPage.getByTestId("promote-Bob")).toBeVisible();

  // bench Bob: his buzzer becomes a view-only note, the scoreboard shows it
  await hostPage.getByTestId("viewer-toggle-Bob").click();
  await expect(bobPage.getByTestId("viewer-note")).toBeVisible();
  await expect(bobPage.getByTestId("buzzer")).toHaveCount(0);
  await expect(hostPage.getByTestId("viewer-badge-Bob")).toBeVisible();
  await expect(hostPage.getByTestId("viewer-toggle-Bob")).toHaveText("Bring back in");

  // and back in
  await hostPage.getByTestId("viewer-toggle-Bob").click();
  await expect(bobPage.getByTestId("buzzer")).toBeVisible();
  await expect(hostPage.getByTestId("viewer-badge-Bob")).toHaveCount(0);

  // lobby host handoff, then hand it straight back
  await hostPage.getByTestId("promote-Bob").click();
  await expect(bobPage.getByTestId("you-are-host")).toBeVisible();
  await expect(hostPage.getByTestId("you-are-host")).toHaveCount(0);
  await expect(bobPage.getByTestId("promote-Marge")).toBeVisible();
  await bobPage.getByTestId("promote-Marge").click();
  await expect(hostPage.getByTestId("you-are-host")).toBeVisible();
  await expect(bobPage.getByTestId("you-are-host")).toHaveCount(0);
});

test("Gamely Jeff Quest: the host flips the marked card to whoever holds the board", async ({ browser, request }) => {
  const code = await createRoom(request);
  const hostPage = await (await browser.newContext()).newPage();
  const bobPage = await (await browser.newContext()).newPage();

  await enterViaUi(hostPage, code, "Ann");
  await enterViaUi(bobPage, code, "Bob");
  await hostPage.getByTestId("host-start_game").click();
  await hostPage.getByTestId("host-finish_intro").click();
  await expect(bobPage.getByTestId("board")).toBeVisible();

  // exactly one marked quest clue on the board — host truth only. The room
  // must find the jackpot by playing, so players see an unmarked board.
  await expect(hostPage.getByTestId("quest-chip")).toHaveCount(1);
  await expect(bobPage.locator('[data-quest="1"]')).toHaveCount(0);
  await expect(bobPage.getByTestId("quest-chip")).toHaveCount(0);

  // nobody holds the board yet — it shows on every screen
  await expect(hostPage.getByTestId("picker-badge")).toContainText("Nobody has the board yet");
  await expect(bobPage.getByTestId("picker-badge")).toContainText("Nobody has the board yet");

  // bankroll Bob with the host's scoreboard controls (each click is +$200)
  await hostPage.getByTestId("adjust-Bob-up").click();
  await hostPage.getByTestId("adjust-Bob-up").click();
  await expect(hostPage.getByTestId("score-Bob").locator("strong")).toHaveText("$400");
  const before = 400;

  // the host decides: give Bob the board with one button
  await hostPage.getByTestId("set-picker-Bob").click();
  await expect(hostPage.getByTestId("picker-badge")).toContainText("Bob picks the next clue");
  await expect(bobPage.getByTestId("picker-badge")).toContainText("Bob picks the next clue");

  // the flip IS the claim — no buzz race, the holder owns it
  await hostPage.locator('[data-quest="1"]').click();
  await expect(hostPage.getByTestId("quest-wager")).toBeVisible();
  await expect(bobPage.getByTestId("quest-question")).toBeVisible(); // the clue text, like the show
  await expect(hostPage.getByTestId("quest-waiting")).toBeVisible(); // host isn't the claimant
  await expect(bobPage.getByTestId("quest-claimant")).toContainText("Bob");
  const max = Number(await bobPage.getByTestId("quest-wager-input").getAttribute("max"));
  expect(max).toBe(before); // show rules: bet what you have
  await bobPage.getByTestId("quest-wager-input").fill(String(max));
  await bobPage.getByTestId("submit-quest-wager").click();
  await expect(bobPage.getByTestId("quest-wager-status")).toContainText(`$${max}`);
  await expect(hostPage.getByTestId("quest-wager-status")).toContainText(`$${max}`);

  // ruled on the wager, not the clue value
  await hostPage.getByTestId("host-begin_quest_answer").click();
  await expect(hostPage.getByTestId("answer-panel")).toBeVisible();
  await expect(hostPage.getByTestId("quest-risk")).toContainText(`$${max}`);
  await hostPage.getByTestId("host-correct").click();

  await expect(hostPage.getByTestId("score-Bob")).toContainText(`$${before + max}`);
  await expect(bobPage.getByTestId("score-Bob")).toContainText(`$${before + max}`);
  await expect(hostPage.getByTestId("quest-chip")).toHaveCount(0); // spent
  await expect(hostPage.getByTestId("picker-badge")).toContainText("Bob picks the next clue"); // holder chooses next
});

test("sharing the hosting: the board-holder chooses and the buzzers open on their own", async ({ browser, request }) => {
  const code = await createRoom(request);
  const hostPage = await (await browser.newContext()).newPage();
  const bobPage = await (await browser.newContext()).newPage();

  await enterViaUi(hostPage, code, "Ann"); // first in: the host
  await enterViaUi(bobPage, code, "Bob");
  await expect(bobPage.getByTestId("host-badge")).toContainText("Ann");

  await hostPage.getByTestId("host-start_game").click();
  await hostPage.getByTestId("host-finish_intro").click();
  await expect(bobPage.getByTestId("board")).toBeVisible();

  // Host runs it all first: the host names the clues, Bob's board is quiet.
  await expect(hostPage.getByTestId("host-mode-badge")).toContainText("Hosting it solo");
  await expect(hostPage.locator('[data-testid^="clue-"]')).not.toHaveCount(0);

  // Share it: the host's board goes quiet (Bob chooses now), the badge shows it.
  await hostPage.getByTestId("host-mode-assisted").click();
  await expect(hostPage.getByTestId("host-mode-badge")).toContainText("Shared hosting");
  await expect(hostPage.getByTestId("assist-note")).toBeVisible();
  // Open board: the hint says whose choice counts on every screen — the
  // host waits, Bob's choice claims the clue.
  await expect(hostPage.getByTestId("pick-hint")).toContainText("Waiting for someone to claim the board");
  await expect(bobPage.getByTestId("pick-hint")).toContainText("Nobody has the board — your call claims it");
  // The pick is the contender's: the generic "give the board" button is gone,
  // only the per-seat no-op buttons when the host is running it solo.
  await expect(hostPage.getByTestId("host-set_picker")).toHaveCount(0);
  // Host's board is quiet, Bob's is live — one side chooses, by design.
  await expect(hostPage.locator('[data-testid^="clue-"]').first()).toBeDisabled();
  await expect(bobPage.locator('[data-testid^="clue-"]').first()).toBeEnabled();

  // Bob chooses himself: no host click, the buzzers open on their own.
  await bobPage.locator('[data-testid^="clue-"]').first().click();
  await expect(hostPage.getByTestId("clue-modal")).toBeVisible();
  await expect(bobPage.getByTestId("buzzer")).toBeEnabled();

  // …and ruling still belongs to the host: the buzzers wait for the call.


  await bobPage.getByTestId("buzzer").click();
  await expect(hostPage.getByTestId("queue-Bob")).toBeVisible();
  await hostPage.getByTestId("select-buzzer-Bob").click();
  await hostPage.getByTestId("host-correct").click();
  await expect(bobPage.getByTestId("score-Bob")).toContainText("$200");
  // Winner holds the board; the hint says whose choice counts next.
  await expect(bobPage.getByTestId("pick-hint")).toContainText("Your pick");
});

test("a miss locks the contender out of the clue and the queue stays callable", async ({ browser, request }) => {
  const code = await createRoom(request);
  const hostPage = await (await browser.newContext()).newPage();
  const bobPage = await (await browser.newContext()).newPage();
  const catPage = await (await browser.newContext()).newPage();

  await enterViaUi(hostPage, code, "Ann"); // first in: the host
  await enterViaUi(bobPage, code, "Bob");
  await enterViaUi(catPage, code, "Cat");
  await hostPage.getByTestId("host-start_game").click();
  await hostPage.getByTestId("host-finish_intro").click();
  await hostPage.locator('[data-testid^="clue-"]').first().click();
  await hostPage.getByTestId("host-open_buzzers").click();

  // Bob rings in, the host calls on him, and he misses.
  await bobPage.getByTestId("buzzer").click();
  await hostPage.getByTestId("select-buzzer-Bob").click();
  await hostPage.getByTestId("host-incorrect").click();

  // The buzzer re-opens, but Bob is locked out of this clue — his buzzer is a
  // dead end, not a trap that lands him in a queue the host can't call on.
  await expect(bobPage.getByTestId("buzzer")).toBeDisabled();
  await expect(bobPage.getByTestId("locked-out-status")).toBeVisible();

  // Cat is still eligible: her buzz is the only one in line, and it's callable.
  await catPage.getByTestId("buzzer").click();
  await expect(hostPage.getByTestId("queue-Cat")).toBeVisible();
  await expect(hostPage.getByTestId("queue-Bob")).toHaveCount(0);
  await hostPage.getByTestId("select-buzzer-Cat").click();
  await hostPage.getByTestId("host-correct").click();
  await expect(catPage.getByTestId("score-Cat")).toContainText("$200");
});

// -- full game ---------------------------------------------------------------
//
// One show, wire to wire: green room -> Jeffpardy -> Bonus Jeffpardy ->
// Ultimate Jeffpardy (category, wagering, clue, reveal) -> final scores.
// Three real contexts, so every snapshot hop rides the live socket. Two clean
// clues are played per board round; the host wraps the round early, the way a
// show ends when the clock runs out.

// Top-row clues, so one can't turn the other over: the marked quest card sits
// at the bottom of each board and is left alone here.
const MAIN_PAIR = ["j-c0-200", "j-c0-400"];
const BONUS_PAIR = ["dj-c0-200", "dj-c0-400"];

async function enterBoard(hostPage, playerPages, code) {
  await enterViaUi(hostPage, code, "Alex"); // first in: the host
  await enterViaUi(playerPages.bob, code, "Bob");
  await enterViaUi(playerPages.carol, code, "Carol");
  await expect(hostPage.getByTestId("roster-Bob")).toBeVisible();
  await expect(hostPage.getByTestId("roster-Carol")).toBeVisible();
  await hostPage.getByTestId("host-start_game").click();
  await expect(hostPage.getByTestId("intro-screen")).toBeVisible();
  await hostPage.getByTestId("host-finish_intro").click();
  await expect(hostPage.getByTestId("board")).toBeVisible();
}

// Pick a clue and run the full buzz -> call -> rule cycle. `answerer` names
// the seat that rings in and wins or loses the clue.
async function playClue(hostPage, answerer, clueId, { correct = true } = {}) {
  await hostPage.getByTestId(`clue-${clueId}`).click();
  await expect(hostPage.getByTestId("clue-modal")).toBeVisible();
  await hostPage.getByTestId("host-open_buzzers").click();
  await answerer.page.getByTestId("buzzer").click();
  await expect(hostPage.getByTestId(`queue-${answerer.name}`)).toBeVisible();
  await hostPage.getByTestId(`select-buzzer-${answerer.name}`).click();
  await expect(hostPage.getByTestId("answerer-name")).toHaveText(answerer.name);
  await hostPage.getByTestId(correct ? "host-correct" : "host-incorrect").click();
}

test("a full game runs from the green room to the final scores", async ({ browser, request }) => {
  const code = await createRoom(request);
  const host = await (await browser.newContext()).newPage();
  const bob = { name: "Bob", page: await (await browser.newContext()).newPage() };
  const carol = { name: "Carol", page: await (await browser.newContext()).newPage() };

  await enterBoard(host, { bob: bob.page, carol: carol.page }, code);

  // -- Jeffpardy ------------------------------------------------------------
  await expect(host.getByTestId("round-banner")).toHaveText("Jeffpardy");
  // One card is marked for the quest — on the host's board alone; the room
  // plays unmarked. Nobody holds the board at the top of a round, so the host
  // seats a picker before any clue changes hands.
  await expect(host.getByTestId("quest-chip")).toHaveCount(1);
  await expect(bob.page.getByTestId("quest-chip")).toHaveCount(0);
  await expect(carol.page.locator('[data-quest="1"]')).toHaveCount(0);
  await expect(host.getByTestId("picker-badge")).toContainText("Nobody has the board yet");
  await host.getByTestId("set-picker-Bob").click();
  await expect(bob.page.getByTestId("picker-badge")).toContainText("Bob picks the next clue");

  // The host names a clue; the answer is the host's alone until it's revealed.
  await host.getByTestId(`clue-${MAIN_PAIR[0]}`).click();
  await expect(bob.page.getByTestId("clue-modal")).not.toContainText("A0-200");
  await host.getByTestId("host-open_buzzers").click();
  await bob.page.getByTestId("buzzer").click();
  await host.getByTestId("select-buzzer-Bob").click();
  await host.getByTestId("host-correct").click();
  await expect(bob.page.getByTestId("score-Bob")).toContainText("$200");
  // A right ruling hands the board to whoever knew it.
  await expect(host.getByTestId("picker-badge")).toContainText("Bob picks the next clue");

  // A second clue, so both seats have skin in the Ultimate wager.
  await playClue(host, carol, MAIN_PAIR[1]);
  await expect(bob.page.getByTestId("score-Carol")).toContainText("$400");

  await host.getByTestId("host-finish_round").click();

  // -- Bonus Jeffpardy ------------------------------------------------------
  await expect(host.getByTestId("intermission")).toBeVisible();
  await host.getByTestId("host-start_next_round").click();
  await expect(host.getByTestId("round-banner")).toHaveText("Bonus Jeffpardy");
  await expect(host.getByTestId("quest-chip")).toHaveCount(1); // re-armed for the new board

  await playClue(host, bob, BONUS_PAIR[0]);
  await expect(bob.page.getByTestId("score-Bob")).toContainText("$400");
  await playClue(host, carol, BONUS_PAIR[1]);
  await expect(bob.page.getByTestId("score-Carol")).toContainText("$800");

  await host.getByTestId("host-finish_round").click();

  // -- Ultimate Jeffpardy ---------------------------------------------------
  // The last board round rolls straight into the Final: category, then wagers.
  await expect(host.getByTestId("final-category")).toBeVisible();
  await expect(host.getByTestId("quest-chip")).toHaveCount(0); // no quest in the Ultimate round
  await expect(host.getByTestId("final-question")).toHaveCount(0); // still sealed
  await host.getByTestId("host-open_final_wagering").click();
  await expect(host.getByTestId("final-wagering")).toBeVisible();

  // The host runs the show and never wagers, so no dead form lands on them.
  await expect(host.getByTestId("wager-input")).toHaveCount(0);
  await expect(host.getByTestId("submit-wager")).toHaveCount(0);

  // The wager is the player's alone: the host sees who is in, not how much.
  await expect(host.getByTestId("wager-placed-Bob")).toBeVisible();
  await expect(bob.page.getByTestId("wager-status")).toHaveCount(0); // Bob hasn't committed
  await bob.page.getByTestId("wager-input").fill("400");
  await bob.page.getByTestId("submit-wager").click();
  await expect(bob.page.getByTestId("wager-status")).toContainText("$400");
  await carol.page.getByTestId("wager-input").fill("300");
  await carol.page.getByTestId("submit-wager").click();

  await host.getByTestId("host-close_final_wagering").click();
  await expect(bob.page.getByTestId("final-question")).toBeVisible();

  // Answers stay private until the reveal.
  await bob.page.getByTestId("final-answer-input").fill("What is a windmill?");
  await bob.page.getByTestId("submit-final-answer").click();
  await expect(bob.page.getByTestId("final-answer-status")).toBeVisible();
  await carol.page.getByTestId("final-answer-input").fill("What is a water wheel?");
  await carol.page.getByTestId("submit-final-answer").click();
  await expect(host.getByTestId("final-answers-list")).toContainText("Bob");
  await expect(bob.page.getByTestId("final-answers-list")).toHaveCount(0); // the host's list, not the room's

  await host.getByTestId("host-start_final_reveal").click();
  await expect(host.getByTestId("final-reveal")).toBeVisible();
  await host.getByTestId("grade-Bob-correct").click();
  await expect(host.getByTestId("graded-Bob")).toBeVisible();
  await host.getByTestId("grade-Carol-incorrect").click();

  // Bob banks 400 + 400 on the line; Carol misses and drops her 300 wager.
  await expect(bob.page.getByTestId("score-Bob")).toContainText("$800");
  await expect(bob.page.getByTestId("score-Carol")).toContainText("$500");

  await host.getByTestId("host-end_game").click();
  await expect(host.getByTestId("review-screen")).toBeVisible();
  await host.getByTestId("host-close_game").click();
  await expect(bob.page.getByTestId("game-over")).toBeVisible();
  await expect(carol.page.getByTestId("game-over")).toBeVisible();
});
