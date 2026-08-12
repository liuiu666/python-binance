const express = require("express");
const http = require("http");
const { WebSocketServer, WebSocket } = require("ws");
const path = require("path");
const fs = require("fs");
const os = require("os");
const { spawn } = require("child_process");
const { EventStore } = require("./lib/event_store");
const { createApiAuth, handleLogin } = require("./lib/auth");
const { createProdStrategyConfig } = require("./lib/prod_strategy_config");
const { createDataHealthService, parseCsvTimeMs, shanghaiTime } = require("./lib/data_health");
const { createTabletRuntime } = require("./lib/tablet_runtime");
const { createSignalResponseService } = require("./lib/signal_response");
const { createTradingEngine } = require("./lib/trading_engine");
const {
  DEFAULT_TRADE_CONFIG,
  amountForStrategyConfig,
  normalizeTradeConfig,
  strategyVariants,
  observedStrategyIds,
  liveStrategyIds,
  applyTradeConfigPatch,
  publicTradeConfig
} = require("./lib/trade_config");
const {
  DEFAULT_PAYOUT_RATE,
  payoutRateForDuration,
  dayKeyForTime,
  shanghaiDayRange,
  deriveOrderLifecycleGate,
  buildLiveOrderHistory
} = require("./lib/trade_history");

const app = express();
const server = http.createServer(app);
const wss = new WebSocketServer({ server, path: "/ws" });
const PUBLIC_DIR = path.join(__dirname, "public");
const DASHBOARD_DIR = path.join(PUBLIC_DIR, "dashboard");
const NORMAL_VISUAL_DATA_FILE = path.join(__dirname, "frontend", "src", "data", "normalRealData.json");
app.use("/dashboard", express.static(DASHBOARD_DIR, { index: "index.html" }));
app.get("/", (req, res) => {
  const dashboardIndex = path.join(DASHBOARD_DIR, "index.html");
  if (fs.existsSync(dashboardIndex)) {
    res.sendFile(dashboardIndex);
    return;
  }
  res.status(503).send("Dashboard has not been built. Run npm run frontend:build.");
});
app.use(express.static(PUBLIC_DIR, { index: false }));
const PORT = process.env.PORT || 3000;
const DATA_DIR = process.env.DATA_DIR || path.join(__dirname, "data");
const SERVER_ID = process.env.SERVER_ID || os.hostname() || "unknown";
const eventStore = new EventStore({ serverId: SERVER_ID });
const apiAuth = createApiAuth(process.env);
const requireApiToken = apiAuth.middleware;

app.post("/api/login", express.json(), (req, res) => {
  const { username, password } = req.body || {};
  const result = handleLogin(username, password);
  if (result.success) {
    res.json({ token: result.token, username: result.username });
  } else {
    res.status(401).json({ error: result.error });
  }
});

app.get("/api/normal-visual-data", (req, res) => {
  if (!fs.existsSync(NORMAL_VISUAL_DATA_FILE)) {
    res.status(404).json({ error: "normal visual data not found" });
    return;
  }
  res.setHeader("Cache-Control", "public, max-age=300");
  res.sendFile(NORMAL_VISUAL_DATA_FILE);
});

