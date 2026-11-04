/* Private artwork audit: isolated accounts/database; never mutates production data. */
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");
const python = process.env.PYTHON_BINARY || "python3";
const chromePath = process.env.PLAYWRIGHT_CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const artNames = {
  kitty: "kitty-stickers-v1.png", daniel: "daniel-stickers-v1.png",
  kitty_pages: "kitty-page-stickers-v2.png", daniel_pages: "daniel-page-stickers-v2.png",
};
const decorativeSelector = ".theme-sticker, .theme-window-decoration";
// Used only when private generated artwork has not been installed on this machine.
const tinyPng = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64");

function assert(value, message) {
  if (!value) throw new Error(message);
}

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

async function waitForHealth(origin, server) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    if (server.exitCode !== null) throw new Error("隔离测试服务器提前退出");
    try { if ((await fetch(origin + "/api/health")).ok) return; } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error("隔离测试服务器启动超时");
}

async function login(page, origin, username) {
  await page.goto(origin + "/", { waitUntil: "domcontentloaded" });
  await page.locator("#authUsername").fill(username);
  await page.locator("#authPassword").fill("test123");
  await page.locator("#authSubmit").click();
  await page.waitForFunction(() => document.querySelector("#authGate")?.classList.contains("hidden"));
  await page.waitForFunction(() => Boolean(document.querySelector(".avatar-a")?.dataset.avatarTheme));
}

async function openAccount(page) {
  await page.locator('.bottom-nav [data-nav="home"]').click();
  await page.locator(".avatar-pair").click();
  await page.locator("#accountModal.open").waitFor();
  await page.locator('[data-action="open-account-section"][data-account-section="appearance"]').click();
}

async function chooseTheme(page, theme) {
  await openAccount(page);
  await page.locator('#themeProfileForm input[value="' + theme + '"]').check();
  await page.locator("#themeProfileForm > button").click();
  await page.waitForFunction(value => document.body.dataset.uiTheme === value, theme);
  await page.waitForFunction(() => !document.querySelector("#themeProfileForm > button")?.disabled);
  await page.locator("#accountModal .sheet-close").click();
  await page.locator("#accountModal").waitFor({ state: "hidden" });
  await page.waitForFunction(() => !document.querySelector("#toast")?.classList.contains("show"));
}

async function chooseDisplayPreferences(page, textSize, density) {
  await openAccount(page);
  await page.locator('#themeProfileForm input[name="uiTextSize"][value="' + textSize + '"]').check();
  await page.locator('#themeProfileForm input[name="stickerDensity"][value="' + density + '"]').check();
  await page.locator("#themeProfileForm > button").click();
  await page.waitForFunction(([size, amount]) => document.body.dataset.uiTextSize === size && document.body.dataset.stickerDensity === amount, [textSize, density]);
  await page.locator("#accountModal .sheet-close").click();
  await page.locator("#accountModal").waitFor({ state: "hidden" });
  await page.waitForFunction(() => !document.querySelector("#toast")?.classList.contains("show"));
}

async function assertDecorations(page, requireVisible) {
  const result = await page.locator(decorativeSelector).evaluateAll(nodes => nodes.map(node => {
    const css = getComputedStyle(node);
    const box = node.getBoundingClientRect();
    const visible = css.display !== "none" && css.visibility !== "hidden" && Number(css.opacity) > 0 && box.width > 0 && box.height > 0 && !node.closest("[hidden]");
    return {
      visible,
      hidden: node.getAttribute("aria-hidden") === "true",
      inert: css.pointerEvents === "none",
      interactive: node.matches("a,button,input,textarea,select,[role=button]") || node.tabIndex >= 0 || Boolean(node.querySelector("a,button,input,textarea,select,[tabindex]:not([tabindex='-1'])")),
      background: css.backgroundImage,
    };
  }));
  assert(result.length > 0, "主题装饰节点缺失");
  assert(result.every(item => item.hidden && !item.interactive), "装饰图片不应进入读屏或键盘操作路径");
  assert(result.every(item => !item.visible || item.inert), "装饰图片会截获用户点击");
  if (requireVisible) assert(result.some(item => item.visible && item.background !== "none"), "选择角色主题后没有可见角色图片");
  else assert(result.every(item => !item.visible), "经典主题或无素材降级仍显示角色装饰");
}

