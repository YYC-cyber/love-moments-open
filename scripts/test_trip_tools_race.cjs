const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const ts = require("../packages/domain-client/node_modules/typescript");
const source = fs.readFileSync("packages/domain-client/src/index.ts", "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const exportsObject = {};
vm.runInNewContext(compiled, {
  exports: exportsObject,
  require: () => ({ uiManifestSchema: { parse: value => value } }),
  Map, JSON, Date, Number, Array, encodeURIComponent,
  fetch: async () => { throw new Error("unused"); },
});

const guard = exportsObject.createTripToolsInFlightGuard();
const alice = { authEpoch: 1, identity: "alice:couple-a:7", tripId: "trip-a" };
const bob = { authEpoch: 2, identity: "bob:couple-b:1", tripId: "trip-b" };
const staleAlice = guard.begin(alice);
let releaseAlice;
const lateAliceResponse = new Promise(resolve => { releaseAlice = resolve; });
guard.invalidate();
const currentBob = guard.begin(bob);
releaseAlice({ items: ["alice-private-item"] });

(async () => {
  const staleValue = await lateAliceResponse;
  let committed = null;
  if (guard.isCurrent(staleAlice, bob)) committed = staleValue;
  assert.equal(committed, null, "a late response from the cleared account must not commit");
  assert.equal(guard.isCurrent(currentBob, bob), true, "the current account request should remain valid");

  const oldTrip = guard.begin(bob);
  const nextTripScope = { ...bob, tripId: "trip-c" };
  guard.begin(nextTripScope);
  assert.equal(guard.isCurrent(oldTrip, nextTripScope), false, "switching trips must invalidate the old trip response");

  const oldPermission = guard.begin(nextTripScope);
  const changedPermission = { ...nextTripScope, identity: "bob:couple-b:2" };
  assert.equal(guard.isCurrent(oldPermission, changedPermission), false, "permission partition changes must reject in-flight data");

  const accountA = { authEpoch: 10, identity: "alice:couple-a:3", tripId: "trip-a" };
  const accountB = { authEpoch: 11, identity: "bob:couple-b:5", tripId: "trip-b" };
  const oldMutationA = guard.begin(accountA);
  guard.begin(accountB);
  const currentMutationA = guard.begin({ ...accountA, authEpoch: 12 });
  assert.equal(
    guard.isCurrent(oldMutationA, { ...accountA, authEpoch: 12 }),
    false,
    "an A-to-B-to-A login cycle must reject the first A mutation response and error",
  );
  assert.equal(
    guard.isCurrent(currentMutationA, { ...accountA, authEpoch: 12 }),
    true,
    "the new A session mutation remains current",
  );

  const app = fs.readFileSync("clients/mobile/App.tsx", "utf8");
  assert.match(app, /tripToolsGuard\.current\.invalidate\(\)/, "clearing the RN session must invalidate trip tool requests");
  assert.match(app, /isTripToolsRequestCurrent/, "RN trip tool commits must use the in-flight scope guard");
  assert.match(app, /refreshNativeTripToolsFromEvent/, "realtime refresh must use the guarded trip-tool loader");
  const updateMutation = app.match(/async function updateNativePacking[\s\S]*?(?=\n  async function searchQuotes)/)?.[0] || "";
  assert.match(
    updateMutation,
    /const request = tripToolsGuard\.current\.begin\(currentTripToolsScope\(tripId\)\)/,
    "packing mutations must allocate a generation-bound request token",
  );
  assert.match(
    updateMutation,
    /await client\.updateTripPackingItem[\s\S]*?if \(!isTripToolsRequestCurrent\(request\)\) return;/,
    "packing mutation success must reject stale A-to-B-to-A responses",
  );
  assert.match(
    updateMutation,
    /catch \(error\) \{\s*if \(!isTripToolsRequestCurrent\(request\)\) return;/,
    "packing mutation errors from stale sessions must not show an alert",
  );
})().catch(error => { console.error(error); process.exitCode = 1; });
