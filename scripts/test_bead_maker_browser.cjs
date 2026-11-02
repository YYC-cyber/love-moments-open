/* Browser acceptance test for the local-only entertainment bead maker. */
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
function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer(); server.once("error", reject);
    server.listen(0, "127.0.0.1", () => { const port = server.address().port; server.close(error => error ? reject(error) : resolve(port)); });
  });
}
async function waitForHealth(origin, child) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (child.exitCode !== null) throw new Error("测试服务器提前退出");
    try { if ((await fetch(origin + "/api/health")).ok) return; } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error("测试服务器启动超时");
}

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "love-bead-maker-"));
  const port = await freePort(); const origin = `http://127.0.0.1:${port}`;
  const server = spawn(process.env.PYTHON_BINARY || "python3", ["main.py"], { cwd: root, env: { ...process.env, APP_HOST: "127.0.0.1", APP_PORT: String(port), DATABASE_PATH: path.join(temp, "test.db"), MEDIA_ROOT: path.join(temp, "media"), BACKUP_ROOT: path.join(temp, "backups"), DEMO_MODE: "true", OPEN_METEO_ENABLED: "false", MEDIA_STORAGE_PROVIDER: "local", LOCAL_ADMIN_NOTIFICATIONS: "false", WATCHDOG_AUTO_RESTART: "false" }, stdio: ["ignore", "pipe", "pipe"] });
  let browser;
  try {
    await waitForHealth(origin, server);
    const options = { headless: true }; if (fs.existsSync(chromePath)) options.executablePath = chromePath;
    browser = await chromium.launch(options);
    const page = await browser.newPage({ viewport: { width: 390, height: 844 }, serviceWorkers: "block", acceptDownloads: true });
    const errors = []; page.on("pageerror", error => errors.push(error.message));
    await page.goto(origin, { waitUntil: "domcontentloaded" });
    await page.locator("#authUsername").fill("tangtang"); await page.locator("#authPassword").fill("12345678"); await page.locator("#authSubmit").click();
    await page.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
    await page.locator('.bottom-nav [data-nav="play"]').click();
    await page.locator('[data-screen="play"].active').waitFor();
    assert(await page.locator(".bottom-nav button").count() === 6, "娱乐入口没有进入主导航");
    assert(await page.locator("#playHubPanel").isVisible(), "娱乐入口没有先显示功能中心");
    assert(!(await page.locator("#beadToolPanel").isVisible()), "娱乐首页不应直接展开拼豆编辑器");
    await page.locator("#playSearch").fill("不存在的功能");
    assert(await page.locator("#playSearchEmpty").isVisible(), "搜索无结果时没有清晰反馈");
    await page.locator("#playSearch").fill("拼豆");
    assert(await page.locator('[data-play-feature="beads"]').isVisible(), "搜索无法找到拼豆功能");
    await page.locator('[data-play-feature="beads"]').click();
    await page.locator("#beadToolPanel:not([hidden])").waitFor();
    await page.waitForFunction(() => document.querySelector("#beadPaletteCatalog")?.value);
    await page.waitForFunction(() => document.querySelector("#beadPaletteStatus")?.textContent?.includes("5mm"));
    assert(await page.locator("#beadPaletteCatalog option").count() >= 1, "实体拼豆色卡没有从服务器加载");
    assert((await page.locator("#beadPaletteStatus").textContent()).includes("5mm"), "没有显示国内常用实体豆规格");
    assert(await page.locator("#beadBoardSize").inputValue() === "58", "人像默认画板不是 58 × 58");
    assert(await page.locator("#beadColorCount").inputValue() === "48", "人像默认颜色不是 48 色");
    const toneCheck = await page.evaluate(() => {
      const dark = new ImageData(12, 12); for (let offset = 0; offset < dark.data.length; offset += 4) { dark.data[offset] = 45; dark.data[offset + 1] = 38; dark.data[offset + 2] = 34; dark.data[offset + 3] = 255; }
      const normal = new ImageData(12, 12); for (let offset = 0; offset < normal.data.length; offset += 4) { normal.data[offset] = 140; normal.data[offset + 1] = 132; normal.data[offset + 2] = 126; normal.data[offset + 3] = 255; }
      return { darkBefore: 45, darkAfter: adjustBeadTones(dark, 12, "auto").data[0], normalBefore: 140, normalAfter: adjustBeadTones(normal, 12, "auto").data[0] };
    });
    assert(toneCheck.darkAfter > toneCheck.darkBefore, `偏暗主体没有自动提亮：${JSON.stringify(toneCheck)}`);
    assert(toneCheck.normalAfter >= toneCheck.normalBefore && toneCheck.normalAfter <= toneCheck.normalBefore + 14, `正常曝光没有受到柔和且受控的提亮：${JSON.stringify(toneCheck)}`);
    await page.locator("#beadImageInput").setInputFiles(process.env.BEAD_TEST_IMAGE || { name: "couple.png", mimeType: "image/png", buffer: png });
    await page.locator('[data-action="bead-generate"]').waitFor({ state: "visible" });
    await page.locator('[data-action="bead-generate"]').click();
    await page.locator("#beadResult:not([hidden])").waitFor();
    assert(await page.locator("#beadVariantChoices button").count() === 3, "一键生成没有提供三套可选方案");
    assert((await page.locator("#beadResultSummary").textContent()).includes("87 × 87"), "高清收藏画没有自动使用高清人像标准");
    assert((await page.locator("#beadResultSummary").textContent()).includes("5mm"), "生成结果没有锁定实体豆规格");
    assert((await page.locator("#beadResultSummary").textContent()).includes("cm"), "生成结果没有给出物理成品尺寸");
    assert(await page.locator("#beadCanvas").getAttribute("width") === "1044", "主板图没有使用高清网格画布");
    await page.locator('[data-action="bead-toggle-zoom"]').click();
    assert(await page.locator(".bead-canvas-wrap").evaluate(node => node.classList.contains("zoomed")), "板图无法放大查看单格");
    await page.locator('[data-action="bead-toggle-zoom"]').click();
    if (process.env.BEAD_TEST_INITIAL_SCREENSHOT) await page.screenshot({ path: process.env.BEAD_TEST_INITIAL_SCREENSHOT, fullPage: true });
    await page.locator('#beadVariantChoices [data-id="easy"]').click();
    assert((await page.locator("#beadResultSummary").textContent()).includes("易制作版"), "三套方案之间无法切换");
    assert((await page.locator("#beadResultSummary").textContent()).includes("87 × 87"), "易制作版不应通过降低画板尺寸牺牲主体清晰度");
    const removalSafety = await page.evaluate(() => {
      const sample = new ImageData(40, 40);
      for (let y = 0; y < 40; y += 1) for (let x = 0; x < 40; x += 1) {
        const offset = (y * 40 + x) * 4; const subject = x >= 12 && x <= 28 && y >= 7 && y <= 38;
        sample.data[offset] = subject ? 239 : 245; sample.data[offset + 1] = subject ? 217 : 230;
        sample.data[offset + 2] = subject ? 205 : 212; sample.data[offset + 3] = 255;
      }
      const removed = removeBeadBackground(sample, 40, 24);
      return { corner: removed.data[3], face: removed.data[(17 * 40 + 20) * 4 + 3], torso: removed.data[(32 * 40 + 20) * 4 + 3] };
    });
    assert(removalSafety.corner === 0, `净背景版没有去除边缘背景：${JSON.stringify(removalSafety)}`);
    assert(removalSafety.face === 255 && removalSafety.torso === 255, `净背景版损伤了中心主体：${JSON.stringify(removalSafety)}`);
    await page.locator("#beadAdvancedSettings summary").click();
    await page.locator("#beadCropControls:not([hidden])").waitFor();
    await page.locator("#beadCropZoom").fill("125");
    assert((await page.locator("#beadSourcePreview").getAttribute("style")).includes("scale(1.25)"), "裁剪预览没有同步主体大小");
    const boardSize = process.env.BEAD_TEST_SIZE || "16"; const colorCount = process.env.BEAD_TEST_COLORS || "8";
    await page.locator("#beadBoardSize").selectOption(boardSize); await page.locator("#beadColorCount").selectOption(colorCount);
    await page.locator('[data-action="bead-generate"]').click();
    await page.locator("#beadResult:not([hidden])").waitFor();
    await page.waitForFunction(expected => document.querySelector("#beadResultSummary")?.textContent?.includes(`${expected} × ${expected}`), Number(boardSize));
    assert(await page.locator("#beadPalette button").count() >= 1, "没有生成颜色统计");
    assert((await page.locator("#beadPalette button").first().textContent()).includes("色"), "没有显示实体拼豆颜色名称");
    assert((await page.locator("#beadResultSummary").textContent()).includes("实体拼豆色"), "没有使用实体拼豆色库");
    const compactSummary = await page.locator("#beadResultSummary").textContent();
    assert(compactSummary.includes(`共 ${Number(boardSize) ** 2} 颗`), `拼豆总数错误：${compactSummary}`);
    const rareBefore = await page.locator('#beadPalette [data-availability="expanded"]').count();
    if (rareBefore) {
      await page.locator(".bead-result-advanced summary").click();
      await page.locator('[data-action="bead-replace-rare"]').click();
      assert(await page.locator('#beadPalette [data-availability="expanded"]').count() === 0, "冷门色没有全部替换为常用色");
      await page.locator('[data-action="bead-reset-edits"]').click();
      assert(await page.locator('#beadPalette [data-availability="expanded"]').count() === rareBefore, "替换冷门色后无法恢复生成结果");
    }
    const cutoutData = await page.evaluate(() => {
      const canvas = document.createElement("canvas"); canvas.width = 64; canvas.height = 64; const context = canvas.getContext("2d");
      context.fillStyle = "#F5E6D4"; context.fillRect(0, 0, 64, 64); context.fillStyle = "#8D2446"; context.fillRect(20, 12, 24, 42);
      return canvas.toDataURL("image/png").split(",")[1];
    });
    await page.locator("#beadImageInput").setInputFiles({ name: "cutout.png", mimeType: "image/png", buffer: Buffer.from(cutoutData, "base64") });
    await page.waitForTimeout(150); await page.locator("#beadBackgroundMode").selectOption("remove");
    assert(await page.locator("#beadBackgroundSensitivityRow").isVisible(), "去背景模式没有显示力度调节");
    await page.locator("#beadBackgroundSensitivity").fill("36"); await page.locator('[data-action="bead-generate"]').click();
    await page.waitForTimeout(100); const cutoutSummary = await page.locator("#beadResultSummary").textContent();
    assert(cutoutSummary.includes("去除背景"), `去背景没有产生透明空格：${cutoutSummary}`);
    assert(!cutoutSummary.includes(`共 ${Number(boardSize) ** 2} 颗`), "去背景后仍把空白区域计为拼豆");
    if (process.env.BEAD_TEST_SCREENSHOT) await page.screenshot({ path: process.env.BEAD_TEST_SCREENSHOT, fullPage: true });
    await page.locator("#beadPalette button").first().click(); await page.locator("#beadCanvas").click({ position: { x: 15, y: 15 } });
    await page.locator('[data-action="bead-save"]').click();
    assert(await page.locator(".bead-saved-card").count() === 1, "本机保存未显示");
    const downloadPromise = page.waitForEvent("download"); await page.locator('[data-action="bead-export"]').click();
    const download = await downloadPromise; assert((await download.suggestedFilename()).endsWith(".png"), "导出文件不是 PNG");
    await page.locator('#beadToolPanel [data-action="back-play-hub"]').click();
    assert(await page.locator("#playHubPanel").isVisible(), "拼豆工具无法返回娱乐中心");
    await page.reload({ waitUntil: "domcontentloaded" }); await page.locator('.bottom-nav [data-nav="play"]').click(); await page.locator('[data-play-feature="beads"]').click();
    assert(await page.locator(".bead-saved-card").count() === 1, "刷新后本机草稿丢失");
    await page.locator('[data-action="bead-open"]').click(); await page.locator("#beadResult:not([hidden])").waitFor();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert(overflow <= 1, `娱乐页面横向溢出 ${overflow}px`); assert(errors.length === 0, errors.join(" | "));
    console.log("娱乐拼豆浏览器验收通过：明暗色阶、大板、去背景、改单格、保存、恢复和高清导出均可用");
  } finally {
    if (browser) await browser.close(); if (server.exitCode === null) server.kill("SIGTERM");
    if (server.exitCode === null) await new Promise(resolve => server.once("exit", resolve)); fs.rmSync(temp, { recursive: true, force: true });
  }
})().catch(error => { console.error(error.stack || error.message); process.exitCode = 1; });