async function assertLayout(page, label) {
  const metrics = await page.evaluate(() => {
    const shell = document.querySelector(".phone-shell");
    const nav = document.querySelector(".bottom-nav").getBoundingClientRect();
    const chatActive = document.querySelector('[data-screen="chat"]')?.classList.contains("active");
    const dock = chatActive ? document.querySelector(".chat-compose-dock").getBoundingClientRect() : null;
    return {
      pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      shellOverflow: shell.scrollWidth - shell.clientWidth,
      nav: { top: nav.top, bottom: nav.bottom, height: nav.height },
      viewport: innerHeight,
      dock: dock && { top: dock.top, bottom: dock.bottom, height: dock.height },
    };
  });
  assert(metrics.pageOverflow <= 1 && metrics.shellOverflow <= 1, label + " 横向溢出 " + JSON.stringify(metrics));
  assert(metrics.nav.height > 0 && metrics.nav.top >= 0 && metrics.nav.bottom <= metrics.viewport + 1, label + " 底部导航离开可视区域 " + JSON.stringify(metrics));
  if (metrics.dock) assert(metrics.dock.top >= 0 && metrics.dock.bottom <= metrics.nav.top + 1, label + " 消息输入框遮挡导航 " + JSON.stringify(metrics));
}

async function assertSmartDraftStability(page, origin) {
  await page.locator('.bottom-nav [data-nav="home"]').click();
  const disclosure = page.locator(".home-smart");
  if (!await disclosure.evaluate(node => node.open)) await disclosure.locator(":scope > summary").click();
  const input = page.locator('[data-smart-form="home"] input');
  await input.waitFor({ state: "visible" });
  const draft = "周六下午想一起散步，先看看附近的公园";
  await input.fill(draft);
  const early = await page.evaluate(async () => {
    const form = document.querySelector('[data-smart-form="home"]');
    const field = form.querySelector("input");
    const button = form.querySelector("button");
    field.focus();
    field.setSelectionRange(3, 9, "backward");
    await loadSmartSurface("home", { quiet: true });
    return {
      sameForm: document.querySelector('[data-smart-form="home"]') === form,
      sameInput: document.querySelector('[data-smart-form="home"] input') === field,
      sameButton: document.querySelector('[data-smart-form="home"] button') === button,
      focused: document.activeElement === field,
      text: field.value,
      start: field.selectionStart,
      end: field.selectionEnd,
      direction: field.selectionDirection,
    };
  });
  assert(early.sameForm && early.sameInput && early.sameButton && early.focused && early.text === draft && early.start === 3 && early.end === 9 && early.direction === "backward", "已有草稿的后台刷新重建表单或丢失焦点/选区：" + JSON.stringify(early));

  // Keep a real server response pending, then begin typing. This exercises
  // the second guard after await api(), not just the fast early-return guard.
  await input.fill("");
  let release;
  const holdResponse = new Promise(resolve => { release = resolve; });
  let heldRequests = 0;
  const routePattern = "**/api/v1/ui/home";
  const delay = async route => {
    if (route.request().method() !== "GET" || heldRequests > 0) return route.continue();
    heldRequests += 1;
    const response = await route.fetch();
    await holdResponse;
    await route.fulfill({ response });
  };
  await page.route(routePattern, delay);
  try {
    const requestArrived = page.waitForRequest(request => request.url() === origin + "/api/v1/ui/home" && request.method() === "GET");
    await page.evaluate(() => {
      const form = document.querySelector('[data-smart-form="home"]');
      window.__privateThemeDraftProbe = {
        form, field: form.querySelector("input"), button: form.querySelector("button"),
        pending: loadSmartSurface("home", { quiet: true }),
      };
    });
    await requestArrived;
    const lateDraft = "请求还没回来，我正在输入周日的约会安排";
    await input.fill(lateDraft);
    await input.evaluate(field => { field.focus(); field.setSelectionRange(4, 12, "forward"); });
    release();
    const late = await page.evaluate(async () => {
      const saved = window.__privateThemeDraftProbe;
      await saved.pending;
      return {
        sameForm: document.querySelector('[data-smart-form="home"]') === saved.form,
        sameInput: document.querySelector('[data-smart-form="home"] input') === saved.field,
        sameButton: document.querySelector('[data-smart-form="home"] button') === saved.button,
        focused: document.activeElement === saved.field,
        text: saved.field.value,
        start: saved.field.selectionStart,
        end: saved.field.selectionEnd,
        direction: saved.field.selectionDirection,
      };
    });
    assert(heldRequests === 1, "没有实际覆盖已发请求、输入后才回包的场景");
    assert(late.sameForm && late.sameInput && late.sameButton && late.focused && late.text === lateDraft && late.start === 4 && late.end === 12 && late.direction === "forward", "迟到的后台响应重建表单或丢失焦点/选区：" + JSON.stringify(late));
  } finally {
    release();
    await page.unroute(routePattern, delay);
    await page.evaluate(() => { delete window.__privateThemeDraftProbe; });
  }
  await input.fill("");
  await disclosure.locator(":scope > summary").click();
}

