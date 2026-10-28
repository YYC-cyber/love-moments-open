/* Deterministic service-worker notification and click-routing audit. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const listeners = {};
const notifications = [];
const navigation = [];
const badgeChanges = [];
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
  fetch: async () => { throw new Error("not used"); },
  caches: {},
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
