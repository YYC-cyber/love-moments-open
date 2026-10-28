/* Isolated two-account mobile/tablet audit for cooking mode. */
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");
const chromePath = process.env.PLAYWRIGHT_CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const assert = (value, message) => { if (!value) throw new Error(message); };
const freePort = () => new Promise((resolve, reject) => {
  const server = net.createServer();
  server.once("error", reject);
  server.listen(0, "127.0.0.1", () => { const port = server.address().port; server.close(error => error ? reject(error) : resolve(port)); });
});
async function waitForHealth(origin, child) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (child.exitCode !== null) throw new Error("测试服务器提前退出");
    try { if ((await fetch(origin + "/api/health")).ok) return; } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error("测试服务器启动超时");
}
async function login(page, origin, username) {
  await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
  await page.locator("#authUsername").fill(username);
  await page.locator("#authPassword").fill("test123");
  await page.locator("#authSubmit").click();
  await page.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
}
async function api(page, endpoint, method = "GET", body = null) {
  return page.evaluate(async ({ endpoint, method, body }) => {
    const response = await fetch(endpoint, {
      method, credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", ...(method === "GET" ? {} : { "Idempotency-Key": `browser-cook-${crypto.randomUUID()}` }) },
      body: body == null ? undefined : JSON.stringify(body),
    });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || String(response.status));
    return value;
  }, { endpoint, method, body });
}
async function assertInvalidCookingLinkClosesLocally(page, origin, sessionId, label) {
  await page.goto(`${origin}/?cook_session=${encodeURIComponent(sessionId)}`, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
  await page.waitForFunction(() => !new URL(location.href).searchParams.has("cook_session"), null, { timeout: 5000 });
  assert(await page.locator('#cookingScreen[aria-hidden="true"]').count() === 1, `${label}没有关闭烹饪模式`);
  assert(!(await page.locator("#toast").textContent()).includes("暂时无法同步"), `${label}被误判为全局离线`);
  assert(await page.locator('.bottom-nav [data-nav="life"]').count() === 1, `${label}后共同空间不可用`);
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
  assert(!new URL(page.url()).searchParams.has("cook_session"), `${label}刷新后再次循环恢复`);
}

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "love-kitchen-browser-"));
  const port = await freePort();
  const origin = `http://127.0.0.1:${port}`;
  const server = spawn(process.env.PYTHON_BINARY || "python3", ["main.py"], {
    cwd: root,
    env: { ...process.env, APP_HOST: "127.0.0.1", APP_PORT: String(port), DATABASE_PATH: path.join(temp, "kitchen.db"), MEDIA_ROOT: path.join(temp, "media"), BACKUP_ROOT: path.join(temp, "backups"), DEMO_MODE: "true", OPEN_METEO_ENABLED: "false", AMAP_WEB_KEY: "", LLM_API_KEY: "", UI_LLM_API_KEY: "", MEDIA_STORAGE_PROVIDER: "local", LOCAL_ADMIN_NOTIFICATIONS: "false", WATCHDOG_AUTO_RESTART: "false" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let logs = ""; server.stdout.on("data", value => { logs += value; }); server.stderr.on("data", value => { logs += value; });
  let browser;
  try {
    await waitForHealth(origin, server);
    const launchOptions = { headless: true };
    if (fs.existsSync(chromePath)) launchOptions.executablePath = chromePath;
    browser = await chromium.launch(launchOptions);
    const phone = await (await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: "block" })).newPage();
    const tablet = await (await browser.newContext({ viewport: { width: 768, height: 1024 }, serviceWorkers: "block" })).newPage();
    const errors = [];
    phone.on("pageerror", error => errors.push("phone: " + error.message));
    tablet.on("pageerror", error => errors.push("tablet: " + error.message));
    await Promise.all([login(phone, origin, "test"), login(tablet, origin, "test1")]);

    await phone.locator('.bottom-nav [data-nav="life"]').click();
    await phone.locator('.section-tabs [data-life-section="meals"]').click();
    await phone.locator('#recipeList [data-action="view-recipe"]').first().click();
    await phone.locator('#recipeDetailModal.open [data-action="open-cooking"]').click();
    await phone.locator('#cookingScreen[aria-hidden="false"]').waitFor();
    const sessionId = (await api(phone, "/api/v1/cook-sessions?status=active")).items[0].id;
    const partnerSession = await api(tablet, `/api/v1/cook-sessions/${sessionId}`);
    assert(partnerSession.visibility === "shared", "伴侣无法读取共享烹饪会话");

    await phone.locator('[data-action="next-cook-step"]').click();
    await phone.waitForFunction(() => document.querySelector("#cookingStepNumber")?.textContent.includes("第 2"));
    assert((await api(tablet, `/api/v1/cook-sessions/${sessionId}`)).current_step === 1, "步骤没有双端同步");
    const timer = await api(tablet, `/api/v1/cook-sessions/${sessionId}/timers`, "POST", { step_index: 1, duration_seconds: 90, label: "双端计时" });
    assert(Date.parse(timer.ends_at) - Date.parse(timer.started_at) === 90000, "服务端绝对计时错误");
    await phone.evaluate(() => window.dispatchEvent(new Event("focus")));
    await phone.waitForFunction(() => document.querySelector("#cookTimerLive")?.textContent.includes("双端计时"));
    const countdownBefore = await phone.locator("#cookTimerLive strong").first().textContent();
    await phone.waitForTimeout(2200);
    const countdownAfter = await phone.locator("#cookTimerLive strong").first().textContent();
    assert(countdownBefore !== countdownAfter, `真实等待后倒计时没有变化：${countdownBefore}`);
    assert(new URL(phone.url()).searchParams.get("cook_session") === sessionId, "烹饪会话没有写入地址栏");
    await phone.reload({ waitUntil: "domcontentloaded" });
    await phone.locator('#cookingScreen[aria-hidden="false"]').waitFor();
    assert((await phone.locator("#cookTimerLive").textContent()).includes("双端计时"), "刷新后没有恢复同一个绝对计时器");

    const degradedContext = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: "block" });
    await degradedContext.addInitScript(() => {
      Object.defineProperty(navigator, "wakeLock", { configurable: true, value: { request: async () => { throw new Error("blocked"); } } });
      Object.defineProperty(window, "SpeechSynthesisUtterance", { configurable: true, value: function Utterance(text) { this.text = text; } });
      Object.defineProperty(window, "speechSynthesis", { configurable: true, value: { cancel() {}, speak() { throw new Error("blocked"); } } });
    });
    const degraded = await degradedContext.newPage();
    await login(degraded, origin, "test");
    await degraded.locator('#cookingScreen[aria-hidden="false"]').waitFor();
    await degraded.waitForFunction(() => document.querySelector("#cookingStatus")?.textContent.includes("屏幕可能熄灭"));
    await degraded.locator('[data-action="speak-cook-step"]').click();
    await degraded.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("朗读启动失败"));
    await degradedContext.close();

    phone.once("dialog", dialog => dialog.accept());
    await phone.locator('[data-action="complete-cooking"]').click();
    await phone.locator("#cookingPantryPanel:not([hidden])").waitFor();
    const selects = phone.locator("[data-cook-pantry-ref]");
    await selects.nth(0).selectOption("low");
    await selects.nth(1).selectOption("out");
    phone.once("dialog", dialog => dialog.accept());
    await phone.locator('[data-action="apply-cook-pantry"]').click();
    await phone.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("库存状态已更新"));
    assert(await phone.locator('[data-action="undo-cook-pantry"]').count() === 2, "每项库存变化没有各自的撤销回执");
    while (await phone.locator('[data-action="undo-cook-pantry"]').count()) {
      const beforeUndoCount = await phone.locator('[data-action="undo-cook-pantry"]').count();
      await phone.locator('[data-action="undo-cook-pantry"]').first().click();
      await phone.waitForFunction(expected => document.querySelectorAll('[data-action="undo-cook-pantry"]').length === expected, beforeUndoCount - 1);
    }

    await assertInvalidCookingLinkClosesLocally(tablet, origin, "missing-cook-session", "不存在的烹饪链接");

    const privateSession = await api(phone, "/api/v1/cook-sessions", "POST", { recipe_id: partnerSession.recipe_id, visibility: "private" });
    await assertInvalidCookingLinkClosesLocally(tablet, origin, privateSession.id, "伴侣的私密烹饪链接");
    await phone.goto(`${origin}/?cook_session=${encodeURIComponent(privateSession.id)}`, { waitUntil: "domcontentloaded" });
    await phone.locator('#cookingScreen[aria-hidden="false"]').waitFor();
    assert(new URL(phone.url()).searchParams.get("cook_session") === privateSession.id, "有效烹饪链接无法恢复");

    const networkPage = await tablet.context().newPage();
    await networkPage.route(`**/api/v1/cook-sessions/network-failure`, route => route.abort("failed"));
    await networkPage.goto(`${origin}/?cook_session=network-failure`, { waitUntil: "domcontentloaded" });
    await networkPage.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("暂时无法同步"));
    assert(new URL(networkPage.url()).searchParams.get("cook_session") === "network-failure", "网络失败被误当成失效链接清除");
    assert(await networkPage.locator("#authGate.hidden").count() === 1, "网络失败后登录态丢失");
    await networkPage.close();

    await tablet.goto(`${origin}/?cook_session=${encodeURIComponent(sessionId)}`, { waitUntil: "domcontentloaded" });
    await tablet.locator('#cookingScreen[aria-hidden="false"]').waitFor();

    for (const page of [phone, tablet]) {
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert(overflow <= 1, "烹饪页面出现横向溢出");
    }

    const prompts = ["test123", "解除情侣"];
    phone.on("dialog", dialog => dialog.accept(prompts.shift() || ""));
    await phone.evaluate(() => document.querySelector('[data-action="unlink-couple"]').click());
    await phone.waitForFunction(() => !document.querySelector("#pairingView")?.hidden);
    assert(await phone.locator('#cookingScreen[aria-hidden="true"]').count() === 1, "解绑后烹饪模式仍然打开");
    assert(!new URL(phone.url()).searchParams.has("cook_session"), "解绑后烹饪链接没有清理");

    await tablet.reload({ waitUntil: "domcontentloaded" });
    await tablet.waitForFunction(() => !document.querySelector("#pairingView")?.hidden);
    assert(await tablet.locator('#cookingScreen[aria-hidden="true"]').count() === 1, "伴侣解绑后旧链接仍打开烹饪模式");
    assert(!new URL(tablet.url()).searchParams.has("cook_session"), "伴侣解绑后旧烹饪链接没有清理");
    assert(!(await tablet.locator("#toast").textContent()).includes("暂时无法同步"), "伴侣解绑被误判为离线");
    await tablet.reload({ waitUntil: "domcontentloaded" });
    await tablet.waitForFunction(() => !document.querySelector("#pairingView")?.hidden);
    assert(!new URL(tablet.url()).searchParams.has("cook_session"), "伴侣解绑旧链接刷新后循环恢复");
    await tablet.close();

    await phone.reload({ waitUntil: "domcontentloaded" });
    await phone.waitForTimeout(2000);
    const postUnlinkView = await phone.evaluate(() => ({
      gateHidden: document.querySelector("#authGate")?.classList.contains("hidden"),
      pairingHidden: document.querySelector("#pairingView")?.hidden,
      accountHidden: document.querySelector("#authAccountView")?.hidden,
    }));
    assert(postUnlinkView.gateHidden === false && postUnlinkView.accountHidden === false, `解绑刷新后没有回到安全登录页：${JSON.stringify(postUnlinkView)}`);
    assert(!new URL(phone.url()).searchParams.has("cook_session"), "解绑后刷新又恢复旧烹饪链接");
    assert(errors.length === 0, "页面脚本错误：" + errors.join(" | "));
    console.log("双端烹饪模式验收通过：快照、步骤同步、绝对多计时器、人工库存、手机/平板布局");
  } catch (error) {
    if (logs.trim()) console.error(logs.slice(-8000));
    throw error;
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) server.kill("SIGTERM");
    if (server.exitCode === null) await new Promise(resolve => server.once("exit", resolve));
    fs.rmSync(temp, { recursive: true, force: true });
  }
})().catch(error => { console.error(error.stack || error.message); process.exitCode = 1; });
