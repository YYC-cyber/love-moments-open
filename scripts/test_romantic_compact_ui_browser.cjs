/* Two-account, three-viewport browser audit for the romantic compact UI. */
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
}

async function assertNoOverflow(page, label) {
  const overflow = await page.evaluate(() => ({
    page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    shell: document.querySelector(".phone-shell").scrollWidth - document.querySelector(".phone-shell").clientWidth,
    offenders: [...document.querySelectorAll(".screen.active *")].map(node => {
      const rect = node.getBoundingClientRect();
      return { tag: node.tagName, cls: node.className || "", id: node.id || "", left: Math.round(rect.left), right: Math.round(rect.right) };
    }).filter(item => item.left < -1 || item.right > document.documentElement.clientWidth + 1).slice(0, 8),
  }));
  assert(overflow.page <= 1 && overflow.shell <= 1, label + " 出现横向溢出：" + JSON.stringify(overflow));
}

async function navigateAllSurfaces(page, label) {
  for (const surface of ["home", "chat", "trip", "timeline", "life"]) {
    await page.locator('.bottom-nav [data-nav="' + surface + '"]').click();
    await page.locator('[data-screen="' + surface + '"].active').waitFor();
    await assertNoOverflow(page, label + " / " + surface);
  }
}

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "love-romantic-ui-"));
  const port = await freePort();
  const origin = "http://127.0.0.1:" + port;
  const env = {
    ...process.env,
    APP_HOST: "127.0.0.1",
    APP_PORT: String(port),
    DATABASE_PATH: path.join(temp, "romantic-ui.db"),
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
    const phoneContext = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: "block" });
    const tabletContext = await browser.newContext({ viewport: { width: 768, height: 1024 }, serviceWorkers: "block" });
    const phone = await phoneContext.newPage();
    const tablet = await tabletContext.newPage();
    const errors = [];
    phone.on("pageerror", error => errors.push("test: " + error.message));
    tablet.on("pageerror", error => errors.push("test1: " + error.message));
    await Promise.all([login(phone, origin, "test"), login(tablet, origin, "test1")]);

    await navigateAllSurfaces(phone, "390×844");
    await navigateAllSurfaces(tablet, "768×1024");

    await phone.locator('.bottom-nav [data-nav="home"]').click();
    assert(await phone.locator(".home-more").evaluate(node => !node.open), "首页低频动态默认没有收起");
    await phone.locator(".home-more > summary").click();
    assert(await phone.locator(".home-more").evaluate(node => node.open), "首页更多生活动态无法展开");

    await phone.locator('.bottom-nav [data-nav="chat"]').click();
    assert(await phone.locator('[data-screen="chat"] .smart-disclosure').evaluate(node => !node.open), "消息智能整理默认没有收起");
    const dock = await phone.locator(".chat-compose-dock").boundingBox();
    const nav = await phone.locator(".bottom-nav").boundingBox();
    assert(dock && nav && dock.y + dock.height <= nav.y + 1, "固定消息输入区遮挡底部导航：" + JSON.stringify({ dock, nav }));
    const unique = "浪漫紧凑版双端消息 " + Date.now();
    await phone.locator("#chatInput").fill(unique);
    await phone.locator("#chatForm .send").click();
    await phone.waitForFunction(text => document.querySelector("#messages")?.textContent.includes(text), unique);

    await tablet.locator('.bottom-nav [data-nav="chat"]').click();
    await tablet.waitForFunction(text => document.querySelector("#messages")?.textContent.includes(text), unique);
    assert(await tablet.locator(".message.message-group-start.message-group-end").count() > 0, "跨账号消息没有得到可读的分组边界");

    await phone.locator('.bottom-nav [data-nav="timeline"]').click();
    assert(await phone.locator(".timeline-tabs [data-view]").count() === 3, "时光首屏不应放置超过三个主视图");
    await phone.locator(".timeline-more > summary").click();
    await phone.locator('.timeline-more [data-view="report"]').click();
    assert(await phone.locator('.timeline-more [data-view="report"]').evaluate(node => node.classList.contains("active")), "时光更多菜单无法切换年度报告");

    await phone.locator('.bottom-nav [data-nav="life"]').click();
    assert(await phone.locator(".life-core-actions > button").count() === 4, "我们页首屏核心入口不是四个");
    assert(await phone.locator(".life-more").evaluate(node => !node.open), "我们页低频入口默认没有收起");
    await phone.locator(".life-more > summary").click();
    assert(await phone.locator(".life-more-actions > button").count() >= 5, "更多与设置缺少原有功能入口");

    await phone.locator('.bottom-nav [data-nav="trip"]').click();
    const tabTops = await phone.locator('[data-screen="trip"] .section-tabs button').evaluateAll(nodes => nodes.map(node => Math.round(node.getBoundingClientRect().top)));
    assert(new Set(tabTops).size === 1, "旅行子导航发生换行");

    await phone.setViewportSize({ width: 1440, height: 900 });
    await navigateAllSurfaces(phone, "1440×900");
    assert(errors.length === 0, "页面脚本错误：" + errors.join(" | "));
    console.log("浪漫紧凑版浏览器验收通过：五页、双账号消息、渐进披露、固定输入区、手机/平板/桌面布局");
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