const SIGNAL_FILE = path.join(DATA_DIR, "live_signals.json");
const SIGNAL_SCRIPT_FILE = path.join(__dirname, "py", "signal_btc.py");
const SIGNAL_STDOUT_FILE = path.join(__dirname, ".sig.out");
const SIGNAL_STDERR_FILE = path.join(__dirname, ".sig.err");
const REPORT_STDOUT_FILE = path.join(__dirname, ".reports.out");
const REPORT_STDERR_FILE = path.join(__dirname, ".reports.err");
const DATA_UPDATE_SCRIPT_FILE = path.join(__dirname, "py", "update_live_data.py");
const DATA_UPDATE_STATUS_FILE = path.join(DATA_DIR, "live_data_update_status.json");
const DATA_UPDATE_STDOUT_FILE = path.join(__dirname, ".data_update.out");
const DATA_UPDATE_STDERR_FILE = path.join(__dirname, ".data_update.err");
const SECOND_DATA_STATUS_FILE = path.join(DATA_DIR, "second_data_status.json");
const SECOND_DATA_FILE = path.join(DATA_DIR, "btcusdt_1s_trades.csv");
const ORDERBOOK_STATUS_FILE = path.join(DATA_DIR, "orderbook_status.json");
const ORDERBOOK_FILE = path.join(DATA_DIR, "btcusdt_orderbook_1s.csv");
const ORDERBOOK_PREDICTION_STATUS_FILE = path.join(DATA_DIR, "orderbook_prediction_status.json");
const AUCTION_DATA_STATUS_FILE = path.join(DATA_DIR, "auction_data_status.json");
const PRICE_FILE = path.join(DATA_DIR, "current_price.json");
const CONFIG_FILE = path.join(DATA_DIR, "trade_config.json");
const PROD_CONFIG_FILE = path.join(DATA_DIR, "prod_config.json");
const {
  normalizeLlmInteger,
  readProdConfig,
  applyProdStrategyParams,
  saveProdStrategyParams,
  strategyRestartFingerprint
} = createProdStrategyConfig({ prodConfigFile: PROD_CONFIG_FILE });
const TRADE_AUDIT_FILE = path.join(DATA_DIR, "trade_audit.jsonl");
const SIGNAL_AUDIT_FILE = path.join(DATA_DIR, "signal_audit.jsonl");
const REAL_BALANCE_FILE = path.join(DATA_DIR, "real_balance.json");
const AUTO_SCRIPT_FILE = path.join(__dirname, "auto_btc.js");
const PRICE_TICKS_FILE = path.join(DATA_DIR, "price_ticks.jsonl");
const ORDER_LIFECYCLE_GATE_FILE = path.join(DATA_DIR, "order_lifecycle_gate.json");
const SECOND_BACKTEST_REPORT_FILE = path.join(DATA_DIR, "second_backtest_report_latest.json");
const BASE_STRATEGY_IDS = ["BTC_10min_SAFE", "BTC_10min_TAKER"];
const PYTHON_EXE = process.env.PYTHON_EXE || "python";
const SERVER_SIM_TRADING_ENABLED = process.env.SERVER_SIM_TRADING_ENABLED === "1";
const MANAGED_PROCESSES_ENABLED = process.env.DISABLE_MANAGED_PROCESSES !== "1";
const ENABLE_ORDERBOOK_SHADOW_TRADES = process.env.ENABLE_ORDERBOOK_SHADOW_TRADES === "1";
const LLM_STRATEGY_ID = "BTC_10min_LLM_GLM52";
const DATA_UPDATE_INTERVAL_MS = Math.max(
  60 * 1000,
  Number(process.env.DATA_UPDATE_INTERVAL_MS || 5 * 60 * 1000)
);
const DATA_HEALTH_FILES = {
  klines1m: {
    file: path.join(DATA_DIR, "btcusdt_1m.csv"),
    timeCol: "open_time",
    maxAgeMs: Number(process.env.DATA_HEALTH_1M_MAX_AGE_MS || 15 * 60 * 1000),
    intervalMs: 60 * 1000,
    maxRecentGapMs: Number(process.env.DATA_HEALTH_1M_MAX_GAP_MS || 90 * 1000)
  },
  taker: {
    file: path.join(DATA_DIR, "btcusdt_taker.csv"),
    timeCol: "timestamp",
    maxAgeMs: Number(process.env.DATA_HEALTH_TAKER_MAX_AGE_MS || 30 * 60 * 1000),
    intervalMs: 5 * 60 * 1000,
    maxRecentGapMs: Number(process.env.DATA_HEALTH_5M_MAX_GAP_MS || 8 * 60 * 1000)
  },
  lsratio: {
    file: path.join(DATA_DIR, "btcusdt_lsratio.csv"),
    timeCol: "timestamp",
    maxAgeMs: Number(process.env.DATA_HEALTH_LSRATIO_MAX_AGE_MS || 30 * 60 * 1000),
    intervalMs: 5 * 60 * 1000,
    maxRecentGapMs: Number(process.env.DATA_HEALTH_5M_MAX_GAP_MS || 8 * 60 * 1000)
  },
  openInterest: {
    file: path.join(DATA_DIR, "btcusdt_open_interest.csv"),
    timeCol: "timestamp",
    maxAgeMs: Number(process.env.DATA_HEALTH_OPEN_INTEREST_MAX_AGE_MS || 30 * 60 * 1000),
    intervalMs: 5 * 60 * 1000,
    maxRecentGapMs: Number(process.env.DATA_HEALTH_5M_MAX_GAP_MS || 8 * 60 * 1000)
  },
  globalLsratio: {
    file: path.join(DATA_DIR, "btcusdt_global_lsratio.csv"),
    timeCol: "timestamp",
    maxAgeMs: Number(process.env.DATA_HEALTH_GLOBAL_LSRATIO_MAX_AGE_MS || 30 * 60 * 1000),
    intervalMs: 5 * 60 * 1000,
    maxRecentGapMs: Number(process.env.DATA_HEALTH_5M_MAX_GAP_MS || 8 * 60 * 1000)
  },
  topAccountLsratio: {
    file: path.join(DATA_DIR, "btcusdt_top_account_lsratio.csv"),
    timeCol: "timestamp",
    maxAgeMs: Number(process.env.DATA_HEALTH_TOP_ACCOUNT_LSRATIO_MAX_AGE_MS || 30 * 60 * 1000),
    intervalMs: 5 * 60 * 1000,
    maxRecentGapMs: Number(process.env.DATA_HEALTH_5M_MAX_GAP_MS || 8 * 60 * 1000)
  },
  funding: {
    file: path.join(DATA_DIR, "btcusdt_funding.csv"),
    timeCol: "fundingTime",
    maxAgeMs: Number(process.env.DATA_HEALTH_FUNDING_MAX_AGE_MS || 12 * 60 * 60 * 1000),
    intervalMs: 8 * 60 * 60 * 1000,
    maxRecentGapMs: Number(process.env.DATA_HEALTH_FUNDING_MAX_GAP_MS || 10 * 60 * 60 * 1000)
  }
};
const SIGNAL_SNAPSHOT_MAX_AGE_MS = Number(process.env.SIGNAL_SNAPSHOT_MAX_AGE_MS || 5 * 60 * 1000);
const REPORT_SCRIPT_ENV = {
  PYTHONUNBUFFERED: "1",
  APP_DIR: __dirname,
  DATA_DIR,
  OMP_NUM_THREADS: "1",
  OPENBLAS_NUM_THREADS: "1",
  MKL_NUM_THREADS: "1",
  NUMEXPR_NUM_THREADS: "1"
};
const SECOND_BACKTEST_SCRIPT_FILE = path.join(__dirname, "py", "run_second_backtest.py");

let signalService = {
  pid: null,
  startedAt: null,
  lastExit: null,
  restarts: 0,
  restartTimer: null
};
let reportRefresh = {
  running: false,
  lastStart: null,
  lastFinish: null,
  lastExitCode: null,
  lastError: null,
  runs: 0
};
let dataUpdate = {
  running: false,
  lastStart: null,
  lastFinish: null,
  lastExitCode: null,
  lastError: null,
  runs: 0,
  pid: null
};
let shuttingDown = false;

function startSignalService(reason = "startup") {
  if (signalService.pid) return;
  try {
    const out = fs.openSync(SIGNAL_STDOUT_FILE, "a");
    const err = fs.openSync(SIGNAL_STDERR_FILE, "a");
    const child = spawn(PYTHON_EXE, [SIGNAL_SCRIPT_FILE], {
      cwd: __dirname,
      windowsHide: true,
      env: { ...process.env, ...REPORT_SCRIPT_ENV },
      stdio: ["ignore", out, err]
    });
    signalService.pid = child.pid;
    signalService.startedAt = Date.now();
    signalService.lastExit = null;
    appendJsonl(TRADE_AUDIT_FILE, {
      serverTime: Date.now(),
      event: "signal_service_start",
      reason,
      pid: child.pid,
      python: PYTHON_EXE
    });
    child.on("exit", (code, signal) => {
      signalService.lastExit = { time: Date.now(), code, signal, pid: child.pid };
      signalService.pid = null;
      appendJsonl(TRADE_AUDIT_FILE, {
        serverTime: Date.now(),
        event: "signal_service_exit",
        code,
        signal,
        pid: child.pid
      });
      if (!shuttingDown) {
        signalService.restarts += 1;
        clearTimeout(signalService.restartTimer);
        signalService.restartTimer = setTimeout(() => startSignalService("restart"), 5000);
      }
    });
  } catch (e) {
    signalService.lastExit = { time: Date.now(), error: String(e && e.message ? e.message : e) };
    appendJsonl(TRADE_AUDIT_FILE, {
      serverTime: Date.now(),
      event: "signal_service_start_error",
      error: signalService.lastExit.error
    });
  }
}

function stopSignalService() {
  shuttingDown = true;
  clearTimeout(signalService.restartTimer);
  if (signalService.pid) {
    try { process.kill(signalService.pid); } catch (e) {}
  }
}

function restartSignalService(reason = "config_update") {
  if (!MANAGED_PROCESSES_ENABLED) return;
  clearTimeout(signalService.restartTimer);
  if (signalService.pid) {
    appendJsonl(TRADE_AUDIT_FILE, {
      serverTime: Date.now(),
      event: "signal_service_restart_requested",
      reason,
      pid: signalService.pid
    });
    try { process.kill(signalService.pid); } catch (e) {}
    return;
  }
  signalService.restarts += 1;
  signalService.restartTimer = setTimeout(() => startSignalService(reason), 1000);
}

