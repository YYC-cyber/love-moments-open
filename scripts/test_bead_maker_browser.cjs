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
    await page.locator("#authUsername").fill("test"); await page.locator("#authPassword").fill("test123"); await page.locator("#authSubmit").click();
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
    assert(await page.locator("#beadPaletteCatalog option").count() >= 4, "国内通用与 MARD 两种尺寸色卡没有全部加载");
    assert((await page.locator("#beadPaletteStatus").textContent()).includes("5mm"), "没有显示国内常用实体豆规格");
    const initialPaletteId = await page.locator("#beadPaletteCatalog").inputValue();
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
    if (process.env.BEAD_TEST_IMAGE) {
      const variantColours = await page.evaluate(() => state.bead.variants.map(item => item.palette.length));
      assert(new Set(variantColours).size === 3, `三套方案实际仍使用同一组色数：${variantColours.join("/")}`);
    }
    assert((await page.locator("#beadResultSummary").textContent()).includes("87 × 87"), "高清收藏画没有自动使用高清人像标准");
    assert((await page.locator("#beadResultSummary").textContent()).includes("5mm"), "生成结果没有锁定实体豆规格");
    assert((await page.locator("#beadResultSummary").textContent()).includes("cm"), "生成结果没有给出物理成品尺寸");
    assert(await page.locator("#beadCanvas").getAttribute("width") === "1740", "主板图没有使用高清逐格色号画布");
    assert(Number(await page.locator("#beadCanvas").getAttribute("data-bead-inset")) > 0, "图纸四边没有预留行列坐标区");
    assert(await page.locator("#beadLegend span").count() >= 1, "图纸下方没有色号与数量图例");
    const labelInk = await page.evaluate(() => {
      const canvas = document.createElement("canvas"); canvas.width = 180; canvas.height = 180;
      const pattern = { size: 3, palette: [[245, 230, 220]], paletteMeta: [{ code: "A12", name: "测试色" }], indices: new Array(9).fill(0) };
      paintBeadCanvas(canvas, pattern, { grid: true, labels: true });
      const context = canvas.getContext("2d"); const base = [245, 230, 220]; let changed = 0;
      for (let y = 20; y < 40; y += 1) for (let x = 20; x < 40; x += 1) {
        const pixel = context.getImageData(x, y, 1, 1).data;
        if (pixel[0] !== base[0] || pixel[1] !== base[1] || pixel[2] !== base[2]) changed += 1;
      }
      return changed;
    });
    assert(labelInk > 0, "逐格色号没有绘制到板图上");
    await page.locator('[data-action="bead-toggle-zoom"]').click();
    assert(await page.locator(".bead-canvas-wrap").evaluate(node => node.classList.contains("zoomed")), "板图无法放大查看单格");
    await page.locator("#beadCanvas").click({ position: { x: 80, y: 80 } });
    assert((await page.locator("#beadCellInspector").textContent()).includes("第 "), "点击格子没有显示坐标和色号");
    if (process.env.BEAD_TEST_CODE_SCREENSHOT) await page.locator("#beadCanvas").screenshot({ path: process.env.BEAD_TEST_CODE_SCREENSHOT });
    await page.locator('[data-action="bead-toggle-zoom"]').click();
    if (process.env.BEAD_TEST_INITIAL_SCREENSHOT) await page.screenshot({ path: process.env.BEAD_TEST_INITIAL_SCREENSHOT, fullPage: true });
    await page.locator("#beadPaletteCatalog").selectOption("mard-reference-2.6mm-48-v1");
    await page.waitForFunction(() => document.querySelector("#beadResultSummary")?.textContent?.includes("MARD") && document.querySelector("#beadResultSummary")?.textContent?.includes("2.6mm"));
    const mardCatalogCheck = await page.evaluate(() => ({
      found: state.bead.catalog?.id === "mard-reference-2.6mm-48-v1",
      white: state.bead.catalog?.colors?.find(item => item.code === "H2")?.hex,
      black: state.bead.catalog?.colors?.find(item => item.code === "H7")?.hex,
    }));
    assert(mardCatalogCheck.found && mardCatalogCheck.white === "#FFFFFF" && mardCatalogCheck.black === "#10161F", `MARD H2/H7 色卡不正确：${JSON.stringify(mardCatalogCheck)}`);
    assert((await page.locator("#beadResultSummary").textContent()).includes("22.6 × 22.6 cm"), "切换 MARD 2.6mm 后没有重算 87 格成品尺寸");
    assert((await page.locator("#beadPaletteStatus").textContent()).includes("H2/H7"), "切换 MARD 后没有显示对应色号体系");
    if (process.env.BEAD_TEST_MARD_SCREENSHOT) await page.screenshot({ path: process.env.BEAD_TEST_MARD_SCREENSHOT, fullPage: true });
    await page.locator("#beadPaletteCatalog").selectOption(initialPaletteId);
    await page.waitForFunction(() => document.querySelector("#beadResultSummary")?.textContent?.includes("国内通用") && document.querySelector("#beadResultSummary")?.textContent?.includes("5mm"));
    if (process.env.BEAD_TEST_CLEAN_SCREENSHOT) {
      await page.locator('#beadVariantChoices [data-id="clean"]').click();
      await page.screenshot({ path: process.env.BEAD_TEST_CLEAN_SCREENSHOT, fullPage: true });
      if (process.env.BEAD_TEST_CLEAN_CANVAS) await page.locator("#beadCanvas").screenshot({ path: process.env.BEAD_TEST_CLEAN_CANVAS });
      if (process.env.BEAD_TEST_CLEAN_EXPORT) {
        const cleanData = await page.locator("#beadCanvas").evaluate(canvas => canvas.toDataURL("image/png").split(",")[1]);
        fs.writeFileSync(process.env.BEAD_TEST_CLEAN_EXPORT, Buffer.from(cleanData, "base64"));
      }
    }
    await page.locator('#beadVariantChoices [data-id="easy"]').click();
    assert((await page.locator("#beadResultSummary").textContent()).includes("易制作版"), "三套方案之间无法切换");
    assert((await page.locator("#beadResultSummary").textContent()).includes("87 × 87"), "易制作版降低了空间分辨率，五官会变糊");
    if (process.env.BEAD_TEST_EASY_SCREENSHOT) await page.screenshot({ path: process.env.BEAD_TEST_EASY_SCREENSHOT, fullPage: true });
    const guideColours = await page.evaluate(() => {
      const canvas = document.createElement("canvas"); canvas.width = 580; canvas.height = 580;
      const pattern = { size: 58, palette: [[255, 255, 255]], indices: new Array(58 * 58).fill(0) };
      paintBeadCanvas(canvas, pattern);
      const context = canvas.getContext("2d");
      return [5, 10, 15, 20].map(step => [...context.getImageData(step * 10, 23, 1, 1).data]);
    });
    assert(guideColours.every(colour => JSON.stringify(colour) === JSON.stringify(guideColours[0])), `每 5 格定位线的颜色或粗细不一致：${JSON.stringify(guideColours)}`);
    await page.locator(".bead-ironing-guide summary").click();
    assert((await page.locator("#beadIroningAdvice").textContent()).includes("亮闪"), "没有提供特殊拼豆的烫制提示");
    const removalSafety = await page.evaluate(() => {
      const sample = new ImageData(40, 40);
      for (let y = 0; y < 40; y += 1) for (let x = 0; x < 40; x += 1) {
        const offset = (y * 40 + x) * 4; const subject = x >= 6 && x <= 34 && y >= 3 && y <= 39;
        sample.data[offset] = subject ? 185 : 245; sample.data[offset + 1] = subject ? 105 : 230;
        sample.data[offset + 2] = subject ? 95 : 212; sample.data[offset + 3] = 255;
      }
      const removed = removeBeadBackground(sample, 40, 24);
      const pale = new ImageData(48, 48);
      for (let y = 0; y < 48; y += 1) for (let x = 0; x < 48; x += 1) {
        const offset = (y * 48 + x) * 4; const subject = x >= 1 && x <= 36 && y >= 4 && y <= 46;
        pale.data[offset] = subject ? 248 : 224 + Math.round(x / 8); pale.data[offset + 1] = subject ? 240 : 205 + Math.round(y / 8);
        pale.data[offset + 2] = subject ? 232 : 190; pale.data[offset + 3] = 255;
        if (subject && (x === 1 || x === 36 || y === 4)) { pale.data[offset] = 118; pale.data[offset + 1] = 75; pale.data[offset + 2] = 70; }
      }
      const paleRemoved = removeBeadBackground(pale, 48, 24);
      const borderSubject = new ImageData(48, 48);
      for (let y = 0; y < 48; y += 1) for (let x = 0; x < 48; x += 1) {
        const offset = (y * 48 + x) * 4;
        const cap = x >= 10 && x <= 37 && y <= 27 && ((x - 23.5) ** 2 / 14 ** 2 + (y - 14) ** 2 / 18 ** 2 <= 1.25);
        const face = x >= 15 && x <= 33 && y >= 17 && y <= 44;
        borderSubject.data[offset] = cap ? 244 + ((x + y) % 3) * 4 : face ? 205 : 235;
        borderSubject.data[offset + 1] = cap ? 242 + ((x * 2 + y) % 3) * 4 : face ? 145 : 190;
        borderSubject.data[offset + 2] = cap ? 238 + ((x + y * 2) % 3) * 4 : face ? 132 : 180;
        borderSubject.data[offset + 3] = 255;
      }
      const borderSubjectRemoved = removeBeadBackground(borderSubject, 48, 32, .72);
      return {
        corner: removed.data[3], halo: removed.data[(18 * 40 + 5) * 4 + 3], face: removed.data[(17 * 40 + 20) * 4 + 3], torso: removed.data[(32 * 40 + 20) * 4 + 3],
        paleCorner: paleRemoved.data[3], paleSubject: paleRemoved.data[(22 * 48 + 14) * 4 + 3], paleSafe: paleRemoved.beadBackgroundDiagnostics?.safe,
        borderCorner: borderSubjectRemoved.data[3], borderCap: borderSubjectRemoved.data[(2 * 48 + 24) * 4 + 3], borderFace: borderSubjectRemoved.data[(30 * 48 + 24) * 4 + 3], borderSafe: borderSubjectRemoved.beadBackgroundDiagnostics?.safe,
      };
    });
    assert(removalSafety.corner === 0, `净背景版没有去除边缘背景：${JSON.stringify(removalSafety)}`);
    assert(removalSafety.halo === 0, `净背景版仍保留了一格背景毛边：${JSON.stringify(removalSafety)}`);
    assert(removalSafety.face === 255 && removalSafety.torso === 255, `净背景版损伤了中心主体：${JSON.stringify(removalSafety)}`);
    assert(removalSafety.paleCorner === 0 && removalSafety.paleSubject === 255 && removalSafety.paleSafe, `净背景版不能通用保护偏左浅色主体：${JSON.stringify(removalSafety)}`);
    assert(removalSafety.borderCorner === 0 && removalSafety.borderCap === 255 && removalSafety.borderFace === 255 && removalSafety.borderSafe, `净背景版误删了触碰单侧边缘的浅色主体：${JSON.stringify(removalSafety)}`);
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
    if (process.env.BEAD_TEST_EXPORT) await download.saveAs(process.env.BEAD_TEST_EXPORT);
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
