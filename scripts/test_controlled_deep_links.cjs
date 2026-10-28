const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const ts = require("../packages/domain-client/node_modules/typescript");

const source = fs.readFileSync("packages/domain-client/src/index.ts", "utf8");
const helperSource = source.slice(source.indexOf("const h5SurfaceNames"), source.indexOf("export interface ManifestCache"));
const compiled = ts.transpileModule(helperSource, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
const moduleExports = {};
vm.runInNewContext(compiled, { exports: moduleExports, Map, Set, encodeURIComponent });

assert.equal(moduleExports.buildH5SmartLink("trip", "hotel_quote"), "/?screen=trip&smart=hotel_quote");
assert.equal(moduleExports.buildH5SmartLink("life", "unknown_card"), "/?screen=life");
assert.equal(moduleExports.buildH5SmartLink("https://evil.example", "javascript:alert(1)"), "/?screen=home");

const web = fs.readFileSync("clients/web/src/main.tsx", "utf8");
assert.match(web, /buildH5SmartLink\(surface, node\.type\)/);
assert.doesNotMatch(web, /location\.assign\(`[^`]*\$\{node\.type\}/);
assert.match(web, /<nav>\{surfaces\.map/);

console.log("受控 H5 跳转验收通过：未知组件不进入 URL，页面只使用五个固定导航面");
