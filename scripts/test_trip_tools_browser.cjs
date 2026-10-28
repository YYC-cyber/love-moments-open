/* Isolated two-account browser acceptance for packing and emergency tools. */
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
  throw new Error("旅行工具测试服务器启动超时");
}
async function login(page, origin, username) {
  await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
  await page.locator("#authUsername").fill(username);
  await page.locator("#authPassword").fill("test123");
  await page.locator("#authSubmit").click();
  await page.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
}
async function request(page, url, method = "GET", body = null) {
  return page.evaluate(async ({ url, method, body }) => {
    const response = await fetch(url, { method, credentials: "same-origin", cache: "no-store", headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
    const value = await response.json(); if (!response.ok) throw new Error(value.error || String(response.status)); return value;
  }, { url, method, body });
}
async function openTools(page, section) {
  await page.locator('[data-nav="trip"]').last().click();
  await page.locator('[data-trip-section="tools"]').click();
  await page.locator(`[data-trip-tool-section="${section}"]`).click();
  await page.locator(`[data-trip-tool-panel="${section}"]`).waitFor({ state: "visible" });
}

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "love-trip-tools-browser-"));
  const port = await freePort(); const origin = `http://127.0.0.1:${port}`;
  const server = spawn(process.env.PYTHON_BINARY || "python3", ["main.py"], {
    cwd: root,
    env: { ...process.env, APP_HOST: "127.0.0.1", APP_PORT: String(port), DATABASE_PATH: path.join(temp, "trip-tools.db"), MEDIA_ROOT: path.join(temp, "media"), BACKUP_ROOT: path.join(temp, "backups"), DEMO_MODE: "true", OPEN_METEO_ENABLED: "false", AMAP_WEB_KEY: "", GEOAPIFY_ENABLED: "false", LLM_API_KEY: "", UI_LLM_API_KEY: "", MEDIA_STORAGE_PROVIDER: "local", LOCAL_ADMIN_NOTIFICATIONS: "false", WATCHDOG_AUTO_RESTART: "false" },
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
    const errors = []; pageA.on("pageerror", error => errors.push(`test: ${error.message}`)); pageB.on("pageerror", error => errors.push(`test1: ${error.message}`));
    pageA.on("dialog", dialog => dialog.accept()); pageB.on("dialog", dialog => dialog.accept());
    await Promise.all([login(pageA, origin, "test"), login(pageB, origin, "test1")]);

    const trip = await request(pageA, "/api/v1/trips/demo-trip");
    await request(pageA, "/api/v1/trips/demo-trip/approve", "POST", { revision: trip.current_revision });
    await request(pageB, "/api/v1/trips/demo-trip/approve", "POST", { revision: trip.current_revision });

    await pageA.evaluate(() => {
      const scope = localStorage.getItem("love-active-scope");
      localStorage.setItem(`love-private:${scope}:love-trip-id`, "trip-does-not-exist");
    });
    await pageA.goto(`${origin}/?screen=trip&trip_tool=emergency&trip_id=demo-trip`, { waitUntil: "domcontentloaded" });
    await pageA.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
    await pageA.waitForFunction(() => document.querySelector("#tripSwitcher")?.value === "demo-trip");
    await pageA.locator('[data-trip-tool-panel="emergency"]').waitFor({ state: "visible" });

    await openTools(pageA, "packing");
    await pageA.locator("#tripPackingLabel").fill("浏览器验收充电线");
    await pageA.locator("#tripPackingForm button").click();
    await pageA.locator("#tripPackingList").getByText("浏览器验收充电线").waitFor();
    await openTools(pageB, "packing");
    await pageB.waitForFunction(() => document.querySelector("#tripPackingList")?.textContent.includes("浏览器验收充电线"));
    const row = pageB.locator(".trip-tool-item", { hasText: "浏览器验收充电线" });
    await row.locator("[data-packing-check]").check();
    await pageA.waitForFunction(() => Array.from(document.querySelectorAll(".trip-tool-item")).some(row => row.textContent.includes("浏览器验收充电线") && row.querySelector("[data-packing-check]")?.checked));

    await openTools(pageA, "emergency");
    await pageA.locator("#tripEmergencyType").selectOption("booking");
    await pageA.locator("#tripEmergencyTitle").fill("仅演示甲可见订单");
    await pageA.locator('[data-emergency-field="provider"]').fill("携程");
    await pageA.locator('[data-emergency-field="booking_reference"]').fill("PRIVATE-BROWSER-REF");
    await pageA.locator("#tripEmergencySaveButton").click();
    await pageA.locator("#tripEmergencyList").getByText("仅演示甲可见订单").waitFor();
    await openTools(pageB, "emergency");
    assert(!(await pageB.locator("#tripEmergencyList").textContent()).includes("仅演示甲可见订单"), "伴侣看到了私密应急卡");

    await openTools(pageA, "emergency");
    await pageA.locator("#tripEmergencyType").selectOption("other");
    await pageA.locator("#tripEmergencyVisibility").selectOption("shared");
    await pageA.locator("#tripEmergencyTitle").fill("双方集合卡");
    await pageA.locator('[data-emergency-field="note"]').fill("酒店大堂集合");
    await pageA.locator("#tripEmergencySaveButton").click();
    await pageA.locator("#tripEmergencyList").getByText("双方集合卡").waitFor();
    await pageB.waitForFunction(() => document.querySelector("#tripEmergencyList")?.textContent.includes("双方集合卡"));

    const sharedCard = pageA.locator(".emergency-item", { hasText: "双方集合卡" });
    await sharedCard.locator("summary").click();
    await sharedCard.locator("[data-action=edit-emergency]").click();
    await pageA.locator("#tripEmergencyVisibility").selectOption("private");
    await pageA.locator("#tripEmergencySaveButton").click();
    await pageB.waitForFunction(() => !document.querySelector("#tripEmergencyList")?.textContent.includes("双方集合卡"));

    await openTools(pageA, "packing");
    const offlineRow = pageA.locator(".trip-tool-item", { hasText: "浏览器验收充电线" });
    const before = await offlineRow.locator("[data-packing-check]").isChecked();
    await contextA.setOffline(true);
    await offlineRow.locator("[data-packing-check]").click();
    assert((await offlineRow.locator("[data-packing-check]").isChecked()) === before, "离线点击错误地乐观修改了行李");
    await contextA.setOffline(false);

    for (const page of [pageA, pageB]) {
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert(overflow <= 1, "旅行工具页面出现横向溢出");
    }
    assert(errors.length === 0, "页面脚本错误：" + errors.join(" | "));
    console.log("旅行工具浏览器验收通过：双账号同步、私密隔离、共享撤回、结构化应急卡、离线拒写和手机/平板布局");
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
