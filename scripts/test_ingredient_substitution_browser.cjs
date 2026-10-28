/* Isolated H5 interaction check for ingredient substitution review. */
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");

function assert(value, message) { if (!value) throw new Error(message); }

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

async function waitForHealth(origin, process) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (process.exitCode !== null) throw new Error(`测试服务器提前退出 (${process.exitCode})`);
    try { if ((await fetch(`${origin}/api/health`)).ok) return; } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error("测试服务器启动超时");
}

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "love-substitution-browser-"));
  const port = await freePort();
  const origin = `http://127.0.0.1:${port}`;
  const env = {
    ...process.env,
    APP_HOST: "127.0.0.1", APP_PORT: String(port), DEMO_MODE: "true",
    DATABASE_PATH: path.join(temp, "test.db"), MEDIA_ROOT: path.join(temp, "media"),
    BACKUP_ROOT: path.join(temp, "backups"), LLM_API_KEY: "", UI_LLM_API_KEY: "",
    AMAP_WEB_KEY: "", OPEN_METEO_ENABLED: "false", OCR_PROVIDER: "mock",
    MEDIA_STORAGE_PROVIDER: "local", LOCAL_ADMIN_NOTIFICATIONS: "false",
  };
  const server = spawn(process.env.PYTHON_BINARY || "python3", ["main.py"], { cwd: root, env, stdio: "pipe" });
  let logs = "";
  server.stdout.on("data", value => { logs += value.toString(); });
  server.stderr.on("data", value => { logs += value.toString(); });
  let browser;
  try {
    await waitForHealth(origin, server);
    const chromePath = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
    browser = await chromium.launch({ headless: true, ...(fs.existsSync(chromePath) ? { executablePath: chromePath } : {}) });
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    await page.goto(origin, { waitUntil: "domcontentloaded" });
    await page.locator("#authUsername").fill("test");
    await page.locator("#authPassword").fill("test123");
    await page.locator("#authSubmit").click();
    await page.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));

    const tomorrow = new Date(Date.now() + 86400000).toLocaleDateString("sv-SE");
    const meal = await page.evaluate(async ({ tomorrow }) => {
      const headers = { "Content-Type": "application/json" };
      const createdResponse = await fetch("/api/v1/meal-cart/items", {
        method: "POST", headers,
        body: JSON.stringify({ recipe_id: "recipe_builtin_kungpao_chicken", planned_date: tomorrow, meal_slot: "dinner", servings: 2, idempotency_key: `browser:meal:${Date.now()}` }),
      });
      if (!createdResponse.ok) throw new Error((await createdResponse.json()).error || "创建餐食失败");
      const created = await createdResponse.json();
      const acceptedResponse = await fetch(`/api/v1/meal-cart/items/${created.id}`, {
        method: "PATCH", headers,
        body: JSON.stringify({ status: "accepted", expected_version: created.version }),
      });
      if (!acceptedResponse.ok) throw new Error((await acceptedResponse.json()).error || "确认餐食失败");
      return acceptedResponse.json();
    }, { tomorrow });

    await page.locator('[data-nav="life"]').last().click();
    await page.locator('[data-life-section="meals"]').first().click();
    await page.locator('[data-meal-section="calendar"]').click();
    await page.locator(`[data-action="select-meal-date"][data-date="${tomorrow}"]`).click();
    const row = page.locator(".meal-row", { hasText: meal.name });
    await row.locator('[data-action="open-ingredient-substitutions"]').click();
    await page.locator('#ingredientSubstitutionModal [data-action="preview-ingredient-substitutions"]', { hasText: "花生米" }).click();
    await page.locator(".ingredient-substitution-card").first().waitFor();
    const text = await page.locator("#ingredientSubstitutionContent").textContent();
    assert(text.includes("本地校准规则"), "页面没有明确建议来源");
    assert(text.includes("需要人工核对"), "未知方法没有保持人工核对状态");
    assert(text.includes("采购变化"), "页面没有显示当前候选专属的采购差异");
    assert(await page.locator('[data-action="apply-ingredient-substitution"]').count() === 0, "不可自动应用的建议错误显示了应用按钮");
    await page.locator('[data-action="dismiss-ingredient-substitution"]').first().click();
    await page.waitForFunction(() => !document.querySelector(".ingredient-substitution-card"));
    console.log("PASS: H5 食材选择 → 本地规则预览 → 风险门禁 → 忽略建议");
  } catch (error) {
    if (logs.trim()) console.error(logs.slice(-6000));
    throw error;
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) server.kill("SIGTERM");
    if (server.exitCode === null) await new Promise(resolve => server.once("exit", resolve));
    fs.rmSync(temp, { recursive: true, force: true });
  }
})().catch(error => { console.error(error.stack || error.message); process.exitCode = 1; });
