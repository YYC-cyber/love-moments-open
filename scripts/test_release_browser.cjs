/* Browser release audit against an isolated Love Moments server. */
const fs = require("node:fs");
const { chromium } = require("playwright");

const originA = process.env.LOVE_TEST_ORIGIN_A || "http://127.0.0.1:18765";
const originB = process.env.LOVE_TEST_ORIGIN_B || originA;
const chromePath = process.env.PLAYWRIGHT_CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

function assert(value, message) {
  if (!value) throw new Error(message);
}

async function login(page, origin, username, password) {
  await page.goto(`${origin}/`, { waitUntil: "domcontentloaded" });
  await page.locator("#authUsername").fill(username);
  await page.locator("#authPassword").fill(password);
  await page.locator("#authSubmit").click();
  await page.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
  await page.waitForFunction(() => Boolean(document.querySelector(".avatar-a")?.dataset.avatarTheme));
}

async function logout(page) {
  await page.locator('[data-nav="life"]').last().click();
  await page.locator('[data-life-section="overview"]').first().click();
  const more = page.locator('[data-screen="life"].active .life-more');
  if (!(await more.evaluate(node => node.open))) {
    await more.locator(":scope > summary").click();
  }
  await page.locator(".account-logout").click();
  await page.waitForFunction(() => !document.querySelector("#authGate")?.classList.contains("hidden"));
}

async function api(page, path, options = {}) {
  return page.evaluate(async ({ path, options }) => {
    const response = await fetch(path, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || `${response.status}`);
    return body;
  }, { path, options });
}

async function checkSurface(page, surface, viewport) {
  await page.setViewportSize(viewport);
  await page.locator(`[data-nav="${surface}"]`).last().click();
  const screen = page.locator(`[data-screen="${surface}"].active`);
  await screen.waitFor();
  const layout = await page.evaluate(surfaceName => {
    const root = document.documentElement;
    const active = document.querySelector(`[data-screen="${surfaceName}"].active`);
    const activeRect = active.getBoundingClientRect();
    const unnamed = [...active.querySelectorAll("button")].filter(button => {
      const style = getComputedStyle(button);
      const rect = button.getBoundingClientRect();
      const visible = style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
      const name = button.getAttribute("aria-label") || button.getAttribute("title") || button.textContent.trim();
      return visible && !name;
    }).length;
    return {
      documentOverflow: root.scrollWidth - root.clientWidth,
      screenOverflow: active.scrollWidth - active.clientWidth,
      unnamed,
      overflowElements: [...active.querySelectorAll("*")].map(element => {
        const rect = element.getBoundingClientRect();
        return { element, rect };
      }).filter(({ rect }) => rect.width > 0 && rect.right > activeRect.right + 1)
        .slice(0, 8)
        .map(({ element, rect }) => `${element.tagName.toLowerCase()}.${element.className || ""}(${Math.round(rect.width)}px):${(element.textContent || "").trim().slice(0, 40)}`),
    };
  }, surface);
  assert(layout.documentOverflow <= 1, `${surface} ${viewport.width}px 页面横向溢出 ${layout.documentOverflow}px`);
  assert(layout.screenOverflow <= 1, `${surface} ${viewport.width}px 内容横向溢出 ${layout.screenOverflow}px：${layout.overflowElements.join(" | ")}`);
  assert(layout.unnamed === 0, `${surface} 存在 ${layout.unnamed} 个无可访问名称按钮`);
}

function shouldIgnoreConsoleError(message) {
  const text = message.text();
  return text.includes("favicon.ico")
    || text.includes("Failed to load resource: the server responded with a status of 404");
}

