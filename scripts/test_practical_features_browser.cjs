/*
 * Task 18 practical-feature acceptance orchestrator.
 *
 * Every scenario uses a fresh temporary SQLite database and media directory.
 * The script never opens the configured production DATABASE_PATH or MEDIA_ROOT.
 */
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");
const chromePath = process.env.PLAYWRIGHT_CHROME_PATH
  || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const assert = (value, message) => { if (!value) throw new Error(message); };
const startedAt = Date.now();
const results = [];
let activeChild = null;

const groups = [
  {
    name: "双人互动",
    coverage: "秘密决策、约会方案、日历、礼物认领、状态卡、表达辅助",
    scripts: ["scripts/test_couple_interactions_browser.cjs"],
  },
  {
    name: "做饭与共同生活",
    coverage: "一周菜单、库存扣减与撤销、食材替代、双端烹饪与计时器",
    scripts: ["scripts/test_ingredient_substitution_browser.cjs", "scripts/test_kitchen_browser.cjs"],
  },
  {
    name: "旅行增强",
    coverage: "协商与真实性、行李、应急卡、共同花费和结算",
    scripts: ["scripts/test_trip_tools_browser.cjs", "scripts/test_trip_expenses_browser.cjs"],
  },
  {
    name: "回忆与记录",
    coverage: "关系搜索、回忆补全、语音回忆、未来信、纸质纪念册",
    scripts: ["scripts/test_memory_browser.cjs", "scripts/test_voice_future_browser.cjs", "scripts/test_album_export_browser.cjs"],
  },
];

function formatDuration(milliseconds) {
  if (milliseconds < 1000) return `${milliseconds}ms`;
  return `${(milliseconds / 1000).toFixed(1)}s`;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const socket = net.createServer();
    socket.once("error", reject);
    socket.listen(0, "127.0.0.1", () => {
      const address = socket.address();
      socket.close(error => error ? reject(error) : resolve(address.port));
    });
  });
}

