/* Isolated two-account browser audit for relationship search and memory completion. */
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
  await page.goto(origin + "/?screen=timeline&memory_search=1", { waitUntil: "domcontentloaded" });
  await page.locator("#authUsername").fill(username);
  await page.locator("#authPassword").fill("test123");
  await page.locator("#authSubmit").click();
  await page.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
  await page.locator('[data-screen="timeline"].active').waitFor();
  await page.locator("#memorySearchPanel[open]").waitFor();
}

async function api(page, endpoint, options = {}) {
  return page.evaluate(async ({ endpointValue, requestOptions }) => {
    const response = await fetch(endpointValue, {
      credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json" }, ...requestOptions,
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || String(response.status));
    return body;
  }, { endpointValue: endpoint, requestOptions: options });
}

async function search(page, query) {
  await page.locator("#memorySearchQuery").fill(query);
  await page.locator("#memorySearchForm button[type=submit]").click();
  await page.waitForFunction(() => !document.querySelector("#memorySearchResults")?.textContent.includes("正在查找"));
}

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "love-memory-browser-"));
  const port = await freePort();
  const origin = "http://127.0.0.1:" + port;
  const env = {
    ...process.env, APP_HOST: "127.0.0.1", APP_PORT: String(port),
    DATABASE_PATH: path.join(temp, "memory.db"), MEDIA_ROOT: path.join(temp, "media"),
    BACKUP_ROOT: path.join(temp, "backups"), DEMO_MODE: "true",
    OPEN_METEO_ENABLED: "false", AMAP_WEB_KEY: "", LLM_API_KEY: "", UI_LLM_API_KEY: "",
    MEDIA_STORAGE_PROVIDER: "local", LOCAL_ADMIN_NOTIFICATIONS: "false", WATCHDOG_AUTO_RESTART: "false",
  };
  const server = spawn(process.env.PYTHON_BINARY || "python3", ["main.py"], { cwd: root, env, stdio: ["ignore", "pipe", "pipe"] });
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
    pageA.on("pageerror", error => errors.push("test: " + error.message));
    pageB.on("pageerror", error => errors.push("test1: " + error.message));
    await Promise.all([login(pageA, origin, "test"), login(pageB, origin, "test1")]);

    const marker = "浏览器回忆-" + Date.now();
    await api(pageA, "/api/v1/moments", { method: "POST", body: JSON.stringify({ title: marker + "-共同", body: "一起看到晚霞", visibility: "shared", occurred_at: "2024-02-14T12:00:00+00:00" }) });
    await api(pageA, "/api/v1/moments", { method: "POST", body: JSON.stringify({ title: marker + "-私密", body: "只属于自己", visibility: "private", occurred_at: "2024-02-14T13:00:00+00:00" }) });
    const incomplete = await api(pageA, "/api/v1/moments", { method: "POST", body: JSON.stringify({ title: marker + "-待补", type: "photo", body: "", location: "杭州", visibility: "shared", occurred_at: "2024-02-14T14:00:00+00:00" }) });

    await search(pageA, marker);
    assert(await pageA.locator(".memory-result-card").count() === 3, "创建者没有看到共同、私密和待补回忆");
    await search(pageB, marker);
    assert(await pageB.locator(".memory-result-card").count() === 2, "伴侣搜索数量泄露或遗漏了私密回忆");
    assert(!(await pageB.locator("#memorySearchResults").textContent()).includes("只属于自己"), "私密正文泄露给伴侣");
    await pageB.locator('.memory-result-open[data-private="0"]').first().click();
    await pageB.locator("#sharedDetailModal.open").waitFor();
    await pageB.locator('#sharedDetailModal [data-action="close-modal"]').click();

    await search(pageA, marker + "-私密");
    pageA.on("dialog", async dialog => {
      if (dialog.type() === "prompt") await dialog.accept("这是我的补充视角");
      else await dialog.dismiss();
    });
    await pageA.locator('.memory-result-open[data-private="1"]').click();
    await pageA.waitForFunction(() => document.querySelector("#memorySearchResults")?.textContent.includes("这是我的补充视角"));
    assert(!await pageA.locator("#sharedDetailModal.open").count(), "私密搜索结果错误打开了双方评论");

    await pageA.locator('[data-action="refresh-memory-completion"]').click();
    const completion = pageA.locator(".memory-completion-item", { hasText: incomplete.title }).first();
    await completion.waitFor();
    await completion.locator('[data-action="complete-memory-suggestion"]').click();
    await pageA.waitForFunction(title => ![...document.querySelectorAll(".memory-completion-item")].some(node => node.textContent.includes(title) && node.textContent.includes("补一句回忆")), incomplete.title);

    await search(pageA, marker);
    const chipCount = await pageA.locator('[data-action="clear-memory-filter"]').count();
    assert(chipCount > 0, "服务端识别条件没有形成可清除筛选 chip");
    await pageA.locator('[data-action="clear-memory-filter"]').first().click();
    for (const page of [pageA, pageB]) {
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert(overflow <= 1, "回忆搜索页面出现横向溢出");
    }
    assert(errors.length === 0, "页面脚本错误：" + errors.join(" | "));
    console.log("回忆浏览器验收通过：双账号权限、共享详情、私密补充、补全提醒、筛选清除、手机和平板布局");
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
