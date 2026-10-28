/* Executable PWA shortcut and offline-write contract audit. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const app = fs.readFileSync("static/app.js", "utf8");
const start = app.indexOf("const PWA_SHORTCUT_INTENTS");
const end = app.indexOf("const state =");
assert.ok(start >= 0 && end > start, "static/app.js must expose the PWA/offline policy before application state");

const sessionValues = new Map();
const sessionStorage = {
  getItem(key) { return sessionValues.get(key) || null; },
  setItem(key, value) { sessionValues.set(key, value); },
  removeItem(key) { sessionValues.delete(key); },
};
let search = "";
let replacedUrl = "";
const context = vm.createContext({
  URLSearchParams,
  JSON,
  Object,
  Number,
  String,
  Set,
  Date,
  sessionStorage,
  location: { pathname: "/", get search() { return search; }, hash: "#kept" },
  history: { state: { existing: true }, replaceState(_state, _title, url) { replacedUrl = url; } },
});
vm.runInContext(app.slice(start, end), context);

function reset(nextSearch = "") {
  sessionValues.clear();
  search = nextSearch;
  replacedUrl = "";
}

// A protected shortcut survives login and pairing, then executes once without
// synthesising any submit/write action. Consuming it preserves unrelated URL
// state and removes only the shortcut parameter.
reset("?action=moment&utm_source=home");
assert.equal(context.capturePwaShortcutIntent(), "moment");
let openedMoments = 0;
let writes = 0;
const momentHandlers = { openMoment() { openedMoments += 1; }, openChat() {}, submit() { writes += 1; } };
assert.equal(context.restorePwaShortcutIntent({ authenticated: false, paired: false }, momentHandlers), false);
assert.equal(context.restorePwaShortcutIntent({ authenticated: true, paired: false }, momentHandlers), false);
assert.equal(openedMoments, 0);
assert.equal(sessionStorage.getItem("love-pending-shortcut"), "moment");
assert.equal(context.restorePwaShortcutIntent({ authenticated: true, paired: true }, momentHandlers), true);
assert.equal(openedMoments, 1);
assert.equal(writes, 0, "a shortcut may open an editor but must never submit it");
assert.equal(sessionStorage.getItem("love-pending-shortcut"), null);
assert.equal(replacedUrl, "/?utm_source=home#kept");
assert.equal(context.restorePwaShortcutIntent({ authenticated: true, paired: true }, momentHandlers), false);
assert.equal(openedMoments, 1, "bootstrap refresh must not replay a consumed shortcut");

// The message shortcut opens/focuses chat but does not send a message. Unknown
// actions are ignored instead of being dispatched as arbitrary action IDs.
reset("?screen=chat&from=pwa");
assert.equal(context.capturePwaShortcutIntent(), "message");
let chatOpened = 0;
const messageHandlers = { openChat() { chatOpened += 1; }, openMoment() {}, submit() { writes += 1; } };
assert.equal(context.restorePwaShortcutIntent({ authenticated: true, paired: true }, messageHandlers), true);
assert.equal(chatOpened, 1);
assert.equal(writes, 0);
assert.equal(replacedUrl, "/?from=pwa#kept");

// Conflicting shortcut parameters must resolve deterministically and be
// consumed together. Otherwise the leftover shortcut would fire after reload.
reset("?action=moment&screen=chat&shared_type=moment&shared_id=moment-1");
assert.equal(context.capturePwaShortcutIntent(), "moment");
assert.equal(context.restorePwaShortcutIntent({ authenticated: true, paired: true }, momentHandlers), true);
assert.equal(replacedUrl, "/?shared_type=moment&shared_id=moment-1#kept");
search = new URL(`https://love.example${replacedUrl}`).search;
assert.equal(context.capturePwaShortcutIntent(), null);
assert.equal(context.restorePwaShortcutIntent({ authenticated: true, paired: true }, messageHandlers), false);
assert.equal(chatOpened, 1, "a conflicting screen shortcut must not execute on refresh");

reset("?action=delete-account");
sessionStorage.setItem("love-pending-shortcut", "moment");
assert.equal(context.capturePwaShortcutIntent(), null);
assert.equal(context.restorePwaShortcutIntent({ authenticated: true, paired: true }, momentHandlers), false);
assert.equal(writes, 0);

// Admission and replay share one strict policy. Only text-message POST and a
// checked-only shopping PATCH are accepted; method, exact route, action and
// payload shape are all authoritative.
const messageBody = { type: "text", content: "离线也要保留", idempotency_key: "message-key" };
const messageSpec = context.offlineMutationSpec("/api/v1/messages", "POST", messageBody);
assert.equal(messageSpec.action, "send_text_message");
const shoppingBody = { checked: true, expected_version: 3, idempotency_key: "shopping-key" };
const shoppingSpec = context.offlineMutationSpec("/api/v1/shopping-lists/items/item-1", "PATCH", shoppingBody);
assert.equal(shoppingSpec.action, "check_shopping_item");
for (const [path, method, body, action] of [
  ["/api/v1/messages", "POST", { type: "image", content: "x", idempotency_key: "x" }, null],
  ["/api/v1/messages", "POST", { type: "text", content: "", idempotency_key: "x" }, null],
  ["/api/v1/messages", "PATCH", messageBody, null],
  ["/api/v1/shopping-lists/items/item-1", "PATCH", { checked: true, name: "tampered", expected_version: 3 }, null],
  ["/api/v1/shopping-lists/items/item-1", "PATCH", { checked: true, expected_version: 0 }, null],
  ["/api/v1/shopping-lists/complete", "POST", {}, null],
  ["/api/v1/moments", "POST", {}, null],
  ["/api/v1/ui/actions/draft/confirm", "POST", {}, null],
  ["/api/v1/trips/trip-1/assistant/messages", "POST", {}, null],
]) assert.equal(context.offlineMutationSpec(path, method, body), action, `${method} ${path} must be rejected`);
assert.equal(context.offlineQueueEntryIsValid({ action: messageSpec.action, path: "/api/v1/messages", method: "POST", body: messageBody }), true);
assert.equal(context.offlineQueueEntryIsValid({ action: "check_shopping_item", path: "/api/v1/messages", method: "POST", body: messageBody }), false, "replay must reject a forged action");
assert.equal(context.offlineQueueEntryIsValid({ path: "/api/v1/messages", method: "POST", body: messageBody }), false, "legacy generic entries must not replay");

for (const action of ["ai_compose", "trip_replan", "inventory_update", "pdf_export", "moment_save", "shared_confirm", "shopping_complete"]) {
  assert.equal(context.offlineActionAllowed(action, false), false, `${action} must explicitly require a connection`);
}
assert.equal(context.offlineActionAllowed("send_text_message", false), true);
assert.equal(context.offlineActionAllowed("check_shopping_item", false), true);

// Exercise the actual IndexedDB queue boundary as well as the pure policy. A
// caller may announce "pending" only after this function resolves.
const policySource = app.slice(start, app.indexOf("const initialPwaShortcutIntent"));
const queueSource = app.slice(app.indexOf("function cachePartition("), app.indexOf("function renderSmartSurface("));
const localValues = new Map([["love-active-scope", "alice:couple-a:3"]]);
const localStorage = {
  getItem(key) { return localValues.get(key) || null; },
  setItem(key, value) { localValues.set(key, value); },
  removeItem(key) { localValues.delete(key); },
  key(index) { return [...localValues.keys()][index]; },
  get length() { return localValues.size; },
};
let rows = [];
function database() {
  return {
    close() {},
    transaction() {
      const tx = {
        objectStore() {
          return {
            get(id) {
              const request = {};
              queueMicrotask(() => { request.result = structuredClone(rows.find(row => row.id === id)); request.onsuccess?.(); queueMicrotask(() => tx.oncomplete?.()); });
              return request;
            },
            getAll() {
              const request = {};
              queueMicrotask(() => { request.result = structuredClone(rows); request.onsuccess?.(); });
              return request;
            },
            put(value) {
              rows = rows.filter(row => row.id !== value.id);
              rows.push(structuredClone(value));
              queueMicrotask(() => tx.oncomplete?.());
            },
            delete(id) {
              rows = rows.filter(row => row.id !== id);
              queueMicrotask(() => tx.oncomplete?.());
            },
          };
        },
      };
      return tx;
    },
  };
}
const queueContext = vm.createContext({
  URLSearchParams, JSON, Object, Number, String, Set, Date, Promise, encodeURIComponent, decodeURIComponent,
  structuredClone, queueMicrotask, localStorage, navigator: { onLine: false }, window: {},
  state: { data: { user: { id: "alice" }, couple: { id: "couple-a", version: 3 }, permission_version: 3 }, offlineFlushPromise: null, manifests: {}, smartIntentDrafts: {}, meals: {}, destinationSearchCache: new Map(), lifeAssistant: {} },
  clientKey: (() => { let sequence = 0; return prefix => `${prefix}-${++sequence}`; })(), document: { querySelectorAll: () => [] }, toast() {}, stopRealtime() {}, resetTripLive() {}, AUTH_SYNC_KEY: "love-auth-sync",
});
vm.runInContext(policySource, queueContext);
vm.runInContext(queueSource, queueContext);
queueContext.openMealCache = async () => database();

(async () => {
  const queued = await queueContext.queueMealMutation("/api/v1/messages", "POST", { type: "text", content: "保留我", idempotency_key: "original-key" });
  assert.equal(rows.length, 1);
  assert.equal(queued.action, "send_text_message");
  assert.equal(rows[0].body.idempotency_key, "original-key");

  // Repeated offline toggles for the same item and partition collapse into the
  // final intent while retaining the first expected version/idempotency key.
  rows = [];
  const [firstToggle] = await Promise.all([
    queueContext.queueMealMutation("/api/v1/shopping-lists/items/item-1", "PATCH", { checked: true, expected_version: 7 }),
    queueContext.queueMealMutation("/api/v1/shopping-lists/items/item-1", "PATCH", { checked: false, expected_version: 8 }),
    queueContext.queueMealMutation("/api/v1/shopping-lists/items/item-1", "PATCH", { checked: true, expected_version: 9 }),
  ]);
  assert.equal(rows.length, 1, "same target toggles must be coalesced");
  assert.equal(rows[0].body.checked, true, "last offline intent must win");
  assert.equal(rows[0].body.expected_version, 7, "the first server version remains replayable");
  assert.equal(rows[0].body.idempotency_key, firstToggle.body.idempotency_key, "coalescing must retain stable idempotency");

  await queueContext.queueMealMutation("/api/v1/shopping-lists/items/item-2", "PATCH", { checked: true, expected_version: 2 });
  assert.equal(rows.length, 2, "different shopping items must never merge");
  queueContext.state.data = { user: { id: "alice" }, couple: { id: "couple-b", version: 1 }, permission_version: 1 };
  localStorage.setItem("love-active-scope", "alice:couple-b:1");
  await queueContext.queueMealMutation("/api/v1/shopping-lists/items/item-1", "PATCH", { checked: false, expected_version: 1 });
  assert.equal(rows.length, 3, "different couple partitions must never merge");
  queueContext.state.data = { user: { id: "bob" }, couple: { id: "couple-a", version: 3 }, permission_version: 3 };
  localStorage.setItem("love-active-scope", "bob:couple-a:3");
  await queueContext.queueMealMutation("/api/v1/shopping-lists/items/item-1", "PATCH", { checked: false, expected_version: 1 });
  assert.equal(rows.length, 4, "different users must never merge even for the same item and couple");

  queueContext.state.data = { user: { id: "alice" }, couple: { id: "couple-a", version: 3 }, permission_version: 3 };
  localStorage.setItem("love-active-scope", "alice:couple-a:3");
  rows = rows.filter(row => row.partition === "alice:couple-a:3");
  const replayed = [];
  queueContext.navigator.onLine = true;
  queueContext.api = async (_path, options) => { replayed.push(JSON.parse(options.body)); };
  await queueContext.performMealMutations();
  assert.deepEqual(replayed.map(item => item.checked), [true, true], "replay sends only final intent for each item");
  assert.equal(rows.length, 0, "successful replay must not leave stale toggles that can 409");

  function deferred() {
    let resolve; let reject;
    const promise = new Promise((onResolve, onReject) => { resolve = onResolve; reject = onReject; });
    return { promise, resolve, reject };
  }

  async function runInFlightToggleRace(firstOutcome) {
    rows = [];
    queueContext.navigator.onLine = true;
    queueContext.state.data = { user: { id: "alice" }, couple: { id: "couple-a", version: 3 }, permission_version: 3 };
    localStorage.setItem("love-active-scope", "alice:couple-a:3");
    const path = "/api/v1/shopping-lists/items/race-item";
    const initial = await queueContext.queueMealMutation(path, "PATCH", { checked: true, expected_version: 20 });
    const firstGate = deferred();
    const firstStarted = deferred();
    const secondStarted = deferred();
    const secondGate = deferred();
    const requests = [];
    let serverVersion = 20;
    let serverChecked = false;
    queueContext.api = async (_path, options) => {
      const payload = JSON.parse(options.body);
      requests.push(payload);
      if (requests.length === 1) {
        firstStarted.resolve();
        await firstGate.promise;
      } else {
        secondStarted.resolve();
        await secondGate.promise;
      }
      if (payload.expected_version !== serverVersion) {
        throw Object.assign(new Error("服务端版本冲突"), { status: 409, code: "version_conflict" });
      }
      serverChecked = payload.checked;
      serverVersion += 1;
      return { id: "race-item", checked: serverChecked, version: serverVersion };
    };

    const replay = queueContext.performMealMutations();
    await firstStarted.promise;
    assert.equal(rows[0].inFlightRevision, rows[0].revision, "replay must persist its claimed revision before sending");
    const changed = await queueContext.queueMealMutation(path, "PATCH", { checked: false, expected_version: 20 });
    assert.equal(changed.id, initial.id, "an in-flight target keeps one queue identity");
    assert.ok(changed.revision > initial.revision, "a newer semantic intent must advance the queue revision");
    assert.notEqual(changed.body.idempotency_key, initial.body.idempotency_key, "an in-flight body change needs a fresh idempotency key");

    if (firstOutcome === "success") firstGate.resolve();
    else if (firstOutcome === "4xx") firstGate.reject(Object.assign(new Error("旧请求冲突"), { status: 409, code: "version_conflict" }));
    else firstGate.reject(new Error("旧请求网络中断"));

    const replayedLatest = await Promise.race([
      secondStarted.promise.then(() => true),
      new Promise(resolve => setTimeout(() => resolve(false), 250)),
    ]);
    assert.equal(replayedLatest, true, `${firstOutcome}: a preserved newer intent must be replayed`);
    assert.equal(rows.length, 1, `${firstOutcome}: old completion must not delete the newer revision`);
    assert.equal(rows[0].revision, changed.revision, `${firstOutcome}: old completion must not overwrite revision`);
    assert.equal(rows[0].body.checked, false, `${firstOutcome}: last intent must remain in the queue`);
    assert.equal(rows[0].body.idempotency_key, changed.body.idempotency_key, `${firstOutcome}: newer idempotency must remain intact`);
    assert.equal(rows[0].status, "pending", `${firstOutcome}: an old failure must not mark a newer intent needs_attention`);
    assert.equal(rows[0].error, undefined, `${firstOutcome}: an old failure must not attach stale error details`);
    assert.equal(requests[1].body, undefined);
    assert.equal(requests[1].checked, false);
    assert.equal(requests[1].idempotency_key, changed.body.idempotency_key);
    assert.equal(requests[1].expected_version, firstOutcome === "success" ? 21 : 20, `${firstOutcome}: only a successful old response advances the expected server version`);
    secondGate.resolve();
    await replay;
    assert.equal(rows.length, 0, `${firstOutcome}: final intent should be removed only after its own success`);
    assert.equal(serverChecked, false, `${firstOutcome}: server must finish at the user's last intent`);
  }

  const raceFailures = [];
  for (const outcome of ["success", "4xx", "network"]) {
    try { await runInFlightToggleRace(outcome); }
    catch (error) { raceFailures.push(`${outcome}: ${error.message}`); }
  }
  assert.deepEqual(raceFailures, [], `in-flight revision races failed:\n${raceFailures.join("\n")}`);

  await assert.rejects(
    queueContext.queueMealMutation("/api/v1/moments", "POST", { title: "must not queue" }),
    /不能离线保存/,
  );
  assert.equal(rows.length, 0, "unsupported writes must not be persisted");

  queueContext.state.data = null;
  await assert.rejects(
    queueContext.queueMealMutation("/api/v1/messages", "POST", { type: "text", content: "no identity", idempotency_key: "identity-key" }),
    /请先登录/,
  );
  assert.equal(rows.length, 0, "missing identity must never produce pending success");

  queueContext.state.data = { user: { id: "alice" }, couple: { id: "couple-a", version: 3 }, permission_version: 3 };
  rows = [{ id: "forged", userId: "alice", partition: "alice:couple-a:3", status: "pending", createdAt: new Date().toISOString(), method: "POST", body: {}, action: "shared_confirm", path: "/api/v1/ui/actions/draft/confirm" }];
  let replayRequests = 0;
  queueContext.navigator.onLine = true;
  queueContext.api = async () => { replayRequests += 1; };
  await queueContext.performMealMutations();
  assert.equal(replayRequests, 0, "replay must not send a forged or unsupported pending record");
  assert.equal(rows[0].status, "needs_attention");
  assert.equal(rows[0].error.code, "invalid_offline_action");

  // Exercise the actual click-dispatch path: every complex online-only action
  // must be rejected before its prompt/button/storage/API branch can run.
  const clickStart = app.indexOf('document.addEventListener("click", async event => {');
  const clickGuard = app.indexOf('if (!guardOnlineUiAction(action)) return;', clickStart);
  assert.ok(clickGuard > clickStart, "the real click dispatcher must call the unified online guard");
  const firstComplexBranch = app.indexOf('if (action === "assistant-recap-edit")', clickStart);
  assert.ok(clickGuard < firstComplexBranch, "online guard must run before every complex action branch");
  const clickPrefix = app.slice(clickStart, clickGuard + 'if (!guardOnlineUiAction(action)) return;'.length) + "\n});";
  const guardedActions = [
    "assistant-recap-edit", "assistant-recap-confirm", "confirm-assistant-plan", "ai-recipe",
    "confirm-recipe-draft", "apply-revision", "generate-trip", "assistant-replan-today",
    "assistant-confirm-change", "confirm-smart-action", "build-shopping-preview", "apply-shopping-preview",
    "complete-shopping", "save-moment", "adopt-plan", "build-plan", "approve-trip", "confirm-date",
  ];
  const guardToasts = [];
  let capturedClick;
  const guardContext = vm.createContext({
    ...context,
    navigator: { onLine: false },
    toast(message) { guardToasts.push(message); },
    document: { addEventListener(name, handler) { if (name === "click") capturedClick = handler; } },
  });
  vm.runInContext(app.slice(start, end), guardContext);
  vm.runInContext(clickPrefix, guardContext);
  for (const action of guardedActions) {
    const element = { dataset: { action }, disabled: false, textContent: "原内容" };
    await capturedClick({ target: { closest(selector) { return selector === "[data-action]" ? element : null; } } });
    assert.equal(element.disabled, false, `${action} must be blocked before button mutation`);
    assert.equal(element.textContent, "原内容", `${action} must be blocked before visual mutation`);
  }
  assert.equal(guardToasts.length, guardedActions.length);
  assert.ok(guardToasts.every(message => message.includes("需联网")), "all offline complex actions use the same explicit message");

  const smartGuard = app.indexOf('if (!requireOnlineMutation("shared_confirm", "智能操作预览")) return;', clickStart);
  const smartBranch = app.indexOf('const smartAction = event.target.closest("[data-smart-action]");', clickStart);
  assert.ok(smartGuard > smartBranch, "smart action previews must guard their real handler before POSTing a draft preview");
  let capturedSmartClick;
  const smartPrefix = app.slice(clickStart, smartGuard + 'if (!requireOnlineMutation("shared_confirm", "智能操作预览")) return;'.length) + "\n}\n});";
  const smartContext = vm.createContext({
    ...context,
    navigator: { onLine: false },
    toast(message) { guardToasts.push(message); },
    document: { addEventListener(name, handler) { if (name === "click") capturedSmartClick = handler; } },
  });
  vm.runInContext(app.slice(start, end), smartContext);
  vm.runInContext(smartPrefix, smartContext);
  await capturedSmartClick({ target: { closest(selector) { return selector === "[data-smart-action]" ? { dataset: { smartAction: "draft-1" } } : null; } } });
  assert.ok(guardToasts.at(-1).includes("需联网"));

  // The real trip submit and impact-analysis forms must also guard before
  // reading fields, mutating buttons, fingerprints or calling APIs.
  for (const [formId, actionName, label] of [["dateForm", "ai_compose", "约会方案"], ["tripForm", "trip_replan", "旅行方案"], ["tripItemEditForm", "trip_replan", "行程影响分析"]]) {
    const marker = `document.getElementById("${formId}").addEventListener("submit", async event => {`;
    const formStart = app.indexOf(marker);
    const guardStatement = `if (!requireOnlineMutation("${actionName}", "${label}")) return;`;
    const formGuard = app.indexOf(guardStatement, formStart);
    assert.ok(formGuard > formStart, `${formId} must call its online guard`);
    const prefix = app.slice(formStart, formGuard + guardStatement.length) + "\n});";
    let submitHandler;
    let fieldReads = 0;
    const formContext = vm.createContext({
      navigator: { onLine: false },
      OFFLINE_QUEUE_ACTIONS: new Set(["send_text_message", "check_shopping_item"]),
      offlineActionAllowed(action, online = false) { return Boolean(online || this.OFFLINE_QUEUE_ACTIONS?.has?.(action)); },
      requireOnlineMutation(_action, guardLabel) { guardToasts.push(`${guardLabel}需联网后才能继续`); return false; },
      document: { getElementById(id) { if (id === formId) return { addEventListener(_name, handler) { submitHandler = handler; } }; fieldReads += 1; throw new Error("offline guard read a field"); } },
    });
    vm.runInContext(prefix, formContext);
    const button = { disabled: false, textContent: "提交" };
    await submitHandler({ preventDefault() {}, submitter: button, currentTarget: {} });
    assert.equal(fieldReads, 0, `${formId} must not read form fields offline`);
    assert.deepEqual(button, { disabled: false, textContent: "提交" });
  }

  // Heart feedback is committed only after sendMessage succeeds (online or a
  // durable offline queue); a failure must restore the original accessible UI.
  const heartStart = app.indexOf("async function sendHeart(");
  const heartEnd = app.indexOf("async function sendMediaMessage", heartStart);
  assert.ok(heartStart >= 0 && heartEnd > heartStart, "send-heart must use the tested sendHeart transaction helper");
  const heartButton = { textContent: "♡", disabled: false, attrs: new Map([["aria-label", "发送爱心"]]), getAttribute(name) { return this.attrs.get(name) ?? null; }, setAttribute(name, value) { this.attrs.set(name, value); }, removeAttribute(name) { this.attrs.delete(name); } };
  const heartToasts = [];
  const heartContext = vm.createContext({ toast(message) { heartToasts.push(message); }, sendMessage: async () => { throw new Error("保存失败"); } });
  vm.runInContext(app.slice(heartStart, heartEnd), heartContext);
  await heartContext.sendHeart(heartButton);
  assert.equal(heartButton.textContent, "♡");
  assert.equal(heartButton.disabled, false);
  assert.equal(heartButton.getAttribute("aria-label"), "发送爱心");
  heartContext.sendMessage = async () => ({ status: "queued" });
  await heartContext.sendHeart(heartButton);
  assert.equal(heartButton.textContent, "♥");
  assert.equal(heartButton.getAttribute("aria-label"), "爱心已离线保存");

  process.stdout.write(JSON.stringify({ ok: true, checks: ["shortcut once after auth", "conflicting shortcuts consumed together", "offline admission and replay allowlist", "shopping last intent coalescing", "in-flight replay revision races", "pending only after durable queue", "actual online-only handler guards", "send-heart commit UI"] }) + "\n");
})().catch(error => {
  console.error(error.stack || error.message);
  process.exitCode = 1;
});
