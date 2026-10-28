const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("static/app.js", "utf8");
const policySource = app.slice(app.indexOf("const PWA_SHORTCUT_INTENTS"), app.indexOf("const initialPwaShortcutIntent"));
const source = app.slice(app.indexOf("function smartCacheKey("), app.indexOf("function renderSmartSurface("));
const values = new Map();
const localStorage = { getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key), key: i => [...values.keys()][i], get length() { return values.size; } };
const scope = { user: { id: "alice" }, couple: { id: "couple-a", version: 3 }, permission_version: 3 };
let rows = [];
function db() {
  return { close() {}, transaction() {
    const tx = { objectStore(name) { return {
      getAll() { const request = {}; queueMicrotask(() => { request.result = structuredClone(rows); request.onsuccess?.(); queueMicrotask(() => tx.oncomplete?.()); }); return request; },
      get(key) { const request = {}; queueMicrotask(() => { request.result = name === "pending" ? structuredClone(rows.find(row => row.id === key)) : values.get(key); request.onsuccess?.(); queueMicrotask(() => tx.oncomplete?.()); }); return request; },
      getAllKeys() { const request = {}; queueMicrotask(() => { request.result = [...values.keys()]; request.onsuccess?.(); queueMicrotask(() => tx.oncomplete?.()); }); return request; },
      delete(id) { rows = rows.filter(row => row.id !== id); values.delete(id); queueMicrotask(() => tx.oncomplete?.()); },
      put(value, key) { if (name === "pending") { rows = rows.filter(row => row.id !== value.id); rows.push(structuredClone(value)); } else values.set(key, value); queueMicrotask(() => tx.oncomplete?.()); }
    }; } }; return tx;
  } };
}
const context = vm.createContext({ console, AUTH_SYNC_KEY: "love-auth-sync", state: { data: scope, smartIntentDrafts: {}, meals: {}, destinationSearchCache: new Map(), decisionSelections: new Map([["decision-old", new Set(["private-option"])]]), datePlaceCandidates: [{ candidate_id: "old-account-place" }], datePlaceCandidateId: "old-account-place", datePlaceSearchSequence: 1 }, localStorage, document: { querySelectorAll: () => [], querySelector: () => null, createElement: tagName => ({ tagName: String(tagName).toUpperCase(), dataset: {}, prepend() {} }) }, navigator: { onLine: true }, window: {}, clientKey: () => "stable-original-key", Date, JSON, Promise, Set, Object, encodeURIComponent, decodeURIComponent, structuredClone, toast() {}, stopRealtime() {}, resetTripLive() {} });
vm.runInContext(policySource, context);
vm.runInContext(source, context);
context.openMealCache = async () => db();
(async () => {
  if (process.argv[2] === "recipe_denial") {
    const detail = { innerHTML: "previous private recipe" };
    const modal = { classList: { contains: () => true } };
    let opened = 0;
    context.document.getElementById = id => id === "recipeDetailContent" ? detail : modal;
    Object.assign(context, { exactRecipeCover: () => null, recipeCoverMarkup: () => "", escapeHtml: value => String(value), renderRecipeIngredients: () => "", renderRecipeAllergens: () => "", renderCoupleFoodSafety: () => "", renderRecipeVideos: () => "", openModal: () => opened++, closeModals() {} });
    vm.runInContext(app.slice(app.indexOf("async function openRecipeDetail("), app.indexOf("function readableRecipeQuantity(")), context);
    const recipe = { id: "recipe-private", name: "private recipe", owner_id: "alice", source_type: "user", base_servings: 1, prep_minutes: 0, cook_minutes: 1, steps: [], variants: [] };
    const cacheKey = context.mealUserCacheKey("recipe:recipe-private");
    for (const status of [403, 404]) {
      values.set(cacheKey, recipe); context.state.meals.openRecipe = recipe; detail.innerHTML = "previous private recipe";
      context.api = async () => { throw Object.assign(new Error("authoritatively denied"), { status }); };
      await assert.rejects(context.openRecipeDetail("recipe-private"), error => error.status === status, `HTTP ${status} must never restore the cached recipe`);
      assert.equal(values.has(cacheKey), false, "denied private cache must be evicted");
      assert.equal(context.state.meals.openRecipe, null);
      assert.equal(detail.innerHTML, "");
      assert.equal(opened, 0, "denied details must never open");
      context.api = async () => { throw new Error("offline"); };
      await assert.rejects(context.openRecipeDetail("recipe-private"), /offline/, "offline retry must not resurrect a denied recipe");
    }
    values.set(cacheKey, recipe);
    await context.openRecipeDetail("recipe-private");
    assert.equal(opened, 1, "a genuine network outage still permits a valid cached detail");
  } else if (process.argv[2] === "worker") {
    const handlers = {};
    const puts = [];
    vm.runInNewContext(fs.readFileSync("sw.js", "utf8"), { URL, Set, Promise, caches: { async open() { return { put(request) { puts.push(request.url); } }; } }, fetch: async () => ({ ok: true, headers: { get: () => "no-store" }, clone() { return this; } }), self: { location: { origin: "https://love.test" }, addEventListener(name, fn) { handlers[name] = fn; } } });
    let work;
    handlers.fetch({ request: { url: "https://love.test/api/v1/media/secret", method: "GET" }, respondWith() { throw new Error("private API must bypass cache"); } });
    handlers.fetch({ request: { url: "https://love.test/static/app.js", method: "GET" }, respondWith(value) { work = value; } });
    await work;
    assert.deepEqual(puts, [], "no-store responses must never enter worker cache");
  } else if (process.argv[2] === "queue") {
    for (const path of ["/api/v1/shopping-lists/items/item-1", "/api/v1/messages"]) for (const status of [400, 403, 404, 409]) {
      rows = [];
      await context.queueMealMutation(path, path.endsWith("messages") ? "POST" : "PATCH", path.endsWith("messages") ? { type: "text", content: "offline draft" } : { checked: true, expected_version: 1 });
      const original = structuredClone(rows[0]);
      let attempts = 0;
      context.api = async (path, options) => { attempts++; assert.equal(JSON.parse(options.body).idempotency_key, original.body.idempotency_key); throw Object.assign(new Error("needs repair"), { status }); };
      await context.flushMealMutations();
      assert.equal(rows.length, 1, `HTTP ${status} must retain queued item`);
      assert.equal(rows[0].status, "needs_attention");
      assert.equal(rows[0].error.status, status);
      assert.equal(rows[0].body.idempotency_key, "stable-original-key");
      await context.flushMealMutations();
      assert.equal(attempts, 1, "attention entries must not be retried automatically");
    }
    rows = [];
    await context.queueMealMutation("/api/v1/messages", "POST", { type: "text", content: "keep private" });
    context.state.data = { ...scope, couple: { id: "couple-b", version: 3 } };
    context.api = async () => { throw new Error("another couple's queue must not run"); };
    await context.flushMealMutations();
    assert.equal(rows[0].status, "pending");
    context.state.data = scope;
    context.api = async () => { rows = []; context.state.data = null; throw Object.assign(new Error("logged out"), { status: 403 }); };
    await context.flushMealMutations();
    assert.equal(rows.length, 0, "a failed in-flight request must not recreate a queue cleared during logout");
  } else if (process.argv[2] === "mobile") {
    const mobileSource = fs.readFileSync("clients/mobile/App.tsx", "utf8");
    assert.match(
      mobileSource,
      /async function loadManifestForSurface[\s\S]*?requestEpoch !== manifestEpoch\.current[\s\S]*?accountEpoch !== authEpoch\.current[\s\S]*?setManifest/,
      "an older surface/account manifest response must not replace the current mobile manifest",
    );
    assert.doesNotMatch(
      mobileSource,
      /getManifest\(surface\)\.then\(setManifest\)/,
      "mobile must not install an unguarded in-flight manifest",
    );
    const ts = require("../packages/domain-client/node_modules/typescript");
    const compiled = ts.transpileModule(fs.readFileSync("packages/domain-client/src/index.ts", "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
    const exports = {};
    let fetchImpl = async () => { throw new Error("offline"); };
    vm.runInNewContext(compiled, { exports, require: () => ({ uiManifestSchema: { parse: value => value } }), Map, JSON, encodeURIComponent, fetch: (...args) => fetchImpl(...args) });
    const storage = { async getItem(key) { return values.get(key) || null; }, async setItem(key, value) { values.set(key, value); }, async getAllKeys() { return [...values.keys()]; }, async multiRemove(keys) { keys.forEach(key => values.delete(key)); } };
    const cache = exports.createPrivateCache(storage);
    await cache.setIdentity({ user_id: "alice", couple_id: "couple-a", permission_version: 3 });
    await cache.setItem("bootstrap", "secret");
    await cache.setItem("ui:home", "manifest");
    assert.ok([...values.keys()].some(key => key.includes("alice:couple-a:3")));
    await cache.setIdentity({ user_id: "alice", couple_id: "couple-b", permission_version: 3 });
    assert.equal(await cache.getItem("bootstrap"), null);
    assert.equal(values.size, 0, "identity changes must clear all previous private data");
    await cache.setItem("meals", "private");
    await cache.setIdentity({ user_id: "bob", couple_id: "couple-b", permission_version: 3 });
    assert.equal(await cache.getItem("meals"), null, "another user must never reuse the previous user's cached snapshot");
    await cache.setItem("meals", "bob private");
    let releaseRead;
    storage.getItem = () => new Promise(resolve => { releaseRead = resolve; });
    const oldRead = cache.getItem("meals");
    await cache.setIdentity(null);
    releaseRead("bob private");
    assert.equal(await oldRead, null, "an in-flight private read cannot escape after logout");
    assert.equal(values.size, 0, "logout must clear meal snapshots too");
    assert.equal(await cache.getItem("meals"), null);
    let releaseResponse;
    fetchImpl = async () => ({ ok: true, json: () => new Promise(resolve => { releaseResponse = resolve; }) });
    const client = new exports.LoveMomentsClient();
    client.setAccessToken("alice-token");
    const request = client.messages();
    await new Promise(resolve => setImmediate(resolve));
    client.setAccessToken("bob-token");
    releaseResponse({ items: [{ content: "alice secret" }] });
    await assert.rejects(request, error => error.status === 409, "old-account HTTP responses must be discarded");
  } else {
    const first = context.smartCacheKey("home");
    context.state.data = { ...scope, couple: { id: "couple-b", version: 3 } };
    assert.notEqual(context.smartCacheKey("home"), first, "couple must partition manifests");
    context.state.data = { ...scope, permission_version: 4 };
    assert.notEqual(context.smartCacheKey("home"), first, "permission version must partition manifests");
    context.state.data = scope;
    localStorage.setItem(first, "private cached content");
    localStorage.setItem(context.mealUserCacheKey("recipe:private"), "private recipe");
    await context.queueMealMutation("/api/v1/shopping-lists/items/item-1", "PATCH", { checked: true, expected_version: 1 });
    await context.clearMealOfflineData("alice");
    assert.equal(localStorage.getItem(first), null, "auth boundary must clear manifests");
    assert.equal(rows.length, 0, "auth boundary must clear queued writes");
    assert.equal(context.state.decisionSelections.size, 0, "auth boundary must clear private decision choices");
    assert.equal(context.state.datePlaceCandidates.length, 0, "auth boundary must clear old-account place candidates");
    assert.equal(context.state.datePlaceCandidateId, null, "auth boundary must clear the selected old-account place");
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
