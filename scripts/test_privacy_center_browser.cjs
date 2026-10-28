/* Isolated two-account browser acceptance for AI privacy grants and the real IndexedDB outbox. */
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
  const socket = net.createServer(); socket.once("error", reject);
  socket.listen(0, "127.0.0.1", () => { const port = socket.address().port; socket.close(error => error ? reject(error) : resolve(port)); });
});
async function waitForHealth(origin, child) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (child.exitCode !== null) throw new Error(`测试服务器提前退出 (${child.exitCode})`);
    try { if ((await fetch(origin + "/api/health")).ok) return; } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error("隐私中心测试服务器启动超时");
}
async function login(page, origin, username) {
  await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
  await page.locator("#authUsername").fill(username);
  await page.locator("#authPassword").fill("test123");
  await page.locator("#authSubmit").click();
  await page.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
}
async function request(page, url, method = "GET", body = null, allowFailure = false) {
  const result = await page.evaluate(async ({ url, method, body }) => {
    const response = await fetch(url, { method, credentials: "same-origin", cache: "no-store", headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
    return { status: response.status, body: await response.json() };
  }, { url, method, body });
  if (!allowFailure && result.status >= 400) throw new Error(result.body.error || String(result.status));
  return allowFailure ? result : result.body;
}
async function openAccount(page) {
  await page.locator('[data-nav="life"]').last().click();
  await page.locator('[data-screen="life"].active .settings').click();
  await page.locator("#accountModal.open").waitFor();
  await page.locator("#deviceList .device-row").first().waitFor();
}
async function closeAccount(page) {
  await page.locator('#accountModal [data-action="close-modal"]').click();
  await page.locator("#accountModal.open").waitFor({ state: "detached" });
}

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "love-privacy-browser-"));
  const port = await freePort(); const origin = `http://127.0.0.1:${port}`;
  const server = spawn(process.env.PYTHON_BINARY || "python3", ["main.py"], {
    cwd: root,
    env: { ...process.env, APP_HOST: "127.0.0.1", APP_PORT: String(port), DATABASE_PATH: path.join(temp, "privacy.db"), MEDIA_ROOT: path.join(temp, "media"), BACKUP_ROOT: path.join(temp, "backups"), DEMO_MODE: "true", OPEN_METEO_ENABLED: "false", AMAP_WEB_KEY: "", GEOAPIFY_ENABLED: "false", LLM_API_KEY: "", UI_LLM_API_KEY: "", MEDIA_STORAGE_PROVIDER: "local", LOCAL_ADMIN_NOTIFICATIONS: "false", WATCHDOG_AUTO_RESTART: "false" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let logs = ""; server.stdout.on("data", value => { logs += value; }); server.stderr.on("data", value => { logs += value; });
  let browser;
  try {
    await waitForHealth(origin, server);
    const launchOptions = { headless: true }; if (fs.existsSync(chromePath)) launchOptions.executablePath = chromePath;
    browser = await chromium.launch(launchOptions);
    const contextA = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: "block" });
    const contextB = await browser.newContext({ viewport: { width: 768, height: 1024 }, serviceWorkers: "block" });
    const pageA = await contextA.newPage(); const pageB = await contextB.newPage();
    pageA.setDefaultTimeout(7000); pageB.setDefaultTimeout(7000);
    const errors = []; pageA.on("pageerror", error => errors.push(`test: ${error.message}`)); pageB.on("pageerror", error => errors.push(`test1: ${error.message}`));
    pageA.on("dialog", dialog => dialog.accept());
    await Promise.all([login(pageA, origin, "test"), login(pageB, origin, "test1")]);

    let job = await request(pageA, "/api/v1/ui/timeline/compose", "POST", { intent: "只整理我明确选择的瞬间", resourceRefs: ["moment:moment-1"] });
    for (let attempt = 0; !job.manifest && attempt < 80; attempt += 1) {
      await new Promise(resolve => setTimeout(resolve, 100));
      job = await request(pageA, `/api/v1/ui/jobs/${job.id}`);
    }
    assert(job.manifest, "按次授权的智能整理任务未完成");
    const privacyA = await request(pageA, "/api/v1/account/privacy");
    const ownGrant = privacyA.grants.find(item => item.resource_id === "moment-1" && item.status === "active");
    assert(ownGrant, "当前账号没有看到自己创建的按次授权");
    assert(!JSON.stringify(privacyA).includes("下次还要一起追日落"), "隐私中心返回了瞬间正文");
    const privacyB = await request(pageB, "/api/v1/account/privacy");
    assert(!privacyB.grants.some(item => item.id === ownGrant.id), "伴侣看到了当前账号的个人 AI 授权");
    const forbidden = await request(pageB, `/api/v1/account/privacy/grants/${ownGrant.id}/revoke`, "POST", { expected_version: ownGrant.version }, true);
    assert(forbidden.status === 403, `伴侣撤回授权应返回 403，实际 ${forbidden.status}`);

    await openAccount(pageA);
    const grantRow = pageA.locator("#privacyGrants .device-row", { hasText: "恋爱瞬间" }).first();
    await grantRow.waitFor();
    assert(!(await grantRow.textContent()).includes("下次还要一起追日落"), "账号中心显示了授权资源正文");
    await grantRow.locator('[data-action="revoke-ai-grant"]').click();
    await pageA.waitForFunction(id => [...document.querySelectorAll("#privacyGrants .device-row")].some(row => row.textContent.includes("恋爱瞬间") && row.textContent.includes("已撤回") && !row.querySelector(`[data-id="${id}"]`)), ownGrant.id);
    const replay = await request(pageA, `/api/v1/account/privacy/grants/${ownGrant.id}/revoke`, "POST", { expected_version: ownGrant.version });
    assert(replay.idempotent === true && replay.status === "revoked", "授权撤回重试不是幂等响应");
    await closeAccount(pageA);

    const abortMessages = route => route.request().method() === "POST" ? route.abort("failed") : route.continue();
    await pageA.route("**/api/v1/messages", abortMessages);
    await pageA.locator('[data-nav="chat"]').last().click();
    const queuedMarker = `隐私中心待同步-${Date.now()}`;
    await pageA.locator("#chatInput").fill(queuedMarker);
    await pageA.locator("#chatForm .send").click();
    await pageA.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("离线保存"));
    await openAccount(pageA);
    let queueRow = pageA.locator("#offlineSyncStatus .device-row", { hasText: "待发送文字消息" });
    await queueRow.waitFor();
    await queueRow.locator('[data-action="retry-offline-mutation"]').click();
    await pageA.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("原记录仍保留"));
    queueRow = pageA.locator("#offlineSyncStatus .device-row", { hasText: "待发送文字消息" });
    assert(await queueRow.count() === 1, "失败重试错误地删除了离线原记录");
    await pageA.unroute("**/api/v1/messages", abortMessages);
    await queueRow.locator('[data-action="retry-offline-mutation"]').click();
    await pageA.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("服务器确认同步"));
    assert(await pageA.locator("#offlineSyncStatus .device-row", { hasText: "待发送文字消息" }).count() === 0, "服务器确认后离线记录仍未清理");
    assert((await request(pageB, "/api/v1/messages")).items.some(item => item.content === queuedMarker), "重试成功的消息没有到达伴侣账号");
    await closeAccount(pageA);

    await pageA.route("**/api/v1/messages", abortMessages);
    await pageA.locator('[data-nav="chat"]').last().click();
    const discardedMarker = `隐私中心丢弃-${Date.now()}`;
    await pageA.locator("#chatInput").fill(discardedMarker);
    await pageA.locator("#chatForm .send").click();
    await pageA.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("离线保存"));
    await openAccount(pageA);
    const discardedRow = pageA.locator("#offlineSyncStatus .device-row", { hasText: "待发送文字消息" });
    await discardedRow.locator('[data-action="discard-offline-mutation"]').click();
    await pageA.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("不会发送到服务器"));
    assert(await pageA.locator("#offlineSyncStatus .device-row", { hasText: "待发送文字消息" }).count() === 0, "明确丢弃后离线记录仍存在");
    await pageA.unroute("**/api/v1/messages", abortMessages);
    assert(!(await request(pageB, "/api/v1/messages")).items.some(item => item.content === discardedMarker), "明确丢弃的消息仍被发送给伴侣");

    await closeAccount(pageA);
    await pageA.route("**/api/v1/messages", abortMessages);
    await pageA.locator('[data-nav="chat"]').last().click();
    const inFlightMarker = `隐私中心发送中-${Date.now()}`;
    await pageA.locator("#chatInput").fill(inFlightMarker);
    await pageA.locator("#chatForm .send").click();
    await pageA.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("离线保存"));
    await pageA.unroute("**/api/v1/messages", abortMessages);
    let releasePost;
    const holdPost = async route => {
      if (route.request().method() !== "POST") return route.continue();
      await new Promise(resolve => { releasePost = resolve; });
      return route.continue();
    };
    await pageA.route("**/api/v1/messages", holdPost);
    await pageA.evaluate(() => { flushMealMutations(); });
    await pageA.waitForFunction(async () => {
      const db = await openMealCache();
      const rows = await new Promise(resolve => { const request = db.transaction("pending").objectStore("pending").getAll(); request.onsuccess = () => resolve(request.result || []); });
      db.close();
      return rows.some(item => Number(item.inFlightRevision) === Number(item.revision));
    });
    await openAccount(pageA);
    const sendingRow = pageA.locator("#offlineSyncStatus .device-row", { hasText: "待发送文字消息" });
    await sendingRow.waitFor();
    assert((await sendingRow.textContent()).includes("发送中 · 不可丢弃"), "发送中操作没有明确状态");
    assert(await sendingRow.locator('[data-action="discard-offline-mutation"]').count() === 0, "发送中操作仍显示丢弃按钮");
    releasePost();
    let delivered = false;
    for (let attempt = 0; attempt < 30 && !delivered; attempt += 1) {
      await new Promise(resolve => setTimeout(resolve, 100));
      delivered = (await request(pageB, "/api/v1/messages")).items.some(item => item.content === inFlightMarker);
    }
    assert(delivered, "发送中的消息没有得到服务器回执");
    await pageA.evaluate(() => loadAccountCenter());
    await pageA.unroute("**/api/v1/messages", holdPost);
    await pageA.waitForFunction(() => !document.querySelector("#offlineSyncStatus .mock-badge")?.textContent.includes("发送中"));
    await closeAccount(pageA);

    const abortPrivacy = route => route.abort("failed");
    await pageA.route("**/api/v1/account/privacy", abortPrivacy);
    await openAccount(pageA);
    assert(await pageA.locator("#deviceList .device-row").count() > 0, "隐私接口失败时设备中心也不可用");
    assert((await pageA.locator("#privacyUsage").textContent()).includes("暂时无法读取"), "隐私接口失败没有分区降级提示");
    await pageA.unroute("**/api/v1/account/privacy", abortPrivacy);

    const accountOverflow = await pageA.locator("#accountModal .sheet").evaluate(element => element.scrollWidth - element.clientWidth);
    assert(accountOverflow <= 1, "手机账号中心出现横向溢出");

    assert(errors.length === 0, "页面脚本错误：" + errors.join(" | "));
    console.log("隐私中心浏览器验收通过：双账号授权隔离、撤回幂等、正文不泄露、离线失败保留、真实重试与明确丢弃");
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
