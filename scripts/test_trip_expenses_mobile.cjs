const assert = require("node:assert/strict");
const fs = require("node:fs");

const source = fs.readFileSync("clients/mobile/App.tsx", "utf8");
assert.match(source, /type TripExpenseList/, "mobile must import the shared expense response type");
assert.match(source, /parseTripExpenseAmount/, "mobile must use the exact shared amount parser");
assert.match(source, /formatTripExpenseCents/, "mobile must use the exact shared formatter");
assert.match(source, /client\.tripExpenses\(/, "mobile must load real trip expenses");
assert.match(source, /client\.createTripExpense\(/, "mobile must add an expense natively");
assert.match(source, /client\.deleteTripExpense\(/, "mobile must soft-delete an expense natively");
assert.match(source, /client\.restoreTripExpense\(/, "mobile must restore a deleted expense natively");
assert.match(source, /client\.settleTripExpenses\(/, "mobile must settle selected expenses natively");
assert.match(source, /花费与线下结清/, "mobile must expose the native expense surface");
const expenseFunctions = source.match(/async function loadNativeTripExpenses[\s\S]*?\n  }\n  async function searchQuotes/)?.[0] || "";
assert.ok(expenseFunctions, "mobile expense functions must be grouped before quote search");
assert.doesNotMatch(expenseFunctions, /privateCache|AsyncStorage|setItem|queue/i, "financial state must not enter offline caches or queues");
console.log("移动端旅行花费契约通过：真实接口、精确金额、增删恢复、线下结清与在线专用边界");
