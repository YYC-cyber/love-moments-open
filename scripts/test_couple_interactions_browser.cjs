const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");
const chromePath = process.env.PLAYWRIGHT_CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const assert = (value, message) => { if (!value) throw new Error(message); };
const freePort = () => new Promise((resolve, reject) => { const socket = net.createServer(); socket.once("error", reject); socket.listen(0, "127.0.0.1", () => { const port = socket.address().port; socket.close(error => error ? reject(error) : resolve(port)); }); });
async function waitForHealth(origin, child) { for (let n = 0; n < 100; n += 1) { if (child.exitCode !== null) throw new Error("测试服务器提前退出"); try { if ((await fetch(origin + "/api/health")).ok) return; } catch (_) {} await new Promise(resolve => setTimeout(resolve, 100)); } throw new Error("测试服务器启动超时"); }
async function login(page, origin, username) { await page.goto(origin + "/", { waitUntil: "domcontentloaded" }); await page.locator("#authUsername").fill(username); await page.locator("#authPassword").fill("test123"); await page.locator("#authSubmit").click(); await page.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden")); }
async function api(page, endpoint) { return page.evaluate(async value => { const response = await fetch(value, { credentials: "same-origin", cache: "no-store" }); const body = await response.json(); if (!response.ok) throw new Error(body.error || String(response.status)); return body; }, endpoint); }
async function openDecisions(page) { const button = page.locator('.life-more-actions [data-life-section="decisions"]'); await button.evaluate(element => element.click()); await page.locator('[data-life-panel="decisions"]:not([hidden])').waitFor(); }

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "love-decisions-browser-"));
  const port = await freePort(); const origin = `http://127.0.0.1:${port}`;
  const server = spawn(process.env.PYTHON_BINARY || "python3", ["main.py"], { cwd: root, env: { ...process.env, APP_HOST: "127.0.0.1", APP_PORT: String(port), DATABASE_PATH: path.join(temp, "app.db"), MEDIA_ROOT: path.join(temp, "media"), BACKUP_ROOT: path.join(temp, "backups"), DEMO_MODE: "true", OPEN_METEO_ENABLED: "false", AMAP_WEB_KEY: "", GEOAPIFY_ENABLED: "false", SERPAPI_ENABLED: "false", LLM_API_KEY: "", UI_LLM_API_KEY: "", LOCAL_ADMIN_NOTIFICATIONS: "false", WATCHDOG_AUTO_RESTART: "false" }, stdio: ["ignore", "pipe", "pipe"] });
  let logs = ""; server.stdout.on("data", value => { logs += value; }); server.stderr.on("data", value => { logs += value; });
  let browser;
  try {
    await waitForHealth(origin, server);
    const launch = { headless: true }; if (fs.existsSync(chromePath)) launch.executablePath = chromePath;
    browser = await chromium.launch(launch);
    const a = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: "block" });
    const b = await browser.newContext({ viewport: { width: 768, height: 1024 }, serviceWorkers: "block" });
    const pageA = await a.newPage(); const pageB = await b.newPage();
    const errors = []; pageA.on("pageerror", e => errors.push(e.message)); pageB.on("pageerror", e => errors.push(e.message));
    await Promise.all([login(pageA, origin, "test"), login(pageB, origin, "test1")]);
    assert(await pageA.locator(".bottom-nav [data-nav]").count() === 5, "不能增加第六个主导航");
    await pageA.locator('.bottom-nav [data-nav="life"]').click();
    assert(await pageA.locator('.life-more-actions [data-life-section="decisions"]').count() === 1, "双人决策必须收在更多中");
    await pageA.locator(".life-more > summary").click();
    await openDecisions(pageA);
    await pageA.locator('[data-action="new-decision"]').first().click();
    await pageA.locator("#decisionTitle").fill("周末吃什么");
    await pageA.locator("#decisionOptions").fill("火锅\n烤肉");
    await pageA.locator("#decisionForm button[type=submit]").click();
    await pageA.locator("#decisionList .decision-card").first().waitFor();
    const decisionId = (await api(pageA, "/api/v1/decisions")).items[0].id;
    const firstOption = (await api(pageA, `/api/v1/decisions/${decisionId}`)).options[0];
    await pageA.locator(`[data-decision-option="${firstOption.id}"]`).click();
    await pageA.locator(`[data-action="submit-decision-vote"][data-id="${decisionId}"]`).click();
    await pageB.reload(); await pageB.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden")); await pageB.locator('.bottom-nav [data-nav="life"]').click(); await pageB.locator(".life-more > summary").click();
    await openDecisions(pageB);
    await pageB.locator(`[data-decision-option="${firstOption.id}"]`).click();
    const before = await pageB.locator(`#decisionList article[data-decision-id="${decisionId}"]`).textContent();
    assert(!before.includes("双方选择") && !before.includes("1 票"), "揭晓前页面泄露了票数或选择");
    await pageB.locator(`[data-action="submit-decision-vote"][data-id="${decisionId}"]`).click();
    await pageB.locator(`[data-action="resolve-decision"][data-id="${decisionId}"]`).click();
    await pageA.reload(); await pageA.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden")); await pageA.locator('.bottom-nav [data-nav="life"]').click(); await openDecisions(pageA);
    await pageA.locator(`[data-action="resolve-decision"][data-id="${decisionId}"]`).click();
    await pageA.locator(`[data-action="convert-decision"][data-id="${decisionId}"]`).first().waitFor();
    const conversionTargets = await pageA.locator(`[data-action="convert-decision"][data-id="${decisionId}"]`).evaluateAll(nodes => [...new Set(nodes.map(node => node.dataset.target))].sort());
    assert(JSON.stringify(conversionTargets) === JSON.stringify(["calendar_todo", "date_plan", "hotel_search", "meal_wish", "restaurant_search", "trip"]), `已确认结果没有覆盖六个安全预填编辑器：${conversionTargets.join(",")}`);
    await pageA.locator(`[data-action="convert-decision"][data-target="trip"]`).click();
    await pageA.locator("#tripModal.open").waitFor();
    assert((await pageA.locator("#tripPrefillNotice").textContent()).includes("尚未保存"), "旅行预填没有明确标记尚未保存");
    assert((await pageA.locator("#tripDestination").inputValue()) === firstOption.label, "旅行没有使用服务器确认的结果预填");
    await pageA.locator('#tripModal [data-action="close-modal"]').click();
    await openDecisions(pageA);
    await pageA.locator(`[data-action="convert-decision"][data-target="meal_wish"]`).click();
    await pageA.locator("#mealWishModal.open").waitFor();
    assert((await pageA.locator("#mealWishModal").textContent()).includes("尚未保存"), "转换预填没有明确标记尚未保存");
    const wishesBefore = (await api(pageA, "/api/v1/meal-wishes")).items.length;
    assert((await api(pageA, "/api/v1/meal-wishes")).items.length === wishesBefore, "打开预填就写入了目标记录");
    await pageA.locator("#mealWishForm .primary-button").click();
    await pageA.waitForFunction(async expected => (await (await fetch("/api/v1/meal-wishes")).json()).items.length === expected, wishesBefore + 1);
    await openDecisions(pageA);
    await pageA.locator(`[data-action="convert-decision"][data-target="hotel_search"]`).click();
    await pageA.locator('[data-trip-panel="tools"]:not([hidden])').waitFor();
    assert((await pageA.locator("#hotelSearchPrefillNotice").textContent()).includes("尚未保存"), "酒店搜索预填没有明确标记尚未保存");
    assert((await pageA.locator("#hotelDestination").inputValue()) === firstOption.label, "酒店查询没有使用服务器确认的结果预填");
    assert((await api(pageA, "/api/v1/meal-wishes")).items.length === wishesBefore + 1, "酒店预填产生了额外目标写入");
    await pageA.locator('.bottom-nav [data-nav="life"]').click();
    await pageA.locator('[data-life-section="dates"]').first().click(); await pageA.locator('[data-life-panel="dates"] [data-action="new-date"]').click();
    await pageA.locator("#dateCity").fill("西湖"); await pageA.locator("#dateRegion").fill("杭州市");
    await pageA.locator('[data-action="search-date-place"]').click();
    await pageA.locator("#datePlaceCandidates button").first().waitFor();
    assert(await pageA.locator("#dateForm button[type=submit]").isDisabled(), "未选择地点时不应允许生成");
    await pageA.locator("#datePlaceCandidates button").first().click();
    assert(!(await pageA.locator("#dateForm button[type=submit]").isDisabled()), "明确选择地点后应允许生成");
    await pageA.locator("#dateForm button[type=submit]").click();
    await pageA.locator("#datePlanList .date-plan-card").first().waitFor();
    const dateText = await pageA.locator("#datePlanList .date-plan-card").first().textContent();
    assert(dateText.includes("预计通勤"), "约会方案没有展示规则估算通勤");
    assert(dateText.includes("天气数据不可用"), "天气不可用时没有展示具体风险提示");

    await pageA.locator('[data-action="add-life"]').first().evaluate(element => element.click());
    await pageA.locator("#calendarModal.open").waitFor();
    await pageA.locator("#calendarTitle").fill("定时双人安排");
    await pageA.locator("#calendarAllDay").uncheck();
    await pageA.locator("#calendarStart").fill("2026-11-21T14:00");
    await pageA.locator("#calendarEnd").fill("2026-11-21T16:00");
    const memberIds = [(await api(pageA, "/api/v1/bootstrap")).user.id, (await api(pageA, "/api/v1/bootstrap")).partner.id];
    await pageA.locator("#calendarParticipants").selectOption(memberIds);
    await pageA.locator("#calendarBlocksTime").check();
    await Promise.all([
      pageA.waitForResponse(response => response.url().endsWith("/api/v1/calendar") && response.request().method() === "POST"),
      pageA.locator("#calendarForm .primary-button").click(),
    ]);
    const timed = (await api(pageA, "/api/v1/calendar")).items.find(item => item.title === "定时双人安排");
    assert(timed && !timed.all_day && timed.ends_at && timed.blocks_time, "H5 没有完整保存定时占用字段");
    assert(timed.participant_ids.length === 2, "H5 没有完整保存参与人");
    await pageA.locator(`[data-action="edit-calendar"][data-id="${timed.id}"]`).evaluate(element => element.click());
    assert(!(await pageA.locator("#calendarAllDay").isChecked()), "编辑定时事项时全天开关回填错误");
    assert((await pageA.locator("#calendarEnd").inputValue()).includes("2026-11-21"), "编辑定时事项时结束时间丢失");
    assert((await pageA.locator("#calendarParticipants").locator("option:checked").count()) === 2, "编辑定时事项时参与人丢失");
    await pageA.locator('#calendarModal [data-action="close-modal"]').click();
    const dailyAllDay = await pageA.evaluate(async ids => {
      const response = await fetch("/api/v1/calendar", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
        entry_type: "event", title: "多日重复安排", starts_at: "2026-11-22", ends_at: "2026-11-25",
        all_day: true, recurrence: "daily", participant_ids: ids, blocks_time: true,
      }) });
      if (!response.ok) throw new Error((await response.json()).error || String(response.status));
      return response.json();
    }, memberIds);
    await pageA.reload(); await pageA.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
    await pageA.locator('.bottom-nav [data-nav="life"]').click();
    await pageA.locator(`[data-action="edit-calendar"][data-id="${dailyAllDay.id}"]`).evaluate(element => element.click());
    assert(await pageA.locator("#calendarAllDay").isChecked(), "编辑多日全天事项时全天开关回填错误");
    assert((await pageA.locator("#calendarEnd").inputValue()) === "2026-11-25", "编辑多日全天事项时结束日期丢失");
    assert((await pageA.locator("#calendarRecurrence").inputValue()) === "daily", "编辑旧 daily 事项时重复规则丢失");
    await Promise.all([
      pageA.waitForResponse(response => response.url().includes(`/api/v1/calendar/${dailyAllDay.id}`) && response.request().method() === "PATCH"),
      pageA.locator("#calendarForm .primary-button").click(),
    ]);
    const dailySaved = (await api(pageA, "/api/v1/calendar")).items.find(item => item.id === dailyAllDay.id);
    assert(dailySaved.recurrence === "daily", "保存编辑后 daily 重复规则被静默改变");
    assert(dailySaved.ends_at === "2026-11-25", "保存编辑后多日全天范围被缩短");

    // Task 6: gift claims remain private to the claimant, including after the
    // creator reloads the shared wishlist.
    const giftTitle = `浏览器私密礼物-${Date.now()}`;
    await pageA.locator('.bottom-nav [data-nav="life"]').click();
    await pageA.locator('[data-life-section="wishlist"]').first().click();
    await pageA.locator('[data-action="open-wishlist-editor"]').click();
    await pageA.locator("#wishlistText").fill(giftTitle);
    await pageA.locator("#wishlistKind").selectOption("gift");
    await pageA.locator("#wishlistNote").fill("公开愿望备注");
    await Promise.all([
      pageA.waitForResponse(response => response.url().endsWith("/api/v1/lists/wishlist/items") && response.request().method() === "POST"),
      pageA.locator("#wishlistForm .primary-button").click(),
    ]);
    const gift = (await api(pageA, "/api/v1/lists/wishlist")).items.find(item => item.text === giftTitle);
    assert(gift && gift.item_kind === "gift", "礼物心愿没有真实保存");
    const creatorGift = pageA.locator(".wishlist .wish-row", { hasText: giftTitle });
    const creatorBeforeClaim = await creatorGift.evaluate(element => element.outerHTML);
    assert(!creatorBeforeClaim.includes("claim") && !creatorBeforeClaim.includes("认领"), "创建者页面提前出现认领信息");

    await pageB.reload(); await pageB.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
    await pageB.locator('.bottom-nav [data-nav="life"]').click();
    await pageB.locator('[data-life-section="wishlist"]').first().click();
    const claimantGift = pageB.locator(".wishlist .wish-row", { hasText: giftTitle });
    await claimantGift.locator('[data-action="claim-wish"]').waitFor();
    pageB.once("dialog", dialog => dialog.accept("只保留在认领者视图"));
    await Promise.all([
      pageB.waitForResponse(response => response.url().includes(`/api/v1/lists/items/${gift.id}/claim`) && response.request().method() === "POST"),
      claimantGift.locator('[data-action="claim-wish"]').click(),
    ]);
    await claimantGift.locator('[data-action="complete-wish-claim"]').waitFor();
    assert((await claimantGift.textContent()).includes("取消认领"), "认领者看不到自己的私密认领操作");

    await pageA.reload(); await pageA.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
    await pageA.locator('.bottom-nav [data-nav="life"]').click();
    await pageA.locator('[data-life-section="wishlist"]').first().click();
    const creatorAfterClaim = await pageA.locator(".wishlist .wish-row", { hasText: giftTitle }).evaluate(element => element.outerHTML);
    assert(creatorAfterClaim === creatorBeforeClaim, "创建者 DOM 在秘密认领前后发生了可观察变化");

    // Expression suggestions are private drafts: applying one changes only
    // the composer and never creates a message by itself.
    await pageA.locator('.bottom-nav [data-nav="chat"]').click();
    const messageCountBeforeExpression = (await api(pageA, "/api/v1/messages")).items.length;
    await pageA.locator("#chatInput").fill("我有点生气，你先不要打扰我");
    await pageA.locator('[data-action="expression-help"][aria-label="帮我表达"]').click();
    await pageA.locator("#expressionModal.open").waitFor();
    await pageA.locator("#expressionMode").selectOption("gentle");
    await Promise.all([
      pageA.waitForResponse(response => response.url().endsWith("/api/v1/expression/suggestions") && response.request().method() === "POST"),
      pageA.locator("#expressionForm .primary-button").click(),
    ]);
    await pageA.locator("#expressionPreview:not([hidden])").waitFor();
    const suggestedText = await pageA.locator("#expressionResult").inputValue();
    assert(suggestedText && suggestedText !== "我有点生气，你先不要打扰我", "表达辅助没有生成可审阅建议");
    await pageA.locator('[data-action="apply-expression"]').click();
    assert((await pageA.locator("#chatInput").inputValue()) === suggestedText, "建议没有只回填到消息输入框");
    assert((await api(pageA, "/api/v1/messages")).items.length === messageCountBeforeExpression, "表达辅助未经确认就发送了消息");

    // Status cards use the exact server enum and a bounded server duration.
    await pageA.locator('.bottom-nav [data-nav="home"]').click();
    await pageA.locator('.status-card[data-action="edit-status"]').click();
    await pageA.locator("#statusCode").selectOption("need_company");
    await pageA.locator("#statusText").fill("今天想有人陪");
    await pageA.locator("#statusDuration").selectOption("30");
    await Promise.all([
      pageA.waitForResponse(response => response.url().endsWith("/api/v1/me/status") && response.request().method() === "PUT"),
      pageA.locator("#statusForm .primary-button").click(),
    ]);
    const statusContext = await api(pageA, "/api/v1/couple/context");
    const myStatus = statusContext.members.find(member => member.is_me);
    assert(myStatus.status_code === "need_company" && myStatus.status_text === "今天想有人陪", "状态卡没有按服务端时限契约保存");
    assert(errors.length === 0, "页面脚本错误：" + errors.join(" | "));
    console.log("双人互动浏览器验收通过：秘密投票、礼物私密认领、表达只回填、限时状态、纯预填、约会与完整日历");
  } catch (error) { if (logs.trim()) console.error(String(logs).slice(-8000)); throw error; }
  finally { if (browser) await browser.close(); if (server.exitCode === null) server.kill("SIGTERM"); fs.rmSync(temp, { recursive: true, force: true }); }
})().catch(error => { console.error(error.stack || error.message); process.exitCode = 1; });
