const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");
const root = path.resolve(__dirname, "..");
const chromePath = process.env.PLAYWRIGHT_CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAF0lEQVR4nGP4z8DA8J+BgYGB4T8DAwMAHhgCAe2hUj8AAAAASUVORK5CYII=", "base64");
function assert(value, message) { if (!value) throw new Error(message); }
function freePort() { return new Promise((resolve, reject) => { const server = net.createServer(); server.once("error", reject); server.listen(0, "127.0.0.1", () => { const port = server.address().port; server.close(error => error ? reject(error) : resolve(port)); }); }); }
async function waitForHealth(origin, child) { for (let attempt = 0; attempt < 100; attempt += 1) { if (child.exitCode !== null) throw new Error("测试服务器提前退出"); try { if ((await fetch(origin + "/api/health")).ok) return; } catch (_) {} await new Promise(resolve => setTimeout(resolve, 100)); } throw new Error("测试服务器启动超时"); }
(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "love-open-beads-")), port = await freePort(), origin = `http://127.0.0.1:${port}`;
  const server = spawn(process.env.PYTHON_BINARY || "python3", ["main.py"], { cwd: root, env: { ...process.env, APP_HOST: "127.0.0.1", APP_PORT: String(port), DATABASE_PATH: path.join(temp, "test.db"), MEDIA_ROOT: path.join(temp, "media"), BACKUP_ROOT: path.join(temp, "backups"), DEMO_MODE: "true", OPEN_METEO_ENABLED: "false", MEDIA_STORAGE_PROVIDER: "local", LOCAL_ADMIN_NOTIFICATIONS: "false", WATCHDOG_AUTO_RESTART: "false" }, stdio: ["ignore", "pipe", "pipe"] });
  let browser;
  try {
    await waitForHealth(origin, server); const options = { headless: true }; if (fs.existsSync(chromePath)) options.executablePath = chromePath; browser = await chromium.launch(options);
    const page = await browser.newPage({ viewport: { width: 390, height: 844 }, serviceWorkers: "block", acceptDownloads: true }); const errors = []; page.on("pageerror", error => errors.push(error.message));
    await page.goto(origin, { waitUntil: "domcontentloaded" }); await page.locator("#authUsername").fill("test"); await page.locator("#authPassword").fill("test123"); await page.locator("#authSubmit").click(); await page.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
    await page.locator('[data-nav="play"]').last().click(); await page.locator('[data-screen="play"].active').waitFor();
    assert(await page.locator("#playHubPanel").isVisible(), "娱乐中心未显示"); assert(!(await page.locator("#beadToolPanel").isVisible()), "拼豆工具不应默认展开");
    await page.locator("#playSearch").fill("不存在的游戏"); assert(await page.locator("#playSearchEmpty").isVisible(), "无搜索结果提示未显示");
    await page.locator("#playSearch").fill("拼豆"); assert(await page.locator('[data-play-feature="beads"]').isVisible(), "搜索未找到拼豆工具");
    await page.locator('[data-play-feature="beads"]').click(); await page.locator("#beadToolPanel:not([hidden])").waitFor();
    await page.locator("#beadImageInput").setInputFiles({ name: "photo.png", mimeType: "image/png", buffer: png }); await page.locator("#beadBoardSize").selectOption("16"); await page.locator('[data-action="bead-generate"]').click(); await page.locator("#beadResult:not([hidden])").waitFor();
    assert((await page.locator("#beadResultSummary").textContent()).includes("256 颗"), "拼豆数量错误"); await page.locator('[data-action="bead-save"]').click(); assert(await page.locator(".bead-saved-card").count() === 1, "保存失败");
    const downloadPromise = page.waitForEvent("download"); await page.locator('[data-action="bead-export"]').click(); assert((await (await downloadPromise).suggestedFilename()).endsWith(".png"), "导出失败");
    await page.locator('[data-action="back-play-hub"]').click(); assert(await page.locator("#playHubPanel").isVisible(), "无法返回娱乐中心");
    await page.reload({ waitUntil: "domcontentloaded" }); await page.locator('[data-nav="play"]').last().click(); await page.locator('[data-play-feature="beads"]').click(); assert(await page.locator(".bead-saved-card").count() === 1, "刷新后草稿丢失"); assert(errors.length === 0, errors.join(" | "));
    console.log("公有版娱乐中心与拼豆浏览器验收通过");
  } finally { if (browser) await browser.close(); if (server.exitCode === null) server.kill("SIGTERM"); if (server.exitCode === null) await new Promise(resolve => server.once("exit", resolve)); fs.rmSync(temp, { recursive: true, force: true }); }
})().catch(error => { console.error(error.stack || error.message); process.exitCode = 1; });
