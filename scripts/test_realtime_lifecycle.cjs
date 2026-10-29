/* Public regression suite: synthetic accounts only; no private deployment access. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const app = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const start = app.indexOf("let realtimeGeneration = 0;");
const end = app.indexOf("function showAccountAuth", start);
const apiStart = app.indexOf("async function api(");
const apiEnd = app.indexOf("function tripCreationRequest", apiStart);
assert(start >= 0 && end > start, "Realtime lifecycle implementation not found");
assert(apiStart >= 0 && apiEnd > apiStart, "Real API implementation not found");

function fixture({ realApi = false } = {}) {
  const requests = [];
  const sources = [];
  const timers = new Map();
  const presence = { textContent: "initial" };
  let nextTimer = 0;
  let refreshes = 0;
  let cleanups = 0;
  let authPrompts = 0;
  const state = { data: { user: { id: "A" } }, cooking: {}, realtime: null, realtimeCursor: 0, online: false };
  const context = vm.createContext({
    state, AbortController,
    localStorage: { getItem: () => "A:couple:1" },
    document: { hidden: false, querySelector: () => presence },
    api: (url, options) => new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })),
    fetch: (url, options) => new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })),
    closeCookingScreen() {}, resetTripLive() {}, resetMemoryDiscovery() {},
    showAccountAuth() { authPrompts += 1; },
    async clearMealOfflineData() { cleanups += 1; context.stopRealtime(); state.data = null; },
    EventSource: class {
      constructor(url) { this.url = url; this.handlers = new Map(); this.closed = false; sources.push(this); }
      addEventListener(name, handler) { this.handlers.set(name, handler); }
      close() { this.closed = true; }
      emit(name, data = {}) { this.handlers.get(name)?.({ data: JSON.stringify(data) }); }
    },
    setTimeout: callback => { const id = ++nextTimer; timers.set(id, callback); return id; },
    clearTimeout: id => timers.delete(id),
    loadBootstrap: () => { refreshes += 1; },
  });
  if (realApi) vm.runInContext(app.slice(apiStart, apiEnd), context, { filename: "actual-api.js" });
  vm.runInContext(app.slice(start, end), context, { filename: "realtime-lifecycle.js" });
  return { context, state, requests, sources, timers, presence,
    get refreshes() { return refreshes; }, get cleanups() { return cleanups; }, get authPrompts() { return authPrompts; } };
}

(async () => {
  const checks = [];
  {
    const test = fixture();
    test.state.data = null;
    await test.context.startRealtime();
    assert.equal(test.requests.length, 0);
    checks.push("unauthenticated state does not start a connection");
  }
  {
    const test = fixture();
    const first = test.context.startRealtime();
    const second = test.context.startRealtime();
    assert.equal(test.requests.length, 1, "parallel bootstraps must share the pending snapshot");
    test.requests[0].resolve({ latest_sequence: 7 });
    await Promise.all([first, second]);
    assert.equal(test.sources.length, 1);
    assert.match(test.sources[0].url, /after=7$/);
    await test.context.startRealtime();
    assert.equal(test.requests.length, 1, "an existing connection must not spawn another");
    checks.push("concurrent starts create one snapshot and one EventSource");
  }
  {
    const test = fixture();
    const pending = test.context.startRealtime();
    test.context.stopRealtime();
    test.state.data = null;
    assert.equal(test.requests[0].options.signal.aborted, true, "logout must abort the pending fetch");
    // Even a transport that delivers despite abort must not resurrect the session.
    test.requests[0].resolve({ latest_sequence: 11 });
    await pending;
    assert.equal(test.sources.length, 0);
    assert.equal(test.state.realtime, null);
    assert.equal(test.state.realtimeCursor, 0);
    checks.push("late snapshot cannot reopen a logged-out session");
  }
  {
    const test = fixture();
    const firstA = test.context.startRealtime();
    test.context.stopRealtime();
    test.state.data = { user: { id: "B" } };
    const b = test.context.startRealtime();
    test.context.stopRealtime();
    test.state.data = { user: { id: "A" } };
    const currentA = test.context.startRealtime();
    test.requests[0].resolve({ latest_sequence: 99 });
    test.requests[1].resolve({ latest_sequence: 88 });
    await Promise.all([firstA, b]);
    const concurrentA = test.context.startRealtime();
    assert.equal(test.requests.length, 3, "old finalizers must not clear the new account's in-flight guard");
    assert.equal(test.sources.length, 0);
    test.requests[2].resolve({ latest_sequence: 5 });
    await Promise.all([currentA, concurrentA]);
    assert.equal(test.sources.length, 1);
    assert.equal(test.state.realtimeCursor, 5);
    checks.push("A-to-B-to-A session generations reject old results");
  }
  {
    const test = fixture();
    const pending = test.context.startRealtime();
    test.state.data = { user: { id: "B" } };
    test.requests[0].resolve({ latest_sequence: 4 });
    await pending;
    assert.equal(test.sources.length, 0, "a changed user must invalidate a result even without an explicit stop");
    checks.push("snapshot belongs to the initiating user");
  }
  {
    const test = fixture();
    const pending = test.context.startRealtime();
    test.requests[0].resolve({ latest_sequence: 3 });
    await pending;
    const source = test.sources[0];
    source.emit("open");
    assert.equal(test.state.online, true);
    source.emit("message.created", { sequence: 4, event_type: "message.created", actor_id: "B" });
    const queuedRefresh = [...test.timers.values()][0];
    assert.equal(typeof queuedRefresh, "function");
    test.context.stopRealtime();
    test.state.online = false;
    const priorPresence = test.presence.textContent;
    source.emit("open");
    source.emit("error");
    source.emit("message.created", { sequence: 20, event_type: "message.created", actor_id: "B" });
    queuedRefresh();
    assert.equal(source.closed, true);
    assert.equal(test.state.online, false);
    assert.equal(test.presence.textContent, priorPresence);
    assert.equal(test.state.realtimeCursor, 4);
    assert.equal(test.timers.size, 0);
    assert.equal(test.refreshes, 0);
    checks.push("closed sources and already queued callbacks cannot mutate current state");
  }
  {
    const test = fixture();
    const denied = test.context.startRealtime();
    test.requests[0].reject(Object.assign(new Error("Unauthorized"), { status: 401 }));
    await assert.rejects(denied, error => error.status === 401, "real authentication failures remain visible");
    const retry = test.context.startRealtime();
    assert.equal(test.requests.length, 2, "failed startup must allow a future retry");
    test.requests[1].resolve({ latest_sequence: 2 });
    await retry;
    assert.equal(test.sources.length, 1);
    checks.push("real 401 propagates and a failed start can be retried");
  }
  {
    const test = fixture();
    const cancelled = test.context.startRealtime();
    test.context.stopRealtime();
    test.requests[0].reject(Object.assign(new Error("cancelled"), { name: "AbortError" }));
    await cancelled;
    assert.equal(test.sources.length, 0);
    checks.push("intentional fetch cancellation is not reported as an application error");
  }
  {
    const test = fixture({ realApi: true });
    const denied = test.context.startRealtime();
    test.requests[0].resolve({ ok: false, status: 401, json: async () => ({ error: "请先登录" }) });
    await assert.rejects(denied, error => error.status === 401);
    assert.equal(test.cleanups, 1);
    assert.equal(test.authPrompts, 1);
    assert.equal(test.requests[0].options.signal.aborted, true, "real 401 cleanup aborts the initializing signal");
    assert.equal(test.sources.length, 0);
    checks.push("real API 401 still rejects after its own session cleanup aborts the signal");
  }
  for (const cancelledBodyRejects of [true, false]) {
    const test = fixture({ realApi: true });
    let releaseBody;
    let failBody;
    let bodyStarted;
    const started = new Promise(resolve => { bodyStarted = resolve; });
    const oldA = test.context.startRealtime();
    test.requests[0].resolve({ ok: false, status: 401, json: () => {
      bodyStarted();
      return new Promise((resolve, reject) => { releaseBody = resolve; failBody = reject; });
    } });
    await started;
    test.context.stopRealtime();
    // The same user's new session has the same storage partition. The signal,
    // not just the partition string, must reject the cancelled old 401 body.
    test.state.data = { user: { id: "A" }, newSession: true };
    const currentA = test.context.startRealtime();
    if (cancelledBodyRejects) failBody(Object.assign(new Error("body cancelled"), { name: "AbortError" }));
    else releaseBody({ error: "请先登录" });
    await oldA;
    assert.equal(test.cleanups, 0, "old 401 must not clear the new session");
    assert.equal(test.authPrompts, 0);
    assert.equal(test.state.data.newSession, true);
    assert.equal(test.requests[1].options.signal.aborted, false);
    test.requests[1].resolve({ ok: true, status: 200, json: async () => ({ latest_sequence: 6 }) });
    await currentA;
    assert.equal(test.sources.length, 1);
    checks.push(cancelledBodyRejects
      ? "real API preserves body AbortError across same-user re-login"
      : "real API rejects late 401 JSON even when the transport ignores abort");
  }
  {
    const test = fixture({ realApi: true });
    const old = test.context.startRealtime();
    test.context.stopRealtime();
    let readBody = false;
    test.requests[0].resolve({ ok: false, status: 401, json: async () => { readBody = true; return {}; } });
    await old;
    assert.equal(readBody, false, "cancelled requests must stop immediately after late response headers");
    assert.equal(test.cleanups, 0);
    checks.push("real API checks cancellation before reading response bodies");
  }
  const smartStart = app.indexOf("async function loadSmartSurface(");
  const smartEnd = app.indexOf("async function composeSmartSurface(", smartStart);
  assert(smartStart >= 0 && smartEnd > smartStart);
  for (const delayed of [false, true]) {
    let protectedDraft = !delayed;
    let renders = 0;
    let resolveManifest;
    const context = vm.createContext({
      smartExamples: { home: "example" },
      state: { data: null, manifests: {}, composingSmartSurfaces: new Set(), pinnedSmartSurfaces: new Set() },
      surfaceHasDraftIntent: () => protectedDraft,
      manifestHasPendingWrite: () => false,
      document: { querySelector: () => ({ existingInput: true }) },
      renderSmartSurface: () => { renders += 1; },
      smartCacheKey: () => "fixture",
      readLocalJson: () => null,
      validManifest: () => false,
      api: () => new Promise(resolve => { resolveManifest = resolve; }),
      saveManifestCache() {},
    });
    vm.runInContext(app.slice(smartStart, smartEnd), context);
    const pending = context.loadSmartSurface("home", { quiet: true });
    if (delayed) { protectedDraft = true; resolveManifest({ sections: [] }); }
    await pending;
    assert.equal(renders, 0, "quiet refresh must preserve the existing input DOM");
    checks.push(delayed ? "late background results preserve typed input" : "existing drafts preserve focus and selection");
  }
  console.log(JSON.stringify({ ok: true, checks }, null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; });