function appendJsonl(file, obj) {
  return eventStore.appendJsonl(file, obj, { normalize: file === TRADE_AUDIT_FILE });
}

function tailJsonl(file, limit) {
  return eventStore.tailJsonl(file, limit);
}

function readJsonl(file) {
  return eventStore.readJsonl(file);
}

function readJsonFile(file, fallback = null) {
  try { return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : fallback; }
  catch (e) { return fallback; }
}

function writeJsonAtomic(file, value) {
  const tempFile = `${file}.${process.pid}.tmp`;
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(tempFile, JSON.stringify(value, null, 2), "utf8");
  fs.renameSync(tempFile, file);
}

// --- 信号响应管线服务 ---
// 先创建 signalResponse（其 dataHealthGate 延迟绑定到下方 dataHealth 服务），
// 再把 signalResponse.signalTimeMs 注入 dataHealth 服务，打破循环依赖。
let dataHealth = null;
let engine = null;  // 交易引擎前向声明，供 signalResponse 的 getServerTrades 延迟绑定

const signalResponse = createSignalResponseService({
  // 动态状态 getter：每次调用读取最新值
  getTradeConfig: () => tradeConfig,
  getCurrentPrice: () => currentPrice,
  getServerTrades: () => (engine ? engine.getTrades() : []),
  getPayoutRate: () => PAYOUT_RATE,
  getSignalExpiryMs: () => SIGNAL_EXPIRY_MS,
  // gate / history 工具（来自 data_health / trade_history 模块）
  dataHealthGate: (signals) => (dataHealth ? dataHealth.dataHealthGate(signals) : { blocked: false, reasons: [] }),
  deriveOrderLifecycleGate,
  buildLiveOrderHistory,
  // 审计与价格 tick 读写
  appendTradeAudit: (obj) => appendJsonl(TRADE_AUDIT_FILE, obj),
  tailTradeAudit: (limit) => tailJsonl(TRADE_AUDIT_FILE, limit),
  readTradeAudit: () => readJsonl(TRADE_AUDIT_FILE),
  readTradeAuditRange: (startMs, endMs) => eventStore.readJsonlRange(TRADE_AUDIT_FILE, startMs, endMs),
  readPriceTicks: () => readJsonl(PRICE_TICKS_FILE),
  readPriceTicksRange: (startMs, endMs) => eventStore.readJsonlRange(PRICE_TICKS_FILE, startMs, endMs),
  writeOrderLifecycleGateSnapshot: (gate) => writeJsonAtomic(ORDER_LIFECYCLE_GATE_FILE, gate),
  // 时间工具（来自 data_health 模块级导出）
  parseCsvTimeMs,
  shanghaiTime,
  // 策略配置工具（来自 trade_config 模块）
  publicTradeConfig,
  amountForStrategyConfig,
  strategyVariants,
  observedStrategyIds,
  liveStrategyIds,
  // 信号文件
  signalFile: SIGNAL_FILE
});

// 数据健康服务依赖信号响应管线的信号时间规则，并通过 getter 读取最新更新状态。
dataHealth = createDataHealthService({
  signalFile: SIGNAL_FILE,
  dataUpdateStatusFile: DATA_UPDATE_STATUS_FILE,
  secondDataStatusFile: SECOND_DATA_STATUS_FILE,
  secondDataFile: SECOND_DATA_FILE,
  orderbookStatusFile: ORDERBOOK_STATUS_FILE,
  orderbookFile: ORDERBOOK_FILE,
  orderbookPredictionStatusFile: ORDERBOOK_PREDICTION_STATUS_FILE,
  auctionDataStatusFile: AUCTION_DATA_STATUS_FILE,
  dataHealthFiles: DATA_HEALTH_FILES,
  signalSnapshotMaxAgeMs: SIGNAL_SNAPSHOT_MAX_AGE_MS,
  signalTimeMs: signalResponse.signalTimeMs,
  getDataUpdate: () => dataUpdate
});
const {
  readLastCsvRows,
  dataHealthGate,
  dataHealthSnapshot,
  secondDataHealthSnapshot,
  orderbookHealthSnapshot,
  auctionDataHealthSnapshot,
  orderbookPredictionResponse,
  orderbookPredictionSnapshot
} = dataHealth;

function runScript(script, cb, args = []) {
  const out = fs.openSync(REPORT_STDOUT_FILE, "a");
  const err = fs.openSync(REPORT_STDERR_FILE, "a");
  let done = false;
  const finish = (code, signal, error) => {
    if (done) return;
    done = true;
    try { fs.closeSync(out); } catch (e) {}
    try { fs.closeSync(err); } catch (e) {}
    cb(code, signal, error);
  };
  const child = spawn(PYTHON_EXE, [script, ...args], {
    cwd: __dirname,
    windowsHide: true,
    env: { ...process.env, ...REPORT_SCRIPT_ENV },
    stdio: ["ignore", out, err]
  });
  child.on("exit", (code, signal) => finish(code, signal));
  child.on("error", (e) => finish(null, null, e));
}

function runDataUpdate(reason = "timer") {
  if (dataUpdate.running) return;
  if (!fs.existsSync(DATA_UPDATE_SCRIPT_FILE)) return;
  dataUpdate.running = true;
  dataUpdate.lastStart = Date.now();
  dataUpdate.lastError = null;
  dataUpdate.runs += 1;
  appendJsonl(TRADE_AUDIT_FILE, { serverTime: Date.now(), event: "data_update_start", reason });
  let out = null;
  let err = null;
  try {
    out = fs.openSync(DATA_UPDATE_STDOUT_FILE, "a");
    err = fs.openSync(DATA_UPDATE_STDERR_FILE, "a");
    const child = spawn(PYTHON_EXE, [DATA_UPDATE_SCRIPT_FILE], {
      cwd: __dirname,
      windowsHide: true,
      env: { ...process.env, ...REPORT_SCRIPT_ENV },
      stdio: ["ignore", out, err]
    });
    dataUpdate.pid = child.pid;
    const finish = (code, signal, error) => {
      try { if (out !== null) fs.closeSync(out); } catch (e) {}
      try { if (err !== null) fs.closeSync(err); } catch (e) {}
      dataUpdate.running = false;
      dataUpdate.pid = null;
      dataUpdate.lastFinish = Date.now();
      dataUpdate.lastExitCode = code;
      dataUpdate.lastError = error ? String(error.message || error) : (code === 0 ? null : `code=${code} signal=${signal || ""}`);
      appendJsonl(TRADE_AUDIT_FILE, {
        serverTime: Date.now(),
        event: dataUpdate.lastError ? "data_update_error" : "data_update_done",
        reason,
        code,
        signal,
        error: dataUpdate.lastError
      });
    };
    child.on("exit", (code, signal) => finish(code, signal));
    child.on("error", (e) => finish(null, null, e));
  } catch (e) {
    try { if (out !== null) fs.closeSync(out); } catch (e2) {}
    try { if (err !== null) fs.closeSync(err); } catch (e2) {}
    dataUpdate.running = false;
    dataUpdate.pid = null;
    dataUpdate.lastFinish = Date.now();
    dataUpdate.lastExitCode = null;
    dataUpdate.lastError = String(e && e.message ? e.message : e);
    appendJsonl(TRADE_AUDIT_FILE, {
      serverTime: Date.now(),
      event: "data_update_error",
      reason,
      error: dataUpdate.lastError
    });
  }
}

