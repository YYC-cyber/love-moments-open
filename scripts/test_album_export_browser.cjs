/* Isolated two-account browser audit for protected paper-album PDF export. */
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

async function stopServer(child) {
  if (!child || child.exitCode !== null) return;
  child.kill("SIGTERM");
  if (child.exitCode === null) await new Promise(resolve => child.once("exit", resolve));
}

async function login(page, origin, username) {
  await page.goto(origin + "/?screen=timeline", { waitUntil: "domcontentloaded" });
  await page.locator("#authUsername").fill(username);
  await page.locator("#authPassword").fill("test123");
  await page.locator("#authSubmit").click();
  await page.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
  await page.locator('[data-screen="timeline"].active').waitFor();
}

async function api(page, endpoint, options = {}, expectedStatus = 200) {
  return page.evaluate(async ({ endpointValue, requestOptions, expected }) => {
    const response = await fetch(endpointValue, {
      credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", ...(requestOptions.headers || {}) },
      ...requestOptions,
    });
    const body = await response.json().catch(() => ({}));
    if (response.status !== expected) throw new Error(`${endpointValue}: ${response.status} ${body.error || ""}`);
    return body;
  }, { endpointValue: endpoint, requestOptions: options, expected: expectedStatus });
}

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "love-album-browser-"));
  const port = await freePort();
  const origin = "http://127.0.0.1:" + port;
  const env = {
    ...process.env, APP_HOST: "127.0.0.1", APP_PORT: String(port),
    DATABASE_PATH: path.join(temp, "album.db"), MEDIA_ROOT: path.join(temp, "media"),
    BACKUP_ROOT: path.join(temp, "backups"), DEMO_MODE: "true",
    OPEN_METEO_ENABLED: "false", AMAP_WEB_KEY: "", LLM_API_KEY: "", UI_LLM_API_KEY: "",
    MEDIA_STORAGE_PROVIDER: "local", LOCAL_ADMIN_NOTIFICATIONS: "false", WATCHDOG_AUTO_RESTART: "false",
  };
  let logs = "";
  let server;
  const startServer = () => {
    server = spawn(process.env.PYTHON_BINARY || "python3", ["main.py"], { cwd: root, env, stdio: ["ignore", "pipe", "pipe"] });
    server.stdout.on("data", value => { logs += value.toString(); });
    server.stderr.on("data", value => { logs += value.toString(); });
    return server;
  };
  startServer();
  let browser;
  const errors = [];
  try {
    await waitForHealth(origin, server);
    const launchOptions = { headless: true };
    if (fs.existsSync(chromePath)) launchOptions.executablePath = chromePath;
    browser = await chromium.launch(launchOptions);
    const contextA = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: "block", acceptDownloads: true });
    const contextB = await browser.newContext({ viewport: { width: 768, height: 1024 }, serviceWorkers: "block" });
    const pageA = await contextA.newPage();
    const pageB = await contextB.newPage();
    pageA.on("pageerror", error => errors.push("test: " + error.message));
    pageB.on("pageerror", error => errors.push("test1: " + error.message));
    await Promise.all([login(pageA, origin, "test"), login(pageB, origin, "test1")]);

    const marker = "浏览器纪念册-" + Date.now();
    const shared = await api(pageA, "/api/v1/moments", { method: "POST", body: JSON.stringify({
      title: marker + "-共同", body: "双方都能导出的正文", visibility: "shared", occurred_at: "2026-05-01T12:00:00+00:00",
    }) }, 201);
    const privateMoment = await api(pageA, "/api/v1/moments", { method: "POST", body: JSON.stringify({
      title: marker + "-私密", body: "仅创建者可选择的正文", visibility: "private", occurred_at: "2026-05-02T12:00:00+00:00",
    }) }, 201);

    await pageA.locator('[data-action="open-year-album"]').click();
    await pageA.locator("#albumExportModal.open").waitFor();
    await pageA.waitForFunction(value => document.querySelector("#albumExportItems")?.textContent.includes(value), marker + "-共同");
    let previewText = await pageA.locator("#albumExportItems").textContent();
    assert(previewText.includes(marker + "-共同"), "共同纪念册遗漏共享内容");
    assert(!previewText.includes(marker + "-私密"), "共同纪念册泄露私密内容");

    await pageA.locator("#albumExportAudience").selectOption("personal");
    await pageA.waitForFunction(value => document.querySelector("#albumExportItems")?.textContent.includes(value), marker + "-私密");
    const privateCheckbox = pageA.locator(`#albumExportItems input[data-resource-id="${privateMoment.id}"]`);
    assert(!await privateCheckbox.isChecked(), "个人私密内容没有默认取消勾选");
    await pageA.locator("#albumExportItems input[type=checkbox]").evaluateAll((inputs, ids) => {
      for (const input of inputs) input.checked = ids.includes(input.dataset.resourceId);
    }, [shared.id, privateMoment.id]);
    await privateCheckbox.check();
    await pageA.locator('[data-action="create-album-export"]').click();
    await pageA.locator("#downloadAlbumExport:not([hidden])").waitFor({ timeout: 20000 });
    const jobId = await pageA.evaluate(() => state.albumExport.job.id);
    await api(pageB, `/api/v1/album-exports/${encodeURIComponent(jobId)}`, {}, 404);

    const downloadPromise = pageA.waitForEvent("download");
    await pageA.locator("#downloadAlbumExport").click();
    const download = await downloadPromise;
    const saved = path.join(temp, "album.pdf");
    await download.saveAs(saved);
    const bytes = fs.readFileSync(saved);
    assert(bytes.subarray(0, 5).toString() === "%PDF-", "浏览器下载的文件不是 PDF");
    assert(bytes.includes(Buffer.from("%%EOF")), "浏览器下载的 PDF 不完整");

    await pageA.locator("#albumExportModal .sheet-close").click();
    await pageA.locator('[data-action="open-year-album"]').click();
    await pageA.locator("#albumExportModal.open").waitFor();
    await pageA.locator(`[data-action="restore-album-export"][data-id="${jobId}"]`).click();
    await pageA.locator("#downloadAlbumExport:not([hidden])").waitFor({ timeout: 3000 });
    assert(await pageA.locator("#albumExportStatus").textContent(), "关闭重开后没有恢复纪念册状态");

    await pageA.reload({ waitUntil: "domcontentloaded" });
    await pageA.locator('[data-action="open-year-album"]').click();
    await pageA.locator(`[data-action="restore-album-export"][data-id="${jobId}"]`).click();
    await pageA.locator("#downloadAlbumExport:not([hidden])").waitFor({ timeout: 3000 });
    assert((await pageA.evaluate(() => state.albumExport.job?.id)) === jobId, "刷新后没有恢复原纪念册任务");

    const freshContext = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: "block" });
    const freshPage = await freshContext.newPage();
    freshPage.on("pageerror", error => errors.push("test-new-context: " + error.message));
    await login(freshPage, origin, "test");
    await freshPage.locator('[data-action="open-year-album"]').click();
    await freshPage.locator(`[data-action="restore-album-export"][data-id="${jobId}"]`).click();
    await freshPage.locator("#downloadAlbumExport:not([hidden])").waitFor({ timeout: 3000 });
    await freshContext.close();

    await pageB.locator('[data-action="open-year-album"]').click();
    await pageB.locator("#albumExportModal.open").waitFor();
    await pageB.waitForFunction(value => document.querySelector("#albumExportItems")?.textContent.includes(value), marker + "-共同");
    previewText = await pageB.locator("#albumExportItems").textContent();
    assert(!previewText.includes(marker + "-私密"), "伴侣预览泄露私密内容");
    assert(await pageB.locator(`[data-action="restore-album-export"][data-id="${jobId}"]`).count() === 0, "伴侣看到了创建者的个人导出任务");

    await stopServer(server);
    startServer();
    await waitForHealth(origin, server);
    await pageA.reload({ waitUntil: "domcontentloaded" });
    await pageA.locator('[data-action="open-year-album"]').click();
    await pageA.locator(`[data-action="restore-album-export"][data-id="${jobId}"]`).click();
    await pageA.locator("#downloadAlbumExport:not([hidden])").waitFor({ timeout: 3000 });
    assert((await pageA.evaluate(() => state.albumExport.job?.id)) === jobId, "服务重启后没有恢复原纪念册任务");

    for (const page of [pageA, pageB]) {
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert(overflow <= 1, "纪念册页面出现横向溢出");
    }
    assert(errors.length === 0, "页面脚本错误：" + errors.join(" | "));
    console.log("纪念册浏览器验收通过：双账号权限、私密默认取消、异步生成、受保护下载、手机和平板布局");
  } catch (error) {
    if (errors.length) console.error(errors.join("\n"));
    if (logs.trim()) console.error(logs.slice(-8000));
    throw error;
  } finally {
    if (browser) await browser.close();
    await stopServer(server);
    fs.rmSync(temp, { recursive: true, force: true });
  }
})().catch(error => { console.error(error.stack || error.message); process.exitCode = 1; });
