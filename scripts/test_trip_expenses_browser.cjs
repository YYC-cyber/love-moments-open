/* Isolated two-account browser acceptance for trip expenses. */
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");
const chromePath = process.env.PLAYWRIGHT_CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const assert = (value, message) => { if (!value) throw new Error(message); };
const acceptanceSource = fs.readFileSync(__filename, "utf8");
const executableAcceptanceSource = acceptanceSource.split("\n").filter(line => !line.includes("保存验收禁止")).join("\n");
assert(!/keyboard\.press\(["']Enter["']\)/.test(executableAcceptanceSource), "保存验收禁止用 Enter 绕过真实指针点击");
assert(!/\.click\(\s*\{[^}]*force\s*:\s*true/.test(executableAcceptanceSource), "保存验收禁止 force click 绕过遮挡检查");
const freePort = () => new Promise((resolve, reject) => {
  const socket = net.createServer(); socket.once("error", reject);
  socket.listen(0, "127.0.0.1", () => { const port = socket.address().port; socket.close(error => error ? reject(error) : resolve(port)); });
});
async function waitForHealth(origin, child) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (child.exitCode !== null) throw new Error(`测试服务器提前退出 (${child.exitCode})`);
    try { if ((await fetch(origin + "/api/health")).ok) return; } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error("旅行花费测试服务器启动超时");
}
async function login(page, origin, username) {
  await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
  await page.locator("#authUsername").fill(username);
  await page.locator("#authPassword").fill("test123");
  await page.locator("#authSubmit").click();
  await page.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
}
async function request(page, url, method = "GET", body = null) {
  return page.evaluate(async ({ url, method, body }) => {
    const response = await fetch(url, { method, credentials: "same-origin", cache: "no-store", headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
    const value = await response.json(); if (!response.ok) throw new Error(value.error || String(response.status)); return value;
  }, { url, method, body });
}
async function openExpenses(page) {
  await page.locator('[data-nav="trip"]').last().click();
  await page.locator('[data-trip-section="tools"]').click();
  await page.locator('[data-trip-tool-section="expenses"]').click();
  await page.locator('[data-trip-tool-panel="expenses"]').waitFor({ state: "visible" });
  await page.waitForFunction(() => !document.querySelector("#tripExpenseList")?.textContent.includes("正在读取"));
}
async function assertRealPointerTarget(locator, label) {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  assert(box && box.width > 0 && box.height > 0, `${label} 没有可点击尺寸`);
  const hit = await locator.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    const target = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    return Boolean(target && (target === element || element.contains(target)));
  });
  assert(hit, `${label} 被滚动容器或其他界面遮挡，真实手指无法点中`);
}

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "love-trip-expenses-browser-"));
  const port = await freePort(); const origin = `http://127.0.0.1:${port}`;
  const server = spawn(process.env.PYTHON_BINARY || "python3", ["main.py"], {
    cwd: root,
    env: { ...process.env, APP_HOST: "127.0.0.1", APP_PORT: String(port), DATABASE_PATH: path.join(temp, "trip-expenses.db"), MEDIA_ROOT: path.join(temp, "media"), BACKUP_ROOT: path.join(temp, "backups"), DEMO_MODE: "true", OPEN_METEO_ENABLED: "false", AMAP_WEB_KEY: "", GEOAPIFY_ENABLED: "false", LLM_API_KEY: "", UI_LLM_API_KEY: "", MEDIA_STORAGE_PROVIDER: "local", LOCAL_ADMIN_NOTIFICATIONS: "false", WATCHDOG_AUTO_RESTART: "false" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let logs = ""; server.stdout.on("data", value => { logs += value; }); server.stderr.on("data", value => { logs += value; });
  let browser;
  try {
    await waitForHealth(origin, server);
    const launchOptions = { headless: true }; if (fs.existsSync(chromePath)) launchOptions.executablePath = chromePath;
    browser = await chromium.launch(launchOptions);
    const contextA = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: "block" });
    const contextB = await browser.newContext({ viewport: { width: 768, height: 1024 }, serviceWorkers: "block" });
    const pageA = await contextA.newPage(); const pageB = await contextB.newPage();
    pageA.setDefaultTimeout(5000); pageB.setDefaultTimeout(5000);
    await pageA.route("**/api/v1/realtime/stream?**", route => route.abort());
    const errors = []; pageA.on("pageerror", error => errors.push(`test: ${error.message}`)); pageB.on("pageerror", error => errors.push(`test1: ${error.message}`));
    pageA.on("dialog", dialog => dialog.accept()); pageB.on("dialog", dialog => dialog.accept());
    await Promise.all([login(pageA, origin, "test"), login(pageB, origin, "test1")]);
    await Promise.all([openExpenses(pageA), openExpenses(pageB)]);

    await pageB.locator("#tripExpenseAmount").fill("4.00");
    await pageB.locator("#tripExpensePayer").selectOption({ label: "演示乙" });
    await pageB.locator("#tripExpenseCategory").selectOption("shopping");
    await pageB.locator("#tripExpenseSplit").selectOption("personal");
    await pageB.locator("#tripExpenseBeneficiary").selectOption({ label: "演示甲" });
    await pageB.locator("#tripExpenseDate").fill("2026-10-28");
    await pageB.locator("#tripExpenseNote").fill("演示甲的个人纪念品");
    const tabletSave = pageB.locator("#tripExpenseSaveButton");
    await assertRealPointerTarget(tabletSave, "平板旅行花费保存按钮");
    await tabletSave.click();
    await pageB.locator("#tripExpenseList").getByText("演示甲的个人纪念品").waitFor();

    await pageA.locator("#tripExpenseAmount").fill("10.01");
    await pageA.locator("#tripExpensePayer").selectOption({ label: "演示甲" });
    await pageA.locator("#tripExpenseCategory").selectOption("food");
    await pageA.locator("#tripExpenseSplit").selectOption("equal");
    await pageA.locator("#tripExpenseDate").fill("2026-10-28");
    await pageA.locator("#tripExpenseNote").fill("浏览器验收晚餐");
    await pageA.locator("#tripExpenseSaveButton").click();
    const firstA = pageA.locator("#tripExpenseList [data-expense-card]", { hasText: "浏览器验收晚餐" });
    await firstA.waitFor();
    assert((await firstA.textContent()).includes("¥10.01"), "金额没有按整数分精确显示");
    await pageB.waitForFunction(() => document.querySelector("#tripExpenseList")?.textContent.includes("浏览器验收晚餐"));
    assert((await pageA.locator("#tripExpenseBalance").textContent()) !== (await pageB.locator("#tripExpenseBalance").textContent()), "双方没有看到各自视角的余额");

    await firstA.locator('[data-action="edit-expense"]').click();
    await pageA.locator("#tripExpenseNote").fill("A 的过期草稿");
    const beforeConflict = await request(pageB, "/api/v1/trips/demo-trip/expenses");
    const staleTarget = beforeConflict.items.find(item => item.note === "浏览器验收晚餐");
    await request(pageB, `/api/v1/trips/demo-trip/expenses/${staleTarget.id}`, "PATCH", { note: "伴侣并发修正", expected_version: staleTarget.version, idempotency_key: "browser:expense:stale-partner" });
    await pageA.locator("#tripExpenseSaveButton").click();
    await pageA.locator("#tripExpenseList").getByText("伴侣并发修正").waitFor();
    assert((await pageA.locator("#toast").textContent()).includes("重新打开"), "过期编辑没有明确要求重新打开最新记录");
    assert((await pageA.locator("#tripExpenseSaveButton").textContent()).includes("记录花费"), "冲突后仍保留旧编辑版本，可无限重复提交");
    assert(await pageA.locator("#tripExpenseCancelButton").isHidden(), "冲突后旧编辑状态没有关闭");

    await pageA.locator("#tripExpenseList [data-expense-card]", { hasText: "伴侣并发修正" }).locator('[data-action="edit-expense"]').click();
    await pageA.locator("#tripExpenseNote").fill("浏览器验收晚餐（已编辑）");
    await pageA.locator("#tripExpenseSaveButton").click();
    await pageA.locator("#tripExpenseList").getByText("浏览器验收晚餐（已编辑）").waitFor();

    const edited = pageA.locator("#tripExpenseList [data-expense-card]", { hasText: "浏览器验收晚餐（已编辑）" });
    await edited.locator('[data-action="delete-expense"]').click();
    await pageA.locator(".trip-expense-history", { has: pageA.locator("#tripExpenseDeletedList") }).locator("summary").click();
    await pageA.locator("#tripExpenseDeletedList").getByText("浏览器验收晚餐（已编辑）").waitFor();
    await pageA.locator('#toast [data-action="restore-expense"]').click();
    await pageA.locator("#tripExpenseList").getByText("浏览器验收晚餐（已编辑）").waitFor();

    await openExpenses(pageA);
    await pageA.waitForFunction(() => document.querySelector("#tripExpenseList")?.textContent.includes("演示甲的个人纪念品"));

    await pageA.locator("#tripExpenseList [data-expense-card]", { hasText: "浏览器验收晚餐（已编辑）" }).locator("[data-expense-settle]").check();
    await pageA.locator("#tripExpenseSettleButton").click();
    await pageA.locator(".trip-expense-history", { has: pageA.locator("#tripExpenseSettledList") }).locator("summary").click();
    await pageA.locator("#tripExpenseSettledList").getByText("浏览器验收晚餐（已编辑）").waitFor();

    const activeBeforeOffline = await pageA.locator("#tripExpenseList [data-expense-card]").count();
    await contextA.setOffline(true);
    await pageA.evaluate(() => window.dispatchEvent(new Event("offline")));
    assert(await pageA.locator("#tripExpenseSaveButton").isDisabled(), "离线时新增按钮仍可写");
    assert(await pageA.locator("#tripExpenseSettleButton").isDisabled(), "离线时结清按钮仍可写");
    assert((await pageA.locator("#tripExpenseList [data-expense-card]").count()) === activeBeforeOffline, "离线切换误改了花费记录");
    await contextA.setOffline(false);
    await pageA.evaluate(() => window.dispatchEvent(new Event("online")));

    for (const page of [pageA, pageB]) {
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert(overflow <= 1, "旅行花费页面出现横向溢出");
    }
    assert(errors.length === 0, "页面脚本错误：" + errors.join(" | "));
    console.log("旅行花费浏览器验收通过：双账号同步、精确金额、编辑、删除恢复、个人支出、结清、离线拒写和手机/平板布局");
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
