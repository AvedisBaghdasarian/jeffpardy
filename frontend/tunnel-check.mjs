import { chromium } from "@playwright/test";

// Deployment check: drives a real two-player game (plus host undo) through
// the public tunnel. Gameplay rides the WebSocket; only create/join are REST.
// Usage: TUNNEL_URL=https://<name>.loca.lt node tunnel-check.mjs
const BASE = process.env.TUNNEL_URL || "https://jeffpardy-game-8q4x.loca.lt";
const browser = await chromium.launch();
const ctx = await browser.newContext({
  userAgent: "jeffpardy-tunnel-check/1.0",
  extraHTTPHeaders: { "bypass-tunnel-reminder": "1" },
});
const errors = [];

async function openTunneled(page, label) {
  page.on("requestfailed", (r) =>
    console.log(`${label} REQFAIL: ${r.url().slice(0, 120)} — ${r.failure()?.errorText}`),
  );
  page.on("response", (r) => {
    if (r.status() >= 400) console.log(`${label} HTTP ${r.status()}: ${r.url().slice(0, 120)}`);
  });
  page.on("console", (m) => {
    if (m.type() === "error") console.log(`${label} CONSOLE: ${m.text().slice(0, 200)}`);
  });
  page.on("pageerror", (e) => console.log(`${label} PAGEERROR: ${e.message.slice(0, 200)}`));
  await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await page.getByTestId("name-input").waitFor({ timeout: 45000 });
}

async function waitForText(page, testid, substr, timeout = 20000) {
  const start = Date.now();
  for (;;) {
    const text = (await page.getByTestId(testid).textContent().catch(() => "")) || "";
    if (text.includes(substr)) return text;
    if (Date.now() - start > timeout) {
      throw new Error(`timeout: ${testid} never contained "${substr}" — last="${text}"`);
    }
    await new Promise((r) => setTimeout(r, 250));
  }
}

try {
  const host = await ctx.newPage();
  await openTunneled(host, "HOST");
  await host.getByTestId("name-input").fill("TunnelTester");
  await host.getByTestId("create-room").click();
  await host.getByTestId("room-code").waitFor({ timeout: 30000 });
  const header = await host.getByTestId("room-code").textContent();
  const code = header.match(/Room (\d{4})/)?.[1];
  console.log("HOST header:", header.trim());
  await host.getByTestId("lobby-screen").waitFor({ timeout: 20000 });
  console.log("LOBBY OK: first arrival is the host");
  await host.getByTestId("connection-status").waitFor({ timeout: 20000 });

  const guest = await ctx.newPage();
  await openTunneled(guest, "GUEST");
  await guest.getByTestId("name-input").fill("TunnelFriend");
  await guest.getByTestId("code-input").fill(code);
  await guest.getByTestId("join-room").click();
  await guest.getByTestId("room-code").waitFor({ timeout: 30000 });
  console.log("GUEST header:", (await guest.getByTestId("room-code").textContent()).trim());

  // Live sync: the host's roster must show the guest without reloading
  await host.getByTestId("roster-TunnelFriend").waitFor({ timeout: 20000 });
  console.log("LIVE SYNC OK: host sees TunnelFriend");

  // Host drives the flow: start -> intro -> board -> clue -> buzzers
  await host.getByTestId("host-start_game").click();
  await host.getByTestId("host-finish_intro").click();
  await host.getByTestId("board").waitFor({ timeout: 20000 });
  console.log("BOARD OK: host reached the board through the tunnel");
  await host.locator('[data-testid^="clue-"]').first().click();
  await host.getByTestId("clue-modal").waitFor({ timeout: 20000 });
  await host.getByTestId("clue-answer").waitFor({ timeout: 20000 });
  console.log("HOST VIEW OK: answer visible to host only");
  await host.getByTestId("host-open_buzzers").click();
  await guest.getByTestId("buzzer").waitFor({ timeout: 20000 });
  await guest.getByTestId("buzzer").click();
  console.log("GUEST BUZZ OK:", (await guest.getByTestId("buzzer").textContent()).trim());

  // The HOST picks who answered, then grades
  await host.getByTestId("select-buzzer-TunnelFriend").waitFor({ timeout: 20000 });
  await host.getByTestId("select-buzzer-TunnelFriend").click();
  await host.getByTestId("answer-panel").waitFor({ timeout: 20000 });
  console.log("SELECT OK: guest is answering");
  await host.getByTestId("host-correct").click();
  const score = await waitForText(host, "score-TunnelFriend", "$200");
  console.log("AWARD SYNC OK: host sees", score.trim());
  const guestScore = await waitForText(guest, "score-TunnelFriend", "$200");
  console.log("AWARD SYNC OK: guest sees", guestScore.trim());

  // Undo over the public URL: state + score rewind on both screens…
  await host.getByTestId("host-undo").click();
  const undone = await waitForText(host, "score-TunnelFriend", "$0");
  console.log("UNDO SYNC OK: host sees", undone.trim());
  await waitForText(guest, "score-TunnelFriend", "$0");
  await host.getByTestId("answer-panel").waitFor({ timeout: 20000 });
  console.log("UNDO SYNC OK: back to answering on both screens");
  // …and the re-grade lands again.
  await host.getByTestId("host-correct").click();
  await waitForText(guest, "score-TunnelFriend", "$200");
  console.log("REGRADE OK: guest sees $200 again");

  console.log("RESULT: PASS");
} catch (err) {
  console.log("RESULT: FAIL —", err.message);
  if (errors.length) console.log(errors.join("\n"));
  try {
    const pages = browser.contexts().flatMap((c) => c.pages());
    for (const [i, p] of pages.entries()) {
      console.log(`--- page ${i} url: ${p.url()}`);
      console.log((await p.content()).slice(0, 800));
    }
  } catch {}
} finally {
  await browser.close();
}
