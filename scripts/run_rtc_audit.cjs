/* Run the real two-account RTC audit with an isolated temporary database. */
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

const root = path.resolve(__dirname, "..");

function freePort() {
  return new Promise((resolve, reject) => {
    const socket = net.createServer();
    socket.once("error", reject);
    socket.listen(0, "127.0.0.1", () => {
      const port = socket.address().port;
      socket.close(error => error ? reject(error) : resolve(port));
    });
  });
}

async function waitForHealth(url, child) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (child.exitCode !== null) throw new Error(`RTC 测试服务器提前退出 (${child.exitCode})`);
    try {
      if ((await fetch(url)).ok) return;
    } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error("RTC 测试服务器启动超时");
}

function runAudit(env) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [path.join(__dirname, "test_shengwang_rtc.cjs")], { cwd: root, env, stdio: "inherit" });
    child.once("error", reject);
    child.once("exit", code => code === 0 ? resolve() : reject(new Error(`RTC 测试退出码 ${code}`)));
  });
}

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "love-moments-rtc-"));
  const port = await freePort();
  const originA = `http://127.0.0.1:${port}`;
  const originB = `http://localhost:${port}`;
  const env = {
    ...process.env,
    APP_HOST: "127.0.0.1",
    APP_PORT: String(port),
    DATABASE_PATH: path.join(temp, "rtc.db"),
    MEDIA_ROOT: path.join(temp, "media"),
    DEMO_MODE: "true",
    OPEN_METEO_ENABLED: "false",
    CORS_ALLOWED_ORIGINS: `${originA},${originB}`,
    LOVE_RTC_ORIGIN_A: originA,
    LOVE_RTC_ORIGIN_B: originB,
  };
  const projectPython = process.platform === "win32"
    ? path.join(root, ".venv", "Scripts", "python.exe")
    : path.join(root, ".venv", "bin", "python");
  const pythonBinary = process.env.PYTHON_BINARY || (fs.existsSync(projectPython) ? projectPython : "python3");
  const server = spawn(pythonBinary, ["main.py"], { cwd: root, env, stdio: ["ignore", "pipe", "pipe"] });
  let logs = "";
  server.stdout.on("data", value => { logs += value.toString(); });
  server.stderr.on("data", value => { logs += value.toString(); });
  try {
    await waitForHealth(`${originA}/api/health`, server);
    await runAudit(env);
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
