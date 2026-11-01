const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");
const chromePath = process.env.PLAYWRIGHT_CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const assert = (value, message) => { if (!value) throw new Error(message); };
const freePort = () => new Promise((resolve, reject) => { const server = net.createServer(); server.once("error", reject); server.listen(0, "127.0.0.1", () => { const port = server.address().port; server.close(error => error ? reject(error) : resolve(port)); }); });
async function waitForHealth(origin, child) { for (let n = 0; n < 100; n += 1) { if (child.exitCode !== null) throw new Error("测试服务器提前退出"); try { if ((await fetch(origin + "/api/health")).ok) return; } catch (_) {} await new Promise(resolve => setTimeout(resolve, 100)); } throw new Error("测试服务器启动超时"); }

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "love-trip-weather-")); const port = await freePort(); const origin = `http://127.0.0.1:${port}`;
  const server = spawn(process.env.PYTHON_BINARY || "python3", ["main.py"], { cwd: root, env: { ...process.env, APP_HOST: "127.0.0.1", APP_PORT: String(port), DATABASE_PATH: path.join(temp, "app.db"), MEDIA_ROOT: path.join(temp, "media"), BACKUP_ROOT: path.join(temp, "backups"), DEMO_MODE: "true", OPEN_METEO_ENABLED: "false", AMAP_WEB_KEY: "", LLM_API_KEY: "", UI_LLM_API_KEY: "", MEDIA_STORAGE_PROVIDER: "local", LOCAL_ADMIN_NOTIFICATIONS: "false", WATCHDOG_AUTO_RESTART: "false" }, stdio: ["ignore", "pipe", "pipe"] });
  let browser;
  try {
    await waitForHealth(origin, server); const options = { headless: true }; if (fs.existsSync(chromePath)) options.executablePath = chromePath; browser = await chromium.launch(options);
    const page = await browser.newPage({ viewport: { width: 390, height: 844 }, serviceWorkers: "block" }); const errors = []; page.on("pageerror", error => errors.push(error.message));
    await page.goto(origin, { waitUntil: "domcontentloaded" }); await page.locator("#authUsername").fill("test"); await page.locator("#authPassword").fill("test123"); await page.locator("#authSubmit").click(); await page.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
    await page.locator('.bottom-nav [data-nav="trip"]').click(); await page.locator('[data-trip-section="weather"]').click(); await page.locator('[data-trip-panel="weather"]:not([hidden])').waitFor();
    await page.waitForFunction(() => document.querySelector("#tripWeatherPanel")?.textContent.includes("天气服务当前未启用"));
    assert(await page.locator('[data-weather-range="10"]').getAttribute("class") === "active", "天气页没有默认选择未来 10 天");
    await page.locator('[data-weather-range="7"]').click(); await page.waitForFunction(() => document.querySelector('[data-weather-range="7"]')?.classList.contains("active"));
    assert((await page.locator("#tripWeatherPanel").textContent()).includes("不会用模拟天气代替") || (await page.locator('[data-trip-panel="weather"]').textContent()).includes("不会用模拟天气代替"), "天气页没有说明真实数据边界");
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert(overflow <= 1, `天气页在 iPhone 宽度横向溢出 ${overflow}px`); assert(errors.length === 0, errors.join(" | "));
    console.log("旅行天气浏览器验收通过：独立页面、真实来源边界、不可用降级与 iPhone 布局均正常");
  } finally { if (browser) await browser.close(); if (server.exitCode === null) server.kill("SIGTERM"); if (server.exitCode === null) await new Promise(resolve => server.once("exit", resolve)); fs.rmSync(temp, { recursive: true, force: true }); }
})().catch(error => { console.error(error.stack || error.message); process.exitCode = 1; });