function refreshLightReports(reason = "timer") {
  if (reportRefresh.running) return;
  reportRefresh.running = true;
  reportRefresh.lastStart = Date.now();
  reportRefresh.lastError = null;
  reportRefresh.runs += 1;
  appendJsonl(TRADE_AUDIT_FILE, {
    serverTime: Date.now(),
    event: "report_refresh_start",
    reason,
    scripts: [path.basename(SECOND_BACKTEST_SCRIPT_FILE)]
  });
  runScript(SECOND_BACKTEST_SCRIPT_FILE, (code, signal, err) => {
    reportRefresh.running = false;
    reportRefresh.lastFinish = Date.now();
    reportRefresh.lastExitCode = code;
    reportRefresh.lastError = err ? String(err.message || err) : (code === 0 ? null : `code=${code} signal=${signal || ""}`);
    appendJsonl(TRADE_AUDIT_FILE, {
      serverTime: Date.now(),
      event: reportRefresh.lastError ? "report_refresh_error" : "report_refresh_done",
      reason,
      script: SECOND_BACKTEST_SCRIPT_FILE,
      error: reportRefresh.lastError
    });
  }, ["--csv", SECOND_DATA_FILE, "--out", SECOND_BACKTEST_REPORT_FILE]);
}

function readRawJson(req, cb) {
  let chunks = [];
  req.on("data", ck => chunks.push(ck));
  req.on("end", () => {
    const raw = Buffer.concat(chunks).toString("utf8");
    let payload = null;
    try { payload = JSON.parse(raw); } catch (e) {}
    if (!payload && raw.indexOf("=") >= 0) {
      try {
        const params = new URLSearchParams(raw);
        payload = {};
        for (const [k, v] of params) {
          if (k === "body" || k === "payload") {
            try { payload = JSON.parse(v); break; } catch (e) {}
          }
          payload[k] = v;
        }
      } catch (e) {}
    }
    cb(payload, raw);
  });
}


// GET /api/signal —— 薄包装：调用 signalResponse.build 组装响应，平板来源同步镜像到影子交易。
app.get("/api/signal", (req, res) => {
  try {
    const source = String(req.query.source || "");
    const signals = signalResponse.build(source);
    if (source === "autojs" || source === "tablet") {
      try { engine.mirrorTabletSignalsToShadow(signals); }
      catch (e) { console.warn("[Shadow] tablet signal mirror failed:", e.message); }
    }
    res.json(signals);
  } catch (e) { res.json({ _config: tradeConfig, _signalAmount: String(tradeConfig.amount) }); }
});
app.get("/api/price", (req, res) => {
  try { res.json(fs.existsSync(PRICE_FILE) ? JSON.parse(fs.readFileSync(PRICE_FILE, "utf8")) : { price: null }); }
  catch (e) { res.json({ price: null }); }
});

app.get("/api/candles", (req, res) => {
  reloadCandlesIfChanged();
  res.json({
    updatedAt: Date.now(),
    candles: candleHistory.slice(-500)
  });
});

app.get("/api/reports", (req, res) => {
  res.json({
    secondBacktest: readJsonFile(SECOND_BACKTEST_REPORT_FILE),
    dataUpdate,
    reportRefresh
  });
});

app.post("/api/reports/refresh", requireApiToken, (req, res) => {
  refreshLightReports("manual_api");
  res.json({ ok: true, reportRefresh });
});

app.get("/api/data-health", (req, res) => {
  res.json(dataHealthSnapshot());
});

app.get("/api/second-data-health", (req, res) => {
  res.json(secondDataHealthSnapshot());
});

app.get("/api/orderbook-health", (req, res) => {
  res.json(orderbookHealthSnapshot());
});

app.get("/api/auction-data-health", (req, res) => {
  res.json(auctionDataHealthSnapshot());
});

app.get("/api/orderbook-prediction", (req, res) => {
  res.json(orderbookPredictionResponse());
});

function orderbookConfirmForSignal(sig) {
  const snap = orderbookPredictionSnapshot();
  const pred = snap.status && snap.status.prediction;
  if (!snap.ok || !pred || !sig || !sig.signal) return { ok: false, reason: "orderbook_not_ready", snap };
  const confidence = Number(pred.confidence);
  const direction = pred.direction;
  const target10 = Array.isArray(pred.targets) ? pred.targets.find(t => Number(t.horizonSec) === 10) : null;
  const predictedBps = Number(target10 && target10.predictedBps);
  const minConfidence = Number(process.env.ORDERBOOK_SHADOW_MIN_CONFIDENCE || 55);
  const minAbsBps = Number(process.env.ORDERBOOK_SHADOW_MIN_ABS_BPS || 0.15);
  if (direction !== sig.signal) return { ok: false, reason: "orderbook_direction_mismatch", snap, pred };
  if (!Number.isFinite(confidence) || confidence < minConfidence) return { ok: false, reason: "orderbook_confidence_low", snap, pred };
  if (!Number.isFinite(predictedBps) || Math.abs(predictedBps) < minAbsBps) return { ok: false, reason: "orderbook_edge_small", snap, pred };
  return { ok: true, snap, pred, target10, confidence, predictedBps };
}

app.post("/api/data-update/refresh", requireApiToken, (req, res) => {
  runDataUpdate("manual_api");
  res.json({ ok: true, dataUpdate });
});

