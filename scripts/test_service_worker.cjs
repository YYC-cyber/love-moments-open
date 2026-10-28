/* Deterministic service-worker notification and click-routing audit. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const listeners = {};
const notifications = [];
const navigation = [];
const badgeChanges = [];
const cacheEntries = new Map([
  ["/", { source: "cached-shell" }],
  ["/static/app.js", { source: "cached-app" }],
]);
const cachePuts = [];
let fetchMode = "fail";
let focused = false;
const existingClient = {
  url: "https://love.example/?screen=home",
  async navigate(url) { navigation.push(url); this.url = url; return this; },
  async focus() { focused = true; return this; }
};
const context = {
  URL,
  Promise,
  Set,
  fetch: async request => {
    if (fetchMode === "fail") throw new Error("offline");
    return {
      ok: true,
      headers: { get: () => fetchMode === "no-store" ? "private, no-store" : "public, max-age=60" },
      clone() { return this; },
      request,
    };
  },
  caches: {
    async open() {
      return {
        async addAll() {},
        async put(key, value) {
          const cacheKey = typeof key === "string" ? key : `${new URL(key.url).pathname}${new URL(key.url).search}`;
          cachePuts.push(cacheKey);
        },
      };
    },
    async match(key) {
      const cacheKey = typeof key === "string" ? key : `${new URL(key.url).pathname}${new URL(key.url).search}`;
      return cacheEntries.get(cacheKey);
    },
    async keys() { return ["older-love-cache"]; },
    async delete() { return true; },
  },
  self: {
    location: { origin: "https://love.example" },
    registration: {
      async showNotification(title, options) { notifications.push({ title, options }); },
      async setAppBadge(value) { badgeChanges.push(value); },
      async clearAppBadge() { badgeChanges.push(0); }
    },
    clients: {
      async matchAll() { return [existingClient]; },
      async openWindow(url) { navigation.push(url); return { url }; }
    },
    addEventListener(name, handler) { listeners[name] = handler; },
    skipWaiting() {}
  }
};
context.self.clients.claim = async () => {};

const source = fs.readFileSync(path.join(__dirname, "..", "sw.js"), "utf8");
vm.runInNewContext(source, context, { filename: "sw.js" });
for (const name of ["install", "activate", "fetch", "push", "notificationclick"]) {
  assert.equal(typeof listeners[name], "function", `missing ${name} listener`);
}

// Offline shortcut URLs are documents for the public shell. Private APIs,
// media and mutations remain completely outside the worker fetch pipeline.
let shortcutResponse;
listeners.fetch({
  request: { url: "https://love.example/?action=moment", method: "GET", mode: "navigate" },
  respondWith(value) { shortcutResponse = value; },
});
Promise.resolve(shortcutResponse).then(response => {
  assert.deepEqual(response, { source: "cached-shell" });
});
for (const request of [
  { url: "https://love.example/api/v1/bootstrap", method: "GET" },
  { url: "https://love.example/api/v1/messages", method: "POST" },
  { url: "https://love.example/api/v1/media/private/content", method: "GET" },
  { url: "https://other.example/static/app.js", method: "GET" },
]) listeners.fetch({ request, respondWith() { throw new Error(`${request.url} must bypass the worker`); } });

// Query variants must update only the canonical public shell key. Static
// failures must not receive root HTML as a misleading fallback.
fetchMode = "cacheable";
let onlineShortcut;
listeners.fetch({
  request: { url: "https://love.example/?screen=chat", method: "GET", mode: "navigate" },
  respondWith(value) { onlineShortcut = value; },
});
Promise.resolve(onlineShortcut).then(async () => {
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(cachePuts.includes("/"));
  assert.equal(cachePuts.includes("/?screen=chat"), false);

  let onlineStatic;
  listeners.fetch({
    request: { url: "https://love.example/static/app.js?v=20261027", method: "GET" },
    respondWith(value) { onlineStatic = value; },
  });
  await onlineStatic;
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(cachePuts.includes("/static/app.js"), "static query variants must update the canonical pathname key");
  assert.equal(cachePuts.includes("/static/app.js?v=20261027"), false, "query variants must not create duplicate cache entries");

  fetchMode = "fail";
  let cachedStatic;
  listeners.fetch({
    request: { url: "https://love.example/static/app.js?v=offline", method: "GET" },
    respondWith(value) { cachedStatic = value; },
  });
  assert.deepEqual(await cachedStatic, { source: "cached-app" }, "offline query variants must hit the bare-path cache entry");

  let staticResponse;
  listeners.fetch({
    request: { url: "https://love.example/static/missing.js", method: "GET" },
    respondWith(value) { staticResponse = value; },
  });
  assert.equal(staticResponse, undefined, "non-allowlisted static requests must bypass the worker");
});

let pushWork;
listeners.push({
  data: { json: () => ({
    type: "incoming_call",
    title: "恋屿语音来电",
    body: "对方正在呼叫你，点击进入恋屿接听。",
    url: "/?screen=chat&call=call-1",
    tag: "call-call-1",
    call_id: "call-1"
  }) },
  waitUntil(promise) { pushWork = promise; }
});
Promise.resolve(pushWork).then(async () => {
  assert.equal(notifications.length, 1);
  assert.equal(notifications[0].title, "恋屿语音来电");
  assert.equal(notifications[0].options.requireInteraction, true);
  assert.equal(notifications[0].options.data.call_id, "call-1");
  assert.equal(notifications[0].options.icon, "/static/assets/love-moments-icon-192.png");
  assert.deepEqual(badgeChanges, [1]);

  let commentPushWork;
  listeners.push({
    data: { json: () => ({
      type: "shared_comment",
      title: "恋屿有一条新的共同留言",
      body: "对方在共同内容中留下了评论，点击查看。",
      url: "/?screen=life&shared_type=wishlist_item&shared_id=wish_12345678",
      tag: "comment-comment_12345678"
    }) },
    waitUntil(promise) { commentPushWork = promise; }
  });
  await commentPushWork;
  assert.equal(notifications.length, 2);
  assert.equal(notifications[1].options.requireInteraction, false);
  assert.equal(notifications[1].options.renotify, false);
  assert.equal(notifications[1].options.data.type, "shared_comment");
  assert.deepEqual(badgeChanges, [1, 1]);

  let clickWork;
  listeners.notificationclick({
    notification: { data: { url: "/?screen=chat&call=call-1" }, close() {} },
    waitUntil(promise) { clickWork = promise; }
  });
  await clickWork;
  assert.equal(navigation[0], "https://love.example/?screen=chat&call=call-1");
  assert.equal(focused, true);
  assert.deepEqual(badgeChanges, [1, 1, 0]);

  let commentClickWork;
  listeners.notificationclick({
    notification: { data: { url: "/?screen=life&shared_type=wishlist_item&shared_id=wish_12345678" }, close() {} },
    waitUntil(promise) { commentClickWork = promise; }
  });
  await commentClickWork;
  assert.equal(navigation[1], "https://love.example/?screen=life&shared_type=wishlist_item&shared_id=wish_12345678");

  let unsafeWork;
  listeners.notificationclick({
    notification: { data: { url: "https://evil.example/steal" }, close() {} },
    waitUntil(promise) { unsafeWork = promise; }
  });
  await unsafeWork;
  assert.equal(navigation[2], "https://love.example/?screen=chat");
  process.stdout.write(JSON.stringify({ ok: true, checks: ["call and shared notification", "app badge", "same-origin detail click routing", "unsafe URL fallback"] }) + "\n");
}).catch(error => {
  console.error(error.stack || error.message);
  process.exitCode = 1;
});
