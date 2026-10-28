const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const app = fs.readFileSync("static/app.js", "utf8");
const h5ValidatorSource = app.slice(
  app.indexOf("function validateTripOfflineSnapshot("),
  app.indexOf("function readTripOfflineSnapshot("),
);
const identity = { user_id: "alice", couple_id: "couple-a", permission_version: 7 };
const generated = "2026-10-28T00:00:00.000Z";
const expires = "2026-10-29T00:00:00.000Z";
const fixture = {
  schema_version: 1,
  identity,
  trip: { id: "trip-a", title: "旅行", destination: "上海", start_date: "2026-11-01", end_date: "2026-11-02", currency: "CNY", confirmed_revision: 2, version: 4 },
  members: [], itinerary: [], packing: { items: [] }, emergency: { items: [] },
  generated_at: generated, expires_at: expires,
};

const h5 = vm.createContext({ state: { data: { user: { id: "alice" }, couple: { id: "couple-a", version: 7 }, permission_version: 7, trip: { id: "trip-a" } } }, Date, Number, Array });
vm.runInContext(h5ValidatorSource, h5);
assert.ok(h5.validateTripOfflineSnapshot(structuredClone(fixture), "trip-a", Date.parse(generated) + 1));
for (const invalid of [
  { ...fixture, identity: { ...identity, user_id: "bob" } },
  { ...fixture, identity: { ...identity, couple_id: "couple-b" } },
  { ...fixture, identity: { ...identity, permission_version: 8 } },
  { ...fixture, trip: { ...fixture.trip, id: "trip-b" } },
  { ...fixture, expires_at: generated },
  { ...fixture, expires_at: "invalid" },
  { ...fixture, generated_at: "2026-10-30T00:00:00.000Z", expires_at: "2026-10-31T00:00:00.000Z" },
  { ...fixture, packing: { items: "not-an-array" } },
  { ...fixture, emergency: { items: null } },
]) assert.equal(h5.validateTripOfflineSnapshot(structuredClone(invalid), "trip-a", Date.parse(generated) + 1), null);
assert.equal(h5.validateTripOfflineSnapshot(structuredClone(fixture), "trip-a", Date.parse(expires)), null, "expired H5 cache must fail closed");

const ts = require("../packages/domain-client/node_modules/typescript");
const compiled = ts.transpileModule(fs.readFileSync("packages/domain-client/src/index.ts", "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
const exportsObject = {};
vm.runInNewContext(compiled, { exports: exportsObject, require: () => ({ uiManifestSchema: { parse: value => value } }), Map, JSON, Date, Number, Array, encodeURIComponent, fetch: async () => { throw new Error("unused"); } });
assert.ok(exportsObject.validateTripOfflineSnapshot(structuredClone(fixture), identity, "trip-a", Date.parse(generated) + 1));
assert.equal(exportsObject.validateTripOfflineSnapshot(structuredClone({ ...fixture, identity: { ...identity, permission_version: 8 } }), identity, "trip-a", Date.parse(generated) + 1), null);
assert.equal(exportsObject.validateTripOfflineSnapshot(structuredClone({ ...fixture, generated_at: "2026-10-30T00:00:00.000Z", expires_at: "2026-10-31T00:00:00.000Z" }), identity, "trip-a", Date.parse(generated) + 1), null);

const offlineSpecSource = app.slice(
  app.indexOf("function offlineMutationSpec("),
  app.indexOf("function offlineQueueEntryIsValid("),
);
const offlinePolicy = vm.createContext({ Set, Object, String, Array, Boolean, Number, RegExp });
vm.runInContext(offlineSpecSource, offlinePolicy);
assert.equal(offlinePolicy.offlineMutationSpec("/api/v1/trips/trip-a/packing/item-a", "PATCH", { checked: true, expected_version: 1 }), null, "packing writes must not be queueable");
assert.equal(offlinePolicy.offlineMutationSpec("/api/v1/trips/trip-a/emergency", "POST", { title: "x" }), null, "emergency writes must not be queueable");
assert.equal(offlinePolicy.offlineMutationSpec("/api/v1/messages", "POST", { type: "text", content: "hello", idempotency_key: "message-0001" }).action, "send_text_message");

const values = new Map();
const storage = {
  async getItem(key) { return values.get(key) || null; },
  async setItem(key, value) { values.set(key, value); },
  async getAllKeys() { return [...values.keys()]; },
  async multiRemove(keys) { keys.forEach(key => values.delete(key)); },
};
const cache = exportsObject.createPrivateCache(storage);
(async () => {
  await cache.setIdentity(identity);
  await cache.setItem("trip-offline:trip-a", JSON.stringify(fixture));
  assert.ok(await cache.getItem("trip-offline:trip-a"));
  await cache.removeItem("trip-offline:trip-a");
  assert.equal(await cache.getItem("trip-offline:trip-a"), null, "authoritative denial must be able to evict one trip snapshot");
  await cache.setItem("trip-offline:trip-a", JSON.stringify(fixture));
  await cache.setIdentity({ ...identity, permission_version: 8 });
  assert.equal(await cache.getItem("trip-offline:trip-a"), null, "permission change must not adopt an old partition");

  let releaseRead;
  const delayedValues = new Map([["love-private:alice:couple-a:7:trip-offline:trip-a", JSON.stringify(fixture)]]);
  const delayedStorage = {
    async getItem(key) { return new Promise(resolve => { releaseRead = () => resolve(delayedValues.get(key) || null); }); },
    async setItem(key, value) { delayedValues.set(key, value); },
    async getAllKeys() { return [...delayedValues.keys()]; },
    async multiRemove(keys) { keys.forEach(key => delayedValues.delete(key)); },
  };
  const delayedCache = exportsObject.createPrivateCache(delayedStorage);
  await delayedCache.setIdentity(identity);
  const staleRead = delayedCache.getItem("trip-offline:trip-a");
  await delayedCache.setIdentity({ user_id: "bob", couple_id: "couple-b", permission_version: 1 });
  releaseRead();
  assert.equal(await staleRead, null, "late reads from the previous identity must be discarded");
  const worker = fs.readFileSync("sw.js", "utf8");
  assert.doesNotMatch(worker, /offline-snapshot/, "private trip snapshots must not enter the service worker cache");
})().catch(error => { console.error(error); process.exitCode = 1; });
