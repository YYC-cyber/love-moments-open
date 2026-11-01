/* Two-account browser acceptance test for the shared couple wheel. */
const fs = require("node:fs"), net = require("node:net"), os = require("node:os"), path = require("node:path");
const { spawn } = require("node:child_process"); const { chromium } = require("playwright");
const root = path.resolve(__dirname, "..");
const chromePath = process.env.PLAYWRIGHT_CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
function assert(value, message) { if (!value) throw new Error(message); }
function freePort() { return new Promise((resolve, reject) => { const server = net.createServer(); server.once("error", reject); server.listen(0, "127.0.0.1", () => { const port = server.address().port; server.close(error => error ? reject(error) : resolve(port)); }); }); }
async function waitForHealth(origin, child) { for (let attempt = 0; attempt < 100; attempt += 1) { if (child.exitCode !== null) throw new Error("测试服务器提前退出"); try { if ((await fetch(origin + "/api/health")).ok) return; } catch (_) {} await new Promise(resolve => setTimeout(resolve, 100)); } throw new Error("测试服务器启动超时"); }
async function login(page, origin, username) { await page.goto(origin, { waitUntil: "domcontentloaded" }); await page.locator("#authUsername").fill(username); await page.locator("#authPassword").fill("test123"); await page.locator("#authSubmit").click(); await page.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden")); }
async function openWheel(page) { await page.locator('.bottom-nav [data-nav="play"]').click(); await page.locator('[data-play-feature="wheel"]').click(); await page.locator("#wheelToolPanel:not([hidden])").waitFor(); await page.locator(".wheel-option").first().waitFor(); }
(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "love-couple-wheel-open-")), port = await freePort(), origin = `http://127.0.0.1:${port}`;
  const server = spawn(process.env.PYTHON_BINARY || "python3", ["main.py"], { cwd: root, env: { ...process.env, APP_HOST: "127.0.0.1", APP_PORT: String(port), DATABASE_PATH: path.join(temp, "test.db"), MEDIA_ROOT: path.join(temp, "media"), BACKUP_ROOT: path.join(temp, "backups"), DEMO_MODE: "true", OPEN_METEO_ENABLED: "false", MEDIA_STORAGE_PROVIDER: "local", LOCAL_ADMIN_NOTIFICATIONS: "false", WATCHDOG_AUTO_RESTART: "false" }, stdio: ["ignore", "pipe", "pipe"] });
  let browser;
  try {
    await waitForHealth(origin, server); const options = { headless: true }; if (fs.existsSync(chromePath)) options.executablePath = chromePath; browser = await chromium.launch(options);
    const contextA = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: "block" }), contextB = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: "block" });
    const pageA = await contextA.newPage(), pageB = await contextB.newPage(), errors = []; pageA.on("pageerror", error => errors.push(`A:${error.message}`)); pageB.on("pageerror", error => errors.push(`B:${error.message}`));
    await Promise.all([login(pageA, origin, "test"), login(pageB, origin, "test1")]); await openWheel(pageA);
    assert(await pageA.locator("[data-wheel-mode]").count() === 3, "没有显示三种转盘模式"); await pageA.locator('[data-wheel-mode="wild"]').click(); await pageA.waitForFunction(() => document.querySelector("#wheelConsentNotice")?.textContent.includes("成年"));
    assert((await pageA.locator("#wheelConsentNotice").textContent()).includes("随时"), "变态版缺少随时停止说明"); await pageA.locator('[data-wheel-mode="normal"]').click();
    await pageA.locator("#wheelOptionInput").fill("一起吃一份双人甜点"); await pageA.locator("#wheelOptionForm button").click(); await pageA.waitForFunction(() => document.querySelector("#wheelOptionList")?.textContent.includes("一起吃一份双人甜点"));
    await openWheel(pageB); await pageB.waitForFunction(() => document.querySelector("#wheelOptionList")?.textContent.includes("一起吃一份双人甜点"));
    await pageA.locator('[data-action="spin-couple-wheel"]').click(); await pageA.waitForFunction(() => document.querySelector("#wheelHistory")?.querySelectorAll(".wheel-history-item").length > 0, null, { timeout: 7000 });
    const overflowA = await pageA.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth), overflowB = await pageB.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert(overflowA <= 1 && overflowB <= 1, `iPhone 页面横向溢出 A=${overflowA}, B=${overflowB}`); assert(errors.length === 0, errors.join(" | "));
    console.log("公开版双人转盘验收通过：三种模式、双方同步、共享结果与 iPhone 布局均正常");
  } finally { if (browser) await browser.close(); if (server.exitCode === null) server.kill("SIGTERM"); if (server.exitCode === null) await Promise.race([new Promise(resolve => server.once("exit", resolve)), new Promise(resolve => setTimeout(resolve, 2500))]); if (server.exitCode === null) server.kill("SIGKILL"); fs.rmSync(temp, { recursive: true, force: true }); }
})().catch(error => { console.error(error.stack || error.message); process.exitCode = 1; });
