/* Isolated two-account browser audit for the couple life assistant. */
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");
const chromePath = process.env.PLAYWRIGHT_CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

function assert(value, message) {
  if (!value) throw new Error(message);
}

function freePort() {
  return new Promise((resolve, reject) => {
    const socket = net.createServer();
    socket.once("error", reject);
    socket.listen(0, "127.0.0.1", () => {
      const port = socket.address().port;
      socket.close(error => error ? reject(error) : resolve(port));
    });
  });
}

async function waitForHealth(origin, child) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (child.exitCode !== null) throw new Error("测试服务器提前退出：" + child.exitCode);
    try {
      const response = await fetch(origin + "/api/health");
      if (response.ok) return;
    } catch (_) {}
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
  await page.locator("#lifeAssistantForm").waitFor();
}

async function api(page, endpoint) {
  return page.evaluate(async endpointValue => {
    const response = await fetch(endpointValue, { credentials: "same-origin", cache: "no-store" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || String(response.status));
    return body;
  }, endpoint);
}

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "love-assistant-browser-"));
  const port = await freePort();
  const origin = "http://127.0.0.1:" + port;
  const env = {
    ...process.env,
    APP_HOST: "127.0.0.1",
    APP_PORT: String(port),
    DATABASE_PATH: path.join(temp, "assistant.db"),
    MEDIA_ROOT: path.join(temp, "media"),
    BACKUP_ROOT: path.join(temp, "backups"),
    DEMO_MODE: "true",
    OPEN_METEO_ENABLED: "false",
    AMAP_WEB_KEY: "",
    LLM_API_KEY: "",
    UI_LLM_API_KEY: "",
    MEDIA_STORAGE_PROVIDER: "local",
    LOCAL_ADMIN_NOTIFICATIONS: "false",
    WATCHDOG_AUTO_RESTART: "false",
  };
  const server = spawn(process.env.PYTHON_BINARY || "python3", ["main.py"], {
    cwd: root,
    env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let logs = "";
  server.stdout.on("data", value => { logs += value.toString(); });
  server.stderr.on("data", value => { logs += value.toString(); });
  let browser;
  try {
    await waitForHealth(origin, server);
    const launchOptions = { headless: true };
    if (fs.existsSync(chromePath)) launchOptions.executablePath = chromePath;
    browser = await chromium.launch(launchOptions);
    const contextA = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: "block" });
    const contextB = await browser.newContext({ viewport: { width: 768, height: 1024 }, serviceWorkers: "block" });
    const pageA = await contextA.newPage();
    const pageB = await contextB.newPage();
    const errors = [];
    for (const [name, page] of [["test", pageA], ["test1", pageB]]) {
      page.on("pageerror", error => errors.push(name + ": " + error.message));
    }
    await Promise.all([login(pageA, origin, "test"), login(pageB, origin, "test1")]);

    await pageA.locator("#lifeAssistantInput").fill("明天晚上想吃番茄炒蛋，加入日历并生成采购清单");
    await pageA.locator("#lifeAssistantForm button").click();
    await pageA.locator("#assistantPlanModal.open").waitFor();
    const planButtonA = pageA.locator('[data-action="confirm-assistant-plan"]');
    await planButtonA.waitFor();
    const planId = await planButtonA.getAttribute("data-id");
    assert(planId, "创建方案后没有真实方案 ID");
    assert((await pageA.locator("#assistantPlanContent").textContent()).includes("双方确认"), "方案详情没有说明双人确认规则");

    await pageB.locator('[data-action="open-assistant-plans"]').click();
    await pageB.locator('[data-action="open-assistant-plan"][data-id="' + planId + '"]').click();
    await pageB.locator('[data-action="confirm-assistant-plan"][data-id="' + planId + '"]').waitFor();
    assert((await pageB.locator("#assistantPlanContent").textContent()).includes("番茄炒蛋"), "伴侣确认前看不到同一方案");

    await pageA.locator('[data-action="confirm-assistant-plan"][data-id="' + planId + '"]').click();
    await pageA.waitForFunction(() => document.querySelector("#assistantPlanContent")?.textContent.includes("我已确认"));
    const calendarBefore = (await api(pageA, "/api/v1/calendar")).items.length;

    await pageB.locator('[data-action="confirm-assistant-plan"][data-id="' + planId + '"]').click();
    await pageB.waitForFunction(() => document.querySelector("#assistantPlanContent")?.textContent.includes("已生效"));
    const calendarAfter = (await api(pageA, "/api/v1/calendar")).items;
    assert(calendarAfter.length === calendarBefore + 1, "双方确认没有且仅写入一条日历记录");
    assert(calendarAfter.some(item => item.title.includes("番茄炒蛋")), "生效方案没有写入真实菜名");
    const shopping = await api(pageB, "/api/v1/shopping-lists/active");
    assert((shopping.items || []).length > 0, "生效方案没有生成采购清单");

    await pageB.locator('#assistantPlanModal [data-action="close-modal"]').click();
    await pageB.locator('[data-action="open-notifications"]').click();
    await pageB.locator("#notificationCenterModal.open").waitFor();
    assert(await pageB.locator(".notification-center-item").count() > 0, "通知中心没有持久记录");
    await pageB.locator('[data-action="read-all-notifications"]').click();
    await pageB.waitForFunction(() => document.querySelector("#notificationUnreadBadge")?.hidden);

    for (const page of [pageA, pageB]) {
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert(overflow <= 1, "生活助理页面出现横向溢出");
    }
    await pageA.setViewportSize({ width: 1440, height: 900 });
    const desktopOverflow = await pageA.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert(desktopOverflow <= 1, "生活助理桌面布局出现横向溢出");
    assert(errors.length === 0, "页面脚本错误：" + errors.join(" | "));
    console.log("双人生活助理浏览器验收通过：共享预览、双人确认、原子写入、通知中心、手机/平板/桌面布局");
  } catch (error) {
    if (logs.trim()) console.error(logs.slice(-8000));
    throw error;
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) server.kill("SIGTERM");
    if (server.exitCode === null) await new Promise(resolve => server.once("exit", resolve));
    fs.rmSync(temp, { recursive: true, force: true });
  }
})().catch(error => {
  console.error(error.stack || error.message);
  process.exitCode = 1;
});