async function waitForHealth(origin, child) {
  for (let attempt = 0; attempt < 150; attempt += 1) {
    if (child.exitCode !== null) throw new Error("临时测试服务器提前退出");
    try {
      if ((await fetch(`${origin}/api/health`, { cache: "no-store" })).ok) return;
    } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error("临时测试服务器启动超时");
}

async function stopChild(child) {
  if (!child || child.exitCode !== null) return;
  child.kill("SIGTERM");
  await Promise.race([
    new Promise(resolve => child.once("exit", resolve)),
    new Promise(resolve => setTimeout(resolve, 5000)),
  ]);
  if (child.exitCode === null) child.kill("SIGKILL");
}

function runNodeScript(script) {
  return new Promise((resolve, reject) => {
    const absolute = path.join(root, script);
    const child = spawn(process.execPath, [absolute], {
      cwd: root,
      env: { ...process.env },
      stdio: "inherit",
    });
    activeChild = child;
    child.once("error", reject);
    child.once("exit", (code, signal) => {
      activeChild = null;
      if (code === 0) resolve();
      else reject(new Error(`${script} 失败（${signal || `退出码 ${code}`}）`));
    });
  });
}

function preflightIsolation() {
  for (const group of groups) {
    for (const script of group.scripts) {
      const source = fs.readFileSync(path.join(root, script), "utf8");
      assert(source.includes("mkdtempSync"), `${script} 没有创建临时测试目录`);
      assert(source.includes("DATABASE_PATH") && source.includes("MEDIA_ROOT"), `${script} 没有隔离数据库和媒体目录`);
      assert(!source.includes("test_remote_production"), `${script} 不得访问正式服务`);
    }
  }
}

async function login(page, origin, username) {
  await page.goto(`${origin}/`, { waitUntil: "domcontentloaded" });
  await page.locator("#authUsername").fill(username);
  await page.locator("#authPassword").fill("test123");
  await page.locator("#authSubmit").click();
  await page.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
}

async function request(page, endpoint, method = "GET", body = undefined) {
  return page.evaluate(async ({ endpointValue, methodValue, bodyValue }) => {
    const response = await fetch(endpointValue, {
      method: methodValue,
      credentials: "same-origin",
      cache: "no-store",
      headers: bodyValue === undefined ? {} : {
        "Content-Type": "application/json",
        "Idempotency-Key": `practical-browser-${crypto.randomUUID()}`,
      },
      body: bodyValue === undefined ? undefined : JSON.stringify(bodyValue),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(`${endpointValue}: ${response.status} ${payload.error || ""}`);
    return payload;
  }, { endpointValue: endpoint, methodValue: method, bodyValue: body });
}

async function openMealCalendar(page) {
  await page.locator('.bottom-nav [data-nav="life"]').click();
  await page.locator('.section-tabs [data-life-section="meals"]').click();
  await page.locator('[data-meal-section="calendar"]').click();
  await page.locator('#weeklyMealPlanPanel').waitFor({ state: "visible" });
}

async function runWeeklyAndTravelBrowserFlow() {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "love-practical-browser-"));
  const port = await freePort();
  const origin = `http://127.0.0.1:${port}`;
  const server = spawn(process.env.PYTHON_BINARY || "python3", ["main.py"], {
    cwd: root,
    env: {
      ...process.env,
      APP_HOST: "127.0.0.1",
      APP_PORT: String(port),
      DATABASE_PATH: path.join(temp, "practical.db"),
      MEDIA_ROOT: path.join(temp, "media"),
      BACKUP_ROOT: path.join(temp, "backups"),
      DEMO_MODE: "true",
      OPEN_METEO_ENABLED: "false",
      AMAP_WEB_KEY: "",
      GEOAPIFY_ENABLED: "false",
      SERPAPI_ENABLED: "false",
      LLM_API_KEY: "",
      UI_LLM_API_KEY: "",
      LOCAL_ADMIN_NOTIFICATIONS: "false",
      WATCHDOG_AUTO_RESTART: "false",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let logs = "";
  server.stdout.on("data", value => { logs += value; });
  server.stderr.on("data", value => { logs += value; });
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
    const pageErrors = [];
    pageA.on("pageerror", error => pageErrors.push(`演示甲：${error.message}`));
    pageB.on("pageerror", error => pageErrors.push(`演示乙：${error.message}`));
    await Promise.all([login(pageA, origin, "test"), login(pageB, origin, "test1")]);

    // Weekly menu: the browser creates the draft, both real accounts confirm,
    // and the result becomes shared meals rather than a success-only button.
    await openMealCalendar(pageA);
    const createWeeklyResponse = pageA.waitForResponse(response => response.url().endsWith("/api/v1/weekly-meal-plans/drafts") && response.request().method() === "POST");
    await pageA.locator('[data-action="create-weekly-meal-plan"]').click();
    const createWeekly = await createWeeklyResponse;
    const createWeeklyBody = await createWeekly.json();
    assert(createWeekly.ok(), `一周菜单生成失败：${createWeekly.status()} ${createWeeklyBody.error || ""}`);
    assert(createWeeklyBody.items?.length >= 2, `一周菜单接口未返回菜品：${JSON.stringify(createWeeklyBody).slice(0, 1000)}`);
    await pageA.waitForTimeout(300);
    const weeklyUi = await pageA.evaluate(() => ({
      itemCount: document.querySelectorAll("#weeklyMealPlanContent .meal-row").length,
      html: document.querySelector("#weeklyMealPlanContent")?.innerHTML || "",
    }));
    let weeklyRaceError = null;
    if (weeklyUi.itemCount === 0) {
      weeklyRaceError = new Error(`一周菜单接口成功但页面未呈现：${weeklyUi.html.slice(0, 600)}；页面错误：${pageErrors.join(" | ")}`);
      // Reload only to continue exercising confirmation and the remaining
      // travel flow.  The original first-click failure remains a release-gate
      // failure and is thrown after all assertions have run.
      await pageA.reload({ waitUntil: "domcontentloaded" });
      await pageA.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
      await openMealCalendar(pageA);
      await pageA.locator('#weeklyMealPlanContent .meal-row').first().waitFor();
    }
    const created = await request(pageA, "/api/v1/weekly-meal-plans/active");
    assert(created.id && created.status === "draft", "浏览器没有真正创建一周菜单草稿");
    assert(created.items.length >= 2 && created.items.every(item => item.recipe), "周菜单没有生成可确认的真实菜品");
    const firstConfirmResponse = pageA.waitForResponse(response => response.url().includes(`/weekly-meal-plans/drafts/${created.id}/confirm`) && response.request().method() === "POST");
    await pageA.locator('[data-action="confirm-weekly-meal-plan"]').click();
    const firstConfirmed = await (await firstConfirmResponse).json();
    assert(firstConfirmed.status === "pending_confirmation", "第一人确认后没有进入等待伴侣状态");

    await pageB.reload({ waitUntil: "domcontentloaded" });
    await pageB.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
    await openMealCalendar(pageB);
    await pageB.locator('[data-action="confirm-weekly-meal-plan"]').waitFor();
    const secondConfirmResponse = pageB.waitForResponse(response => response.url().includes(`/weekly-meal-plans/drafts/${created.id}/confirm`) && response.request().method() === "POST");
    await pageB.locator('[data-action="confirm-weekly-meal-plan"]').click();
    const applied = await (await secondConfirmResponse).json();
    assert(applied.status === "applied" && applied.result?.meal_item_ids?.length >= 2, "双方确认后周菜单没有写入共同用餐计划");
    const cart = await request(pageA, "/api/v1/meal-cart");
    assert(cart.items.some(item => applied.result.meal_item_ids.includes(item.id)), "生效周菜单没有出现在共同点菜车");

    // Travel negotiation currently has no independent H5 editor. Exercise its
    // authenticated two-account HTTP contract from two real browser sessions so
    // cookie/origin/permission/reveal behavior is still release-gated.
    const tripId = "demo-trip";
    const open = await request(pageA, `/api/v1/trips/${tripId}/negotiation`);
    assert(open.state === "open" && open.candidates.length > 0, "旅行协商没有读取当前方案项目");
    const preferencesA = open.candidates.map((item, index) => ({
      item_id: item.id,
      preference: index === 0 ? "must" : (index === 1 ? "avoid" : "okay"),
      expected_item_version: item.version,
    }));
    const submittedA = await request(pageA, `/api/v1/trips/${tripId}/negotiation/preferences`, "PUT", {
      expected_trip_version: open.trip_version,
      expected_round: open.round,
      start_new_round: false,
      preferences: preferencesA,
    });
    assert(submittedA.state === "waiting_for_partner" && submittedA.my_preferences?.length === open.candidates.length, "第一人的旅行选择没有私密保存");
    const partnerBefore = await request(pageB, `/api/v1/trips/${tripId}/negotiation`);
    assert(partnerBefore.partner_submitted && !partnerBefore.my_preferences && !partnerBefore.ballots, "揭晓前向伴侣泄露了旅行选择");
    const preferencesB = partnerBefore.candidates.map((item, index) => ({
      item_id: item.id,
      preference: index === 0 ? "must" : "okay",
      expected_item_version: item.version,
    }));
    const revealedB = await request(pageB, `/api/v1/trips/${tripId}/negotiation/preferences`, "PUT", {
      expected_trip_version: partnerBefore.trip_version,
      expected_round: partnerBefore.round,
      start_new_round: false,
      preferences: preferencesB,
    });
    const revealedA = await request(pageA, `/api/v1/trips/${tripId}/negotiation`);
    assert(revealedB.state === "revealed" && JSON.stringify(revealedA) === JSON.stringify(revealedB), "双方没有获得相同的旅行协商结果");

    const realism = await request(pageA, `/api/v1/trips/${tripId}/realism-checks`, "POST", {
      expected_trip_version: revealedA.trip_version,
      expected_item_versions: Object.fromEntries(revealedA.candidates.map(item => [item.id, item.version])),
    });
    assert(realism.source_type === "rules" && realism.actionable && Array.isArray(realism.findings), "旅行真实性检查没有返回可追溯结果");
    const partnerRealism = await request(pageB, `/api/v1/trips/${tripId}/realism-checks/current`);
    assert(partnerRealism.id === realism.id && partnerRealism.findings.length === realism.findings.length, "伴侣没有看到同一份旅行真实性检查");
    assert(pageErrors.length === 0, `页面脚本错误：${pageErrors.join(" | ")}`);
    if (weeklyRaceError) throw weeklyRaceError;
    console.log("定向双账号验收通过：一周菜单双方确认、旅行秘密协商揭晓、真实性检查共享");
  } catch (error) {
    error.message += `\n临时服务器日志：\n${logs.slice(-6000)}`;
    throw error;
  } finally {
    if (browser) await browser.close();
    await stopChild(server);
    fs.rmSync(temp, { recursive: true, force: true });
  }
}

async function runCheck(name, coverage, action) {
  const begin = Date.now();
  process.stdout.write(`\n\n▶ ${name}\n  ${coverage}\n`);
  try {
    await action();
    const elapsed = Date.now() - begin;
    results.push({ name, coverage, status: "PASS", elapsed });
    process.stdout.write(`✓ ${name}（${formatDuration(elapsed)}）\n`);
  } catch (error) {
    const elapsed = Date.now() - begin;
    results.push({ name, coverage, status: "FAIL", elapsed, error: error.message });
    console.error(`✗ ${name}（${formatDuration(elapsed)}）：${error.message.split("\n")[0]}`);
    return false;
  }
  return true;
}

function printSummary() {
  process.stdout.write("\n\n实用功能双账号验收汇总\n");
  process.stdout.write("=".repeat(72) + "\n");
  for (const result of results) {
    process.stdout.write(`${result.status.padEnd(4)}  ${result.name.padEnd(24)} ${formatDuration(result.elapsed)}\n`);
    process.stdout.write(`      ${result.coverage}\n`);
    if (result.error) process.stdout.write(`      ${result.error.split("\n")[0]}\n`);
  }
  process.stdout.write("-".repeat(72) + "\n");
  process.stdout.write(`总耗时：${formatDuration(Date.now() - startedAt)}；正式数据访问：0\n`);
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.once(signal, () => {
    if (activeChild && activeChild.exitCode === null) activeChild.kill(signal);
    process.exitCode = 130;
  });
}

(async () => {
  preflightIsolation();
  await runCheck(
    "补充双账号端到端",
    "H5 一周菜单确认 + 浏览器会话中的旅行协商和真实性检查",
    runWeeklyAndTravelBrowserFlow,
  );
  for (const group of groups) {
    await runCheck(group.name, group.coverage, async () => {
      for (const script of group.scripts) await runNodeScript(script);
    });
  }
  printSummary();
  if (results.some(result => result.status === "FAIL")) process.exitCode = 1;
})().catch(error => {
  console.error(`\n实用功能双账号验收无法启动：${error.stack || error.message}`);
  printSummary();
  process.exitCode = 1;
});
