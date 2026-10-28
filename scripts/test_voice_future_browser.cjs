/* Isolated two-account browser audit for voice memories and future letters. */
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");
const chromePath = process.env.PLAYWRIGHT_CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const assert = (value, message) => { if (!value) throw new Error(message); };

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      server.close(error => error ? reject(error) : resolve(port));
    });
  });
}

async function waitForHealth(origin, child) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (child.exitCode !== null) throw new Error("测试服务器提前退出：" + child.exitCode);
    try { if ((await fetch(origin + "/api/health")).ok) return; } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error("测试服务器启动超时");
}

async function login(page, origin, username) {
  await page.goto(origin + "/?screen=timeline&memory_tools=1", { waitUntil: "domcontentloaded" });
  await page.locator("#authUsername").fill(username);
  await page.locator("#authPassword").fill("test123");
  await page.locator("#authSubmit").click();
  await page.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
  await page.locator('[data-screen="timeline"].active').waitFor();
  await page.locator(".memory-tools").waitFor();
}

async function api(page, endpoint, options = {}) {
  return page.evaluate(async ({ endpointValue, requestOptions }) => {
    const response = await fetch(endpointValue, {
      credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", ...(requestOptions.headers || {}) },
      ...requestOptions,
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || String(response.status));
    return body;
  }, { endpointValue: endpoint, requestOptions: options });
}

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "love-voice-future-browser-"));
  const port = await freePort();
  const origin = "http://127.0.0.1:" + port;
  const env = {
    ...process.env, APP_HOST: "127.0.0.1", APP_PORT: String(port),
    DATABASE_PATH: path.join(temp, "voice-future.db"), MEDIA_ROOT: path.join(temp, "media"),
    BACKUP_ROOT: path.join(temp, "backups"), DEMO_MODE: "true",
    OPEN_METEO_ENABLED: "false", AMAP_WEB_KEY: "", LLM_API_KEY: "", UI_LLM_API_KEY: "",
    MEDIA_STORAGE_PROVIDER: "local", LOCAL_ADMIN_NOTIFICATIONS: "false", WATCHDOG_AUTO_RESTART: "false",
  };
  const server = spawn(process.env.PYTHON_BINARY || "python3", ["main.py"], { cwd: root, env, stdio: ["ignore", "pipe", "pipe"] });
  let logs = "";
  server.stdout.on("data", value => { logs += value.toString(); });
  server.stderr.on("data", value => { logs += value.toString(); });
  let browser;
  const errors = [];
  try {
    await waitForHealth(origin, server);
    const launchOptions = { headless: true, args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"] };
    if (fs.existsSync(chromePath)) launchOptions.executablePath = chromePath;
    browser = await chromium.launch(launchOptions);
    const contextA = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: "block", permissions: ["microphone"] });
    const contextB = await browser.newContext({ viewport: { width: 768, height: 1024 }, serviceWorkers: "block" });
    await contextA.addInitScript(() => {
      class LocalRecognition {
        start() { window.__loveTestRecognition = this; }
        stop() {}
      }
      Object.defineProperty(window, "SpeechRecognition", { configurable: true, value: LocalRecognition });
      Object.defineProperty(window, "webkitSpeechRecognition", { configurable: true, value: LocalRecognition });
    });
    const pageA = await contextA.newPage();
    const pageB = await contextB.newPage();
    pageA.on("pageerror", error => errors.push("test: " + error.message));
    pageB.on("pageerror", error => errors.push("test1: " + error.message));
    pageA.on("response", async response => {
      if (response.url().includes("/api/v1/media") && response.status() >= 400) {
        errors.push(`media ${response.status()}: ${await response.text().catch(() => "")}`);
      }
    });
    await Promise.all([login(pageA, origin, "test"), login(pageB, origin, "test1")]);

    const voiceMarker = "浏览器原声-" + Date.now();
    await pageA.locator('[data-action="open-voice-memory"]').click();
    await pageA.locator("#voiceMemoryModal.open").waitFor();
    await pageA.locator("#voiceMemoryName").fill(voiceMarker);
    await pageA.locator("#voiceTranscript").fill("这是本人确认的语音文字");
    await pageA.locator('[data-action="start-voice-recording"]').click();
    await pageA.waitForTimeout(700);
    await pageA.evaluate(() => window.__loveTestRecognition.onresult({
      resultIndex: 0,
      results: [{ 0: { transcript: "，随后又有本地识别" }, length: 1 }],
    }));
    assert(await pageA.evaluate(() => state.voiceTranscriptEdited && state.voiceTranscriptMethod === "manual"), "人工编辑后被后续浏览器识别错误提升了来源");
    await pageA.locator('[data-action="stop-voice-recording"]').click();
    await pageA.locator("#voicePreview:not([hidden])").waitFor();
    await pageA.locator('[data-action="save-voice-memory"]').click();
    await pageA.waitForTimeout(1000);
    if (await pageA.locator("#voiceMemoryModal.open").count()) {
      throw new Error("录音保存失败：" + (await pageA.locator("#toast").textContent()) + " " + errors.join(" | "));
    }
    await pageA.locator("#voiceMemoryModal").waitFor({ state: "hidden" });
    await pageA.waitForFunction(marker => document.querySelector("#timelineBody")?.textContent.includes(marker), voiceMarker);
    const savedVoice = await pageA.evaluate(async marker => {
      const moment = state.data.moments.find(item => item.title === marker);
      const response = await fetch(`/api/v1/voice-memories/${encodeURIComponent(moment.id)}`);
      return response.json();
    }, voiceMarker);
    assert(savedVoice.transcript.method === "manual" && savedVoice.transcript.source_type === "human", "混合编辑的语音文字来源未保持为本人填写");

    await pageB.reload({ waitUntil: "domcontentloaded" });
    await pageB.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
    await pageB.locator('[data-screen="timeline"]').click();
    await pageB.waitForFunction(marker => document.querySelector("#timelineBody")?.textContent.includes(marker), voiceMarker);
    assert(!await pageB.locator(".phone-shell").getAttribute("inert"), "页面在没有弹窗时仍处于不可交互状态");
    await pageB.evaluate(marker => {
      const article = [...document.querySelectorAll("#timelineBody article")].find(node => node.textContent.includes(marker));
      const button = article?.querySelector('[data-action="open-shared-detail"]');
      if (!button) throw new Error("找不到语音回忆详情按钮");
      button.click();
    }, voiceMarker);
    await pageB.locator("#sharedDetailModal.open audio").waitFor();
    assert((await pageB.locator("#sharedDetailContent").textContent()).includes("这是本人确认的语音文字"), "伴侣没有看到真实保存的语音文字");
    assert(await pageB.locator('[data-action="save-voice-transcript-correction"]').count() === 0, "伴侣错误获得了语音文字编辑按钮");
    await pageB.locator('#sharedDetailModal [data-action="close-modal"]').click();

    const futureMarker = "未来秘密-" + Date.now();
    await pageA.locator('[data-action="open-future-letter"]').click();
    await pageA.locator("#futureLetterName").fill("给未来的我们");
    await pageA.locator("#futureLetterBody").fill(futureMarker);
    await pageA.locator("#futureLetterForm button").click();
    await pageA.locator("#futureLetterDetailModal.open").waitFor();
    assert((await api(pageB, "/api/v1/future-letters?scope=delivered")).total === 0, "未来信在投递前向收件人泄露了数量");
    await pageA.locator('[data-action="prepare-seal-future-letter"]').click();
    await pageA.locator('[data-action="seal-future-letter"]').click();
    await pageA.locator("#futureLetterModal").waitFor({ state: "hidden" });
    assert((await api(pageB, "/api/v1/future-letters?scope=delivered")).total === 0, "封存信在到期前对收件人可见");
    await pageA.locator(".future-letter-card", { hasText: "给未来的我们" }).click();
    await pageA.waitForFunction(marker => {
      const detail = document.getElementById("futureLetterDetail");
      return document.getElementById("futureLetterDetailModal")?.classList.contains("open")
        && detail && !detail.textContent.includes(marker)
        && detail.textContent.includes("正文已安全封存");
    }, futureMarker);
    assert(!(await pageA.locator("#futureLetterDetail").textContent()).includes(futureMarker), "作者封存后仍能预览正文");
    await pageA.locator('[data-action="cancel-future-letter"]').click();
    await pageA.locator("#futureLetterDetailModal").waitFor({ state: "hidden" });
    await pageA.locator(".future-letter-card", { hasText: "私密草稿" }).waitFor();
    assert((await pageA.locator(".future-letter-card", { hasText: "私密草稿" }).count()) > 0, "取消封存没有恢复私密草稿");

    for (const page of [pageA, pageB]) {
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert(overflow <= 1, "语音/未来信页面出现横向溢出");
    }
    assert(errors.length === 0, "页面脚本错误：" + errors.join(" | "));
    console.log("语音与未来信浏览器验收通过：真实录音、混合编辑来源降级、双账号播放/文字权限、封存零泄露、取消恢复、手机和平板布局");
  } catch (error) {
    if (typeof errors !== "undefined" && errors.length) console.error(errors.join("\n"));
    if (logs.trim()) console.error(logs.slice(-8000));
    throw error;
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) server.kill("SIGTERM");
    if (server.exitCode === null) await new Promise(resolve => server.once("exit", resolve));
    fs.rmSync(temp, { recursive: true, force: true });
  }
})().catch(error => { console.error(error.stack || error.message); process.exitCode = 1; });