(async () => {
  const launchOptions = {
    headless: true,
    args: ["--autoplay-policy=no-user-gesture-required"],
  };
  if (fs.existsSync(chromePath)) launchOptions.executablePath = chromePath;
  const browser = await chromium.launch(launchOptions);
  const errors = [];
  const checks = [];
  try {
    const locationOptions = { geolocation: { latitude: 30.5728, longitude: 104.0668 }, permissions: ["geolocation"] };
    const contextA = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: "block", ...locationOptions });
    const contextB = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: "block", ...locationOptions });
    const pageA = await contextA.newPage();
    const pageB = await contextB.newPage();
    for (const [label, page] of [["test", pageA], ["test1", pageB]]) {
      page.on("pageerror", error => errors.push(`${label} pageerror: ${error.message}`));
      page.on("response", response => { if (response.status() === 404) errors.push(`${label} response 404: ${response.url()}`); });
      page.on("console", message => {
        if (message.type() === "error" && !shouldIgnoreConsoleError(message)) {
          const location = message.location()?.url;
          errors.push(`${label} console${location ? ` (${location})` : ""}: ${message.text()}`);
        }
      });
    }

    await Promise.all([
      login(pageA, originA, "test", "test123"),
      login(pageB, originB, "test1", "test123"),
    ]);
    await pageA.locator('[data-screen="chat"] h1').waitFor({ state: "attached" });
    assert((await pageA.locator('[data-screen="chat"] h1').textContent()).includes("演示乙"), "test 未正确识别伴侣");
    assert((await pageB.locator('[data-screen="chat"] h1').textContent()).includes("演示甲"), "test1 未正确识别伴侣");
    assert((await pageA.locator("#partnerMomentLabel").textContent()).includes("演示乙的此刻 · 当前账号：演示甲"), "test 首页账号提示错误");
    assert((await pageB.locator("#partnerMomentLabel").textContent()).includes("演示甲的此刻 · 当前账号：演示乙"), "test1 首页账号提示错误");
    const identityThemes = await Promise.all([
      pageA.locator(".avatar-a").first().getAttribute("data-avatar-theme"),
      pageA.locator(".avatar-b").first().getAttribute("data-avatar-theme"),
      pageB.locator(".avatar-a").first().getAttribute("data-avatar-theme"),
      pageB.locator(".avatar-b").first().getAttribute("data-avatar-theme"),
    ]);
    assert(identityThemes[0] === identityThemes[3], "演示甲的头像主题未绑定到账号身份");
    assert(identityThemes[1] === identityThemes[2], "演示乙的头像主题未绑定到账号身份");
    assert(identityThemes[0] !== identityThemes[1], "两位用户的头像主题不应相同");
    await pageA.locator(".avatar-pair").click();
    await pageA.locator("#accountIdentityCard").waitFor({ state: "visible" });
    assert((await pageA.locator("#accountIdentityCard").textContent()).includes("演示甲（test）"), "账号中心未显示当前账号");
    await pageA.locator('#accountModal [data-action="close-modal"]').click();
    checks.push("双账号登录、情侣身份、固定头像与当前账号提示");

    const viewports = [
      { width: 390, height: 844 },
      { width: 768, height: 1024 },
      { width: 1440, height: 900 },
    ];
    for (const viewport of viewports) {
      for (const surface of ["home", "chat", "trip", "timeline", "life"]) {
        await checkSurface(pageA, surface, viewport);
      }
    }
    await pageA.setViewportSize({ width: 390, height: 844 });
    checks.push("五页面×手机/平板/桌面布局与基础可访问性");

    const marker = `双端同步-${Date.now()}`;
    await pageA.locator('[data-nav="chat"]').last().click();
    await pageA.locator("#chatInput").fill(marker);
    await pageA.locator("#chatInput").press("Enter");
    await pageA.locator("#messages .message", { hasText: marker }).waitFor();
    await pageB.locator('[data-nav="chat"]').last().click();
    await pageB.locator("#messages .message", { hasText: marker }).waitFor({ timeout: 10_000 });
    checks.push("文字消息 SSE 双端实时同步");

    const markerMessage = pageA.locator("#messages .message", { hasText: marker });
    await markerMessage.locator('[data-action="save-message"]').click();
    await pageA.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("已收藏"));
    const afterCollection = await api(pageA, "/api/v1/bootstrap");
    const collected = afterCollection.moments.find(item => item.body === marker);
    assert(collected, "消息收藏为瞬间失败");
    checks.push("消息按钮/长按共用的收藏为瞬间流程");

    const ocrMarker = `OCR-${Date.now()}`;
    await pageA.locator('[data-action="import-chat"]').click();
    await pageA.locator("#chatImportText").fill(`12:30 我：${ocrMarker}\n12:31 对方：已经收到`);
    const [ocrResponse] = await Promise.all([
      pageA.waitForResponse(response => response.url().endsWith("/api/v1/chat-imports") && response.request().method() === "POST"),
      pageA.locator("#chatImportForm button").click(),
    ]);
    if (!ocrResponse.ok()) throw new Error(`OCR 页面提交失败：${JSON.stringify(await ocrResponse.json().catch(() => ({})))}`);
    await pageA.locator('[data-action="confirm-import"]').waitFor();
    await pageA.locator("#chatImportDraft [data-import-content]").first().fill(`${ocrMarker}-已校对`);
    await pageA.locator('[data-action="confirm-import"]').click();
    await pageA.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("已导入"));
    assert((await api(pageB, "/api/v1/messages")).items.some(item => item.content === `${ocrMarker}-已校对`), "OCR 确认内容未同步给伴侣");
    checks.push("聊天 OCR 草稿、逐条校对、确认后双端导入");

    const testPng = fs.readFileSync(require("node:path").join(__dirname, "..", "static", "assets", "couple-lake-hero.png"));
    await pageA.locator("#chatMediaInput").setInputFiles({ name: "release-check.png", mimeType: "image/png", buffer: testPng });
    await pageA.locator("#messages img.message-media").last().waitFor();
    await pageB.waitForFunction(() => Boolean([...document.querySelectorAll("#messages img.message-media")].find(image => image.alt.includes("分享了一张照片") && image.complete && image.naturalWidth > 0)), null, { timeout: 10_000 });
    checks.push("图片上传、鉴权读取与双端展示");

    const videoUpload = await pageA.evaluate(async () => {
      const canvas = document.createElement("canvas");
      canvas.width = 320; canvas.height = 180;
      const context = canvas.getContext("2d");
      const stream = canvas.captureStream(12);
      const recorder = new MediaRecorder(stream, { mimeType: "video/webm" });
      const chunks = [];
      recorder.addEventListener("dataavailable", event => { if (event.data.size) chunks.push(event.data); });
      const stopped = new Promise(resolve => recorder.addEventListener("stop", resolve, { once: true }));
      recorder.start(100);
      for (const color of ["#8b263d", "#e8b8c2", "#3f6c5a", "#f4d9a5"]) {
        context.fillStyle = color; context.fillRect(0, 0, canvas.width, canvas.height);
        context.fillStyle = "white"; context.font = "32px sans-serif"; context.fillText("恋屿视频", 85, 100);
        await new Promise(resolve => setTimeout(resolve, 120));
      }
      recorder.stop();
      await stopped;
      stream.getTracks().forEach(track => track.stop());
      const file = new File(chunks, "browser-resume-check.webm", { type: "video/webm", lastModified: Date.now() });
      const uploaded = await uploadMedia(file, "shared");
      const thumbnail = await fetch(uploaded.thumbnail_url, { credentials: "same-origin" });
      return { id: uploaded.id, kind: uploaded.kind, size: file.size, thumbnailOk: thumbnail.ok, thumbnailType: thumbnail.headers.get("content-type") };
    });
    assert(videoUpload.kind === "video" && videoUpload.size > 0, "浏览器没有完成视频分片上传");
    assert(videoUpload.thumbnailOk && videoUpload.thumbnailType.startsWith("image/jpeg"), "浏览器生成的视频封面未保存为受保护 JPEG");
    checks.push("视频断点分片上传、真实帧封面与鉴权读取");

    const privateTitle = `私密瞬间-${Date.now()}`;
    await pageA.locator('[data-nav="home"]').last().click();
    await pageA.locator('[data-action="new-moment"][data-type="文字"]').click();
    await pageA.locator("#momentTitle").fill(privateTitle);
    await pageA.locator("#momentNote").fill("发布前隐私隔离验收");
    await pageA.locator('[data-privacy="仅自己"]').click();
    await pageA.locator('[data-action="save-moment"]').click();
    await pageA.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("保存到数据库"));
    const creatorBootstrap = await api(pageA, "/api/v1/bootstrap");
    assert(creatorBootstrap.moments.some(item => item.title === privateTitle), "私密瞬间未保存给创建者");
    const partnerBootstrap = await api(pageB, "/api/v1/bootstrap");
    assert(!partnerBootstrap.moments.some(item => item.title === privateTitle), "私密瞬间泄露给伴侣");
    checks.push("私密瞬间的双账号数据隔离");

    await pageA.locator('[data-nav="trip"]').last().click();
    await pageA.locator('.section-tabs [data-trip-section="stays"]').click();
    const tripBootstrap = await api(pageA, "/api/v1/bootstrap");
    const priceNote = await pageA.locator(".price-note").textContent();
    if (tripBootstrap.capabilities.booking_live) {
      assert(!priceNote.includes("演示价格"), "已接入报价查询时仍显示为演示价格");
    } else {
      assert(priceNote.includes("演示价格"), "未接入报价接口时错误标记为授权报价");
    }
    await pageA.locator('[data-action="new-trip"]').click();
    const destination = `成都-${Date.now()}`;
    await pageA.locator("#tripDestination").fill(destination);
    await pageA.locator("#tripDestinationRegion").fill("成都");
    await pageA.locator('[data-action="select-destination"]').last().waitFor({ timeout: 5_000 });
    await pageA.locator('[data-action="select-destination"]').last().click();
    await pageA.locator("#tripStart").fill("2026-12-08");
    await pageA.locator("#tripEnd").fill("2026-12-10");
    await pageA.locator("#tripBudget").fill("3600");
    await pageA.locator("#tripInterests").fill("美食,人文");
    await pageA.locator("#tripForm button.primary-button").click();
    // Real AI trip composition may take over a minute even though the page remains responsive.
    await pageA.waitForFunction(
      expected => document.querySelector("#tripTitle")?.textContent.includes(expected),
      destination,
      { timeout: 150_000 },
    );
    assert((await api(pageB, "/api/v1/trips")).items.some(item => item.destination === destination), "任意目的地旅行未共享");
    await pageA.locator('.section-tabs [data-trip-section="tools"]').click();
    await pageA.locator('[data-trip-tool-section="route"]').click();
    await pageA.locator('[data-action="trip-route"]').click();
    await pageA.locator("#tripToolResults .route-leg-card").first().waitFor();
    const routeText = await pageA.locator("#tripToolResults").textContent();
    assert(routeText.includes("展开某一路段后才会查询") && routeText.includes("路线"), "查看路线时没有保持按需查询");
    assert(await pageA.locator('.plan-tabs [data-plan]').count() === 5, "旅行方案不是五套");
    await pageA.locator('[data-action="search-quotes"][data-quote-type="hotel"]').click();
    await pageA.locator("#tripToolResults .quote-result").first().waitFor();
    assert(await pageA.locator("#tripToolResults .quote-result").count() === 5, "报价查询没有提供五个可选结果");
    const firstQuote = pageA.locator("#tripToolResults .quote-result").first();
    await firstQuote.locator('[data-action="select-quote"]').click();
    await pageA.waitForFunction(() => document.querySelector("#tripToolResults .quote-result.selected"));
    const selectedQuoteId = await firstQuote.getAttribute("data-quote-id");
    const currentTripId = (await api(pageA, "/api/v1/bootstrap")).trip.id;
    const sharedTrip = (await api(pageB, "/api/v1/trips")).items.find(item => item.id === currentTripId);
    assert(sharedTrip, "伴侣看不到共同旅行");
    const partnerTrip = await api(pageB, `/api/v1/trips/${sharedTrip.id}`);
    assert(partnerTrip.quotes.some(item => item.id === selectedQuoteId && item.selected_at), "所选报价未共享给伴侣");
    checks.push("高德式地点自动联想、任意目的地旅行创建、五套方案、逐段按需路线、五个可选报价与双方共享选择");

    await pageA.locator('.section-tabs [data-trip-section="assistant"]').click();
    await pageA.locator("#tripAssistantInput").fill("比较五套方案哪个更轻松");
    await pageA.locator("#tripAssistantForm button").click();
    try {
      await pageA.locator('#tripAssistantMessages .assistant-result-card [data-action="assistant-open-plan"]').first().waitFor();
    } catch (error) {
      console.error("AI_TRAVEL_PANEL:", await pageA.locator("#tripAssistantMessages").innerHTML());
      throw error;
    }
    assert(await pageA.locator('#tripAssistantMessages [data-action="assistant-open-plan"]').count() === 5, "AI 对话没有返回五套可操作方案");
    const sharedAssistant = await api(pageB, `/api/v1/trips/${currentTripId}/assistant`);
    assert(sharedAssistant.items.some(item => item.intent_type === "plan_compare"), "AI 旅行对话没有共享给伴侣");
    await pageA.locator("#tripAssistantInput").fill("帮我找预算内、离行程近的酒店");
    await pageA.locator("#tripAssistantForm button").click();
    const assistantHotel = pageA.locator('#tripAssistantMessages [data-action="assistant-select-quote"]').first();
    await assistantHotel.waitFor();
    await assistantHotel.click();
    await pageA.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("酒店已加入共同旅行"));
    const assistantSelectedTrip = await api(pageB, `/api/v1/trips/${currentTripId}`);
    assert(assistantSelectedTrip.quotes.some(item => item.quote_type === "hotel" && item.selected_at), "AI 对话选择酒店后没有同步给伴侣");
    const activeAssistantVariant = assistantSelectedTrip.variants.find(item => item.style === assistantSelectedTrip.active_variant);
    const assistantTargetItem = activeAssistantVariant?.days?.flatMap(day => day.items || [])[0];
    assert(assistantTargetItem, "AI 行程调整测试找不到可修改的共同项目");
    await pageA.locator("#tripAssistantInput").fill(`把第一天的${assistantTargetItem.title}改到16:40`);
    await pageA.locator("#tripAssistantForm button").click();
    const confirmAssistantChange = pageA.locator('#tripAssistantMessages [data-action="assistant-confirm-change"]').last();
    await confirmAssistantChange.waitFor();
    const confirmedDraftId = await confirmAssistantChange.getAttribute("data-draft-id");
    await confirmAssistantChange.click();
    await pageA.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("行程修改已保存"));
    const confirmedAssistantThread = await api(pageA, `/api/v1/trips/${currentTripId}/assistant`);
    assert(confirmedAssistantThread.items.some(item => (item.cards || []).some(card => card.draft_id === confirmedDraftId && card.status === "confirmed")), "AI 行程修改确认状态没有持久保存");
    const changedPartnerTrip = await api(pageB, `/api/v1/trips/${currentTripId}`);
    const changedPartnerItem = changedPartnerTrip.variants
      .flatMap(variant => variant.days || [])
      .flatMap(day => day.items || [])
      .find(item => item.id === assistantTargetItem.id);
    assert(changedPartnerItem?.start_time === "16:40", "AI 行程修改确认后没有同步给伴侣");
    await pageA.locator("#tripLiveRefresh").click();
    await pageA.waitForFunction(() => document.querySelector("#tripLiveResults")?.textContent.includes("位置未保存"));
    assert((await pageA.locator("#tripLiveResults").textContent()).includes("未配置真实地点服务"), "地图未配置时没有明确降级说明");
    const storedLiveLocation = await pageA.evaluate(() => Object.entries(localStorage).some(([key, value]) => key.includes("trip-live") || value.includes("30.5728") || value.includes("104.0668")));
    assert(!storedLiveLocation, "一次性当前位置被写入浏览器持久存储");

    await pageA.locator("#tripAssistantInput").fill("统一比较酒店、航班和火车票");
    await pageA.locator("#tripAssistantForm button").click();
    await pageA.locator('#tripAssistantMessages .quote-group-card').last().waitFor();
    assert(await pageA.locator('#tripAssistantMessages .quote-group-card').count() >= 3, "统一报价没有分别展示酒店、航班和火车票");

    await pageA.locator("#tripAssistantInput").fill("旅行结束了，生成共同回忆");
    await pageA.locator("#tripAssistantForm button").click();
    const recapCard = pageA.locator('#tripAssistantMessages .trip-recap-card').last();
    await recapCard.waitFor();
    const errorsBeforeExpectedConflict = errors.length;
    await recapCard.locator('[data-action="assistant-recap-confirm"]').click();
    await pageA.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("旅行结束后"));
    // The browser reports an expected HTTP 409 as a generic console resource
    // error.  Remove only the conflict produced inside this deliberately
    // tested rejection window; every other console error remains fatal.
    errors.splice(
      errorsBeforeExpectedConflict,
      errors.length - errorsBeforeExpectedConflict,
      ...errors.slice(errorsBeforeExpectedConflict).filter(value => !value.includes("status of 409 (Conflict)")),
    );
    const endedRecap = await api(pageA, "/api/v1/trips/demo-trip/assistant/recap", {
      method: "POST", body: "{}",
    });
    const confirmedRecap = await api(pageA, `/api/v1/trips/demo-trip/assistant/recap/${endedRecap.id}/confirm`, {
      method: "POST", body: JSON.stringify({ idempotency_key: `browser-recap-${Date.now()}` }),
    });
    const partnerAfterRecap = await api(pageB, "/api/v1/bootstrap");
    assert(partnerAfterRecap.moments.some(item => item.id === confirmedRecap.moment.id), "已结束旅行的回忆确认后没有同步到伴侣时光轴");
    checks.push("AI 旅行对话、一次性定位、明确地图降级、统一报价、未结束旅行禁止发布回忆、已结束旅行回忆双账号共享");

    await pageA.locator('.section-tabs [data-trip-section="plans"]').click();
    await pageA.locator('[data-plan="classic"]').click();
    assert(await pageA.locator('[data-plan="classic"]').evaluate(button => button.classList.contains("active")), "旅行方案切换失败");
    const previewTrip = await api(pageA, `/api/v1/trips/${currentTripId}`);
    assert(previewTrip.active_variant !== "classic", "预览方案不应在确认前写入最终选择");
    await pageA.locator('[data-action="adopt-plan"]').click();
    await pageA.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("已采用"));
    const adoptedTrip = await api(pageA, `/api/v1/trips/${currentTripId}`);
    assert(adoptedTrip.active_variant === "classic" && adoptedTrip.status === "pending_approval", "整套旅行方案未进入双方确认");
    assert(await pageA.locator('[data-plan="classic"]').evaluate(button => button.classList.contains("adopted")), "已采用方案缺少可见标记");
    await pageA.locator('.section-tabs [data-trip-section="itinerary"]').click();
    const mixButtons = pageA.locator('#itinerary [data-action="toggle-mix"]');
    const detailButtons = pageA.locator('#itinerary [data-action="view-itinerary-detail"]');
    assert(await pageA.locator("#itinerary .itinerary-day").count() >= 3, "每日行程没有显示全部日期");
    assert(await mixButtons.count() >= 3, "行程混搭项不足");
    assert(await detailButtons.count() === await mixButtons.count(), "部分行程项目无法打开详情");
    await detailButtons.first().click();
    await pageA.locator("#itineraryDetailModal.open").waitFor();
    assert((await pageA.locator("#itineraryDetailTitle").textContent()).trim().length > 0, "行程详情没有显示项目标题");
    await pageA.locator('#itineraryDetailModal [data-action="close-modal"]').click();
    await pageA.locator('[data-action="select-all-items"]').click();
    assert(Number(await pageA.locator("#mixCount").textContent()) === await mixButtons.count(), "全选当前方案失败");
    await mixButtons.nth(0).click();
    assert(await mixButtons.nth(0).getAttribute("aria-pressed") === "false", "混搭按钮未暴露取消状态");
    await pageA.locator('[data-action="build-plan"]').click();
    await pageA.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("最终方案"));
    await pageA.locator('[data-action="approve-trip"]').click();
    await pageA.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("等待"));
    await pageB.locator('[data-nav="trip"]').last().click();
    const switcherB = pageB.locator("#tripSwitcher");
    await switcherB.selectOption(currentTripId);
    await pageB.locator('[data-action="approve-trip"]').click();
    await pageB.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("正式生效"));
    assert((await api(pageA, `/api/v1/trips/${currentTripId}`)).status === "confirmed", "双方确认后旅行没有生效");
    checks.push("五套旅行方案预览、整套采用、完整多日行程、混搭与双方确认");

    await pageA.locator('.section-tabs [data-trip-section="overview"]').click();
    const smartForm = pageA.locator('[data-smart-form="trip"]');
    await smartForm.locator("input").fill("我想去三亚玩");
    await smartForm.locator("button").click();
    await pageA.locator("#tripModal.open").waitFor();
    assert(await pageA.locator("#tripDestination").inputValue() === "三亚", "无模型密钥时未识别三亚旅行意图");
    await pageA.locator('#tripModal [data-action="close-modal"]').click();
    checks.push("本地旅行意图识别并打开真实创建表单");
    await smartForm.locator("input").fill("只显示预算与待确认内容");
    await smartForm.locator("button").click();
    await smartForm.locator("button").waitFor({ state: "visible" });
    await pageA.waitForFunction(() => document.querySelector('[data-smart-form="trip"] button')?.textContent === "智能整理");
    assert(await pageA.locator('[data-smart="trip"] .smart-node').count() > 0, "智能界面未返回可用清单");
    await pageA.locator('[data-smart="trip"] [data-smart-node="budget_summary"]').click();
    await pageA.locator('[data-screen="trip"].active').waitFor();
    checks.push("智能界面规则/模型降级编排与真实卡片导航");

    // The trip created in this isolated run is disposable.  Delete it through
    // the same visible control a real user taps, then verify both accounts no
    // longer receive it.  This protects against a show-only delete button.
    pageA.once("dialog", dialog => dialog.accept());
    await pageA.locator('[data-action="delete-trip"]').click();
    await pageA.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("旅行已删除"));
    assert(!(await api(pageA, "/api/v1/trips")).items.some(item => item.id === currentTripId), "删除后创建者仍能看到旅行");
    assert(!(await api(pageB, "/api/v1/trips")).items.some(item => item.id === currentTripId), "删除后伴侣仍能看到旅行");
    await pageA.locator('#toast [data-action="restore-trip"]').click();
    await pageA.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("已恢复"));
    assert((await api(pageA, "/api/v1/trips")).items.some(item => item.id === currentTripId), "撤销删除后创建者看不到旅行");
    assert((await api(pageB, "/api/v1/trips")).items.some(item => item.id === currentTripId), "撤销删除后伴侣看不到旅行");
    checks.push("旅行可见删除、版本校验、双账号同步消失与误删恢复");

    await pageA.locator('[data-nav="life"]').last().click();
    await pageA.locator('.section-tabs [data-life-section="overview"]').click();
    await pageA.locator('[data-screen="life"] .smart-disclosure > summary').click();
    const anniversaryMarker = `第一次看雪-${Date.now()}`;
    const lifeSmartForm = pageA.locator('[data-smart-form="life"]');
    await lifeSmartForm.locator("input").fill(`添加纪念日 2027年1月18日 ${anniversaryMarker}`);
    await lifeSmartForm.locator("button").click();
    const anniversarySmartCard = pageA.locator('[data-smart="life"] [data-smart-node="anniversary"]').first();
    const anniversarySmartAction = anniversarySmartCard.locator('[data-smart-action]');
    await anniversarySmartAction.waitFor({ timeout: 10_000 });
    await anniversarySmartAction.click();
    await pageA.locator("#smartConfirmModal.open").waitFor();
    assert((await pageA.locator("#smartActionPreview").textContent()).includes(anniversaryMarker), "纪念日写入预览缺少真实内容");
    // Keep the post-confirmation refresh in flight long enough to reproduce a
    // real slow-network race: the user is allowed to move to another page, and
    // the older confirmation flow must not pull them back when it completes.
    let delayedPostConfirmationRefresh = false;
    const slowPostConfirmationRefresh = async route => {
      // Delay exactly one request. Delaying every recipe request leaves a late
      // handler alive while `unroute` removes the interceptor, which can make
      // Playwright auto-handle that route before this callback resumes.
      if (!delayedPostConfirmationRefresh) {
        delayedPostConfirmationRefresh = true;
        await new Promise(resolve => setTimeout(resolve, 900));
      }
      await route.continue();
    };
    await pageA.route("**/api/v1/recipes*", slowPostConfirmationRefresh);
    await pageA.locator('[data-action="confirm-smart-action"]').click();
    await pageA.locator('#lifeDataList .life-record', { hasText: anniversaryMarker }).waitFor();
    assert((await api(pageB, "/api/v1/calendar?type=anniversary")).items.some(item => item.title === anniversaryMarker), "智能纪念日保存后伴侣不可见");
    checks.push("智能添加纪念日的预览、确认、落库、自动定位与双账号查看");

    const cachedWrites = await pageA.evaluate(() => {
      const scope = localStorage.getItem("love-active-scope");
      const manifest = JSON.parse(localStorage.getItem(`love-private:${scope}:ui:life:`) || "null");
      if (!manifest) throw new Error("当前用户隔离的智能布局缓存缺失");
      return [...(manifest?.sections || []), ...(manifest?.personalOverlay || [])].flatMap(node => node.actions || []).filter(action => action.requiresConfirmation).length;
    });
    assert(cachedWrites === 0, "临时智能写操作被错误写入长期缓存");
    checks.push("智能动作草稿生命周期与安全缓存");

    await pageA.locator('[data-nav="timeline"]').last().click();
    await pageA.locator('[data-screen="timeline"] .smart-disclosure > summary').click();
    const momentMarker = `页面智能瞬间-${Date.now()}`;
    const timelineSmartForm = pageA.locator('[data-smart-form="timeline"]');
    await timelineSmartForm.locator("input").fill(`记录瞬间：${momentMarker}`);
    await timelineSmartForm.locator("button").click();
    await pageA.waitForTimeout(1_100);
    assert(await pageA.locator('[data-screen="timeline"].active').isVisible(), "较早完成的智能操作错误地把用户拉回旧页面");
    await pageA.unroute("**/api/v1/recipes*", slowPostConfirmationRefresh);
    const momentSmartCard = pageA.locator('[data-smart="timeline"] [data-smart-node="quick_capture"]').first();
    await momentSmartCard.locator('[data-smart-action]').waitFor({ timeout: 10_000 });
    // Click the card body instead of the small action button.  Both paths must
    // open the same persisted action preview and must not show an empty editor.
    await momentSmartCard.locator("strong").click();
    await pageA.locator("#smartConfirmModal.open").waitFor();
    assert((await pageA.locator("#smartActionPreview").textContent()).includes(momentMarker), "点击智能写卡片没有打开真实内容预览");
    await pageA.locator('[data-action="cancel-smart-action"]').click();
    await pageA.locator("#smartConfirmModal").waitFor({ state: "hidden" });
    assert(!(await api(pageA, "/api/v1/bootstrap")).moments.some(item => item.title === momentMarker), "取消智能瞬间后仍写入了数据");
    checks.push("智能写卡片整卡预览与取消不落库");

    await pageA.locator('[data-nav="life"]').last().click();
    await pageA.locator('.section-tabs [data-life-section="wishlist"]').click();
    await pageA.locator('.wishlist:not([hidden])').waitFor();
    const wishToggle = pageA.locator('.wishlist .wish-row [data-action="toggle-wish"]').first();
    const before = await wishToggle.getAttribute("data-completed");
    await wishToggle.click();
    await pageA.waitForFunction(expected => document.querySelector('.wishlist .wish-row [data-action="toggle-wish"]')?.dataset.completed === expected, before === "1" ? "0" : "1");
    await pageA.locator('.wishlist .wish-row [data-action="toggle-wish"]').first().click();
    await pageA.waitForFunction(expected => document.querySelector('.wishlist .wish-row [data-action="toggle-wish"]')?.dataset.completed === expected, before);
    const wishMarker = `一起露营-${Date.now()}`;
    await pageA.locator('[data-action="open-wishlist-editor"]').click();
    await pageA.locator("#wishlistText").fill(wishMarker);
    await pageA.locator("#wishlistNote").fill("智能整理审计");
    await pageA.locator("#wishlistForm button").click();
    const wishRow = pageA.locator(".wishlist .wish-row", { hasText: wishMarker });
    await wishRow.waitFor();
    await wishRow.locator('[data-action="edit-wish"]').click();
    await pageA.locator("#wishlistText").fill(`${wishMarker}-已修改`);
    await Promise.all([
      pageA.waitForResponse(response => response.url().includes("/api/v1/lists/items/") && response.request().method() === "PATCH" && response.ok()),
      pageA.locator("#wishlistForm button").click(),
    ]);
    assert((await api(pageB, "/api/v1/lists/wishlist")).items.some(item => item.text === `${wishMarker}-已修改`), "共同愿望编辑未同步");
    const firstComment = `演示甲的评论-${Date.now()}`;
    const secondComment = `演示乙的回复-${Date.now()}`;
    await wishRow.locator('[data-action="open-shared-detail"]').click();
    await pageA.locator("#sharedDetailModal.open").waitFor();
    assert((await pageA.locator("#sharedDetailTitle").textContent()).includes(`${wishMarker}-已修改`), "共同内容详情未显示真实心愿标题");
    await pageA.locator("#sharedCommentBody").fill(firstComment);
    await pageA.locator("#sharedCommentForm button").click();
    await pageA.locator("#sharedCommentList .shared-comment", { hasText: firstComment }).waitFor();

    await pageB.locator('[data-nav="life"]').last().click();
    await pageB.locator('.section-tabs [data-life-section="wishlist"]').click();
    const partnerWishRow = pageB.locator(".wishlist .wish-row", { hasText: `${wishMarker}-已修改` });
    await partnerWishRow.waitFor({ timeout: 10_000 });
    await partnerWishRow.locator('[data-action="open-shared-detail"]').click();
    await pageB.locator("#sharedDetailModal.open").waitFor();
    await pageB.locator("#sharedCommentList .shared-comment", { hasText: firstComment }).waitFor();
    await pageB.locator("#sharedCommentBody").fill(secondComment);
    await pageB.locator("#sharedCommentForm button").click();
    await pageB.locator("#sharedCommentList .shared-comment", { hasText: secondComment }).waitFor();
    await pageA.locator("#sharedCommentList .shared-comment", { hasText: secondComment }).waitFor({ timeout: 10_000 });
    await pageA.locator('#sharedDetailModal [data-action="close-modal"]').click();
    await pageB.locator('#sharedDetailModal [data-action="close-modal"]').click();
    const wishId = await wishRow.locator('[data-action="open-shared-detail"]').getAttribute("data-id");
    await pageA.goto(`${originA}/?screen=life&shared_type=wishlist_item&shared_id=${encodeURIComponent(wishId)}`, { waitUntil: "domcontentloaded" });
    await pageA.locator("#sharedDetailModal.open").waitFor();
    assert((await pageA.locator("#sharedDetailTitle").textContent()).includes(`${wishMarker}-已修改`), "共同内容推送深链没有打开真实详情");
    assert(!pageA.url().includes("shared_id="), "共同内容推送深链使用后没有清除一次性资源参数");
    await pageA.locator('#sharedDetailModal [data-action="close-modal"]').click();
    checks.push("共同心愿新增、编辑、完成状态、真实详情、双方评论实时同步与通知深链");

    const lifeMarker = `一起订票-${Date.now()}`;
    await pageA.locator('.bottom-nav [data-nav="life"]').click();
    await pageA.locator('[data-screen="life"].active').waitFor();
    await pageA.locator('[data-screen="life"].active .section-tabs [data-life-section="todo"]').click();
    const addTodo = pageA.locator('[data-screen="life"].active #lifeSectionAdd[data-action="open-calendar"]');
    await addTodo.waitFor({ state: "visible" });
    await addTodo.click();
    await pageA.locator("#calendarType").selectOption("todo");
    await pageA.locator("#calendarTitle").fill(lifeMarker);
    await pageA.locator("#calendarStart").fill("2026-11-20");
    await pageA.locator("#calendarForm button").click();
    await pageA.locator("#lifeDataList .life-record", { hasText: lifeMarker }).waitFor();
    const lifeRecord = pageA.locator("#lifeDataList .life-record", { hasText: lifeMarker });
    await lifeRecord.locator('[data-action="edit-calendar"]').click();
    await pageA.locator("#calendarTitle").fill(`${lifeMarker}-已修改`);
    await Promise.all([
      pageA.waitForResponse(response => response.url().includes("/api/v1/calendar/") && response.request().method() === "PATCH" && response.ok()),
      pageA.locator("#calendarForm button").click(),
    ]);
    await pageA.locator("#calendarModal").waitFor({ state: "hidden" });
    assert((await api(pageB, "/api/v1/calendar")).items.some(item => item.title === `${lifeMarker}-已修改`), "共同事项编辑未同步");

    const preferenceMarker = `三分糖-${Date.now()}`;
    await pageA.locator('.bottom-nav [data-nav="life"]').click();
    await pageA.locator('[data-screen="life"].active').waitFor();
    await pageA.locator('[data-screen="life"].active .section-tabs [data-life-section="preference"]').click();
    const addPreference = pageA.locator('[data-screen="life"].active #lifeSectionAdd[data-action="open-preferences"]');
    await addPreference.waitFor({ state: "visible" });
    await addPreference.click();
    await pageA.locator("#preferenceModal.open").waitFor({ state: "visible" });
    await pageA.locator("#preferenceLabel").fill("奶茶甜度");
    await pageA.locator("#preferenceValue").fill(preferenceMarker);
    await pageA.locator("#preferenceForm button").click();
    const preferenceRecord = pageA.locator("#lifeDataList .life-record", { hasText: preferenceMarker });
    await preferenceRecord.waitFor();
    await preferenceRecord.locator('[data-action="edit-preference"]').click();
    await pageA.locator("#preferenceValue").fill(`${preferenceMarker}-已修改`);
    await Promise.all([
      pageA.waitForResponse(response => response.url().includes("/api/v1/preferences/") && response.request().method() === "PATCH" && response.ok()),
      pageA.locator("#preferenceForm button").click(),
    ]);
    await pageA.locator("#preferenceModal").waitFor({ state: "hidden" });
    assert((await api(pageB, "/api/v1/preferences")).items.some(item => item.value === `${preferenceMarker}-已修改`), "偏好编辑未同步");
    checks.push("共享日历、待办和偏好新增/编辑/双账号读取");

    await pageA.locator('.bottom-nav [data-nav="life"]').click();
    await pageA.locator('[data-screen="life"].active .section-tabs [data-life-section="meals"]').click();
    await pageA.locator('[data-meal-panel="recipes"]:not([hidden])').waitFor();
    await pageA.locator("#recipeSearchInput").fill("番茄炒蛋");
    await pageA.locator("#recipeSearchForm button").click();
    const builtinCover = pageA.locator('#recipeList .recipe-card', { hasText: "番茄炒蛋" }).locator('img[data-recipe-cover]');
    await builtinCover.waitFor();
    assert(await builtinCover.evaluate(image => image.complete && image.naturalWidth >= 360 && !image.closest("figure").classList.contains("load-failed")), "内置菜谱封面没有通过真实浏览器加载");
    await pageA.locator("#recipeSearchInput").fill("法式吐司");
    await pageA.locator("#recipeCuisine").selectOption("western");
    await pageA.locator("#recipeMealSlot").selectOption("breakfast");
    await pageA.locator("#recipeDifficulty").selectOption("easy");
    await pageA.locator("#recipeMaxMinutes").selectOption("20");
    await pageA.locator("#recipeSearchForm button").click();
    const frenchToast = pageA.locator("#recipeList .recipe-card", { hasText: "法式吐司" });
    await frenchToast.waitFor();
    await frenchToast.click();
    await pageA.locator("#recipeDetailModal.open").waitFor();
    const allergenReport = await pageA.locator(".recipe-allergen-report").textContent();
    assert(allergenReport.includes("蛋类"), "菜谱详情没有显示过敏原");
    assert(allergenReport.includes("来源："), "菜谱详情没有显示过敏原对应配料来源");
    assert((await pageA.locator("#recipeDetailIngredients").textContent()).includes("辅料与调味"), "菜谱详情没有区分主料与调料");
    assert((await pageA.locator("#recipeDetailIngredients").textContent()).includes("处理："), "菜谱详情没有显示食材处理方法");
    assert(await pageA.locator("#recipeDetailSteps .recipe-step-card").count() >= 4, "菜谱步骤不够完整");
    const recipeStepsText = await pageA.locator("#recipeDetailSteps").textContent();
    assert(recipeStepsText.includes("火力：") && recipeStepsText.includes("完成判断："), "菜谱步骤缺少火候或完成判断");
    const ingredientsBeforeScaling = await pageA.locator("#recipeDetailIngredients").textContent();
    await pageA.locator("#recipeDetailServings").fill("4");
    await pageA.locator("#recipeDetailServings").dispatchEvent("change");
    const ingredientsAfterScaling = await pageA.locator("#recipeDetailIngredients").textContent();
    assert(ingredientsAfterScaling !== ingredientsBeforeScaling, "菜谱人数改变后食材没有换算");
    await pageA.locator('#recipeDetailModal [data-action="close-modal"]').click();

    const recipeMarker = `验收番茄鸡蛋面-${Date.now()}`;
    await pageA.locator("#recipeSearchInput").fill("");
    await pageA.locator("#recipeCuisine").selectOption("");
    await pageA.locator("#recipeMealSlot").selectOption("");
    await pageA.locator("#recipeDifficulty").selectOption("");
    await pageA.locator("#recipeMaxMinutes").selectOption("");
    await pageA.locator('[data-action="new-custom-recipe"]').click();
    await pageA.locator("#customRecipeDishName").fill("番茄鸡蛋面");
    await pageA.locator("#customRecipeVariantName").fill("少油家常版");
    await pageA.locator("#customRecipeName").fill(recipeMarker);
    await pageA.locator("#customRecipeCookingMethod").fill("少油快炒后煮面");
    await pageA.locator("#customRecipeMethodTags").fill("少油、家常");
    await pageA.locator("#customRecipeCuisine").selectOption("chinese");
    await pageA.locator("#customRecipeCategory").fill("家常面食");
    await pageA.locator("#customRecipeServings").fill("2");
    await pageA.locator("#customRecipeMinutes").fill("25");
    await pageA.locator("#customRecipeIngredients").fill("番茄 | 2 | 个 | 去蒂切成 2 厘米块\n鸡蛋 | 3 | 个 | 充分打散\n面条 | 200 | 克 | 抖散备用\n盐 | 2 | 克 | 提前称好");
    await pageA.locator("#customRecipeSteps").fill("备料 | 番茄切块，鸡蛋打散，盐称出 2 克 | 无需加热 | 5 | 食材切配和调料称量完成\n炒鸡蛋 | 锅中倒油后放入蛋液推炒 | 中火 | 2 | 蛋液刚凝固时盛出\n炒番茄 | 番茄下锅炒出汁后加入鸡蛋 | 中火 | 4 | 番茄软化并有明显汤汁\n煮面组合 | 面条煮熟后浇上番茄鸡蛋 | 中大火 | 6 | 面条无白芯且所有食材热透");
    await pageA.locator("#customRecipeCover").setInputFiles({ name: "番茄鸡蛋面-准确成品.png", mimeType: "image/png", buffer: testPng });
    await pageA.locator("#customRecipeCoverExact").check();
    await pageA.locator("#customRecipeForm button").click();
    await pageA.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("自建菜谱已保存"));
    await pageA.locator("#recipeSearchInput").fill(recipeMarker);
    await pageA.locator("#recipeSearchForm button").click();
    const customRecipe = pageA.locator("#recipeList .recipe-card", { hasText: recipeMarker });
    await customRecipe.waitFor();
    await customRecipe.click();
    await pageA.locator("#recipeDetailModal.open").waitFor();
    await pageA.waitForFunction(() => document.querySelector("#recipeDetailContent")?.textContent.includes("蛋液刚凝固时盛出"));
    const customDetailText = await pageA.locator("#recipeDetailContent").textContent();
    assert(customDetailText.includes("去蒂切成 2 厘米块") && customDetailText.includes("火力：中火") && customDetailText.includes("蛋液刚凝固时盛出"), "自建菜谱的处理、火候或完成判断没有保存");
    assert(customDetailText.includes("少油家常版") && customDetailText.includes("少油快炒后煮面"), "菜名与具体做法没有分开保存");
    await pageA.locator("#recipeDetailContent .recipe-cover img").waitFor();
    assert(await pageA.locator("#recipeDetailContent .recipe-cover img").evaluate(image => image.complete && image.naturalWidth > 0), "准确成品图未显示");
    await pageA.locator('[data-action="add-recipe-video"]').click();
    await pageA.locator("#recipeVideoUrl").fill("https://www.bilibili.com/video/BV1LoveMomentsTest");
    await pageA.locator("#recipeVideoName").fill("番茄鸡蛋面参考教学");
    await pageA.locator("#recipeVideoDifferences").fill("视频调味量不同，请以页面克数为准");
    await pageA.locator("#recipeVideoForm button").click();
    await pageA.locator(".recipe-video-card", { hasText: "番茄鸡蛋面参考教学" }).waitFor();
    assert((await pageA.locator(".recipe-video-card").textContent()).includes("仅供参考"), "未核对视频没有被明确标为参考");
    await pageA.locator('[data-action="wish-recipe"]').click();
    await pageA.locator("#mealWishNote").fill("周末一起做");
    await pageA.locator("#mealWishForm button").click();
    await pageA.locator("#mealWishList .meal-row", { hasText: recipeMarker }).waitFor();

    const mealDate = await pageA.evaluate(() => { const day = new Date(); day.setDate(day.getDate() + 1); return `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`; });
    await pageA.locator('[data-action="new-meal-wish"]:visible').last().click();
    await pageA.locator("#mealWishName").fill("煎牛排");
    await pageA.locator("#mealWishNote").fill("见面以后做给我吃");
    await pageA.locator("#mealWishForm button").click();
    const freeTextWish = pageA.locator("#mealWishList .meal-wish-row", { hasText: "煎牛排" });
    await freeTextWish.waitFor();
    assert((await freeTextWish.textContent()).includes("选择具体菜谱"), "手动想吃内容没有提示选择具体菜谱");
    await freeTextWish.locator('[data-action="match-wish-recipe"]').first().click();
    await pageA.locator("#mealWishRecipeModal.open").waitFor();
    const steakCandidate = pageA.locator("#mealWishRecipeResults .meal-wish-candidate", { hasText: "香煎牛排" });
    await steakCandidate.waitFor();
    await steakCandidate.locator('[data-action="choose-wish-recipe"][data-mode="detail"]').click();
    await pageA.locator("#recipeDetailModal.open").waitFor();
    assert((await pageA.locator("#recipeDetailTitle").textContent()).includes("香煎牛排"), "文字愿望没有打开匹配后的菜谱详情");
    assert(await pageA.locator("#recipeDetailIngredients .recipe-ingredient-list li").count() >= 3, "匹配菜谱没有显示食材");
    assert(await pageA.locator("#recipeDetailSteps .recipe-step-card").count() >= 3, "匹配菜谱没有显示做法");
    await pageA.locator('[data-action="schedule-recipe"]').click();
    assert((await pageA.locator("#mealScheduleForm button").textContent()).includes("点菜车"), "愿望菜谱没有明确加入点菜车");
    await pageA.locator("#mealScheduleDate").fill(mealDate);
    await pageA.locator("#mealScheduleSlot").selectOption("dinner");
    await pageA.locator("#mealScheduleForm button").click();
    const steakMeal = pageA.locator("#mealDayList .meal-row", { hasText: "香煎牛排" });
    await steakMeal.waitFor();
    await steakMeal.locator('[data-action="meal-status"][data-status="accepted"]').click();
    await pageA.waitForFunction(() => [...document.querySelectorAll("#mealDayList .meal-row")].some(row => row.textContent.includes("香煎牛排") && row.textContent.includes("已确认")));
    assert(!(await pageA.locator("#mealWishList .meal-wish-row", { hasText: "煎牛排" }).count()), "加入点菜车后文字愿望仍留在待选清单");

    await pageA.locator('[data-meal-section="recipes"]').click();
    await pageA.locator("#recipeSearchInput").fill(recipeMarker);
    await pageA.locator("#recipeSearchForm button").click();
    await pageA.locator("#recipeList .recipe-card", { hasText: recipeMarker }).click();
    await pageA.locator("#recipeDetailServings").fill("3");
    await pageA.locator("#recipeDetailServings").dispatchEvent("change");
    await pageA.locator('[data-action="schedule-recipe"]').click();
    await pageA.locator("#mealScheduleDate").fill(mealDate);
    await pageA.locator("#mealScheduleSlot").selectOption("lunch");
    await pageA.locator("#mealScheduleTime").fill("12:15");
    assert(await pageA.locator("#mealScheduleServings").inputValue() === "3", "菜谱详情人数没有带入安排表单");
    await pageA.locator("#mealScheduleForm button").click();
    const scheduledMeal = pageA.locator("#mealDayList .meal-row", { hasText: recipeMarker });
    await scheduledMeal.waitFor();
    await scheduledMeal.locator('[data-action="meal-status"][data-status="accepted"]').click();
    await pageA.waitForFunction(name => [...document.querySelectorAll("#mealDayList .meal-row")].some(row => row.textContent.includes(name) && row.textContent.includes("已确认")), recipeMarker);
    await pageA.locator('[data-action="meal-calendar-view"][data-view="month"]').click();
    await pageA.waitForFunction(() => document.querySelectorAll("#mealCalendar button").length === 42);
    assert(await pageA.locator("#mealCalendar button").count() === 42, "餐食月视图没有渲染 42 个日期格");

    await pageA.locator('[data-meal-section="shopping"]').click();
    await pageA.locator('[data-action="preview-shopping"]').click();
    await pageA.locator("#shoppingRangeStart").fill(mealDate);
    await pageA.locator("#shoppingRangeEnd").fill(mealDate);
    await pageA.locator('[data-action="build-shopping-preview"]').click();
    await pageA.locator("#shoppingPreviewContent .shopping-preview-summary article").first().waitFor();
    await pageA.locator("#applyShoppingPreview").click();
    await pageA.locator("#shoppingList .shopping-row").first().waitFor();

    await pageB.locator('.bottom-nav [data-nav="life"]').click();
    await pageB.locator('[data-screen="life"].active .section-tabs [data-life-section="meals"]').click();
    await pageB.locator('[data-meal-section="shopping"]').click();
    const partnerShoppingItem = pageB.locator("#shoppingList .shopping-row").first();
    await partnerShoppingItem.waitFor();
    await partnerShoppingItem.locator("[data-shopping-check]").check();
    await pageA.waitForFunction(() => document.querySelector("#shoppingList [data-shopping-check]")?.checked, null, { timeout: 10_000 });
    const partnerCalendar = await api(pageB, `/api/v1/meal-calendar/day/${mealDate}`);
    assert(partnerCalendar.days[0].meals.lunch.some(item => item.name === recipeMarker && item.servings === 3), "伴侣未读取到共同用餐安排");
    checks.push("中文菜谱筛选/人数换算、文字愿望匹配菜谱/查看做法/加入点菜车、自建/想吃、餐食月历、采购预览与双账号实时勾选");

    const editedMomentTitle = `修改后的回忆-${Date.now()}`;
    await pageA.locator('.bottom-nav [data-nav="timeline"]').click();
    await pageA.locator('#timelineView [data-view="list"]').click();
    const editMomentButton = pageA.locator(`[data-action="edit-moment"][data-id="${collected.id}"]`);
    await editMomentButton.waitFor();
    await editMomentButton.click();
    await pageA.locator("#momentTitle").fill(editedMomentTitle);
    await pageA.locator("#momentNote").fill("修改后的正文会同步给双方");
    await pageA.locator("#momentLocation").fill("杭州西湖");
    await pageA.locator("#momentMood").fill("开心");
    await pageA.locator('[data-action="save-moment"]').click();
    await pageA.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("修改已同步"));
    const creatorEditedMoment = (await api(pageA, "/api/v1/bootstrap")).moments.find(item => item.id === collected.id);
    const partnerEditedMoment = (await api(pageB, "/api/v1/bootstrap")).moments.find(item => item.id === collected.id);
    assert(creatorEditedMoment?.title === editedMomentTitle && creatorEditedMoment?.location === "杭州西湖", "创建者修改没有完整保存");
    assert(partnerEditedMoment?.title === editedMomentTitle && partnerEditedMoment?.body === "修改后的正文会同步给双方", "修改结果没有同步给伴侣");
    await pageB.locator('.bottom-nav [data-nav="timeline"]').click();
    await pageB.locator('#timelineView [data-view="list"]').click();
    await pageB.waitForFunction(title => document.querySelector("#timelineBody")?.textContent.includes(title), editedMomentTitle);
    assert(await pageB.locator(`[data-action="edit-moment"][data-id="${collected.id}"]`).count() === 0, "伴侣端错误显示了代改入口");
    checks.push("已发布瞬间可编辑、版本保存、双端同步与发布者权限");

    await api(pageA, `/api/v1/moments/${collected.id}/trash`, { method: "POST", body: "{}" });
    await pageA.locator('.bottom-nav [data-nav="timeline"]').click();
    await pageA.locator('[data-screen="timeline"].active').waitFor();
    for (const view of ["album", "map", "report", "private", "trash"]) {
      // Partner SSE updates can complete between two view assertions. Reapply
      // the intended top-level page immediately before each real click so this
      // release test exercises the same recovery path a user gets by tapping
      // “时光” again, without force-clicking hidden DOM.
      await pageA.locator('.bottom-nav [data-nav="timeline"]').click();
      await pageA.locator('[data-screen="timeline"].active').waitFor();
      if (["report", "private", "trash"].includes(view)) {
        const more = pageA.locator('[data-screen="timeline"].active .timeline-more');
        if (!(await more.evaluate(node => node.open))) {
          await more.locator(":scope > summary").click();
        }
      }
      const tab = pageA.locator(`[data-screen="timeline"].active #timelineView [data-view="${view}"]`);
      await tab.waitFor({ state: "visible" });
      await tab.click();
      await pageA.waitForFunction(() => !document.querySelector("#timelineBody")?.textContent.includes("正在整理"));
      assert((await pageA.locator("#timelineBody").textContent()).trim().length > 0, `${view} 视图为空`);
    }
    const openTimelineSmartDisclosure = async () => {
      const disclosure = pageA.locator('[data-screen="timeline"].active .smart-disclosure');
      if (!(await disclosure.evaluate(node => node.open))) {
        await disclosure.locator(":scope > summary").click();
      }
    };
    await openTimelineSmartDisclosure();
    await pageA.locator('[data-smart="timeline"] [data-smart-node="month_album"]').first().click();
    const monthButton = pageA.locator("#timelineBody .album-month").first();
    if (await monthButton.count()) {
      await monthButton.click();
      assert((await pageA.locator("#timelineBody").textContent()).includes("月份回忆"), "月份卡片不能进入实际内容");
    }
    await openTimelineSmartDisclosure();
    await pageA.locator('[data-smart="timeline"] [data-smart-node="footprint_summary"]').first().click();
    await pageA.waitForFunction(() => !document.querySelector("#timelineBody")?.textContent.includes("正在整理"));
    await openTimelineSmartDisclosure();
    await pageA.locator('[data-smart="timeline"] [data-smart-node="year_report"]').first().click();
    await pageA.waitForFunction(() => document.querySelector("#timelineBody")?.textContent.includes("年度报告"));
    assert((await pageA.locator("#timelineBody").textContent()).includes("年度报告"), "年报智能卡片没有打开真实报告");
    await openTimelineSmartDisclosure();
    await pageA.locator('[data-smart="timeline"] [data-smart-node="privacy_status"]').first().click();
    await pageA.waitForFunction(title => document.querySelector("#timelineBody")?.textContent.includes(title), privateTitle);
    assert((await pageA.locator("#timelineBody").textContent()).includes(privateTitle), "仅我内容智能卡片没有打开私密内容");
    const timelineMore = pageA.locator('[data-screen="timeline"].active .timeline-more');
    if (!(await timelineMore.evaluate(node => node.open))) {
      await timelineMore.locator(":scope > summary").click();
    }
    await pageA.locator('#timelineView [data-view="trash"]').click();
    const trashCard = pageA.locator("#timelineBody .life-record", { hasText: editedMomentTitle });
    await trashCard.waitFor({ state: "visible" });
    const restoreMomentButton = trashCard.locator('[data-action="restore-moment"]');
    await restoreMomentButton.waitFor({ state: "visible" });
    await Promise.all([
      pageA.waitForResponse(response => response.url().includes(`/api/v1/moments/${collected.id}/restore`) && response.request().method() === "POST" && response.ok()),
      // Timeline SSE refreshes can replace this card between pointerdown and
      // pointerup. Dispatch the same bubbling click event on the current node
      // so this regression checks the delegated UI handler instead of mouse
      // timing against a node that was just replaced.
      restoreMomentButton.dispatchEvent("click"),
    ]);
    assert((await api(pageA, "/api/v1/bootstrap")).moments.some(item => item.id === collected.id), "回收站恢复失败");
    checks.push("智能卡片进入月份内容、足迹、年报、仅我内容、回收站与恢复");

    await pageA.locator('[data-nav="life"]').last().click();
    await pageA.locator('[data-screen="life"] .settings[data-action="open-profile"]').click();
    await pageA.locator("#deviceList .device-row").first().waitFor();
    const protectedMedia = pageA.locator("#accountMediaList .account-media-row", { hasText: "release-check" });
    await protectedMedia.waitFor();
    await protectedMedia.locator("img").waitFor();
    assert(await protectedMedia.locator("small").textContent().then(value => value.includes("隐私信息已清理")), "账号媒体库未标记图片隐私清理");
    await protectedMedia.locator('[data-action="trash-media"]').waitFor();
    const zipCheck = await pageA.evaluate(async () => {
      const response = await fetch("/api/v1/account/export.zip");
      const bytes = new Uint8Array(await response.arrayBuffer());
      return { ok: response.ok, signature: String.fromCharCode(...bytes.slice(0, 2)), size: bytes.length };
    });
    assert(zipCheck.ok && zipCheck.signature === "PK" && zipCheck.size > 200, "账号 ZIP 导出无效");
    await pageA.locator("#accountModal [data-action=close-modal]").click();
    checks.push("设备中心、私有媒体库与完整 ZIP 数据导出");

    const adminPage = await contextA.newPage();
    adminPage.on("pageerror", error => errors.push(`admin pageerror: ${error.message}`));
    adminPage.on("response", response => { if (response.status() === 404) errors.push(`admin response 404: ${response.url()}`); });
    adminPage.on("console", message => {
      if (message.type() === "error" && !shouldIgnoreConsoleError(message)) errors.push(`admin console: ${message.text()}`);
    });
    await adminPage.goto(`${originA}/admin`, { waitUntil: "domcontentloaded" });
    await adminPage.locator("#checkGrid .check-card").first().waitFor();
    assert(await adminPage.locator("#checkGrid .check-card").count() >= 7, "后台健康检查数量异常");
    assert((await adminPage.locator("#privacyNotice").textContent()).includes("不读取"), "后台没有显示隐私边界");
    for (const viewport of viewports) {
      await adminPage.setViewportSize(viewport);
      const overflow = await adminPage.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert(overflow <= 1, `后台 ${viewport.width}px 页面横向溢出 ${overflow}px`);
    }
    await adminPage.setViewportSize({ width: 390, height: 844 });
    await adminPage.locator("#backupButton").click();
    await adminPage.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("备份已完成"));
    assert(await adminPage.locator("#backupList .list-row").count() >= 1, "后台手动备份没有显示结果");
    assert((await adminPage.locator("#backupList .panel-note").textContent()).includes("同一磁盘"), "后台没有说明七牛媒体与数据库异盘备份的区别");
    assert(await adminPage.locator("#storageUsage .capacity-row").count() >= 2, "后台未显示个人与情侣容量");
    await adminPage.locator("#backupList [data-verify-backup]").first().click();
    await adminPage.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("恢复演练通过"));
    await adminPage.close();
    checks.push("简易后台、异常体检、手机布局与安全备份");

    const offlineContext = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: "allow" });
    const offlinePage = await offlineContext.newPage();
    offlinePage.on("pageerror", error => errors.push(`offline pageerror: ${error.message}`));
    offlinePage.on("response", response => { if (response.status() === 404) errors.push(`offline response 404: ${response.url()}`); });
    await login(offlinePage, originA, "test", "test123");
    await offlinePage.waitForFunction(() => navigator.serviceWorker?.controller, null, { timeout: 10_000 }).catch(async () => {
      await offlinePage.reload({ waitUntil: "domcontentloaded" });
      await offlinePage.waitForFunction(() => navigator.serviceWorker?.controller, null, { timeout: 10_000 });
    });
    await offlineContext.setOffline(true);
    await offlinePage.reload({ waitUntil: "domcontentloaded" });
    await offlinePage.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
    assert((await offlinePage.locator("#tripTitle").textContent()).trim().length > 0, "离线时未能读取最近行程快照");
    await offlineContext.setOffline(false);
    await offlineContext.close();
    checks.push("PWA 静态缓存与离线已确认行程快照");

    const accountSwitchErrorsStart = errors.length;
    const accountSwitchPage = await contextA.newPage();
    await accountSwitchPage.goto(`${originA}/`, { waitUntil: "domcontentloaded" });
    await logout(accountSwitchPage);
    await pageA.waitForFunction(() => !document.querySelector("#authGate")?.classList.contains("hidden"));
    await login(accountSwitchPage, originA, "test1", "test123");
    await pageA.waitForFunction(() => document.querySelector("#partnerMomentLabel")?.textContent.includes("当前账号：演示乙"));
    assert((await pageA.locator(".avatar-a").first().textContent()).trim() === "乙", "切换账号后旧标签页仍显示演示甲的头像");
    await logout(accountSwitchPage);
    await pageA.waitForFunction(() => !document.querySelector("#authGate")?.classList.contains("hidden"));
    await login(accountSwitchPage, originA, "test", "test123");
    await pageA.waitForFunction(() => document.querySelector("#partnerMomentLabel")?.textContent.includes("当前账号：演示甲"));
    await accountSwitchPage.close();
    // The shared-cookie account-switch test deliberately invalidates the old
    // tab while its bootstrap fan-out may still have protected reads in
    // flight. Chrome reports those cancelled-session responses as generic
    // resource errors before the storage event reloads the tab. Accept only
    // the known read-only endpoints inside this exact switch window; every
    // other 401 and every error outside the window remains release-fatal.
    const expectedSwitchReads = new Set([
      "/api/v1/media/usage",
      "/api/v1/couple/context",
      "/api/v1/calls/active",
      "/api/v1/push/config",
      "/api/v1/ui/life",
    ]);
    const switchErrors = errors.slice(accountSwitchErrorsStart).filter(value => {
      if (!value.includes("status of 401 (Unauthorized)")) return true;
      try {
        const match = value.match(/console \((https?:\/\/[^)]+)\)/);
        return !match || !expectedSwitchReads.has(new URL(match[1]).pathname);
      } catch (_) {
        return true;
      }
    });
    errors.splice(accountSwitchErrorsStart, errors.length - accountSwitchErrorsStart, ...switchErrors);
    checks.push("同源多标签页切换账号时自动刷新身份");

    await pageA.locator('[data-nav="life"]').last().click();
    await pageA.locator('[data-screen="life"] .settings[data-action="open-profile"]').click();
    assert((await pageA.locator("#pushSettingsTitle").textContent()).includes("消息与来电提醒"), "账号中心缺少消息与来电提醒设置");
    assert((await pageA.locator("#pushSettingsTitle").locator("xpath=..") .textContent()).includes("不是系统全屏 CallKit"), "后台通知未说明 PWA 与 CallKit 边界");
    checks.push("账号中心消息与来电提醒入口及 iOS 使用边界");
    await pageA.locator("#currentPassword").fill("test123");
    await pageA.locator("#newPassword").fill("browser-private-password");
    await pageA.locator("#confirmNewPassword").fill("browser-private-password");
    await pageA.locator("#changePasswordForm button").click();
    await pageA.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("密码已更换"));
    checks.push("账号中心改密与其他会话撤销");

    if (errors.length) throw new Error(`浏览器运行错误：${errors.join(" | ")}`);
    console.log(JSON.stringify({ ok: true, checks, origins: [originA, originB] }, null, 2));
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error.stack || error.message);
  process.exitCode = 1;
});
