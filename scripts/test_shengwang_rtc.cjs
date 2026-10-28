/* Live two-browser RTC smoke test. Requires the local server and Playwright. */
const fs = require("node:fs");
const { chromium } = require("playwright");

const primaryOrigin = process.env.LOVE_RTC_ORIGIN_A || "http://127.0.0.1:18765";
const partnerOrigin = process.env.LOVE_RTC_ORIGIN_B || "http://localhost:18765";

async function login(page, origin, username, password) {
  await page.goto(`${origin}/?screen=chat&webrtcTestTone=1`, { waitUntil: "domcontentloaded" });
  await page.locator("#authUsername").fill(username);
  await page.locator("#authPassword").fill(password);
  await page.locator("#authSubmit").click();
  await page.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
  await page.locator('[data-nav="chat"]').click();
  await page.locator('[data-screen="chat"].active').waitFor();
}

async function remoteAudioState(page) {
  return page.locator("#remoteCallAudio").evaluate(element => ({
    track: element.dataset.remoteTrack || "",
    bytes: Number(element.dataset.receivedBytes || 0),
    status: document.querySelector("#callStatus")?.textContent || "",
  }));
}

(async () => {
  const launchOptions = {
    headless: true,
    args: [
      "--use-fake-device-for-media-stream",
      "--use-fake-ui-for-media-stream",
      "--autoplay-policy=no-user-gesture-required",
    ],
  };
  const requestedBrowser = process.env.PLAYWRIGHT_CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (fs.existsSync(requestedBrowser)) launchOptions.executablePath = requestedBrowser;
  const browser = await chromium.launch(launchOptions);
  const errors = [];
  let first;
  try {
    const firstContext = await browser.newContext({ serviceWorkers: "block" });
    const secondContext = await browser.newContext({ serviceWorkers: "block" });
    await firstContext.grantPermissions(["microphone"], { origin: primaryOrigin });
    await secondContext.grantPermissions(["microphone"], { origin: partnerOrigin });
    first = await firstContext.newPage();
    const second = await secondContext.newPage();
    for (const [name, page] of [["caller", first], ["callee", second]]) {
      page.on("pageerror", error => errors.push(`${name}: ${error.message}`));
      page.on("console", message => {
        if (message.type() === "error") errors.push(`${name}: ${message.text()}`);
      });
    }

    await Promise.all([
      login(first, primaryOrigin, "test", "test123"),
      login(second, partnerOrigin, "test1", "test123"),
    ]);
    const provider = await first.evaluate(() => fetch("/api/v1/rtc/config").then(response => response.json()));
    if (provider.provider !== "shengwang" || !provider.shengwangAvailable) {
      throw new Error(`声网没有启用：${provider.warning || provider.provider}`);
    }

    await first.locator('[data-action="start-call"]').click();
    await first.waitForFunction(() => document.querySelector("#callStatus")?.textContent.includes("等待 TA 接听"), null, { timeout: 30_000 });
    await second.locator("#callScreen.open [data-action='accept-call']").waitFor({ timeout: 30_000 });
    await second.locator("#callScreen.open [data-action='accept-call']").click();

    try {
      await Promise.all([
        first.waitForFunction(() => document.querySelector("#remoteCallAudio")?.dataset.remoteTrack === "playing", null, { timeout: 45_000 }),
        second.waitForFunction(() => document.querySelector("#remoteCallAudio")?.dataset.remoteTrack === "playing", null, { timeout: 45_000 }),
      ]);
    } catch (error) {
      const diagnostics = await Promise.all([first, second].map(page => page.evaluate(() => ({
        status: document.querySelector("#callStatus")?.textContent || "",
        provider: window.AgoraRTC ? "sdk-loaded" : "sdk-missing",
        connection: typeof state !== "undefined" ? state.agoraClient?.connectionState || "no-client" : "state-unavailable",
        remoteUsers: typeof state !== "undefined" ? state.agoraClient?.remoteUsers?.map(user => ({ uid: user.uid, hasAudio: Boolean(user.audioTrack), published: user.hasAudio })) || [] : [],
        localTrack: typeof state !== "undefined" ? Boolean(state.agoraLocalTrack) : false,
        remoteTrack: document.querySelector("#remoteCallAudio")?.dataset.remoteTrack || "",
        receivedBytes: document.querySelector("#remoteCallAudio")?.dataset.receivedBytes || "0",
      }))));
      throw new Error(`声网远端音轨超时：${JSON.stringify(diagnostics)}；浏览器日志：${errors.join(" | ") || "无"}`);
    }
    await Promise.all([
      first.waitForFunction(() => Number(document.querySelector("#remoteCallAudio")?.dataset.receivedBytes || 0) > 0, null, { timeout: 15_000 }),
      second.waitForFunction(() => Number(document.querySelector("#remoteCallAudio")?.dataset.receivedBytes || 0) > 0, null, { timeout: 15_000 }),
    ]);

    const caller = await remoteAudioState(first);
    const callee = await remoteAudioState(second);
    const muteButton = first.locator("#callScreen.open [data-action='mute']");
    await muteButton.click();
    await first.waitForFunction(() => document.querySelector("#callScreen.open [data-action='mute']")?.classList.contains("selected"));
    await muteButton.click();
    await first.waitForFunction(() => !document.querySelector("#callScreen.open [data-action='mute']")?.classList.contains("selected"));
    await first.locator("#callScreen.open [data-action='end-call']").click();
    await first.waitForFunction(() => !document.querySelector("#callScreen")?.classList.contains("open"), null, { timeout: 10_000 });
    await second.waitForFunction(() => !document.querySelector("#callScreen")?.classList.contains("open"), null, { timeout: 15_000 });

    const fatalErrors = errors.filter(value => !value.includes("favicon.ico"));
    if (fatalErrors.length) throw new Error(`浏览器错误：${fatalErrors.join(" | ")}`);
    console.log(JSON.stringify({
      ok: true,
      provider: provider.provider,
      sdkVersion: provider.sdkVersion,
      origins: [new URL(primaryOrigin).origin, new URL(partnerOrigin).origin],
      caller,
      callee,
    }, null, 2));
  } finally {
    if (first && !first.isClosed()) {
      await first.evaluate(async () => {
        const active = await fetch("/api/v1/calls/active").then(response => response.json()).catch(() => ({}));
        if (active.call) await fetch(`/api/v1/calls/${active.call.id}/end`, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ duration_seconds: 0, note: "自动化测试异常清理" }),
        }).catch(() => {});
      }).catch(() => {});
    }
    await browser.close();
  }
})().catch(error => {
  console.error(error.stack || error.message);
  process.exitCode = 1;
});