function contentFingerprint(database) {
  const result = spawnSync(python, ["-c", `import hashlib,json,sqlite3,sys
db=sqlite3.connect(sys.argv[1])
tables=('moments','trips','meal_wishes','wishlist_items','shared_comments','calendar_entries')
result={}
for name in tables:
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone():
        result[name]=db.execute('SELECT * FROM '+name+' ORDER BY rowid').fetchall()
result['messages']=db.execute('SELECT id,couple_id,sender_id,type,content,metadata,sent_at FROM messages ORDER BY id').fetchall()
print(hashlib.sha256(json.dumps(result,ensure_ascii=False,sort_keys=True).encode()).hexdigest())
`, database], { encoding: "utf8" });
  assert(result.status === 0, "内容完整性快照失败：" + result.stderr);
  return result.stdout.trim();
}

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "love-private-theme-"));
  fs.mkdirSync(path.join(root, "tmp"), { recursive: true });
  const screenshotDir = fs.mkdtempSync(path.join(root, "tmp", "private-theme-audit-"));
  const database = path.join(temp, "audit.db");
  const artRoot = path.join(temp, "theme-assets");
  fs.mkdirSync(artRoot);
  const copiedRealArt = [];
  for (const [theme, filename] of Object.entries(artNames)) {
    const original = path.join(root, "data", "theme-assets", filename);
    const target = path.join(artRoot, filename);
    if (fs.existsSync(original)) { fs.copyFileSync(original, target); copiedRealArt.push(theme); }
    else fs.writeFileSync(target, tinyPng);
  }
  const port = await freePort();
  const origin = "http://127.0.0.1:" + port;
  const env = {
    ...process.env, APP_HOST: "127.0.0.1", APP_PORT: String(port), DATABASE_PATH: database,
    MEDIA_ROOT: path.join(temp, "media"), BACKUP_ROOT: path.join(temp, "backups"),
    DEMO_MODE: "true", REGISTRATION_MODE: "open", ADMIN_USERNAMES: "test",
    OPEN_METEO_ENABLED: "false", AMAP_WEB_KEY: "", GEOAPIFY_ENABLED: "false", GEOAPIFY_API_KEY: "",
    LLM_API_KEY: "", UI_LLM_API_KEY: "", OCR_PROVIDER: "mock", ALIBABA_CLOUD_ACCESS_KEY_ID: "",
    ALIBABA_CLOUD_ACCESS_KEY_SECRET: "", OCR_API_URL: "", OCR_API_KEY: "", MEDIA_STORAGE_PROVIDER: "local",
    QINIU_ACCESS_KEY: "", QINIU_SECRET_KEY: "", SUPABASE_URL: "", SUPABASE_ANON_KEY: "",
    LOCAL_ADMIN_NOTIFICATIONS: "false", WATCHDOG_AUTO_RESTART: "false", RTC_PROVIDER: "webrtc",
    SERPAPI_ENABLED: "false", SERPAPI_API_KEY: "", JUHE_TRAIN_ENABLED: "false", JUHE_TRAIN_API_KEY: "",
    BOOKING_API_URL: "", BOOKING_API_KEY: "", CORS_ALLOWED_ORIGINS: origin,
  };
  const server = spawn(python, ["main.py"], { cwd: root, env, stdio: ["ignore", "pipe", "pipe"] });
  let logs = "";
  server.stdout.on("data", value => { logs += value.toString(); });
  server.stderr.on("data", value => { logs += value.toString(); });
  let browser;
  const errors = [];
  let screenshotCount = 0;
  try {
    await waitForHealth(origin, server);
    for (const url of ["/api/v1/theme-assets", ...Object.values(artNames).map(name => "/api/v1/theme-assets/" + name)]) {
      const response = await fetch(origin + url, { redirect: "manual" });
      assert([401, 403].includes(response.status), "未登录可以读取私用角色资源：" + url + " " + response.status);
    }
    browser = await chromium.launch({ headless: true, ...(fs.existsSync(chromePath) ? { executablePath: chromePath } : {}) });
    const contexts = await Promise.all([browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: "block" }), browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: "block" })]);
    const pages = await Promise.all(contexts.map(context => context.newPage()));
    pages.forEach((page, index) => page.on("pageerror", error => errors.push(index + ": " + error.message)));
    await Promise.all(pages.map((page, index) => login(page, origin, index === 0 ? "test" : "test1")));
    const before = contentFingerprint(database);
    for (const [index, theme] of ["kitty", "daniel"].entries()) {
      const page = pages[index];
      await page.waitForFunction(() => document.body.getAttribute("data-theme-art-ready")?.includes("kitty") && document.body.getAttribute("data-theme-art-ready")?.includes("daniel"));
      const response = await contexts[index].request.get(origin + "/api/v1/theme-assets");
      assert(response.ok(), "登录后无法获取角色资源清单");
      const manifest = await response.json();
      for (const artTheme of Object.keys(artNames)) {
        const resource = manifest.assets?.[artTheme];
        assert(typeof resource === "string" && resource.startsWith("/api/v1/theme-assets/"), "角色资源清单应只包含本站受保护地址");
        const art = await contexts[index].request.get(origin + resource);
        assert(art.ok() && art.headers()["content-type"]?.startsWith("image/png"), "登录后角色图片加载失败");
        assert((await art.body()).equals(fs.readFileSync(path.join(artRoot, artNames[artTheme]))), "私有角色资源返回了错误文件");
        assert(/private/.test(art.headers()["cache-control"] || "") && /no-cache/.test(art.headers()["cache-control"] || ""), "私有角色资源必须仅限当前账号缓存并在使用前重新验证");
      }
      await chooseTheme(page, theme);
      await assertDecorations(page, true);
      await page.locator('.bottom-nav [data-nav="chat"]').click();
      assert(await page.locator('.theme-page-ribbon .theme-sticker--chat').count() === 1, "消息页缺少对应情景贴纸");
      await page.locator('.bottom-nav [data-nav="trip"]').click();
      assert(await page.locator('.theme-page-ribbon .theme-sticker--map').count() === 1, "旅行页缺少对应情景贴纸");
      await page.locator('.bottom-nav [data-nav="timeline"]').click();
      assert(await page.locator('.theme-page-ribbon .theme-sticker--album').count() === 1, "时光页缺少对应情景贴纸");
    }
    assert(await pages[0].evaluate(() => document.body.dataset.uiTheme) === "kitty", "伴侣修改主题覆盖了本人偏好");
    assert(await pages[1].evaluate(() => document.body.dataset.uiTheme) === "daniel", "第二个账号主题保存失败");

    for (const [index, theme] of ["kitty", "daniel"].entries()) {
      const page = pages[index];
      for (const viewport of [{ width: 390, height: 844 }, { width: 768, height: 1024 }, { width: 1440, height: 900 }]) {
        await page.setViewportSize(viewport);
        for (const surface of ["home", "chat", "trip", "timeline", "play", "life"]) {
          await page.locator('.bottom-nav [data-nav="' + surface + '"]').click();
          await page.locator('[data-screen="' + surface + '"].active').waitFor();
          await assertLayout(page, theme + " " + viewport.width + " " + surface);
          await assertDecorations(page, true);
          await page.screenshot({ path: path.join(screenshotDir, theme + "-" + viewport.width + "-" + surface + ".png"), animations: "disabled" });
          screenshotCount += 1;
          if (surface === "life") {
            await page.locator(".life-core-actions").evaluate(node => node.scrollIntoView({ block: "center", behavior: "instant" }));
            await page.screenshot({ path: path.join(screenshotDir, theme + "-" + viewport.width + "-life-shortcuts.png"), animations: "disabled" });
            screenshotCount += 1;
          }
        }
        await openAccount(page);
        await page.locator("#accountModal .theme-window-decoration").waitFor({ state: "attached" });
        await page.screenshot({ path: path.join(screenshotDir, theme + "-" + viewport.width + "-account.png"), animations: "disabled" });
        screenshotCount += 1;
        await page.locator("#accountModal .sheet-close").click();
        await page.locator("#accountModal").waitFor({ state: "hidden" });
        await page.locator('[data-screen="home"] [data-action="new-moment"][data-type="文字"]').click();
        await page.locator("#momentModal.open").waitFor();
        await page.locator("#momentModal .theme-window-decoration").waitFor({ state: "attached" });
        await page.locator("#momentTitle").fill("仅用于可编辑性检查，不保存");
        await page.screenshot({ path: path.join(screenshotDir, theme + "-" + viewport.width + "-moment.png"), animations: "disabled" });
        screenshotCount += 1;
        await page.locator("#momentModal .sheet-close").click();
        await page.locator("#momentModal").waitFor({ state: "hidden" });
      }
      await page.reload({ waitUntil: "domcontentloaded" });
      await page.waitForFunction(value => document.body.dataset.uiTheme === value, theme);
      await page.waitForFunction(() => Boolean(document.body.getAttribute("data-theme-art-ready")));
    }

    await pages[0].setViewportSize({ width: 390, height: 844 });
    await chooseDisplayPreferences(pages[0], "large", "full");
    await pages[0].locator('.bottom-nav [data-nav="life"]').click();
    await pages[0].locator('[data-life-section="meals"]').first().click();
    assert(await pages[0].locator('#mealWorkspace .theme-context-sticker').evaluate(node => getComputedStyle(node).display !== "none"), "丰富贴纸模式没有显示做饭情景贴纸");
    await assertLayout(pages[0], "大字与丰富贴纸 390");

    await chooseDisplayPreferences(pages[1], "standard", "minimal");
    await pages[1].locator('.bottom-nav [data-nav="chat"]').click();
    assert(await pages[1].locator('[data-screen="chat"] .theme-page-ribbon').evaluate(node => getComputedStyle(node).display === "none"), "简洁贴纸模式仍显示页面贴纸");
    assert(await pages[0].evaluate(() => document.body.dataset.stickerDensity) === "full", "伴侣修改贴纸数量影响了本人设置");

    await chooseDisplayPreferences(pages[0], "standard", "cute");
    await chooseDisplayPreferences(pages[1], "standard", "cute");
    await assertSmartDraftStability(pages[0], origin);
    assert(contentFingerprint(database) === before, "切换主题或关闭未保存弹窗改变了现有内容");

    await chooseTheme(pages[0], "classic");
    await assertDecorations(pages[0], false);
    assert(await pages[1].evaluate(() => document.body.dataset.uiTheme) === "daniel", "经典主题切换影响伴侣");
    await pages[0].locator('.bottom-nav [data-nav="life"]').click();
    await pages[0].locator('[data-life-section="overview"]').first().click();
    await pages[0].locator('.life-more > summary').click();
    await pages[0].locator(".account-logout").click();
    await pages[0].waitForFunction(() => !document.querySelector("#authGate")?.classList.contains("hidden"));
    const cleared = await pages[0].evaluate(() => ({
      ready: document.body.getAttribute("data-theme-art-ready"),
      kitty: getComputedStyle(document.body).getPropertyValue("--kitty-sticker-sheet").trim(),
      daniel: getComputedStyle(document.body).getPropertyValue("--daniel-sticker-sheet").trim(),
    }));
    assert(!cleared.ready && (!cleared.kitty || cleared.kitty === "none") && (!cleared.daniel || cleared.daniel === "none"), "退出登录后私用角色资源状态未清理 " + JSON.stringify(cleared));

    // Empty manifest is a supported deployment state, not an application failure.
    const fallbackContext = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: "block" });
    const fallback = await fallbackContext.newPage();
    fallback.on("pageerror", error => errors.push("fallback: " + error.message));
    let fallbackRequests = 0;
    await fallback.route("**/api/v1/theme-assets", route => {
      fallbackRequests += 1;
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ assets: {} }) });
    });
    await login(fallback, origin, "test1");
    await fallback.waitForFunction(() => document.body.dataset.uiTheme === "daniel");
    await fallback.waitForTimeout(150);
    assert(fallbackRequests > 0, "未验证无素材清单降级路径");
    assert(!await fallback.evaluate(() => document.body.getAttribute("data-theme-art-ready")), "无素材时仍标记角色资源就绪");
    await assertDecorations(fallback, false);
    await fallback.locator('.bottom-nav [data-nav="chat"]').click();
    await fallback.locator("#chatInput").fill("降级时输入框仍可使用，不发送");
    await assertLayout(fallback, "无素材降级");
    await openAccount(fallback);
    await fallback.locator("#accountModal .sheet-close").click();
    await fallback.locator("#accountModal").waitFor({ state: "hidden" });
    assert(contentFingerprint(database) === before, "无素材降级或退出登录改变了既有内容");
    assert(errors.length === 0, "页面脚本异常：" + errors.join(" | "));
    console.log("私用角色主题验收通过：登录保护、双账号独立偏好、五页三尺寸、弹窗/导航、读屏装饰隔离、退出清理、无素材降级、内容保全；智能输入草稿的即时/迟到刷新均保持 DOM、焦点与选区。");
    console.log("截图 " + screenshotCount + " 张（私有忽略目录）：" + screenshotDir);
    console.log("真实角色素材：" + (copiedRealArt.join("、") || "未安装，本次使用测试 PNG"));
  } catch (error) {
    if (logs.trim()) console.error(logs.slice(-6000));
    console.error("排查截图目录：" + screenshotDir);
    throw error;
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) server.kill("SIGTERM");
    if (server.exitCode === null) await new Promise(resolve => server.once("exit", resolve));
    fs.rmSync(temp, { recursive: true, force: true });
  }
})().catch(error => {
  console.error(error.stack || error.message);
  process.exitCode = 1;
});
