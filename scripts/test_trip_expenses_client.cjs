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
  Map, Set, JSON, Date, Number, Array, String, RegExp, Math, encodeURIComponent,
  crypto: { randomUUID: () => "00000000-0000-4000-8000-000000000001" },
  fetch: async () => { throw new Error("unused"); },
});

const parse = exportsObject.parseTripExpenseAmount;
const format = exportsObject.formatTripExpenseCents;
assert.equal(typeof parse, "function", "domain client must export the exact expense amount parser");
assert.equal(typeof format, "function", "domain client must export the exact expense formatter");

assert.equal(parse("12"), 1200);
assert.equal(parse("12.3"), 1230);
assert.equal(parse("12.34"), 1234);
assert.equal(parse("0.01"), 1);
assert.equal(parse("90071992547409.91"), Number.MAX_SAFE_INTEGER);
for (const invalid of ["", " ", "12.", ".12", "12.345", "-1", "+1", "1e3", "NaN", "Infinity", "0", "0.00", "90071992547409.92"]) {
  assert.equal(parse(invalid), null, `must reject ${invalid}`);
}
assert.equal(format(1, "CNY"), "¥0.01");
assert.equal(format(1001, "CNY"), "¥10.01");
assert.equal(format(-501, "CNY"), "-¥5.01");
assert.equal(format(1234, "USD"), "USD 12.34");

const parserSource = source.match(/export function parseTripExpenseAmount[\s\S]*?\n}/)?.[0] || "";
assert.doesNotMatch(parserSource, /parseFloat|Math\.round|Number\(/, "amount parsing must not use floating point conversion");
for (const method of [
  "tripExpenses", "createTripExpense", "updateTripExpense",
  "deleteTripExpense", "restoreTripExpense", "settleTripExpenses",
]) {
  assert.match(source, new RegExp(`${method}\\(`), `domain client must expose ${method}`);
}