function liveOrderHistory(options = {}) {
  const opts = typeof options === "number" ? { limit: options } : (options || {});
  const mode = opts.mode === "day" ? "day" : "page";
  const dayRange = mode === "day" ? shanghaiDayRange(opts.day || dayKeyForTime(Date.now())) : null;
  const settleBufferMs = Number(process.env.TRADE_HISTORY_SETTLE_BUFFER_MS || 90 * 60 * 1000);
  const auditRows = dayRange
    ? eventStore.readJsonlRange(TRADE_AUDIT_FILE, dayRange.startMs, dayRange.endMs + settleBufferMs)
    : readJsonl(TRADE_AUDIT_FILE);
  const priceTicks = dayRange
    ? eventStore.readJsonlRange(PRICE_TICKS_FILE, dayRange.startMs, dayRange.endMs + settleBufferMs)
    : readJsonl(PRICE_TICKS_FILE);
  const availableDays = mode === "day"
    ? [...new Set(eventStore.readTradeAuditTimes(10000).map(time => dayKeyForTime(time)))].sort((a, b) => b.localeCompare(a))
    : undefined;
  const llmLogsByDecisionId = Object.fromEntries(
    readJsonl(SIGNAL_AUDIT_FILE)
      .filter(row => row && row.llm_decision_id && (row.llm_prompt || row.llm_response))
      .map(row => [row.llm_decision_id, {
        llm_decision_id: row.llm_decision_id,
        llm_model: row.llm_model || null,
        llm_prompt: row.llm_prompt || null,
        llm_response: row.llm_response || null
      }])
  );
  return buildLiveOrderHistory({
    auditRows,
    priceTicks,
    serverTrades: engine.getTrades(),
    currentPrice,
    payoutRate: PAYOUT_RATE,
    mode,
    day: dayRange ? dayRange.day : opts.day,
    availableDays,
    llmLogsByDecisionId,
    ...opts
  });
}

// 平板运行时模块仅在此装配依赖；动态状态必须通过 getter 保持实时。
createTabletRuntime({
  port: PORT,
  dataDir: DATA_DIR,
  autoScriptFile: AUTO_SCRIPT_FILE,
  serverSimTradingEnabled: SERVER_SIM_TRADING_ENABLED,
  managedProcessesEnabled: MANAGED_PROCESSES_ENABLED,
  pythonExe: PYTHON_EXE,
  signalScriptFile: SIGNAL_SCRIPT_FILE,
  serverId: eventStore.serverId,
  publicBaseUrl: process.env.PUBLIC_BASE_URL,
  tailAudit: limit => tailJsonl(TRADE_AUDIT_FILE, limit),
  appendAudit: item => appendJsonl(TRADE_AUDIT_FILE, item),
  apiAuthPublicInfo: apiAuth.publicInfo(),
  getRealBalance: () => realBalance,
  getSignalService: () => signalService,
  getDataUpdate: () => dataUpdate
}).registerRoutes(app);

// --- State ---
const PAYOUT_RATE = DEFAULT_PAYOUT_RATE;
const DURATION_MS = 30 * 60 * 1000;  // 30 minutes
const WINDOW_SEC = 60;  // 60-second trading window (wider than before)
const AUTO_TRADE_AMOUNT = 100;  // Auto-trade 100 USDT per signal
const AUTO_TRADE_ENABLED = SERVER_SIM_TRADING_ENABLED;
const SIGNAL_EXPIRY_MS = 120 * 1000;  // Signal valid for 2 minutes
const STRATEGY_COOLDOWN_MS = 10 * 60 * 1000;
const SHADOW_EXECUTION_DELAY_MS = Math.max(0, Number(process.env.SHADOW_EXECUTION_DELAY_MS || 5000));

let currentPrice = null;
let priceHistory = [];
const MAX_HISTORY = 600;
// trades / nextTradeId / account / lastSignals / shadowTrades 等交易状态
// 已迁入 lib/trading_engine.js，由 engine 独占持有。
let realBalance = normalizeRealBalance(readJsonFile(REAL_BALANCE_FILE, null));

function preloadCandles() {
  const file = path.join(DATA_DIR, "btcusdt_1m.csv");
  const rows = readLastCsvRows(file, 500);
  const candles = [];
  for (const row of rows) {
    const openTimeMs = parseCsvTimeMs(row["open_time"]);
    const open = parseFloat(row["open"]);
    const high = parseFloat(row["high"]);
    const low = parseFloat(row["low"]);
    const close = parseFloat(row["close"]);
    if (Number.isFinite(openTimeMs) && Number.isFinite(open) && Number.isFinite(high) && Number.isFinite(low) && Number.isFinite(close)) {
      candles.push({
        time: Math.floor(openTimeMs / 1000),
        open,
        high,
        low,
        close,
        timeMs: openTimeMs
      });
    }
  }
  candles.sort((a, b) => a.time - b.time);
  return candles.slice(-500);
}

let candleHistory = [];
let candleSourceMtimeMs = 0;
try {
  candleHistory = preloadCandles();
  const candleFile = path.join(DATA_DIR, "btcusdt_1m.csv");
  candleSourceMtimeMs = fs.existsSync(candleFile) ? fs.statSync(candleFile).mtimeMs : 0;
  console.log(`[Candles] Successfully preloaded ${candleHistory.length} historical candles from CSV.`);
} catch (e) {
  console.log("[Candles] Failed to preload:", e);
}

function reloadCandlesIfChanged(force = false) {
  try {
    const file = path.join(DATA_DIR, "btcusdt_1m.csv");
    if (!fs.existsSync(file)) return false;
    const mtimeMs = fs.statSync(file).mtimeMs;
    if (!force && mtimeMs === candleSourceMtimeMs) return false;
    candleHistory = preloadCandles();
    candleSourceMtimeMs = mtimeMs;
    return true;
  } catch (e) {
    return false;
  }
}

function normalizeRealBalance(raw) {
  if (!raw || typeof raw !== "object") return { amount: null, time: null, device: null };
  const amount = Number(raw.amount);
  return {
    amount: Number.isFinite(amount) && amount >= 0 ? amount : null,
    time: Number(raw.time) || null,
    device: raw.device || null,
    source: raw.source || null
  };
}

function persistRealBalance() {
  try {
    fs.writeFileSync(REAL_BALANCE_FILE, JSON.stringify(realBalance, null, 2), "utf8");
  } catch (e) {}
}

function broadcastRealBalance() {
  wss.clients.forEach(cl => {
    if (cl.readyState === WebSocket.OPEN) cl.send(JSON.stringify({ type: "balance", ...realBalance }));
  });
}

function updateRealBalanceFromPayload(payload, source = "unknown") {
  if (!payload || typeof payload !== "object") return null;
  const rawAmount = source === "trade-audit"
    ? (payload.balance !== undefined ? payload.balance :
      (payload.realBalance && payload.realBalance.amount !== undefined ? payload.realBalance.amount : null))
    : (payload.amount !== undefined ? payload.amount :
      (payload.balance !== undefined ? payload.balance :
        (payload.realBalance && payload.realBalance.amount !== undefined ? payload.realBalance.amount : null)));
  const amt = Number(rawAmount);
  if (!Number.isFinite(amt) || amt < 0) return null;
  const deviceName = payload.device || payload.deviceId || (payload.realBalance && payload.realBalance.device) || "unknown";
  realBalance = {
    amount: amt,
    time: Number(payload.balanceTime || payload.time || payload.clientTime) || Date.now(),
    device: deviceName,
    source
  };
  persistRealBalance();
  broadcastRealBalance();
  return realBalance;
}


