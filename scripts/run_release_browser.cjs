/* Start an isolated temporary server, run browser E2E, then always stop it. */
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

const root = path.resolve(__dirname, "..");

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      server.close(error => error ? reject(error) : resolve(port));
    });
  });
}

async function waitForHealth(url, child) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (child.exitCode !== null) throw new Error(`测试服务器提前退出 (${child.exitCode})`);
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error("测试服务器启动超时");
}

function runNode(script, env) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [script], { cwd: root, env, stdio: "inherit" });
    child.once("error", reject);
    child.once("exit", code => code === 0 ? resolve() : reject(new Error(`${path.basename(script)} 退出码 ${code}`)));
  });
}

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "love-moments-release-"));
  const port = await freePort();
  const originA = `http://127.0.0.1:${port}`;
  const originB = `http://localhost:${port}`;
  const env = {
    ...process.env,
    APP_HOST: "127.0.0.1",
    APP_PORT: String(port),
    DATABASE_PATH: path.join(temp, "audit.db"),
    MEDIA_ROOT: path.join(temp, "media"),
    BACKUP_ROOT: path.join(temp, "backups"),
    ADMIN_USERNAMES: "test",
    DEMO_MODE: "true",
    REGISTRATION_MODE: "open",
    OPEN_METEO_ENABLED: "false",
    AMAP_WEB_KEY: "",
    GEOAPIFY_ENABLED: "false",
    GEOAPIFY_API_KEY: "",
    LLM_API_KEY: "",
    UI_LLM_API_KEY: "",
    OCR_PROVIDER: "mock",
    ALIBABA_CLOUD_ACCESS_KEY_ID: "",
    ALIBABA_CLOUD_ACCESS_KEY_SECRET: "",
    OCR_API_URL: "",
    OCR_API_KEY: "",
    MEDIA_STORAGE_PROVIDER: "local",
    QINIU_ACCESS_KEY: "",
    QINIU_SECRET_KEY: "",
    SUPABASE_URL: "",
    SUPABASE_ANON_KEY: "",
    LOCAL_ADMIN_NOTIFICATIONS: "false",
    WATCHDOG_AUTO_RESTART: "false",
    RTC_PROVIDER: "webrtc",
    SERPAPI_ENABLED: "false",
    SERPAPI_API_KEY: "",
    JUHE_TRAIN_ENABLED: "false",
    JUHE_TRAIN_API_KEY: "",
    BOOKING_API_URL: "",
    BOOKING_API_KEY: "",
    CORS_ALLOWED_ORIGINS: `${originA},${originB}`,
    LOVE_TEST_ORIGIN_A: originA,
    LOVE_TEST_ORIGIN_B: originB,
  };
  const server = spawn(process.env.PYTHON_BINARY || "python3", ["main.py"], { cwd: root, env, stdio: ["ignore", "pipe", "pipe"] });
  let logs = "";
  server.stdout.on("data", value => { logs += value.toString(); });
  server.stderr.on("data", value => { logs += value.toString(); });
  try {
    await waitForHealth(`${originA}/api/health`, server);
    await runNode(path.join(__dirname, "test_release_browser.cjs"), env);
  } catch (error) {
    if (logs.trim()) console.error(logs.slice(-8000));
    throw error;
  } finally {
    if (server.exitCode === null) server.kill("SIGTERM");
    if (server.exitCode === null) await new Promise(resolve => server.once("exit", resolve));
    fs.rmSync(temp, { recursive: true, force: true });
  }
})().catch(error => {
  console.error(error.stack || error.message);
  process.exitCode = 1;
});
