const assert = require("node:assert");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const repoRoot = path.resolve(__dirname, "..");

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "api-smoke-test-"));
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function requestJson(baseUrl, urlPath, options = {}) {
  const body = options.body === undefined ? null : JSON.stringify(options.body);
  return new Promise((resolve, reject) => {
    const req = http.request(baseUrl + urlPath, {
      method: options.method || "GET",
      headers: {
        ...(body ? { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) } : {}),
        ...(options.headers || {})
      },
      timeout: 5000
    }, res => {
      const chunks = [];
      res.on("data", chunk => chunks.push(chunk));
      res.on("end", () => {
        const text = Buffer.concat(chunks).toString("utf8");
        let json = null;
        try { json = text ? JSON.parse(text) : null; } catch (e) {}
        resolve({ status: res.statusCode, text, json });
      });
    });
    req.on("error", reject);
    req.on("timeout", () => req.destroy(new Error("request timeout")));
    if (body) req.write(body);
    req.end();
  });
}

function startServer({ port, dataDir, token = "" }) {
  return spawn(process.execPath, ["server.js"], {
    cwd: repoRoot,
    env: {
      ...process.env,
      PORT: String(port),
      DATA_DIR: dataDir,
      DISABLE_MANAGED_PROCESSES: "1",
      SERVER_ID: `api-smoke-${port}`,
      PUBLIC_BASE_URL: "",
      API_TOKEN: token,
      CODEX_API_TOKEN: "",
      TRADE_API_TOKEN: ""
    },
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"]
  });
}

async function waitForRuntime(baseUrl) {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    try {
      const res = await requestJson(baseUrl, "/api/runtime");
      if (res.status === 200 && res.json) return res.json;
    } catch (e) {}
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error("server did not become ready");
}

async function withServer(options, fn) {
  const dataDir = tempDir();
  const port = await freePort();
  const child = startServer({ port, dataDir, token: options.token || "" });
  const logs = [];
  child.stdout.on("data", data => logs.push(data.toString()));
  child.stderr.on("data", data => logs.push(data.toString()));
  try {
    const baseUrl = `http://127.0.0.1:${port}`;
    const runtime = await waitForRuntime(baseUrl);
    await fn({ baseUrl, runtime, dataDir, port });
  } finally {
    if (!child.killed) child.kill();
    await new Promise(resolve => child.once("exit", resolve));
    fs.rmSync(dataDir, { recursive: true, force: true });
  }
}

test("server serves React dashboard and read APIs", async () => {
  await withServer({}, async ({ baseUrl, runtime }) => {
    assert.equal(runtime.managedProcessesEnabled, false);
    assert.match(runtime.serverId, /^api-smoke-/);

    const root = await requestJson(baseUrl, "/");
    assert.equal(root.status, 200);
    assert.match(root.text, /\/dashboard\/assets\//);

    const history = await requestJson(baseUrl, "/api/trade-history?limit=10");
    assert.equal(history.status, 200);
    assert.equal(history.json.summary.total, 0);
  });
});

test("server trade audit import dedupes through API", async () => {
  await withServer({}, async ({ baseUrl }) => {
    const body = {
      source: "api-smoke",
      items: [
        { event: "order_done", clientTime: 1710000000000, device: "tablet", direction: "DOWN", amount: 5, duration: 1 }
      ]
    };
    const first = await requestJson(baseUrl, "/api/trade-audit/import", { method: "POST", body });
    const second = await requestJson(baseUrl, "/api/trade-audit/import", { method: "POST", body });
    assert.equal(first.status, 200);
    assert.equal(first.json.imported, 1);
    assert.equal(second.status, 200);
    assert.equal(second.json.imported, 0);
    assert.equal(second.json.skipped, 1);
  });
});

test("server write APIs require token when configured", async () => {
  await withServer({ token: "secret" }, async ({ baseUrl }) => {
    const denied = await requestJson(baseUrl, "/api/config", {
      method: "POST",
      body: { amount: "9" }
    });
      assert.equal(denied.status, 401);

    const allowed = await requestJson(baseUrl, "/api/config", {
      method: "POST",
      headers: { "X-API-Token": "secret" },
      body: { amount: "9", duration: "10", queueOrderPolicy: "10_then_30" }
    });
    assert.equal(allowed.status, 200);
    assert.equal(allowed.json.amount, "9");
    assert.equal(allowed.json.duration, "10");
    assert.equal(allowed.json.queueOrderPolicy, "10_then_30");
  });
});