let lastBinanceFetchTime = 0;
function fetchBinancePriceFallback() {
  const now = Date.now();
  if (now - lastBinanceFetchTime < 1800) return;
  lastBinanceFetchTime = now;
  
  fetch("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
    .then(res => res.json())
    .then(data => {
      const price = parseFloat(data.price);
      if (!isNaN(price) && price > 0) {
        try {
          fs.writeFileSync(PRICE_FILE, JSON.stringify({ price: String(price), time: Date.now() }));
        } catch (err) {}
      }
    })
    .catch(() => {});
}

function readPrice() {
  fetchBinancePriceFallback();
  try {
    if (fs.existsSync(PRICE_FILE)) {
      const data = JSON.parse(fs.readFileSync(PRICE_FILE, "utf8"));
      const price = parseFloat(data.price);
      const time = parseInt(data.time) || Date.now();
      if (!isNaN(price) && price > 0) {
        const lastTime = priceHistory.length > 0 ? priceHistory[priceHistory.length - 1].time : 0;
        if (time > lastTime || price !== currentPrice) {
          currentPrice = price;
          priceHistory.push({ time, price });
          appendJsonl(PRICE_TICKS_FILE, { time, price });
          if (priceHistory.length > MAX_HISTORY) priceHistory = priceHistory.slice(-MAX_HISTORY);

          // Real-time K-line Candlestick Aggregation
          const tickMinuteMs = Math.floor(time / 60000) * 60000;
          const tickMinuteSec = tickMinuteMs / 1000;
          
          if (candleHistory.length === 0) {
            candleHistory.push({
              time: tickMinuteSec,
              open: price,
              high: price,
              low: price,
              close: price,
              timeMs: tickMinuteMs
            });
          } else {
            const lastCandle = candleHistory[candleHistory.length - 1];
            if (lastCandle.timeMs === tickMinuteMs) {
              // Update last candle
              lastCandle.high = Math.max(lastCandle.high, price);
              lastCandle.low = Math.min(lastCandle.low, price);
              lastCandle.close = price;
            } else {
              // Push a new candle
              candleHistory.push({
                time: tickMinuteSec,
                open: price,
                high: price,
                low: price,
                close: price,
                timeMs: tickMinuteMs
              });
              if (candleHistory.length > 500) {
                candleHistory = candleHistory.slice(-500);
              }
            }
          }

          broadcastPrice();
        }
      }
    }
  } catch (e) {}
}
setInterval(readPrice, 2000);
readPrice();

function broadcastPrice() {
  reloadCandlesIfChanged();
  const lastCandle = candleHistory.length > 0 ? candleHistory[candleHistory.length - 1] : null;
  const msg = JSON.stringify({
    type: "price",
    price: currentPrice,
    time: Date.now(),
    history: priceHistory.slice(-300),
    candle: lastCandle,
    candles: candleHistory.slice(-500)
  });
  wss.clients.forEach(c => { if (c.readyState === WebSocket.OPEN) c.send(msg); });
}

wss.on("connection", (ws) => {
  engine.handleWebSocketConnection(ws);
});

// --- Dynamic Trade Config ---
let tradeConfig = (() => {
  try {
    if (fs.existsSync(CONFIG_FILE)) {
      const saved = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'));
      return normalizeTradeConfig(saved);
    }
  } catch (e) {}
  return normalizeTradeConfig(DEFAULT_TRADE_CONFIG);
})();

// --- 交易引擎 ---
// 全部交易状态（trades / shadowTrades / account / autoTradeLog 等）由引擎模块独占持有。
// 动态状态通过 getter 注入，保证每次读取最新值；WebSocket 广播通过 publish 注入，不直接依赖 wss。
engine = createTradingEngine({
  // 动态状态 getter
  getTradeConfig: () => tradeConfig,
  getCurrentPrice: () => currentPrice,
  getRealBalance: () => realBalance,
  // 信号服务接口
  buildSignalResponse: signalResponse.build,
  signalIsActionableNow: signalResponse.signalIsActionableNow,
  signalActionableMs: signalResponse.signalActionableMs,
  signalActionableTime: signalResponse.signalActionableTime,
  signalReferencePrice: signalResponse.signalReferencePrice,
  llmLogSnapshotForDecision: signalResponse.llmLogSnapshotForDecision,
  currentStrategyVariants: signalResponse.currentStrategyVariants,
  currentLiveStrategyIds: signalResponse.currentLiveStrategyIds,
  amountForStrategy: signalResponse.amountForStrategy,
  // orderbook 确认
  orderbookConfirmForSignal,
  // 审计写入与缓存失效
  appendTradeAudit: (item) => appendJsonl(TRADE_AUDIT_FILE, item),
  tailTradeAudit: (limit) => tailJsonl(TRADE_AUDIT_FILE, limit),
  invalidateTradeEvent: (eventName) => signalResponse.invalidateTradeDerivedCaches(eventName),
  // WebSocket 广播（不直接传 wss）
  publish: (type, payload) => {
    const msg = JSON.stringify({ type, ...payload });
    wss.clients.forEach(c => { if (c.readyState === WebSocket.OPEN) c.send(msg); });
  },
  // 市场快照（用于 WebSocket init 消息）
  getMarketSnapshot: () => ({ price: currentPrice, priceHistory, candles: candleHistory, realBalance }),
  // 常量配置
  payoutRate: PAYOUT_RATE,
  windowSec: WINDOW_SEC,
  autoTradeAmount: AUTO_TRADE_AMOUNT,
  autoTradeEnabled: AUTO_TRADE_ENABLED,
  serverSimTradingEnabled: SERVER_SIM_TRADING_ENABLED,
  enableOrderbookShadowTrades: ENABLE_ORDERBOOK_SHADOW_TRADES,
  strategyCooldownMs: STRATEGY_COOLDOWN_MS,
  shadowExecutionDelayMs: SHADOW_EXECUTION_DELAY_MS
});
engine.start();

function saveTradeConfig() {
  try { fs.writeFileSync(CONFIG_FILE, JSON.stringify(publicTradeConfig(tradeConfig), null, 2)); } catch (e) {}
}

app.get("/api/config", (req, res) => {
  const prodConfig = readProdConfig();
  const merged = normalizeTradeConfig({
    ...tradeConfig,
    strategyVariants: strategyVariants(tradeConfig).map(variant => ({
      ...variant,
      tailPct: Number(prodConfig[variant.id]?.norm_tail_pct ?? variant.tailPct)
    }))
  });
  res.json(publicTradeConfig(merged));
});

app.post("/api/config", requireApiToken, express.json(), (req, res) => {
  const beforeVariants = strategyRestartFingerprint(tradeConfig);
  const result = applyTradeConfigPatch(tradeConfig, req.body, { autoTradeSafetyGate: signalResponse.autoTradeSafetyGate });
  tradeConfig = result.tradeConfig;
  for (const event of result.auditEvents) {
    appendJsonl(TRADE_AUDIT_FILE, { serverTime: Date.now(), ...event });
  }
  saveTradeConfig();
  saveProdStrategyParams(tradeConfig);
  const afterVariants = strategyRestartFingerprint(tradeConfig);
  if (afterVariants !== beforeVariants) {
    appendJsonl(TRADE_AUDIT_FILE, {
      serverTime: Date.now(),
      event: "strategy_params_updated",
      strategyVariants: strategyVariants(tradeConfig)
    });
    restartSignalService("strategy_params_updated");
  }
  console.log("[Config] Updated:", JSON.stringify(tradeConfig));
  res.json({ ...publicTradeConfig(tradeConfig), safetyBlocked: result.safetyBlocked, forceAutoTrade: result.forceAutoTrade });
});

function readLlmDirectionConfig() {
  const prod = readProdConfig();
  const current = prod[LLM_STRATEGY_ID] && typeof prod[LLM_STRATEGY_ID] === "object"
    ? prod[LLM_STRATEGY_ID]
    : {};
  const variant = strategyVariants(tradeConfig).find(item => item.id === LLM_STRATEGY_ID) || {};
  return { current, variant };
}

function publicLlmDirectionConfig() {
  const { current, variant } = readLlmDirectionConfig();
  return {
    strategyId: LLM_STRATEGY_ID,
    enabled: variant.enabled === true,
    tradeEnabled: variant.tradeEnabled === true,
    apiUrl: String(current.llm_api_url || "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions"),
    apiKey: String(current.llm_api_key || ""),
    apiKeyConfigured: !!current.llm_api_key,
    model: String(current.llm_model || "glm-5.2"),
    intervalSec: normalizeLlmInteger(current.llm_interval_sec, 600, 5, 86400),
    maxTokens: normalizeLlmInteger(current.llm_max_tokens, 8000, 1, 32768)
  };
}

app.get("/api/llm-config", (req, res) => {
  res.json(publicLlmDirectionConfig());
});

app.post("/api/llm-config", requireApiToken, express.json({ limit: "200kb" }), (req, res) => {
  const body = req.body && typeof req.body === "object" ? req.body : {};
  const { current } = readLlmDirectionConfig();
  // Key 按用户要求在接口、页面和 prod_config 中均使用完整明文；空值可显式清除。
  const nextKey = body.apiKey === undefined ? String(current.llm_api_key || "") : String(body.apiKey).trim();
  const prod = readProdConfig();
  prod[LLM_STRATEGY_ID] = {
    ...current,
    llm_api_url: String(body.apiUrl ?? current.llm_api_url ?? "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions").trim(),
    llm_api_key: nextKey,
    llm_model: String(body.model ?? current.llm_model ?? "glm-5.2").trim(),
    llm_interval_sec: normalizeLlmInteger(body.intervalSec, current.llm_interval_sec || 600, 5, 86400),
    llm_max_tokens: normalizeLlmInteger(body.maxTokens, current.llm_max_tokens || 8000, 1, 32768)
  };
  writeJsonAtomic(PROD_CONFIG_FILE, prod);
  restartSignalService("llm_config_updated");
  res.json({ ok: true, ...publicLlmDirectionConfig() });
});

app.get("/api/llm-status", (req, res) => {
  let signal = null;
  try {
    const rows = fs.existsSync(SIGNAL_FILE) ? JSON.parse(fs.readFileSync(SIGNAL_FILE, "utf8")) : {};
    signal = rows[LLM_STRATEGY_ID] || null;
  } catch (e) {}
  const config = publicLlmDirectionConfig();
  const snapshotMs = Number(signal?._snapshot_time_ms || signal?.generated_at_ms || Date.parse(signal?.generated_at || signal?.time || ""));
  const stale = !Number.isFinite(snapshotMs) || Date.now() - snapshotMs > Math.max(300000, config.intervalSec * 2000);
  const reason = String(signal?.reason || "");
  let state = "running";
  if (!config.enabled) state = "disabled";
  else if (!signal) state = "starting";
  else if (reason.startsWith("llm_error:")) state = "error";
  else if (stale) state = "stale";
  else if (!signal.signal) state = "blocked";
  res.json({
    strategyId: LLM_STRATEGY_ID,
    state,
    signal,
    lastError: state === "error" ? reason : null,
    apiKeyConfigured: config.apiKeyConfigured
  });
});

app.post("/api/llm-predict-now", requireApiToken, (req, res) => {
  const config = publicLlmDirectionConfig();
  if (!config.enabled) {
    res.status(409).json({ ok: false, reason: "llm_disabled" });
    return;
  }
  restartSignalService("llm_predict_now");
  res.json({ ok: true, reason: "signal_service_restarted" });
});


// --- Manual Trade Command ---
const MANUAL_TRADE_TTL_MS = Math.max(30000, Number(process.env.MANUAL_TRADE_TTL_MS || 30000));
const MANUAL_TRADE_MAX_ATTEMPTS = Math.max(1, Math.min(10, Number(process.env.MANUAL_TRADE_MAX_ATTEMPTS || 3)));
const MANUAL_RETRYABLE_ABORT_REASONS = new Set([
  "amount_failed",
  "duration_failed",
  "cannot_wake_screen",
  "balance_before_unavailable",
  "confirm_not_found"
]);
let manualTrade = null; // { direction: 'UP'|'DOWN', amount: '5', duration: '30', time: Date.now() }

function normalizeManualAmount(value, fallback) {
  const n = Math.floor(Number(value));
  if (!Number.isFinite(n) || n < 5) return String(Math.max(5, Math.floor(Number(fallback) || 5)));
  return String(n);
}

function normalizeManualDuration(value, fallback) {
  const n = Math.round(Number(value));
  if (!Number.isFinite(n) || n < 1) return String(Math.max(1, Math.round(Number(fallback) || 10)));
  return String(n);
}

function manualAuditRowsForCommand(command) {
  if (!command || !command.time) return [];
  const start = Number(command.time) - 1000;
  const direction = String(command.direction || "");
  const amount = String(command.amount || "");
  const duration = String(command.duration || "");
  return tailJsonl(TRADE_AUDIT_FILE, 120)
    .filter(row => row && row.strategyId === "manual")
    .filter(row => ["order_attempt", "order_abort", "order_done", "order_unverified"].includes(row.event))
    .filter(row => Number(row.clientTime || row.serverTime || 0) >= start)
    .filter(row => !direction || row.direction === direction)
    .filter(row => !amount || String(row.amount || "") === amount)
    .filter(row => !duration || String(row.duration || "") === duration)
    .sort((a, b) => Number(a.clientTime || a.serverTime || 0) - Number(b.clientTime || b.serverTime || 0));
}

function manualDeleteDecision(command) {
  if (!command) return { clear: true, reason: "empty" };
  const rows = manualAuditRowsForCommand(command);
  const attempts = rows.filter(row => row.event === "order_attempt").length;
  const terminal = rows.slice().reverse().find(row => row.event === "order_abort" || row.event === "order_done" || row.event === "order_unverified");
  if (!terminal) return { clear: false, reason: "awaiting_execution_report", attempts };
  if (terminal.event === "order_done" || terminal.event === "order_unverified") {
    return { clear: true, reason: terminal.event, attempts, terminal };
  }
  const abortReason = String(terminal.reason || "");
  const ageMs = Date.now() - Number(command.time || 0);
  const retryable = MANUAL_RETRYABLE_ABORT_REASONS.has(abortReason);
  if (retryable && attempts < MANUAL_TRADE_MAX_ATTEMPTS && ageMs < MANUAL_TRADE_TTL_MS) {
    return { clear: false, retry: true, reason: abortReason, attempts, terminal };
  }
  return { clear: true, reason: abortReason || "order_abort", attempts, terminal };
}

app.get('/api/manual', (req, res) => {
  if (manualTrade && Date.now() - Number(manualTrade.time || 0) > MANUAL_TRADE_TTL_MS) {
    manualTrade = null;
  }
  res.json(manualTrade);
});

app.post('/api/manual', requireApiToken, express.json(), (req, res) => {
  const { direction, amount, duration } = req.body;
  if (direction !== 'UP' && direction !== 'DOWN') { res.json({ error: 'invalid direction' }); return; }
  manualTrade = {
    direction,
    amount: normalizeManualAmount(amount, tradeConfig.amount),
    duration: normalizeManualDuration(duration, tradeConfig.duration),
    time: Date.now()
  };
  console.log('[Manual] Trade command:', JSON.stringify(manualTrade));
  res.json(manualTrade);
});

app.delete('/api/manual', requireApiToken, (req, res) => {
  const decision = manualDeleteDecision(manualTrade);
  if (!decision.clear) {
    if (manualTrade) {
      manualTrade.retryCount = decision.attempts || 0;
      manualTrade.lastAbortReason = decision.reason || null;
      manualTrade.lastAbortTime = decision.terminal ? Number(decision.terminal.clientTime || decision.terminal.serverTime || 0) : null;
    }
    res.json({ cleared: false, retry: !!decision.retry, reason: decision.reason, attempts: decision.attempts || 0, manualTrade });
    return;
  }
  manualTrade = null;
  res.json({ cleared: true, reason: decision.reason, attempts: decision.attempts || 0 });
});

// --- Trade audit reported by AutoJS tablet and server simulator ---
app.get('/api/trade-audit', (req, res) => {
  const limit = Math.min(500, Math.max(1, Number(req.query.limit) || 100));
  res.json({ serverId: eventStore.serverId, items: tailJsonl(TRADE_AUDIT_FILE, limit) });
});

app.get('/api/trade-history', (req, res) => {
  const pageSize = Math.min(300, Math.max(10, Number(req.query.pageSize || req.query.limit) || 80));
  const page = Math.max(1, Number(req.query.page) || 1);
  const kind = req.query.kind === "shadow" ? "shadow" : req.query.kind === "all" ? "all" : "real";
  const mode = req.query.mode === "page" ? "page" : "day";
  const day = String(req.query.day || dayKeyForTime(Date.now()));
  res.json(liveOrderHistory({ limit: pageSize, page, pageSize, kind, mode, day }));
});

app.post('/api/trade-audit/import', requireApiToken, express.json({ limit: "20mb" }), (req, res) => {
  const body = req.body || {};
  const items = Array.isArray(body) ? body : body.items;
  if (!Array.isArray(items)) {
    res.status(400).json({ error: "items must be an array" });
    return;
  }
  const source = body.source || body.importSource || "manual_import";
  const result = eventStore.importJsonl(TRADE_AUDIT_FILE, items, { importSource: source });
  for (const item of items) signalResponse.invalidateTradeDerivedCaches(item && item.event);
  res.json({ ok: true, ...result });
});

function storeTradeAudit(payload) {
  updateRealBalanceFromPayload(payload, "trade-audit");
  const llmDecisionId = payload.llm_decision_id || payload.signal?.llm_decision_id || null;
  const llmLog = signalResponse.llmLogSnapshotForDecision(llmDecisionId);
  const item = {
    serverTime: Date.now(),
    price: currentPrice,
    ...payload,
    ...(llmLog || {}),
    realBalance
  };
  const written = appendJsonl(TRADE_AUDIT_FILE, item) || item;
  signalResponse.invalidateTradeDerivedCaches(written.event);
  return written;
}

app.post('/api/trade-audit', requireApiToken, (req, res) => {
  readRawJson(req, (payload, raw) => {
    if (!payload || typeof payload !== 'object') {
      res.status(400).json({ error: 'invalid body', raw: raw.substring(0, 200) });
      return;
    }
    res.json({ ok: true, item: storeTradeAudit(payload) });
  });
});

// --- Real balance (reported by auto_btc.js on tablet) ---
app.get('/api/balance', (req, res) => {
  res.json(realBalance);
});

app.post('/api/balance', requireApiToken, (req, res) => {
  readRawJson(req, (payload, raw) => {
    if (!payload) {
      console.log('[Balance POST] failed to parse body');
      res.status(400).json({ error: 'invalid body', raw: raw.substring(0, 200) });
      return;
    }
    const updated = updateRealBalanceFromPayload(payload, 'balance-api');
    if (!updated) {
      console.log('[Balance POST] invalid amount:', payload.amount);
      res.status(400).json({ error: 'invalid amount' });
      return;
    }
    console.log('[Balance] ' + realBalance.device + ': ' + realBalance.amount + ' USDT');
    res.json(realBalance);
  });
});

function shutdown(reason = "shutdown") {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log(`[Server] Shutting down: ${reason}`);
  stopSignalService();
  engine.stop();
  if (dataUpdate.pid) {
    try { process.kill(dataUpdate.pid); } catch (e) {}
  }
  for (const client of wss.clients) {
    try { client.close(1001, "server_shutdown"); } catch (e) {}
  }
  try { wss.close(); } catch (e) {}
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 8000).unref();
}

process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("exit", stopSignalService);

server.listen(PORT, '0.0.0.0', () => {
  try {
    saveTradeConfig();
    saveProdStrategyParams(tradeConfig);
  } catch (e) {
    console.warn("[Config] startup sync failed:", e.message);
  }
  if (MANAGED_PROCESSES_ENABLED) {
    runDataUpdate("server_listen");
    startSignalService("server_listen");
    setInterval(() => runDataUpdate("timer"), DATA_UPDATE_INTERVAL_MS);
    // Heavy reports/backtests are manual only; never start them on server boot.
    // refreshLightReports("server_listen");
  } else {
    console.log("[Server] Managed Python processes disabled by DISABLE_MANAGED_PROCESSES=1");
  }
  console.log(`BTC 二元期权 http://localhost:${PORT} | 自动交易: ${AUTO_TRADE_ENABLED}`);
});
