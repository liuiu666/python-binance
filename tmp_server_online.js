const express = require("express");
const http = require("http");
const { WebSocketServer, WebSocket } = require("ws");
const path = require("path");
const fs = require("fs");
const os = require("os");
const { spawn } = require("child_process");
const { EventStore } = require("./lib/event_store");
const { createApiAuth, handleLogin } = require("./lib/auth");
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
const LLM_CONFIG_FILE = path.join(DATA_DIR, "llm_config.json");
const LLM_STATUS_FILE = path.join(DATA_DIR, "llm_status.json");
const LLM_ONCE_SCRIPT_FILE = path.join(__dirname, "py", "llm_consensus_once.py");
const PROD_CONFIG_FILE = path.join(DATA_DIR, "prod_config.json");
const TRADE_AUDIT_FILE = path.join(DATA_DIR, "trade_audit.jsonl");
const LLM_TRAINING_SAMPLES_FILE = path.join(DATA_DIR, "llm_training_samples.jsonl");
const REAL_BALANCE_FILE = path.join(DATA_DIR, "real_balance.json");
const AUTO_SCRIPT_FILE = path.join(__dirname, "auto_btc.js");
const PRICE_TICKS_FILE = path.join(DATA_DIR, "price_ticks.jsonl");
const SECOND_BACKTEST_REPORT_FILE = path.join(DATA_DIR, "second_backtest_report_latest.json");
const BASE_STRATEGY_IDS = ["BTC_10min_SAFE", "BTC_10min_TAKER"];
const PYTHON_EXE = process.env.PYTHON_EXE || "python";
const SERVER_SIM_TRADING_ENABLED = process.env.SERVER_SIM_TRADING_ENABLED === "1";
const MANAGED_PROCESSES_ENABLED = process.env.DISABLE_MANAGED_PROCESSES !== "1";
const ENABLE_ORDERBOOK_SHADOW_TRADES = process.env.ENABLE_ORDERBOOK_SHADOW_TRADES === "1";
const LLM_STRATEGY_ID = "BTC_10min_LLM_CONSENSUS";
const LLM_REMOVED_RESPONSE = {
  removed: true,
  enabled: false,
  message: "大模型预测功能已移除"
};
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
let lossDensityCache = {
  checkedAt: 0,
  rows: []
};
let executionFailureCache = {
  checkedAt: 0,
  rows: []
};
let shuttingDown = false;

function invalidateTradeDerivedCaches(event) {
  const name = String(event || "");
  if (name === "order_abort" || name === "order_unverified" || name === "order_done") {
    executionFailureCache = { checkedAt: 0, rows: [] };
  }
  if (name === "order_done" || name === "shadow_trade_open" || name === "shadow_trade_settle") {
    lossDensityCache = { checkedAt: 0, rows: [] };
  }
}

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

function readCsvHeader(file) {
  try {
    const fd = fs.openSync(file, "r");
    const stat = fs.fstatSync(fd);
    const len = Math.min(stat.size, 4096);
    const buf = Buffer.alloc(len);
    fs.readSync(fd, buf, 0, len, 0);
    fs.closeSync(fd);
    return buf.toString("utf8").split(/\r?\n/)[0].split(",");
  } catch (e) {
    return [];
  }
}

function readLastCsvRows(file, limit = 200, bytes = 512 * 1024) {
  try {
    if (!fs.existsSync(file)) return [];
    const header = readCsvHeader(file);
    if (!header.length) return [];
    const stat = fs.statSync(file);
    const len = Math.min(stat.size, bytes);
    const fd = fs.openSync(file, "r");
    const buf = Buffer.alloc(len);
    fs.readSync(fd, buf, 0, len, stat.size - len);
    fs.closeSync(fd);
    let lines = buf.toString("utf8").split(/\r?\n/).filter(Boolean);
    if (stat.size > len && lines.length) lines = lines.slice(1);
    const dataLines = lines.filter(line => line !== header.join(",") && !line.startsWith(header[0] + ",")).slice(-limit);
    return dataLines.map(line => {
      const cells = line.split(",");
      const row = {};
      header.forEach((col, idx) => { row[col] = cells[idx]; });
      return row;
    });
  } catch (e) {
    return [];
  }
}

function parseCsvTimeMs(value) {
  if (value === undefined || value === null) return null;
  let s = String(value).trim();
  if (!s) return null;
  s = s.replace(" ", "T").replace(/(\.\d{3})\d+/, "$1");
  const ms = Date.parse(s);
  return Number.isFinite(ms) ? ms : null;
}

function shanghaiTime(ms) {
  if (!Number.isFinite(ms)) return null;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  }).format(new Date(ms));
}

function csvDataHealth(name, spec, now) {
  const exists = fs.existsSync(spec.file);
  const rows = readLastCsvRows(spec.file, name === "klines1m" ? 300 : 120);
  const times = rows
    .map(row => parseCsvTimeMs(row[spec.timeCol]))
    .filter(ms => Number.isFinite(ms))
    .sort((a, b) => a - b);
  const reasons = [];
  if (!exists) reasons.push(`${name}_missing`);
  if (exists && !times.length) reasons.push(`${name}_empty_or_unparseable`);

  const lastMs = times.length ? times[times.length - 1] : null;
  const ageMs = lastMs === null ? null : now - lastMs;
  if (ageMs !== null && ageMs > spec.maxAgeMs) reasons.push(`${name}_stale`);

  let maxObservedGapMs = null;
  for (let i = 1; i < times.length; i += 1) {
    const gap = times[i] - times[i - 1];
    if (maxObservedGapMs === null || gap > maxObservedGapMs) maxObservedGapMs = gap;
  }
  if (maxObservedGapMs !== null && maxObservedGapMs > spec.maxRecentGapMs) reasons.push(`${name}_recent_gap`);

  return {
    ok: reasons.length === 0,
    reasons,
    lastTime: lastMs === null ? null : new Date(lastMs).toISOString(),
    lastTimeMs: lastMs,
    lastTimeShanghai: lastMs === null ? null : shanghaiTime(lastMs),
    displayTimeZone: "Asia/Shanghai",
    ageMs,
    maxAgeMs: spec.maxAgeMs,
    rowsChecked: times.length,
    maxObservedGapMs,
    maxRecentGapMs: spec.maxRecentGapMs
  };
}

function signalTimeMs(sig) {
  if (!sig || typeof sig !== "object") return null;
  return parseCsvTimeMs(sig.actionable_time || sig.candle_close_time || sig.time);
}

function normalizeEpochMs(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return null;
  return n < 1000000000000 ? n * 1000 : n;
}

function signalSnapshotTimeMs(signals) {
  if (!signals || typeof signals !== "object") return null;
  const directMs = (
    signals._snapshot_time_ms ??
    signals._snapshotTimeMs ??
    signals._updated_at_ms ??
    signals.updatedAtMs
  );
  const normalized = normalizeEpochMs(directMs);
  if (normalized !== null) return normalized;
  const directTime = (
    signals._snapshot_time ??
    signals._snapshotTime ??
    signals._updated_at ??
    signals.updatedAt
  );
  return parseCsvTimeMs(directTime);
}

function signalFileMtimeMs() {
  try {
    return fs.statSync(SIGNAL_FILE).mtimeMs;
  } catch (e) {
    return null;
  }
}

function configuredMaxActionableLagMs() {
  return 60 * 1000;
}

function roundNullable(value, digits = 4) {
  const n = Number(value);
  if (!Number.isFinite(n)) return null;
  const mul = 10 ** digits;
  return Math.round(n * mul) / mul;
}

function signalReferencePrice(sig) {
  const timing = sig && sig.entry_timing;
  const candidates = [
    timing && timing.reference_price,
    sig && sig.entry,
    sig && sig.price,
    timing && timing.current_price
  ];
  for (const value of candidates) {
    const n = Number(value);
    if (Number.isFinite(n) && n > 0) return n;
  }
  return null;
}

function signalExecutionContext(sig) {
  if (!sig || typeof sig !== "object" || sig.shadow) return null;
  const actionableMs = signalTimeMs(sig);
  const maxLagMs = configuredMaxActionableLagMs();
  const now = Date.now();
  const referencePrice = signalReferencePrice(sig);
  const livePrice = Number(currentPrice);
  const priceChangeBps = (
    Number.isFinite(livePrice) && livePrice > 0 && Number.isFinite(referencePrice) && referencePrice > 0
  ) ? ((livePrice - referencePrice) / referencePrice) * 10000 : null;
  let directionMoveBps = null;
  if (priceChangeBps !== null && (sig.signal === "UP" || sig.signal === "DOWN")) {
    directionMoveBps = sig.signal === "UP" ? priceChangeBps : -priceChangeBps;
  }
  return {
    server_time: isoTime(now),
    current_price: Number.isFinite(livePrice) ? livePrice : null,
    signal_price: referencePrice,
    actionable_time_ms: actionableMs,
    actionable_age_ms: actionableMs === null ? null : now - actionableMs,
    max_actionable_lag_ms: maxLagMs,
    price_change_bps: roundNullable(priceChangeBps, 4),
    direction_move_bps: roundNullable(directionMoveBps, 4)
  };
}

function blockSignalForExecution(sig, context, reason) {
  return {
    ...sig,
    signal: null,
    confidence: null,
    execution_blocked: true,
    execution_block_reason: reason,
    blocked_signal: sig.blocked_signal || sig.signal || null,
    blocked_confidence: sig.blocked_confidence || (sig.confidence == null ? null : sig.confidence),
    execution_context: context
  };
}

function applyExecutionFreshnessGate(signals) {
  const out = { ...signals };
  for (const [strategyId, sig] of Object.entries(signals || {})) {
    if (strategyId.startsWith("_") || !sig || typeof sig !== "object" || sig.shadow) continue;
    const context = signalExecutionContext(sig);
    if (!context) continue;
    const next = { ...sig, execution_context: context };
    if (!sig.signal) {
      out[strategyId] = next;
      continue;
    }
    if (context.actionable_time_ms === null) {
      out[strategyId] = blockSignalForExecution(next, context, "signal_time_parse_failed");
      continue;
    }
    if (context.actionable_age_ms > context.max_actionable_lag_ms) {
      out[strategyId] = blockSignalForExecution(next, context, "stale_actionable_signal");
      continue;
    }
    out[strategyId] = next;
  }
  return out;
}

function dataHealthGate(signals) {
  const now = Date.now();
  const files = {};
  const reasons = [];
  for (const [name, spec] of Object.entries(DATA_HEALTH_FILES)) {
    files[name] = csvDataHealth(name, spec, now);
    if (name === "klines1m") reasons.push(...files[name].reasons);
  }

  const updateStatus = readJsonFile(DATA_UPDATE_STATUS_FILE, null);
  const updateFailed = !!(updateStatus && updateStatus.ok === false);

  const realSignals = Object.entries(signals || {})
    .filter(([key, sig]) => !key.startsWith("_") && sig && typeof sig === "object" && !sig.shadow);
  const signalTimes = realSignals
    .map(([strategyId, sig]) => ({ strategyId, ms: signalTimeMs(sig), blocked: !!sig.data_health_blocked }))
    .filter(row => Number.isFinite(row.ms));
  const signalFileExists = fs.existsSync(SIGNAL_FILE);
  const snapshotMs = signalSnapshotTimeMs(signals);
  const fileMtimeMs = signalFileExists ? signalFileMtimeMs() : null;
  const latestSignal = signalTimes.sort((a, b) => b.ms - a.ms)[0] || null;
  let freshnessMs = snapshotMs ?? fileMtimeMs ?? (latestSignal ? latestSignal.ms : null);
  let freshnessSource = null;
  if (snapshotMs !== null) freshnessSource = "snapshot_time";
  else if (fileMtimeMs !== null) freshnessSource = "file_mtime";
  else if (latestSignal) freshnessSource = "signal_time";
  const signalAgeMs = freshnessMs === null ? null : now - freshnessMs;

  if (!signalFileExists) {
    reasons.push("signal_file_missing");
  } else if (!realSignals.length || freshnessMs === null) {
    reasons.push("signal_snapshot_missing");
  }
  if (signalAgeMs !== null && signalAgeMs > SIGNAL_SNAPSHOT_MAX_AGE_MS) reasons.push("signal_snapshot_stale");
  if (realSignals.some(([, sig]) => sig && sig.data_health_blocked)) reasons.push("signal_process_data_health_blocked");

  const uniqueReasons = [...new Set(reasons)];
  return {
    allow: uniqueReasons.length === 0,
    blocked: uniqueReasons.length > 0,
    reasons: uniqueReasons,
    files,
    update: {
      running: dataUpdate.running,
      lastStart: dataUpdate.lastStart,
      lastFinish: dataUpdate.lastFinish,
      lastExitCode: dataUpdate.lastExitCode,
      lastError: dataUpdate.lastError,
      runs: dataUpdate.runs,
      failedButFilesFresh: updateFailed && uniqueReasons.length === 0,
      status: updateStatus
    },
    signal: {
      strategies: realSignals.map(([strategyId]) => strategyId),
      latestTime: freshnessMs === null ? null : new Date(freshnessMs).toISOString(),
      latestTimeMs: freshnessMs,
      latestTimeShanghai: freshnessMs === null ? null : shanghaiTime(freshnessMs),
      latestStrategyTime: latestSignal ? new Date(latestSignal.ms).toISOString() : null,
      latestStrategyTimeMs: latestSignal ? latestSignal.ms : null,
      latestStrategyTimeShanghai: latestSignal ? shanghaiTime(latestSignal.ms) : null,
      freshnessSource,
      displayTimeZone: "Asia/Shanghai",
      ageMs: signalAgeMs,
      maxAgeMs: SIGNAL_SNAPSHOT_MAX_AGE_MS
    }
  };
}

function autoTradeSafetyGate(config = tradeConfig) {
  // realTradingEnabled 是当前 30m 实盘总闸；废弃的 autoTrade_10m 不再参与判定，避免总闸已开却被旧字段误拦截。
  // 策略级硬白名单由 liveStrategyIds() 执行，BTC_30min 与所有 BTC_10min 仍然不可交易。
  const allow = !!(config && config.realTradingEnabled);
  return {
    allow,
    blocked: !allow,
    verdict: allow ? "manual_real_trading_enabled" : "manual_real_trading_disabled",
    requiredVerdict: "trade_config.realTradingEnabled",
    manualOverride: allow,
    overrideSource: allow ? "trade_config.realTradingEnabled" : null
  };
}

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

function amountForStrategy(strategyId, sig) {
  if (sig && sig.amount && sig.fixed_amount === true) return String(sig.amount);
  const baseAmount = amountForStrategyConfig(strategyId, tradeConfig);
  if (sig && sig.amount) return String(sig.amount);
  return String(baseAmount);
}

function currentStrategyVariants() {
  return strategyVariants(tradeConfig).filter(v => v.enabled);
}

function currentObservedStrategyIds() {
  return observedStrategyIds(tradeConfig);
}

function currentLiveStrategyIds() {
  return liveStrategyIds(tradeConfig);
}

const ENTRY_TIMING_ENABLED = true;
const ENTRY_TIMING_POLICIES = {
  BTC_10min_SAFE: {
    name: "direct_after_signal",
    type: "none"
  },
  BTC_10min_TAKER: {
    name: "pullback_0bp_then_confirm_5m",
    type: "pullback_then_confirm",
    pullbackBps: 0,
    maxWaitMin: 5,
    minPullbackDelayMs: 60000,
    minConfirmDelayMs: 60000
  },
  SECOND_VW_CONFIRM: {
    name: "eta_target_price",
    type: "eta_target_price",
    pullbackBps: null,
    maxWaitSec: null
  }
};
const entryTimingState = {};
const entryTimingAllowedSignals = {};

function entryTimingPolicyForStrategy(strategyId) {
  if (String(strategyId || "").startsWith("BTC_10min_SAFE")) return ENTRY_TIMING_POLICIES.BTC_10min_SAFE;
  if (String(strategyId || "").startsWith("BTC_10min_TAKER")) return ENTRY_TIMING_POLICIES.BTC_10min_TAKER;
  if (String(strategyId || "").startsWith("BTC_10min_SECOND_VW_")) return ENTRY_TIMING_POLICIES.SECOND_VW_CONFIRM;
  return null;
}

function isoTime(ms) {
  return new Date(ms).toISOString();
}

function attachDisplayTimes(sig) {
  if (!sig || typeof sig !== "object" || sig.shadow) return sig;
  const signalMs = parseCsvTimeMs(sig.time);
  const actionableMs = signalTimeMs(sig);
  return {
    ...sig,
    time_ms: signalMs,
    time_shanghai: signalMs === null ? null : shanghaiTime(signalMs),
    actionable_time_ms_display: actionableMs,
    actionable_time_shanghai: actionableMs === null ? null : shanghaiTime(actionableMs),
    display_time_zone: "Asia/Shanghai"
  };
}

function signalActionableMs(sig) {
  const t = sig && (sig.actionable_time || sig.candle_close_time || sig.time);
  const ms = t ? parseCsvTimeMs(t) : NaN;
  return Number.isFinite(ms) ? ms : 0;
}

function entryTimingKey(strategyId, sig) {
  return [
    strategyId,
    sig && sig.signal,
    sig && (sig.actionable_time || sig.candle_close_time || sig.time || "")
  ].join("|");
}

function directionOk(direction, later, reference) {
  if (!Number.isFinite(later) || !Number.isFinite(reference)) return false;
  return direction === "UP" ? later > reference : later < reference;
}

function pullbackOk(direction, price, reference, bps) {
  if (!Number.isFinite(price) || !Number.isFinite(reference)) return false;
  const move = reference * Number(bps || 0) / 10000;
  return direction === "UP" ? price <= reference - move : price >= reference + move;
}

function priceMoveBps(later, reference) {
  if (!Number.isFinite(later) || !Number.isFinite(reference) || reference <= 0) return null;
  return ((later - reference) / reference) * 10000;
}

function hasActionableTimeMargin(sig, marginMs = 15000) {
  const actionableMs = signalTimeMs(sig);
  if (!actionableMs) return true;
  return Date.now() - actionableMs <= configuredMaxActionableLagMs() - marginMs;
}

function blockSignalForEntryTiming(sig, state, reason) {
  const out = { ...sig };
  out.signal = null;
  out.confidence = null;
  out.entry_timing = {
    enabled: true,
    ok: false,
    reason,
    policy: state.policy.name,
    reference_price: state.referencePrice,
    current_price: currentPrice,
    started_at: isoTime(state.startedAt),
    expires_at: isoTime(state.expiresAt),
    pullback_seen: !!state.pullbackSeen,
    pullback_price: state.pullbackPrice || null,
    pullback_time: state.pullbackTime ? isoTime(state.pullbackTime) : null
  };
  return out;
}

function latchedEntrySignal(strategyId, sig) {
  const latched = entryTimingAllowedSignals[strategyId];
  if (!latched) return null;
  const now = Date.now();
  if (now > Number(latched.expiresAt || 0)) {
    delete entryTimingAllowedSignals[strategyId];
    return null;
  }
  if (sig && sig.signal && sig.signal !== latched.signal) return null;
  return {
    ...(sig || latched.signalPayload),
    ...latched.signalPayload,
    entry_timing: {
      ...(latched.signalPayload.entry_timing || {}),
      latched: true,
      latch_expires_at: isoTime(latched.expiresAt)
    }
  };
}

function allowSignalForEntryTiming(sig, state, reason) {
  const now = Date.now();
  if (!state.allowedAt) {
    state.allowedAt = now;
    state.allowedActionableTime = isoTime(now);
  } else if (now - state.allowedAt > SIGNAL_EXPIRY_MS) {
    delete entryTimingState[state.strategyId];
    return blockSignalForEntryTiming(sig, state, "entry_timing_entry_window_elapsed");
  }
  const allowed = {
    ...sig,
    actionable_time: state.allowedActionableTime,
    entry_timing: {
      enabled: true,
      ok: true,
      reason,
      policy: state.policy.name,
      original_actionable_time: state.originalActionableTime,
      reference_price: state.referencePrice,
      current_price: currentPrice,
      started_at: isoTime(state.startedAt),
      entry_time: state.allowedActionableTime,
      pullback_seen: !!state.pullbackSeen,
      pullback_price: state.pullbackPrice || null,
      pullback_time: state.pullbackTime ? isoTime(state.pullbackTime) : null
    }
  };
  entryTimingAllowedSignals[state.strategyId] = {
    signal: sig.signal,
    expiresAt: Math.min(state.allowedAt + SIGNAL_EXPIRY_MS, state.expiresAt + SIGNAL_EXPIRY_MS),
    signalPayload: allowed
  };
  return allowed;
}

function applyEntryTimingForSignal(strategyId, sig) {
  const policy = entryTimingPolicyForStrategy(strategyId);
  if (sig && sig.bypass_entry_timing) return sig;
  const latched = latchedEntrySignal(strategyId, sig);
  if (latched) return latched;
  if (!ENTRY_TIMING_ENABLED || !policy || !sig || !sig.signal) return sig;
  if (policy.type === "none") return sig;

  const now = Date.now();
  const actionableMs = signalActionableMs(sig);
  if (!actionableMs || actionableMs > now) {
    return {
      ...sig,
      signal: null,
      confidence: null,
      entry_timing: { enabled: true, ok: false, policy: policy.name, reason: "wait_actionable_time" }
    };
  }

  const key = entryTimingKey(strategyId, sig);
  let state = entryTimingState[strategyId];
  if (!state || state.key !== key) {
    const referencePrice = Number.isFinite(Number(currentPrice)) ? Number(currentPrice) : Number(sig.price);
    state = {
      key,
      policy,
      strategyId,
      signal: sig.signal,
      referencePrice,
      startedAt: now,
      originalActionableMs: actionableMs,
      originalActionableTime: sig.actionable_time || sig.candle_close_time || sig.time || null,
      earliestPullbackAt: actionableMs + Number(policy.minPullbackDelayMs || 0),
      expiresAt: actionableMs + Number(policy.maxWaitMin || 0) * 60000,
      pullbackSeen: false,
      pullbackPrice: null,
      pullbackTime: null
    };
    entryTimingState[strategyId] = state;
    if (policy.type === "eta_target_price") {
      const waitSec = Number(sig.eta_max_wait_sec || policy.maxWaitSec || 45);
      const targetBps = Number(sig.eta_target_bps || policy.pullbackBps || 2);
      state.expiresAt = actionableMs + waitSec * 1000;
      state.etaTargetBps = targetBps;
      state.etaMaxWaitSec = waitSec;
      state.etaTargetPrice = Number(sig.eta_entry_target_price);
    }
    appendJsonl(TRADE_AUDIT_FILE, {
      serverTime: now,
      event: "entry_timing_start",
      strategyId,
      signal: sig.signal,
      policy: policy.name,
      referencePrice,
      originalActionableTime: state.originalActionableTime,
      expiresAt: state.expiresAt
    });
  }

  if (!Number.isFinite(Number(currentPrice))) {
    return blockSignalForEntryTiming(sig, state, "missing_live_price");
  }
  if (now > state.expiresAt) {
    appendJsonl(TRADE_AUDIT_FILE, {
      serverTime: now,
      event: "entry_timing_expired",
      strategyId,
      signal: sig.signal,
      policy: policy.name,
      referencePrice: state.referencePrice,
      currentPrice
    });
    delete entryTimingState[strategyId];
    return blockSignalForEntryTiming(sig, state, "expired_without_confirmation");
  }
  if (now < state.earliestPullbackAt) {
    return blockSignalForEntryTiming(sig, state, "waiting_first_1m_check");
  }

  const price = Number(currentPrice);
  if (policy.type === "pullback_within") {
    if (pullbackOk(sig.signal, price, state.referencePrice, policy.pullbackBps)) {
      appendJsonl(TRADE_AUDIT_FILE, {
        serverTime: now,
        event: "entry_timing_allow",
        strategyId,
        signal: sig.signal,
        policy: policy.name,
        referencePrice: state.referencePrice,
        entryPrice: price
      });
      return allowSignalForEntryTiming(sig, state, "pullback_seen");
    }
    return blockSignalForEntryTiming(sig, state, "waiting_pullback");
  }

  if (policy.type === "eta_target_price") {
    const targetBps = Number(state.etaTargetBps || sig.eta_target_bps || 2);
    const targetPrice = Number(state.etaTargetPrice);
    const upConfirmBps = Number(sig.up_reversal_confirm_bps ?? 0.0);
    const upConfirmMaxSec = Number(sig.up_reversal_confirm_max_sec ?? 20);
    const hitTarget = Number.isFinite(targetPrice)
      ? (sig.signal === "UP" ? price <= targetPrice : price >= targetPrice)
      : pullbackOk(sig.signal, price, state.referencePrice, targetBps);
    if (sig.signal === "UP") {
      if (hitTarget && !state.upTargetHitAt) {
        state.upTargetHitAt = now;
        state.upTargetPrice = price;
        state.upTargetLow = price;
        appendJsonl(TRADE_AUDIT_FILE, {
          serverTime: now,
          event: "entry_timing_pullback_seen",
          strategyId,
          signal: sig.signal,
          policy: policy.name,
          referencePrice: state.referencePrice,
          targetPrice: Number.isFinite(targetPrice) ? targetPrice : null,
          targetBps,
          entryPrice: price,
          confirmBps: upConfirmBps,
          confirmMaxSec: upConfirmMaxSec
        });
      }
      if (!state.upTargetHitAt) {
        return blockSignalForEntryTiming(sig, state, "waiting_eta_target_price");
      }
      state.upTargetLow = Math.min(Number(state.upTargetLow || price), price);
      const reboundBps = priceMoveBps(price, Number(state.upTargetLow));
      const confirmOk = Number(upConfirmBps) <= 0 || (reboundBps !== null && reboundBps >= upConfirmBps);
      if (!confirmOk) {
        const reason = now > Number(state.upTargetHitAt) + upConfirmMaxSec * 1000
          ? "up_reversal_confirm_failed"
          : "waiting_up_reversal_confirm";
        return blockSignalForEntryTiming(sig, state, reason);
      }
      if (!hasActionableTimeMargin(sig)) {
        delete entryTimingState[strategyId];
        return blockSignalForEntryTiming(sig, state, "entry_timing_insufficient_actionable_margin");
      }
      appendJsonl(TRADE_AUDIT_FILE, {
        serverTime: now,
        event: "entry_timing_allow",
        strategyId,
        signal: sig.signal,
        policy: policy.name,
        referencePrice: state.referencePrice,
        targetPrice: Number.isFinite(targetPrice) ? targetPrice : null,
        targetBps,
        entryPrice: price,
        upTargetLow: state.upTargetLow,
        reboundBps
      });
      return allowSignalForEntryTiming(sig, state, "up_reversal_confirmed");
    }
    if (hitTarget) {
      if (!hasActionableTimeMargin(sig)) {
        delete entryTimingState[strategyId];
        return blockSignalForEntryTiming(sig, state, "entry_timing_insufficient_actionable_margin");
      }
      appendJsonl(TRADE_AUDIT_FILE, {
        serverTime: now,
        event: "entry_timing_allow",
        strategyId,
        signal: sig.signal,
        policy: policy.name,
        referencePrice: state.referencePrice,
        targetPrice: Number.isFinite(targetPrice) ? targetPrice : null,
        targetBps,
        entryPrice: price
      });
      return allowSignalForEntryTiming(sig, state, "eta_target_hit");
    }
    return blockSignalForEntryTiming(sig, state, "waiting_eta_target_price");
  }

  if (policy.type === "pullback_then_confirm") {
    if (!state.pullbackSeen) {
      if (pullbackOk(sig.signal, price, state.referencePrice, policy.pullbackBps)) {
        state.pullbackSeen = true;
        state.pullbackPrice = price;
        state.pullbackTime = now;
        appendJsonl(TRADE_AUDIT_FILE, {
          serverTime: now,
          event: "entry_timing_pullback_seen",
          strategyId,
          signal: sig.signal,
          policy: policy.name,
          referencePrice: state.referencePrice,
          pullbackPrice: price
        });
      }
      return blockSignalForEntryTiming(sig, state, "waiting_pullback");
    }
    if (now < state.pullbackTime + Number(policy.minConfirmDelayMs || 0)) {
      return blockSignalForEntryTiming(sig, state, "waiting_confirm_1m");
    }
    if (directionOk(sig.signal, price, Number(state.pullbackPrice))) {
      appendJsonl(TRADE_AUDIT_FILE, {
        serverTime: now,
        event: "entry_timing_allow",
        strategyId,
        signal: sig.signal,
        policy: policy.name,
        referencePrice: state.referencePrice,
        pullbackPrice: state.pullbackPrice,
        entryPrice: price
      });
      return allowSignalForEntryTiming(sig, state, "pullback_confirmed");
    }
    return blockSignalForEntryTiming(sig, state, "waiting_direction_confirm");
  }

  return sig;
}

function applyEntryTiming(signals) {
  const out = { ...signals };
  for (const strategyId of currentLiveStrategyIds()) {
    const next = applyEntryTimingForSignal(strategyId, signals[strategyId]);
    if (next) out[strategyId] = next;
    else delete out[strategyId];
    if (!signals[strategyId] || !signals[strategyId].signal) delete entryTimingState[strategyId];
  }
  return out;
}

function applyAutoTradeSafetyGate(signals) {
  const gate = autoTradeSafetyGate();
  if (!gate.blocked) return { signals, gate };
  const out = { ...signals };
  for (const [strategyId, sig] of Object.entries(signals)) {
    if (!sig || typeof sig !== "object" || sig.shadow) continue;
    out[strategyId] = {
      ...sig,
      signal: null,
      confidence: null,
      safety_blocked: true,
      safety_block_reason: gate.verdict,
      blocked_signal: sig.signal || null,
      blocked_confidence: sig.confidence == null ? null : sig.confidence
    };
  }
  return { signals: out, gate };
}

function applyDataHealthGate(signals, gate) {
  if (!gate.blocked) return { signals, gate };
  const blanketReasons = (gate.reasons || []).filter(reason => reason !== "signal_process_data_health_blocked");
  if (!blanketReasons.length) return { signals, gate };
  const out = { ...signals };
  for (const [strategyId, sig] of Object.entries(signals)) {
    if (!sig || typeof sig !== "object" || sig.shadow) continue;
    out[strategyId] = {
      ...sig,
      signal: null,
      confidence: null,
      data_health_blocked: true,
      data_health_block_reasons: blanketReasons,
      blocked_signal: sig.blocked_signal || sig.signal || null,
      blocked_confidence: sig.blocked_confidence || (sig.confidence == null ? null : sig.confidence)
    };
  }
  return { signals: out, gate };
}

function lossDensityPolicyForVariant(variant) {
  if (
    !variant
    || !["SECOND_NORMAL_STATE_V11", "SECOND_NORMAL_ROUTER_V21"].includes(variant.base)
    || variant.lossDensityEnabled !== true
  ) return null;
  const window = Math.max(2, Math.min(50, Number(variant.lossDensityWindow) || 6));
  const losses = Math.max(1, Math.min(window, Number(variant.lossDensityLosses) || 3));
  const defaultMinTrades = Math.min(window, Math.max(losses, losses + 1));
  const minTrades = Math.max(losses, Math.min(window, Number(variant.lossDensityMinTrades) || defaultMinTrades));
  const cooldownSec = Math.max(60, Math.min(86400, Number(variant.lossDensityCooldownSec) || 28800));
  const lookbackHours = Math.max(1, Math.min(720, Number(variant.lossDensityLookbackHours) || 72));
  const streakEnabled = variant.lossStreakEnabled === true || variant.base === "SECOND_NORMAL_ROUTER_V21";
  const streakCount = Math.max(1, Math.min(20, Number(variant.lossStreakCount) || 2));
  const streakCooldownSec = Math.max(60, Math.min(86400, Number(variant.lossStreakCooldownSec) || 3600));
  return { window, losses, minTrades, cooldownSec, lookbackHours, streakEnabled, streakCount, streakCooldownSec };
}

function recentLossDensityRows(now, lookbackHours) {
  const ttlMs = Number(process.env.LOSS_DENSITY_CACHE_MS || 15000);
  if (lossDensityCache.rows.length && now - lossDensityCache.checkedAt < ttlMs) {
    return lossDensityCache.rows;
  }
  const startMs = now - Math.max(1, Number(lookbackHours) || 72) * 60 * 60 * 1000;
  const endMs = now + 2 * 60 * 1000;
  try {
    const auditRows = eventStore.readJsonlRange(TRADE_AUDIT_FILE, startMs, endMs);
    const priceTicks = eventStore.readJsonlRange(PRICE_TICKS_FILE, startMs, endMs);
    const history = buildLiveOrderHistory({
      auditRows,
      priceTicks,
      serverTrades: trades,
      currentPrice,
      payoutRate: PAYOUT_RATE,
      now,
      mode: "page",
      kind: "all",
      limit: 300,
      pageSize: 300
    });
    lossDensityCache = {
      checkedAt: now,
      rows: Array.isArray(history.recent) ? history.recent : []
    };
  } catch (e) {
    lossDensityCache = { checkedAt: now, rows: [] };
  }
  return lossDensityCache.rows;
}

function isShadowHistoryRow(row) {
  return String(row && row.source || "").startsWith("shadow:") || row && row.event === "shadow_trade";
}

function settledRowsForLossDensity(strategyId, variant, policy, now) {
  const rows = recentLossDensityRows(now, policy.lookbackHours)
    .filter(row => row && row.strategyId === strategyId && (row.status === "won" || row.status === "lost"))
    .map(row => ({
      status: row.status,
      source: row.source || "",
      openTime: Number(row.openTime || 0),
      settleTime: Number(row.settleTime || row.openTime || 0)
    }))
    .filter(row => Number.isFinite(row.settleTime) && row.settleTime > 0)
    .sort((a, b) => a.settleTime - b.settleTime);

  const realRows = rows.filter(row => !isShadowHistoryRow(row));
  const shadowRows = rows.filter(row => isShadowHistoryRow(row));
  if (tradeConfig.realTradingEnabled && variant.tradeEnabled !== false) return realRows;
  if (shadowRows.length) return shadowRows;
  return rows;
}

function lossDensityStateForStrategy(strategyId, variant, now = Date.now()) {
  const policy = lossDensityPolicyForVariant(variant);
  if (!policy) return null;
  const rows = settledRowsForLossDensity(strategyId, variant, policy, now);
  const rolling = [];
  let streak = 0;
  let lastTrigger = null;
  let lastStreakTrigger = null;
  for (const row of rows) {
    streak = row.status === "lost" ? streak + 1 : 0;
    if (policy.streakEnabled && streak >= policy.streakCount) {
      lastStreakTrigger = {
        triggerTime: row.settleTime,
        lossCount: streak
      };
      streak = 0;
    }
    rolling.push(row.status);
    while (rolling.length > policy.window) rolling.shift();
    const lossCount = rolling.filter(status => status === "lost").length;
    if (rolling.length >= policy.minTrades && lossCount >= policy.losses) {
      lastTrigger = {
        triggerTime: row.settleTime,
        lossCount,
        windowStatuses: rolling.slice()
      };
      rolling.length = 0;
    }
  }
  const densityUntil = lastTrigger ? lastTrigger.triggerTime + policy.cooldownSec * 1000 : 0;
  const streakUntil = lastStreakTrigger ? lastStreakTrigger.triggerTime + policy.streakCooldownSec * 1000 : 0;
  const cooldownUntil = Math.max(densityUntil, streakUntil);
  const blocked = Boolean(cooldownUntil && now < cooldownUntil);
  return {
    enabled: true,
    blocked,
    policy,
    historyCount: rows.length,
    recentStatuses: rows.slice(-policy.window).map(row => row.status),
    lastTrigger,
    lastStreakTrigger,
    cooldownUntil: blocked ? cooldownUntil : null,
    cooldownUntilIso: blocked ? new Date(cooldownUntil).toISOString() : null,
    cooldownUntilShanghai: blocked ? shanghaiTime(cooldownUntil) : null
  };
}

function blockSignalForLossDensity(sig, state) {
  const cooldownDetail = `正态回归失效冷却：最近${state.policy.window}单窗口内，已观察至少${state.policy.minTrades}单且亏损达到${state.policy.losses}单，暂停到 ${state.cooldownUntilShanghai || state.cooldownUntilIso}`;
  return {
    ...({
    ...sig,
    signal: null,
    confidence: null,
    high_conf: false,
    loss_density_blocked: true,
    loss_density: state,
    blocked_signal: sig && (sig.blocked_signal || sig.signal) || null,
    blocked_confidence: sig && (sig.blocked_confidence || sig.confidence) || null,
    reason: "loss_density_cooldown",
    signal_detail: `正态回归失效冷却：最近${state.policy.window}单内亏损达到${state.policy.losses}单，暂停到 ${state.cooldownUntilShanghai || state.cooldownUntilIso}`
  }),
    signal_detail: cooldownDetail
  };
}

function applyLossDensityGate(signals) {
  const variants = currentStrategyVariants();
  const byId = new Map(variants.map(variant => [variant.id, variant]));
  const out = { ...signals };
  const states = {};
  const now = Date.now();
  for (const strategyId of currentObservedStrategyIds()) {
    const variant = byId.get(strategyId);
    const state = lossDensityStateForStrategy(strategyId, variant, now);
    if (!state) continue;
    states[strategyId] = state;
    const sig = out[strategyId];
    if (!sig || typeof sig !== "object") continue;
    out[strategyId] = { ...sig, loss_density: state };
    if (state.blocked && sig.signal) {
      out[strategyId] = blockSignalForLossDensity(sig, state);
    }
  }
  return { signals: out, gate: { strategies: states } };
}

function recentExecutionFailureRows(now) {
  const ttlMs = Number(process.env.EXECUTION_FAILURE_CACHE_MS || 5000);
  if (executionFailureCache.rows.length && now - executionFailureCache.checkedAt < ttlMs) {
    return executionFailureCache.rows;
  }
  const lookbackMs = Math.max(
    10 * 60 * 1000,
    Number(process.env.EXECUTION_FAILURE_LOOKBACK_MS || 3 * 60 * 60 * 1000)
  );
  try {
    executionFailureCache = {
      checkedAt: now,
      rows: eventStore.readJsonlRange(TRADE_AUDIT_FILE, now - lookbackMs, now + 60 * 1000)
    };
  } catch (e) {
    executionFailureCache = { checkedAt: now, rows: [] };
  }
  return executionFailureCache.rows;
}

function executionFailureStateForStrategy(strategyId, now = Date.now()) {
  const baseCooldownMs = Math.max(
    60 * 1000,
    Number(process.env.EXECUTION_FAILURE_COOLDOWN_MS || 10 * 60 * 1000)
  );
  const repeatedWindowMs = Math.max(
    10 * 60 * 1000,
    Number(process.env.EXECUTION_FAILURE_REPEAT_WINDOW_MS || 3 * 60 * 60 * 1000)
  );
  const repeatedThreshold = Math.max(
    2,
    Number(process.env.EXECUTION_FAILURE_REPEAT_THRESHOLD || 3)
  );
  const repeatedCooldownMs = Math.max(
    baseCooldownMs,
    Number(process.env.EXECUTION_FAILURE_REPEAT_COOLDOWN_MS || 60 * 60 * 1000)
  );
  const amountFailedCooldownMs = Math.max(
    baseCooldownMs,
    Number(process.env.EXECUTION_AMOUNT_FAILED_COOLDOWN_MS || 30 * 60 * 1000)
  );
  const strategyRows = recentExecutionFailureRows(now)
    .filter(row => row && row.strategyId === strategyId)
    .map(row => ({
      event: row.event,
      reason: row.reason || "unknown",
      serverTime: Number(row.serverTime || row.clientTime || 0),
      amount: row.amount,
      duration: row.duration,
      device: row.device || null
    }))
    .filter(row => Number.isFinite(row.serverTime) && row.serverTime > 0)
    .sort((a, b) => a.serverTime - b.serverTime);
  const lastSuccess = [...strategyRows].reverse().find(row => row.event === "order_done") || null;
  const rows = strategyRows
    .filter(row => row.event === "order_abort" || row.event === "order_unverified")
    .filter(row => row.reason !== "stale_actionable_signal_before_click")
    .filter(row => !lastSuccess || row.serverTime > lastSuccess.serverTime);
  const last = rows[rows.length - 1] || null;
  const recentSinceWindowStart = last
    ? rows.filter(row => row.serverTime >= last.serverTime - repeatedWindowMs)
    : [];
  let cooldownMs = baseCooldownMs;
  let mode = "single_failure";
  if (last && last.reason === "amount_failed") {
    cooldownMs = Math.max(cooldownMs, amountFailedCooldownMs);
    mode = "amount_failed";
  }
  if (recentSinceWindowStart.length >= repeatedThreshold) {
    cooldownMs = Math.max(cooldownMs, repeatedCooldownMs);
    mode = "repeated_failure";
  }
  const cooldownUntil = last ? last.serverTime + cooldownMs : 0;
  const blocked = Boolean(last && now < cooldownUntil);
  return {
    enabled: true,
    blocked,
    recentCount: rows.length,
    recentCountInWindow: recentSinceWindowStart.length,
    last,
    lastReasonLabel: last ? executionFailureReasonLabel(last.reason) : null,
    lastSuccessTime: lastSuccess ? lastSuccess.serverTime : null,
    lastSuccessIso: lastSuccess ? new Date(lastSuccess.serverTime).toISOString() : null,
    cooldownMs,
    mode,
    policy: {
      baseCooldownMs,
      amountFailedCooldownMs,
      repeatedWindowMs,
      repeatedThreshold,
      repeatedCooldownMs
    },
    cooldownUntil: blocked ? cooldownUntil : null,
    cooldownUntilIso: blocked ? new Date(cooldownUntil).toISOString() : null,
    cooldownUntilShanghai: blocked ? shanghaiTime(cooldownUntil) : null
  };
}

function executionFailureReasonLabel(reason) {
  const key = String(reason || "");
  const map = {
    amount_failed: "金额输入失败",
    duration_failed: "周期选择失败",
    cannot_wake_screen: "平板屏幕唤醒失败",
    balance_before_unavailable: "下单前余额读取失败",
    balance_not_decreased: "余额未变化，无法确认成交",
    confirm_not_found: "确认按钮没找到",
    signal_time_parse_failed_before_click: "信号时间解析失败",
    stale_actionable_signal_before_click: "点击前信号已过期",
    order_failed: "下单执行失败",
    unknown: "未知执行失败"
  };
  return map[key] || key || "未知执行失败";
}

function blockSignalForExecutionFailure(sig, state) {
  const reason = state && state.last ? state.last.reason : "order_failed";
  const label = executionFailureReasonLabel(reason);
  return {
    ...sig,
    signal: null,
    confidence: null,
    execution_failure_blocked: true,
    execution_failure: state,
    execution_failure_label: label,
    blocked_signal: sig && (sig.blocked_signal || sig.signal) || null,
    blocked_confidence: sig && (sig.blocked_confidence || sig.confidence) || null,
    reason: "recent_order_failure_cooldown",
    signal_detail: `最近实盘下单失败：${label}，暂停到 ${state.cooldownUntilShanghai || state.cooldownUntilIso}`
  };
}

function applyExecutionFailureGate(signals) {
  const out = { ...signals };
  const states = {};
  const now = Date.now();
  for (const strategyId of currentLiveStrategyIds()) {
    const state = executionFailureStateForStrategy(strategyId, now);
    states[strategyId] = state;
    const sig = out[strategyId];
    if (!sig || typeof sig !== "object") continue;
    out[strategyId] = { ...sig, execution_failure: state };
    if (state.blocked && sig.signal) {
      out[strategyId] = blockSignalForExecutionFailure(sig, state);
    }
  }
  return { signals: out, gate: { strategies: states } };
}

function buildSignalResponse(source = "") {
  const rawSignals = fs.existsSync(SIGNAL_FILE) ? JSON.parse(fs.readFileSync(SIGNAL_FILE, "utf8")) : {};
  const observedIds = currentObservedStrategyIds();
  const liveIds = currentLiveStrategyIds();
  const signalMeta = Object.fromEntries(
    Object.entries(rawSignals).filter(([key]) => key.startsWith("_"))
  );
  // 30m 配置是主链路；原始文件中的旧 10m 信号仍保留给页面只读查看。
  const legacy10mIds = Object.keys(rawSignals).filter(key => /^(POC_|MID_|EVO_|BTC_10min)/.test(key));
  const displayIds = [...new Set([...observedIds, ...legacy10mIds])];
  const liveRawSignals = {
    ...signalMeta,
    ...Object.fromEntries(
      displayIds
        .filter(strategyId => rawSignals[strategyId])
        .map(strategyId => [strategyId, rawSignals[strategyId]])
    )
  };
  const timedSignals = applyEntryTiming(liveRawSignals);
  const freshSignals = applyExecutionFreshnessGate(timedSignals);
  const health = applyDataHealthGate(freshSignals, dataHealthGate(freshSignals));
  const lossDensity = applyLossDensityGate(health.signals);
  const executionFailure = applyExecutionFailureGate(lossDensity.signals);
  const safety = source === "dashboard"
    ? { signals: executionFailure.signals, gate: autoTradeSafetyGate() }
    : applyAutoTradeSafetyGate(executionFailure.signals);
  
  // Clone signals to prevent modifying in-memory cache
  const signals = JSON.parse(JSON.stringify(safety.signals));
  for (const [strategyId, sig] of Object.entries(signals)) {
    if (!strategyId.startsWith("_")) signals[strategyId] = attachDisplayTimes(sig);
  }
  
  // If not requested by the dashboard, apply safety overrides to prevent real trades if disabled.
  if (source !== "dashboard") {
    if (!tradeConfig.realTradingEnabled) {
      for (const strategyId of observedIds) {
        if (signals[strategyId]) signals[strategyId].signal = null;
      }
    } else {
      const tradeable = new Set(liveIds);
      for (const strategyId of observedIds) {
        if (!tradeable.has(strategyId) && signals[strategyId]) signals[strategyId].signal = null;
      }
    }
  } else {
    const tradeable = new Set(liveIds);
    for (const strategyId of observedIds) {
      if (signals[strategyId]) {
        signals[strategyId].trade_enabled = tradeable.has(strategyId);
      }
    }
  }
  if (source === "autojs" || source === "tablet") {
    const tradeable = new Set(liveIds);
    for (const strategyId of observedIds) {
      if (!tradeable.has(strategyId) && signals[strategyId]) {
        if (signals[strategyId]) signals[strategyId].signal = null;
      }
    }
  }

  const strategyAmounts = {};
  for (const strategyId of observedIds) {
    if (signals[strategyId]) strategyAmounts[strategyId] = amountForStrategy(strategyId, signals[strategyId]);
  }
  const legacySig = observedIds.map(id => signals[id]).find(Boolean);
  const legacyAmount = legacySig ? amountForStrategy(legacySig.strategy_id, legacySig) : String(tradeConfig.amount);

  // Supply backward compatible config keys for old tablet/scripts
  const configCopy = {
    ...publicTradeConfig(tradeConfig),
    // 兼容字段只反映当前实盘总闸；实际可入队策略仍以 liveStrategyIds() 的唯一候选白名单为准。
    autoTrade: tradeConfig.realTradingEnabled
  };

  // signal API 同步返回策略角色和观察模式，前端无需根据名称猜测主次关系。
  const variantsById = new Map(currentStrategyVariants().map(variant => [variant.id, variant]));
  const displayVariants = [];
  for (const strategyId of displayIds) {
    if (!signals[strategyId]) continue;
    const variant = variantsById.get(strategyId);
    const isStableAnchor = strategyId === "BTC_30min";
    const isShadow30m = !isStableAnchor && /^BTC_30min_.+/.test(strategyId);
    const displayVariant = variant || {
      id: strategyId,
      base: isStableAnchor || isShadow30m ? "POC_NORMAL" : strategyId,
      label: isStableAnchor ? "BTC_30min 稳定底座" : isShadow30m ? "30m Shadow 候选" : `${strategyId}（旧 10m 链路）`,
      role: isStableAnchor ? "stable_anchor" : isShadow30m ? "launch_candidate" : "legacy_10m",
      observationMode: isStableAnchor ? "watch" : "shadow",
      enabled: true,
      tradeEnabled: false,
      duration: isStableAnchor || isShadow30m ? "30" : "10",
    };
    displayVariants.push(displayVariant);
    signals[strategyId].strategy_role = displayVariant.role;
    signals[strategyId].observation_mode = displayVariant.observationMode;
    signals[strategyId].trade_enabled = variant ? liveIds.includes(strategyId) : false;
  }

  return {
    ...signals,
    _config: configCopy,
    _strategyVariants: displayVariants,
    _strategyAmounts: strategyAmounts,
    _signalAmount: legacyAmount,
    _entryTimingEnabled: ENTRY_TIMING_ENABLED,
    _entryTimingPolicies: Object.fromEntries(observedIds.map(id => [id, entryTimingPolicyForStrategy(id)])),
    _execution: {
      serverTime: isoTime(Date.now()),
      serverTimeMs: Date.now(),
      serverTimeShanghai: shanghaiTime(Date.now()),
      displayTimeZone: "Asia/Shanghai",
      currentPrice: Number.isFinite(Number(currentPrice)) ? Number(currentPrice) : null,
      maxActionableLagMs: configuredMaxActionableLagMs()
    },
    _dataHealthGate: health.gate,
    _lossDensityGate: lossDensity.gate,
    _executionFailureGate: executionFailure.gate,
    _autoTradeSafetyGate: safety.gate
  };
}

app.get("/api/signal", (req, res) => {
  try {
    const source = String(req.query.source || "");
    const signals = buildSignalResponse(source);
    if (source === "autojs" || source === "tablet") {
      try { mirrorTabletSignalsToShadow(signals); }
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
  let signals = {};
  try { signals = fs.existsSync(SIGNAL_FILE) ? JSON.parse(fs.readFileSync(SIGNAL_FILE, "utf8")) : {}; } catch (e) {}
  res.json(dataHealthGate(signals));
});

app.get("/api/second-data-health", (req, res) => {
  const status = readJsonFile(SECOND_DATA_STATUS_FILE, {});
  let file = { exists: false, size: 0, mtime: null };
  try {
    const stat = fs.statSync(SECOND_DATA_FILE);
    file = { exists: true, size: stat.size, mtime: stat.mtime.toISOString() };
  } catch (e) {}
  const lastTs = status.last_ts ? Date.parse(status.last_ts) : null;
  const ageMs = Number.isFinite(lastTs) ? Date.now() - lastTs : null;
  const maxAgeMs = Number(process.env.SECOND_DATA_MAX_AGE_MS || 120000);
  res.json({
    ok: !!status.ok && file.exists && ageMs !== null && ageMs <= maxAgeMs,
    ageMs,
    maxAgeMs,
    status: {
      ...status,
      last_ts_ms: status.last_ts ? Date.parse(status.last_ts) : null,
      last_ts_shanghai: status.last_ts ? shanghaiTime(Date.parse(status.last_ts)) : null,
      display_time_zone: "Asia/Shanghai"
    },
    file
  });
});

app.get("/api/orderbook-health", (req, res) => {
  const status = readJsonFile(ORDERBOOK_STATUS_FILE, {});
  let file = { exists: false, size: 0, mtime: null };
  try {
    const stat = fs.statSync(ORDERBOOK_FILE);
    file = { exists: true, size: stat.size, mtime: stat.mtime.toISOString() };
  } catch (e) {}
  const lastTs = status.last_ts ? Date.parse(status.last_ts) : null;
  const ageMs = Number.isFinite(lastTs) ? Date.now() - lastTs : null;
  const maxAgeMs = Number(process.env.ORDERBOOK_MAX_AGE_MS || 30000);
  res.json({
    ok: !!status.ok && file.exists && ageMs !== null && ageMs <= maxAgeMs,
    ageMs,
    maxAgeMs,
    status: {
      ...status,
      last_ts_ms: status.last_ts ? Date.parse(status.last_ts) : null,
      last_ts_shanghai: status.last_ts ? shanghaiTime(Date.parse(status.last_ts)) : null,
      display_time_zone: "Asia/Shanghai"
    },
    file
  });
});

app.get("/api/auction-data-health", (req, res) => {
  const status = readJsonFile(AUCTION_DATA_STATUS_FILE, {});
  const streams = status && typeof status.streams === "object" ? status.streams : {};
  const eventAgeMs = Number(status.event_age_ms);
  const depthAgeMs = Number(streams.depth_updates && streams.depth_updates.age_ms);
  const maxAgeMs = Number(process.env.AUCTION_DATA_MAX_AGE_MS || 15000);
  const depthMaxAgeMs = Number(process.env.AUCTION_DEPTH_MAX_AGE_MS || 15000);
  const statusTime = status.updated_at ? Date.parse(status.updated_at) : null;
  const statusAgeMs = Number.isFinite(statusTime) ? Date.now() - statusTime : null;
  res.json({
    ok: !!status.ok
      && Number.isFinite(eventAgeMs) && eventAgeMs <= maxAgeMs
      && Number.isFinite(depthAgeMs) && depthAgeMs <= depthMaxAgeMs
      && Number.isFinite(statusAgeMs) && statusAgeMs <= maxAgeMs,
    eventAgeMs: Number.isFinite(eventAgeMs) ? eventAgeMs : null,
    depthAgeMs: Number.isFinite(depthAgeMs) ? depthAgeMs : null,
    statusAgeMs,
    maxAgeMs,
    depthMaxAgeMs,
    status: {
      ...status,
      updated_at_shanghai: Number.isFinite(statusTime) ? shanghaiTime(statusTime) : null,
      display_time_zone: "Asia/Shanghai"
    }
  });
});

app.get("/api/orderbook-prediction", (req, res) => {
  const status = readJsonFile(ORDERBOOK_PREDICTION_STATUS_FILE, {});
  const lastTs = status.last_ts ? Date.parse(status.last_ts) : null;
  const ageMs = Number.isFinite(lastTs) ? Date.now() - lastTs : null;
  const maxAgeMs = Number(process.env.ORDERBOOK_PREDICTION_MAX_AGE_MS || 30000);
  res.json({
    ok: !!status.ok && ageMs !== null && ageMs <= maxAgeMs,
    ageMs,
    maxAgeMs,
    status: {
      ...status,
      last_ts_shanghai: status.last_ts ? shanghaiTime(Date.parse(status.last_ts)) : null,
      display_time_zone: "Asia/Shanghai"
    }
  });
});

function orderbookPredictionSnapshot() {
  const status = readJsonFile(ORDERBOOK_PREDICTION_STATUS_FILE, {});
  const lastTs = status.last_ts ? Date.parse(status.last_ts) : null;
  const ageMs = Number.isFinite(lastTs) ? Date.now() - lastTs : null;
  const maxAgeMs = Number(process.env.ORDERBOOK_PREDICTION_MAX_AGE_MS || 30000);
  return {
    ok: !!status.ok && ageMs !== null && ageMs <= maxAgeMs,
    ageMs,
    maxAgeMs,
    status
  };
}

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

function localHttpUrls() {
  const nets = os.networkInterfaces();
  const urls = [];
  for (const [name, items] of Object.entries(nets)) {
    for (const ni of items || []) {
      if (ni.family !== "IPv4" || ni.internal) continue;
      urls.push({
        interface: name,
        address: ni.address,
        url: `http://${ni.address}:${PORT}`
      });
    }
  }
  return urls;
}

function autoScriptVersion() {
  try {
    const text = fs.readFileSync(AUTO_SCRIPT_FILE, "utf8");
    const m = text.match(/SCRIPT_VERSION\s*=\s*["']([^"']+)["']/);
    return m ? m[1] : null;
  } catch (e) {
    return null;
  }
}

function runtimeInfo() {
  const urls = localHttpUrls();
  const preferred = urls.find(x => x.address.startsWith("192.168.")) || urls[0] || null;
  const publicBase = String(process.env.PUBLIC_BASE_URL || "").replace(/\/+$/, "");
  const base = publicBase || (preferred ? preferred.url : `http://127.0.0.1:${PORT}`);
  return {
    serverId: eventStore.serverId,
    dataDir: DATA_DIR,
    port: Number(PORT),
    urls,
    tabletUrl: base,
    tabletPageUrl: `${base}/tablet.html`,
    auditUrl: `${base}/api/trade-audit`,
    signalUrl: `${base}/api/signal`,
    scriptUrl: `${base}/auto_btc.js`,
    loaderUrl: `${base}/auto_btc_loader.js`,
    bootstrapUrl: `${base}/auto_btc_bootstrap.js`,
    scriptVersion: autoScriptVersion(),
    serverSimTradingEnabled: SERVER_SIM_TRADING_ENABLED,
    managedProcessesEnabled: MANAGED_PROCESSES_ENABLED,
    apiAuth: apiAuth.publicInfo()
  };
}

function tabletDiagnostics() {
  const runtime = runtimeInfo();
  const rows = tailJsonl(TRADE_AUDIT_FILE, 200);
  const autojsEventNames = new Set([
    "autojs_loader_start",
    "autojs_loader_exec",
    "autojs_loader_error",
    "autojs_start",
    "autojs_keepalive_status",
    "autojs_heartbeat",
    "runtime_screen_wake",
    "runtime_relaunch_app",
    "runtime_keepalive_failed",
    "signal_tradeable",
    "signal_skipped",
    "order_attempt",
    "order_abort",
    "order_unverified",
    "order_done",
    "runtime_loop_error"
  ]);
  const autojsRows = rows.filter(r => autojsEventNames.has(r.event));
  const latestTabletPagePing = [...rows].reverse().find(r => (
    r.event === "tablet_page_ping" && r.source !== "codex_local_probe"
  )) || null;
  const latestEvent = autojsRows.length ? autojsRows[autojsRows.length - 1] : null;
  const latestHeartbeat = [...autojsRows].reverse().find(r => r.event === "autojs_heartbeat") || null;
  const latestKeepAliveStatus = [...autojsRows].reverse().find(r => (
    r.event === "autojs_keepalive_status" ||
    r.event === "runtime_screen_wake" ||
    r.event === "runtime_relaunch_app" ||
    r.event === "runtime_keepalive_failed" ||
    (r.event === "autojs_heartbeat" && r.keepAlive)
  )) || null;
  const latestKeepAliveFailure = [...autojsRows].reverse().find(r => r.event === "runtime_keepalive_failed") || null;
  const latestOrderDone = [...autojsRows].reverse().find(r => r.event === "order_done") || null;
  const keepAliveStatus = latestKeepAliveStatus?.keepAlive || latestHeartbeat?.keepAlive || null;
  const runningScriptVersion = latestHeartbeat?.version || latestEvent?.version || null;
  const scriptVersionMatches = !runtime.scriptVersion || !runningScriptVersion || runtime.scriptVersion === runningScriptVersion;
  const now = Date.now();
  const ageOf = row => row && row.serverTime ? now - Number(row.serverTime) : null;
  const heartbeatAgeMs = ageOf(latestHeartbeat);
  const keepAliveStatusAgeMs = ageOf(latestKeepAliveStatus);
  const keepAliveFailureAgeMs = ageOf(latestKeepAliveFailure);
  const eventAgeMs = ageOf(latestEvent);
  const tabletPagePingAgeMs = ageOf(latestTabletPagePing);
  const balanceAgeMs = realBalance && realBalance.time ? now - Number(realBalance.time) : null;
  let status = "waiting_for_autojs_events";
  if (latestOrderDone) status = "has_order_done";
  else if (heartbeatAgeMs != null && heartbeatAgeMs <= 120000) status = "autojs_online_waiting_for_order_done";
  else if (latestEvent) status = "autojs_seen_waiting_for_order_done";

  const checks = {
    serverReachable: true,
    latestScriptServed: !!runtime.scriptVersion,
    servedScriptVersion: runtime.scriptVersion,
    runningScriptVersion,
    scriptVersionMatches,
    tabletPageSeen: tabletPagePingAgeMs != null && tabletPagePingAgeMs <= 120000,
    loaderStarted: autojsRows.some(r => r.event === "autojs_loader_start"),
    loaderError: autojsRows.some(r => r.event === "autojs_loader_error"),
    autojsStarted: autojsRows.some(r => r.event === "autojs_start"),
    heartbeatOnline: heartbeatAgeMs != null && heartbeatAgeMs <= 120000,
    keepAliveReported: !!keepAliveStatus,
    writeSettingsGranted: keepAliveStatus?.writeSettingsGranted === true,
    screenTimeoutNever: Number(keepAliveStatus?.screenOffTimeoutMs) >= 2147483000,
    batteryOptimizationIgnored: keepAliveStatus?.batteryOptimizationIgnored === true,
    screenOn: keepAliveStatus?.screenOn === true,
    keepAliveFailureRecent: keepAliveFailureAgeMs != null && keepAliveFailureAgeMs <= 600000,
    balanceRecent: balanceAgeMs != null && balanceAgeMs <= 120000,
    orderDoneSeen: !!latestOrderDone
  };
  const nextAction = checks.heartbeatOnline && !checks.scriptVersionMatches
    ? `服务器脚本已更新为 ${runtime.scriptVersion}，平板仍在运行 ${runningScriptVersion || "未知版本"}；请重启一次 AutoJS 脚本加载新版。`
    : checks.heartbeatOnline && checks.scriptVersionMatches
      ? `平板已运行新版 ${runningScriptVersion || runtime.scriptVersion}，等待下一次信号或手动5U测试单。`
    : !checks.tabletPageSeen && !checks.heartbeatOnline
      ? `Open ${runtime.tabletPageUrl} on the tablet to confirm tablet network access.`
    : checks.loaderError && !checks.autojsStarted
      ? "Loader ran but failed; check AutoJS log for autojs_loader_error and retry the loader URL."
    : !checks.loaderStarted
      ? `Tablet browser reaches server; run loader ${runtime.loaderUrl} or bootstrap ${runtime.bootstrapUrl} in AutoJS.`
    : !checks.autojsStarted
      ? `Loader ran; wait for autojs_start or run latest auto_btc.js from ${runtime.scriptUrl} directly.`
    : !checks.heartbeatOnline
      ? "AutoJS was seen but heartbeat is stale; restart the tablet script and confirm it can POST /api/trade-audit."
      : checks.keepAliveFailureRecent
        ? "AutoJS recently reported a keepalive failure; check tablet screen, lock state, and battery/background permissions."
      : keepAliveStatus && keepAliveStatus.writeSettingsGranted === false
        ? "AutoJS is online but lacks WRITE_SETTINGS; grant Modify system settings so screen timeout can be set to never."
      : keepAliveStatus && keepAliveStatus.batteryOptimizationIgnored === false
        ? "AutoJS is online but battery optimization may still stop it; allow unrestricted/background running on the tablet."
      : !checks.orderDoneSeen
        ? "Tablet is online; wait for the next tradeable signal or place a small manual test order."
        : "Live order audit is active; collect settled trades before raising stake.";

  return {
    status,
    runtime,
    checks,
    nextAction,
    latestEvent,
    latestEventAgeMs: eventAgeMs,
    latestTabletPagePing,
    latestTabletPagePingAgeMs: tabletPagePingAgeMs,
    latestHeartbeat,
    latestHeartbeatAgeMs: heartbeatAgeMs,
    latestKeepAliveStatus,
    latestKeepAliveStatusAgeMs: keepAliveStatusAgeMs,
    latestKeepAliveFailure,
    latestKeepAliveFailureAgeMs: keepAliveFailureAgeMs,
    keepAliveStatus,
    latestOrderDone,
    balance: realBalance,
    balanceAgeMs,
    recentAutojsEvents: autojsRows.slice(-20)
  };
}

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
  return buildLiveOrderHistory({
    auditRows,
    priceTicks,
    serverTrades: trades,
    currentPrice,
    payoutRate: PAYOUT_RATE,
    mode,
    day: dayRange ? dayRange.day : opts.day,
    availableDays,
    ...opts
  });
}

app.get("/api/runtime", (req, res) => {
  res.json(runtimeInfo());
});

app.get("/api/signal-service", (req, res) => {
  res.json({
    serverId: eventStore.serverId,
    ...signalService,
    running: !!signalService.pid,
    python: PYTHON_EXE,
    script: SIGNAL_SCRIPT_FILE,
    dataUpdate
  });
});

app.get("/api/tablet-diagnostics", (req, res) => {
  res.json(tabletDiagnostics());
});

app.post("/api/tablet-page-ping", express.json({ limit: "20kb" }), (req, res) => {
  const item = {
    serverTime: Date.now(),
    event: "tablet_page_ping",
    ip: req.ip,
    userAgent: req.get("user-agent") || null,
    runtime: runtimeInfo(),
    ...(req.body && typeof req.body === "object" ? req.body : {})
  };
  appendJsonl(TRADE_AUDIT_FILE, item);
  res.json({ ok: true, item });
});

app.get("/auto_btc.js", (req, res) => {
  res.type("text/javascript").sendFile(AUTO_SCRIPT_FILE);
});

app.get("/auto_btc_loader.js", (req, res) => {
  const runtime = runtimeInfo();
  const code = `"auto";

var LOADER_VERSION = "2026-06-07-loader1";
var SCRIPT_URL = "${runtime.scriptUrl}";
var AUDIT_URL = "${runtime.auditUrl}";

function logMsg(msg) {
    var t = new Date().toLocaleTimeString();
    console.log("[loader " + t + "] " + msg);
}

function postLoaderEvent(event, extra) {
    try {
        var payload = {
            event: event,
            loaderVersion: LOADER_VERSION,
            scriptUrl: SCRIPT_URL,
            clientTime: Date.now(),
            device: device.brand + "_" + (device.model || "").replace(/\\s+/g, "")
        };
        if (extra) {
            for (var k in extra) payload[k] = extra[k];
        }
        try {
            http.postJson(AUDIT_URL, payload, { timeout: 2500 });
        } catch (e1) {
            http.post(AUDIT_URL, { payload: JSON.stringify(payload) }, { timeout: 2500 });
        }
    } catch (e) {}
}

logMsg("fetch " + SCRIPT_URL);
postLoaderEvent("autojs_loader_start", {});
try {
    var res = http.get(SCRIPT_URL, { timeout: 8000 });
    if (!res || res.statusCode != 200) {
        var code = res ? res.statusCode : "no response";
        logMsg("download failed: " + code);
        postLoaderEvent("autojs_loader_error", { reason: "download_failed", statusCode: code });
        toast("auto_btc download failed: " + code);
        exit();
    }
    var script = res.body.string();
    logMsg("downloaded " + script.length + " bytes");
    postLoaderEvent("autojs_loader_exec", { bytes: script.length });
    engines.execScript("auto_btc_latest", script);
} catch (e) {
    logMsg("loader error: " + e);
    postLoaderEvent("autojs_loader_error", { reason: String(e) });
    toast("auto_btc loader error");
}
`;
  res.type("text/javascript").send(code);
});

app.get("/auto_btc_bootstrap.js", (req, res) => {
  const runtime = runtimeInfo();
  const code = `"auto";

var LOADER_URL = "${runtime.loaderUrl}";

console.log("[bootstrap] fetch " + LOADER_URL);
try {
    var res = http.get(LOADER_URL, { timeout: 8000 });
    if (!res || res.statusCode != 200) {
        var code = res ? res.statusCode : "no response";
        console.log("[bootstrap] loader download failed: " + code);
        toast("loader download failed: " + code);
        exit();
    }
    var loader = res.body.string();
    console.log("[bootstrap] downloaded loader " + loader.length + " bytes");
    engines.execScript("auto_btc_loader_remote", loader);
} catch (e) {
    console.log("[bootstrap] error: " + e);
    toast("bootstrap error");
}
`;
  res.type("text/javascript").send(code);
});

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
let trades = [];
let nextTradeId = 1;
let account = { balance: 10000.0, totalTrades: 0, wins: 0, losses: 0, totalPnl: 0 };
let lastSignals = {};
let shadowTrades = [];
let nextShadowTradeId = 1;
let lastShadowSignals = {};
let shadowSignalKeys = new Set();
let shadowSignalKeysLoaded = false;
let lastStrategyTradeAt = {};
let autoTradeLog = [];
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

function getTradeWindowStatus() {
  const now = new Date();
  const sec = now.getSeconds();
  const min = now.getMinutes();
  // Window opens at every 5-min boundary for 60 seconds
  const nextBoundary = (Math.floor(min / 5) + 1) * 5;
  const inWindow = sec < WINDOW_SEC && min % 5 === 0;
  let secUntilNext;
  if (inWindow) { secUntilNext = 0; }
  else {
    secUntilNext = ((nextBoundary - min) * 60) - sec;
    if (secUntilNext < 0) secUntilNext += 300;
  }
  return { inWindow, secUntilNext: Math.max(0, secUntilNext), windowClosesIn: inWindow ? (WINDOW_SEC - sec) : 0 };
}

let lastWindowStatus = null;
function broadcastWindowStatus() {
  const status = getTradeWindowStatus();
  if (status.inWindow !== lastWindowStatus || status.inWindow) {
    lastWindowStatus = status.inWindow;
    const msg = JSON.stringify({ type: "window", ...status });
    wss.clients.forEach(c => { if (c.readyState === WebSocket.OPEN) c.send(msg); });
  }
}
setInterval(broadcastWindowStatus, 1000);

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

function broadcastState() {
  const msg = JSON.stringify({
    type: "state",
    account: { ...account },
    activeTrades: trades.filter(t => t.status === "active"),
    recentTrades: trades.filter(t => t.status !== "active").slice(-20).reverse(),
    autoTradeLog: autoTradeLog.slice(-10).reverse(),
    autoTradeEnabled: tradeConfig.realTradingEnabled && tradeConfig.autoTrade_10m,
    serverSimTradingEnabled: SERVER_SIM_TRADING_ENABLED,
    realBalance
  });
  wss.clients.forEach(c => { if (c.readyState === WebSocket.OPEN) c.send(msg); });
}

function broadcastTradeUpdate(trade) {
  wss.clients.forEach(c => { if (c.readyState === WebSocket.OPEN) c.send(JSON.stringify({ type: "trade_update", trade })); });
}

function placeTrade(direction, amount, source, durationMin) {
  if (!currentPrice) return null;
  const amt = parseFloat(amount);
  if (isNaN(amt) || amt < 1 || amt > account.balance) return null;
  const dur = Math.max(1, Number(durationMin) || 30);
  const trade = {
    id: nextTradeId++, direction, amount: amt, strikePrice: currentPrice,
    openTime: Date.now(), settleTime: Date.now() + dur * 60 * 1000, duration: String(dur),
    status: "active", settlePrice: null, payout: null, source: source || "manual"
  };
  account.balance -= amt;
  trades.push(trade);
  appendJsonl(TRADE_AUDIT_FILE, {
    event: "server_trade_open",
    tradeId: trade.id,
    source: trade.source,
    direction: trade.direction,
    amount: trade.amount,
    duration: trade.duration,
    openTime: trade.openTime,
    strikePrice: trade.strikePrice
  });
  broadcastState();
  return trade;
}

function placeShadowTrade(strategyId, sig, variant, extra = {}) {
  if (!currentPrice || !sig || !sig.signal) return null;
  const amount = Number(amountForStrategy(strategyId, sig));
  if (!Number.isFinite(amount) || amount <= 0) return null;
  const dur = Math.max(1, Number(sig.duration || sig.interval_min || variant?.duration || tradeConfig.duration || 10));
  const now = Date.now();
  const signalOpenTime = signalActionableMs(sig) || now;
  const signalStrikePrice = signalReferencePrice(sig) || currentPrice;
  const trade = {
    id: nextShadowTradeId++,
    direction: sig.signal,
    amount,
    strikePrice: null,
    openTime: null,
    settleTime: null,
    duration: String(dur),
    status: "pending",
    settlePrice: null,
    payout: null,
    payoutRate: payoutRateForDuration(dur, PAYOUT_RATE),
    source: "shadow:" + strategyId,
    confidence: sig.confidence,
    rsi_value: sig.rsi_value,
    avg_prob: sig.avg_prob,
    signalTime: sig.time,
    actionableTime: sig.actionable_time || sig.candle_close_time || sig.time || null,
    signalEntryPrice: signalStrikePrice,
    executionStrikePrice: null,
    executionOpenTime: null,
    shadowRequestedAt: now,
    shadowExecutionDelayMs: SHADOW_EXECUTION_DELAY_MS,
    ...extra.tradeFields
  };
  shadowTrades.push(trade);
  broadcastTradeUpdate(trade);
  setTimeout(() => {
    if (trade.status !== "pending") return;
    if (!currentPrice) {
      trade.status = "cancelled";
      trade.cancelReason = "shadow_execution_price_missing";
      broadcastTradeUpdate(trade);
      return;
    }
    const executionOpenTime = Date.now();
    const executionStrikePrice = currentPrice;
    trade.status = "active";
    trade.strikePrice = executionStrikePrice;
    trade.openTime = executionOpenTime;
    trade.settleTime = executionOpenTime + dur * 60 * 1000;
    trade.executionStrikePrice = executionStrikePrice;
    trade.executionOpenTime = executionOpenTime;
    appendJsonl(TRADE_AUDIT_FILE, {
      event: "shadow_trade_open",
      serverTime: executionOpenTime,
      tradeId: trade.id,
      source: trade.source,
      strategyId,
      tradeEnabled: variant ? variant.tradeEnabled !== false : null,
      direction: trade.direction,
      amount: trade.amount,
      duration: trade.duration,
      openTime: trade.openTime,
      strikePrice: trade.strikePrice,
      signalEntryPrice: trade.signalEntryPrice,
      executionStrikePrice: trade.executionStrikePrice,
      executionOpenTime: trade.executionOpenTime,
      executionDelayMs: trade.executionOpenTime - signalOpenTime,
      shadowQueueDelayMs: trade.executionOpenTime - trade.shadowRequestedAt,
      confidence: trade.confidence,
      avg_prob: trade.avg_prob,
      signalTime: trade.signalTime,
      actionableTime: trade.actionableTime,
      ...extra.auditFields
    });
    broadcastTradeUpdate(trade);
  }, SHADOW_EXECUTION_DELAY_MS);
  return trade;
}

function shadowSignalKey(strategyId, sig) {
  return [
    strategyId,
    sig && sig.signal || "",
    sig && sig.time || "",
    sig && (sig.actionable_time || sig.candle_close_time || sig.time) || ""
  ].join("|");
}

function shadowAuditSignalKey(row) {
  if (!row || row.event !== "shadow_trade_open" || !row.strategyId) return "";
  if (row.shadowSignalKey) return String(row.shadowSignalKey);
  return [
    row.strategyId,
    row.direction || "",
    row.signalTime || "",
    row.actionableTime || row.signalTime || ""
  ].join("|");
}

function loadShadowSignalKeys() {
  if (shadowSignalKeysLoaded) return;
  shadowSignalKeysLoaded = true;
  for (const row of tailJsonl(TRADE_AUDIT_FILE, 2000)) {
    const key = shadowAuditSignalKey(row);
    if (key) shadowSignalKeys.add(key);
  }
}

function shadowSignalAlreadyRecorded(strategyId, sig) {
  loadShadowSignalKeys();
  const key = shadowSignalKey(strategyId, sig);
  return shadowSignalKeys.has(key) || lastShadowSignals[strategyId] === key;
}

function rememberShadowSignal(strategyId, sig) {
  const key = shadowSignalKey(strategyId, sig);
  shadowSignalKeys.add(key);
  lastShadowSignals[strategyId] = key;
  return key;
}

function mirrorTabletSignalsToShadow(signals) {
  if (!tradeConfig.shadowTradingEnabled || !currentPrice) return;
  const variants = currentStrategyVariants();
  const liveIds = new Set(currentLiveStrategyIds());
  for (const variant of variants) {
    const strategyId = variant.id;
    if (!liveIds.has(strategyId)) continue;
    const sig = signals && signals[strategyId];
    if (!sig || !sig.signal || shadowSignalAlreadyRecorded(strategyId, sig)) continue;
    if (shadowTrades.some(t => ["pending", "active"].includes(t.status) && t.source === "shadow:" + strategyId)) continue;
    const sigTime = signalActionableMs(sig);
    if (!sigTime || Date.now() < sigTime || Date.now() - sigTime > configuredMaxActionableLagMs()) continue;
    const key = shadowSignalKey(strategyId, sig);
    const trade = placeShadowTrade(strategyId, sig, variant, {
      auditFields: {
        shadowType: "tablet_signal_mirror",
        shadowSignalKey: key,
        shadowSource: "api_signal_autojs"
      }
    });
    if (!trade) continue;
    rememberShadowSignal(strategyId, sig);
    autoTradeLog.push({
      time: new Date().toISOString(),
      strategy: strategyId,
      signal: sig.signal,
      confidence: sig.confidence,
      price: currentPrice,
      amount: trade.amount,
      tradeId: "shadow:" + trade.id,
      mode: "tablet_signal_mirror"
    });
  }
}

function hasStrategyCooldown(strategyId) {
  const last = Number(lastStrategyTradeAt[strategyId] || 0);
  return last && Date.now() - last < STRATEGY_COOLDOWN_MS;
}

function markStrategyCooldown(strategyId) {
  if (strategyId) lastStrategyTradeAt[strategyId] = Date.now();
}

function checkShadowTrades() {
  if (!tradeConfig.shadowTradingEnabled) return;
  if (!currentPrice) return;
  try {
    const signals = buildSignalResponse("dashboard");
    const variants = currentStrategyVariants();
    const liveIds = new Set(currentLiveStrategyIds());
    for (const variant of variants) {
      const strategyId = variant.id;
      if (liveIds.has(strategyId)) continue;
      const sig = signals[strategyId];
      if (!sig || !sig.signal) continue;
      if (hasStrategyCooldown(strategyId)) continue;
      if (shadowTrades.some(t => ["pending", "active"].includes(t.status) && t.source === "shadow:" + strategyId)) continue;
      const sigTime = signalActionableMs(sig);
      if (!sigTime || Date.now() < sigTime || Date.now() - sigTime > configuredMaxActionableLagMs()) continue;
      const key = [sig.signal, sig.time || "", sig.actionable_time || sig.candle_close_time || ""].join("|");
      if (lastShadowSignals[strategyId] === key) continue;
      const trade = placeShadowTrade(strategyId, sig, variant);
      if (trade) {
        markStrategyCooldown(strategyId);
        lastShadowSignals[strategyId] = key;
        autoTradeLog.push({
          time: new Date().toISOString(),
          strategy: strategyId,
          signal: sig.signal,
          confidence: sig.confidence,
          price: currentPrice,
          amount: trade.amount,
          tradeId: "shadow:" + trade.id,
          mode: "shadow"
        });
      }
    }
  } catch (e) {}
}

function checkOrderbookShadowTrades() {
  if (!ENABLE_ORDERBOOK_SHADOW_TRADES) return;
  if (!tradeConfig.shadowTradingEnabled || !currentPrice) return;
  try {
    const signals = buildSignalResponse("dashboard");
    const variants = currentStrategyVariants();
    for (const variant of variants) {
      const baseStrategyId = variant.id;
      const sig = signals[baseStrategyId];
      if (!sig || !sig.signal) continue;
      const confirm = orderbookConfirmForSignal(sig);
      if (!confirm.ok) continue;

      const strategyId = `OB_CONFIRM_${baseStrategyId}`;
      if (hasStrategyCooldown(strategyId)) continue;
      if (shadowTrades.some(t => ["pending", "active"].includes(t.status) && t.source === "shadow:" + strategyId)) continue;
      const sigTime = signalActionableMs(sig);
      if (!sigTime || Date.now() < sigTime || Date.now() - sigTime > configuredMaxActionableLagMs()) continue;
      const key = [
        sig.signal,
        sig.time || "",
        sig.actionable_time || sig.candle_close_time || "",
        confirm.pred && confirm.pred.timestamp || ""
      ].join("|");
      if (lastShadowSignals[strategyId] === key) continue;
      const shadowSig = {
        ...sig,
        strategy_id: strategyId,
        confidence: Math.min(99, Math.round(((Number(sig.confidence) || 0) + confirm.confidence) / 2)),
        orderbook_confirmed: true,
        orderbook_direction: confirm.pred.direction,
        orderbook_confidence: confirm.confidence,
        orderbook_predicted_bps_10s: Number(confirm.predictedBps.toFixed(4)),
        orderbook_predicted_price_10s: confirm.target10 ? confirm.target10.predictedPrice : null,
      };
      const trade = placeShadowTrade(strategyId, shadowSig, { ...variant, tradeEnabled: false }, {
        tradeFields: {
          orderbookConfirmed: true,
          baseStrategyId,
          orderbookPrediction: {
            timestamp: confirm.pred.timestamp,
            direction: confirm.pred.direction,
            confidence: confirm.confidence,
            predictedBps10s: Number(confirm.predictedBps.toFixed(4)),
            predictedPrice10s: confirm.target10 ? confirm.target10.predictedPrice : null,
            mid: confirm.pred.mid
          }
        },
        auditFields: {
          shadowType: "orderbook_confirm",
          baseStrategyId,
          orderbookConfirmed: true,
          orderbookDirection: confirm.pred.direction,
          orderbookConfidence: confirm.confidence,
          orderbookPredictedBps10s: Number(confirm.predictedBps.toFixed(4)),
          orderbookPredictedPrice10s: confirm.target10 ? confirm.target10.predictedPrice : null,
          orderbookMid: confirm.pred.mid,
          orderbookTs: confirm.pred.timestamp
        }
      });
      if (trade) {
        markStrategyCooldown(strategyId);
        lastShadowSignals[strategyId] = key;
        autoTradeLog.push({
          time: new Date().toISOString(),
          strategy: strategyId,
          signal: sig.signal,
          confidence: shadowSig.confidence,
          price: currentPrice,
          amount: trade.amount,
          tradeId: "shadow:" + trade.id,
          mode: "orderbook_shadow"
        });
      }
    }
  } catch (e) {}
}

// Auto-trade logic (Shadow / Sim Trading Engine)
function checkAutoTrade() {
  if (!SERVER_SIM_TRADING_ENABLED || !AUTO_TRADE_ENABLED || !tradeConfig.shadowTradingEnabled || !currentPrice) return;
  const status = getTradeWindowStatus();
  if (!status.inWindow) return;
  
  // Read current signal (use "dashboard" source to get full unaltered signals for shadow execution)
  try {
    if (!fs.existsSync(SIGNAL_FILE)) return;
    const signals = buildSignalResponse("dashboard");
    for (const strategyId of currentLiveStrategyIds()) {
      if (!tradeConfig.autoTrade_10m) continue;
      const sig = signals[strategyId];
      if (!sig || !sig.signal) continue;
      if (hasStrategyCooldown(strategyId)) continue;
      if (trades.some(t => t.status === "active" && t.source === "auto:" + strategyId)) continue;

      const sigTime = signalActionableMs(sig);
      if (!sigTime || Date.now() < sigTime || Date.now() - sigTime > configuredMaxActionableLagMs()) continue;

      const last = lastSignals[strategyId];
      if (last && last.signal === sig.signal && last.time === sig.time) continue;
      const autoAmt = Number(amountForStrategy(strategyId, sig));
      const trade = placeTrade(sig.signal, autoAmt, "auto:" + strategyId, sig.duration || sig.interval_min);
      if (trade) {
        markStrategyCooldown(strategyId);
        lastSignals[strategyId] = { signal: sig.signal, time: sig.time, confidence: sig.confidence };
        autoTradeLog.push({
          time: new Date().toISOString(),
          strategy: strategyId,
          signal: sig.signal,
          confidence: sig.confidence,
          price: currentPrice,
          amount: autoAmt,
          tradeId: trade.id
        });
        console.log(`[Shadow Auto] #${trade.id} ${strategyId} ${sig.signal} ${sig.confidence}% @ ${currentPrice} (${autoAmt} USDT)`);
        broadcastTradeUpdate(trade);
      }
    }
  } catch (e) {}
}
setInterval(checkAutoTrade, 3000);
setInterval(checkShadowTrades, 3000);
setInterval(checkOrderbookShadowTrades, 3000);

function settleTrades() {
  const now = Date.now();
  trades.filter(t => t.status === "active" && now >= t.settleTime).forEach(t => {
    const sp = currentPrice || t.strikePrice;
    let won = t.direction === "UP" ? sp > t.strikePrice : sp < t.strikePrice;
    const tie = sp === t.strikePrice;
    if (tie) { t.status = "tie"; t.settlePrice = sp; t.payout = t.amount; account.balance += t.amount; }
    else if (won) {
      const payoutRate = payoutRateForDuration(t.duration, PAYOUT_RATE);
      t.status = "won";
      t.settlePrice = sp;
      t.payoutRate = payoutRate;
      t.payout = t.amount + t.amount * payoutRate;
      account.balance += t.payout;
      account.wins++;
      account.totalPnl += t.amount * payoutRate;
    }
    else { t.status = "lost"; t.settlePrice = sp; t.payout = 0; account.losses++; account.totalPnl -= t.amount; }
    account.totalTrades++;
    appendJsonl(TRADE_AUDIT_FILE, {
      event: "server_trade_settle",
      tradeId: t.id,
      source: t.source,
      direction: t.direction,
      amount: t.amount,
      duration: t.duration,
      openTime: t.openTime,
      settleTime: now,
      strikePrice: t.strikePrice,
      settlePrice: sp,
      status: t.status,
      payoutRate: payoutRateForDuration(t.duration, PAYOUT_RATE),
      payout: t.payout
    });
    console.log(`[Settle] #${t.id} ${t.status} ${t.direction} strike=${t.strikePrice} settle=${sp} pnl=${t.status === "won" ? "+" + (t.payout - t.amount).toFixed(2) : t.status === "lost" ? "-" + t.amount.toFixed(2) : "0"}`);
    broadcastTradeUpdate(t);
  });
  shadowTrades.filter(t => t.status === "active" && now >= t.settleTime).forEach(t => {
    const sp = currentPrice || t.strikePrice;
    const tie = sp === t.strikePrice;
    const won = t.direction === "UP" ? sp > t.strikePrice : sp < t.strikePrice;
    if (tie) {
      t.status = "tie";
      t.settlePrice = sp;
      t.payout = t.amount;
    } else if (won) {
      t.status = "won";
      t.settlePrice = sp;
      t.payout = t.amount + t.amount * payoutRateForDuration(t.duration, PAYOUT_RATE);
    } else {
      t.status = "lost";
      t.settlePrice = sp;
      t.payout = 0;
    }
    appendJsonl(TRADE_AUDIT_FILE, {
      event: "shadow_trade_settle",
      serverTime: now,
      tradeId: t.id,
      source: t.source,
      strategyId: String(t.source || "").replace(/^shadow:/, ""),
      direction: t.direction,
      amount: t.amount,
      duration: t.duration,
      openTime: t.openTime,
      settleTime: now,
      strikePrice: t.strikePrice,
      settlePrice: sp,
      status: t.status,
      payoutRate: payoutRateForDuration(t.duration, PAYOUT_RATE),
      payout: t.payout
    });
    broadcastTradeUpdate(t);
  });
  if (trades.length > 200) trades = trades.filter(t => t.status === "active" || trades.indexOf(t) > trades.length - 101);
  if (shadowTrades.length > 500) shadowTrades = shadowTrades.filter(t => t.status === "active" || shadowTrades.indexOf(t) > shadowTrades.length - 301);
}
setInterval(settleTrades, 1000);
setInterval(broadcastState, 2000);

wss.on("connection", (ws) => {
  const wsInit = {
    type: "init", price: currentPrice, time: Date.now(), history: priceHistory.slice(-300),
    candles: candleHistory,
    account: { ...account }, activeTrades: trades.filter(t => t.status === "active"),
    recentTrades: trades.filter(t => t.status !== "active").slice(-20).reverse(),
    autoTradeLog: autoTradeLog.slice(-10).reverse(),
    autoTradeEnabled: tradeConfig.realTradingEnabled && tradeConfig.autoTrade_10m,
    serverSimTradingEnabled: SERVER_SIM_TRADING_ENABLED,
    realBalance
  };
  const wStatus = getTradeWindowStatus();
  ws.send(JSON.stringify({ ...wsInit, ...wStatus }));
  
  ws.on("message", (raw) => {
    try {
      const msg = JSON.parse(raw);
      if (msg.type === "place_trade") {
        const status = getTradeWindowStatus();
        if (!status.inWindow) {
          ws.send(JSON.stringify({ type: "error", message: "不在交易窗口，下次窗口在 " + status.secUntilNext + " 秒后" }));
          return;
        }
        const { direction, amount } = msg;
        if (direction !== "UP" && direction !== "DOWN") { ws.send(JSON.stringify({ type: "error", message: "方向无效" })); return; }
        const trade = placeTrade(direction, amount || 100, "manual");
        if (trade) {
          ws.send(JSON.stringify({ type: "trade_placed", trade }));
        } else {
          ws.send(JSON.stringify({ type: "error", message: "下单失败（余额不足或价格未就绪）" }));
        }
      }
      if (msg.type === "toggle_auto") {
        // Allow toggling auto-trade from UI (for future use)
      }
    } catch (e) {}
  });
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

let llmConfig = {
  enabled: false,
  strategy: {
    id: LLM_STRATEGY_ID,
    base: "LLM_CONSENSUS",
    label: "大模型共识 10分钟",
    enabled: false,
    tradeEnabled: false,
    amount: "5",
    duration: "10",
    horizonSec: 600
  },
  predictionIntervalSec: 600,
  orderbookEnabled: false,
  providers: []
};

function saveTradeConfig() {
  try { fs.writeFileSync(CONFIG_FILE, JSON.stringify(publicTradeConfig(tradeConfig), null, 2)); } catch (e) {}
}

function saveLlmConfig() {
  // LLM prediction has been removed. Keep legacy config files untouched.
}

function readLlmStatus() {
  return readJsonFile(LLM_STATUS_FILE, {
    strategyId: LLM_STRATEGY_ID,
    state: "idle",
    updatedAt: null,
    activeTrade: null,
    lastPrediction: null,
    lastSignal: null,
    lastError: null
  });
}

function writeLlmStatus(patch) {
  const status = { ...readLlmStatus(), ...patch, strategyId: LLM_STRATEGY_ID, updatedAt: Date.now() };
  try { fs.writeFileSync(LLM_STATUS_FILE, JSON.stringify(status, null, 2), "utf8"); } catch (e) {}
  return status;
}

function llmSignalFromStatus(status = readLlmStatus()) {
  const signal = status.lastSignal;
  if (!llmConfig.enabled || !llmConfig.strategy.enabled || !signal || !signal.signal) {
    return {
      strategy_id: LLM_STRATEGY_ID,
      signal: null,
      confidence: null,
      reason: status.state === "waiting_settle" ? "waiting_current_trade_settle" : "waiting_llm_consensus",
      status
    };
  }
  const actionMs = Number(signal.actionableMs || 0);
  const expiresMs = Number(signal.expiresMs || 0);
  if (!actionMs || Date.now() > expiresMs) {
    return {
      ...signal,
      signal: null,
      confidence: null,
      reason: status.state === "waiting_settle" ? "waiting_current_trade_settle" : "signal_window_expired",
      status
    };
  }
  return { ...signal, status };
}

let llmPredictRunning = false;
let llmNextTimer = null;

function settleLlmActiveTrade(status) {
  const active = status && status.activeTrade;
  if (!active || Number(active.settleTimeMs || 0) > Date.now()) return false;
  const entry = Number(active.entryPrice);
  const settle = Number(currentPrice);
  const direction = active.direction;
  const decided = Number.isFinite(entry) && Number.isFinite(settle) && (direction === "UP" || direction === "DOWN");
  const won = decided ? (direction === "UP" ? settle > entry : settle < entry) : null;
  const sample = {
    serverTime: Date.now(),
    event: "llm_consensus_settle",
    strategyId: LLM_STRATEGY_ID,
    direction,
    amount: active.amount,
    signalTime: active.openSignalTimeMs,
    settleTime: active.settleTimeMs,
    entryPrice: Number.isFinite(entry) ? entry : null,
    settlePrice: Number.isFinite(settle) ? settle : null,
    retBps: decided ? Number(((settle / entry - 1) * 10000).toFixed(4)) : null,
    won,
    dataAgeMs: active.dataAgeMs ?? null,
    modelLatencyMs: active.modelLatencyMs ?? null,
    votes: active.votes || []
  };
  appendJsonl(TRADE_AUDIT_FILE, sample);
  appendJsonl(LLM_TRAINING_SAMPLES_FILE, {
    ...sample,
    sampleType: "settled_signal",
    modelReasons: (active.votes || []).map(v => ({
      provider: v.provider,
      model: v.model,
      direction: v.direction,
      confidence: v.confidence,
      reason: v.reason || ""
    }))
  });
  writeLlmStatus({ activeTrade: null });
  return true;
}

function scheduleLlmPrediction(delayMs = null) {
  clearTimeout(llmNextTimer);
  if (!MANAGED_PROCESSES_ENABLED) return;
  const intervalMs = Math.max(3000, Number(llmConfig.predictionIntervalSec || 5) * 1000);
  const waitMs = delayMs === null ? intervalMs : Math.max(0, delayMs);
  llmNextTimer = setTimeout(runLlmPredictionLoop, waitMs);
}

function runLlmPredictionLoop() {
  if (!MANAGED_PROCESSES_ENABLED) return;
  if (llmPredictRunning) {
    scheduleLlmPrediction(1000);
    return;
  }
  const status = readLlmStatus();
  const active = status.activeTrade;
  settleLlmActiveTrade(status);
  if (!llmConfig.enabled || !llmConfig.strategy.enabled) {
    writeLlmStatus({ state: "disabled", lastSignal: null });
    scheduleLlmPrediction();
    return;
  }
  if (active && Number(active.settleTimeMs || 0) > Date.now()) {
    writeLlmStatus({ state: "waiting_settle", nextPredictionAt: Number(active.settleTimeMs) });
    scheduleLlmPrediction(Number(active.settleTimeMs) - Date.now());
    return;
  }
  if (!fs.existsSync(SECOND_DATA_FILE)) {
    writeLlmStatus({ state: "waiting_data", lastSignal: null, lastError: "second_data_file_missing" });
    scheduleLlmPrediction();
    return;
  }
  llmPredictRunning = true;
  const predictionStartedAt = Date.now();
  const intervalMs = Math.max(3000, Number(llmConfig.predictionIntervalSec || 5) * 1000);
  writeLlmStatus({ state: "predicting", lastSignal: null, lastError: null });
  const args = [
    LLM_ONCE_SCRIPT_FILE,
    "--config", LLM_CONFIG_FILE,
    "--csv", SECOND_DATA_FILE
  ];
  if (llmConfig.orderbookEnabled && fs.existsSync(ORDERBOOK_FILE)) args.push("--orderbook", ORDERBOOK_FILE);
  const child = spawn(PYTHON_EXE, args, {
    cwd: __dirname,
    windowsHide: true,
    env: { ...process.env, ...REPORT_SCRIPT_ENV, PYTHONUNBUFFERED: "1", LLM_MAX_TOKENS: process.env.LLM_MAX_TOKENS || "4096", LLM_TIMEOUT: process.env.LLM_TIMEOUT || "150" },
    stdio: ["ignore", "pipe", "pipe"]
  });
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", chunk => { stdout += chunk.toString("utf8"); });
  child.stderr.on("data", chunk => { stderr += chunk.toString("utf8"); });
  child.on("exit", code => {
    llmPredictRunning = false;
    let nextDelayMs = intervalMs;
    try {
      if (code !== 0) throw new Error((stderr || `llm script exited ${code}`).slice(-1000));
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop() || "{}";
      const prediction = JSON.parse(line);
      const now = Date.now();
      const predictionLatencyMs = now - predictionStartedAt;
      const predictionDataMs = Number.isFinite(Date.parse(prediction.time || ""))
        ? Date.parse(prediction.time)
        : predictionStartedAt;
      const predictionDataAgeMs = now - predictionDataMs;
      const maxPredictionAgeMs = configuredMaxActionableLagMs();
      const nextIntervalAt = Math.max(now + 3000, predictionStartedAt + intervalMs);
      nextDelayMs = nextIntervalAt - now;
      const base = {
        lastPrediction: prediction,
        activeTrade: null,
        nextPredictionAt: nextIntervalAt
      };
      const predictionAudit = {
        serverTime: now,
        event: "llm_consensus_prediction",
        strategyId: LLM_STRATEGY_ID,
        ok: !!prediction.ok,
        reason: prediction.reason || null,
        direction: prediction.direction || null,
        confidence: prediction.confidence == null ? null : prediction.confidence,
        entryPrice: prediction.entryPrice,
        dataTime: prediction.time,
        dataTimeCn: prediction.time_cn,
        latencyMs: predictionLatencyMs,
        dataAgeMs: predictionDataAgeMs,
        maxDataAgeMs: maxPredictionAgeMs,
        votes: prediction.votes || [],
        failed: prediction.failed || [],
        providers: prediction.providers || [],
        rules: prediction.rules || {}
      };
      appendJsonl(TRADE_AUDIT_FILE, predictionAudit);
      appendJsonl(LLM_TRAINING_SAMPLES_FILE, { ...predictionAudit, sampleType: "prediction" });
      if (prediction.ok && prediction.direction) {
        const amount = Math.max(5, Math.round(Number(llmConfig.strategy.amount || 5)));
        const signal = {
          strategy_id: LLM_STRATEGY_ID,
          signal: prediction.direction,
          confidence: Math.round(Number(prediction.confidence || 0) * 100),
          confidence_raw: prediction.confidence,
          amount: String(amount),
          fixed_amount: true,
          interval_min: 10,
          duration: "10",
          time: isoTime(now),
          data_time: prediction.time || null,
          data_age_ms: predictionDataAgeMs,
          model_latency_ms: predictionLatencyMs,
          actionable_time: isoTime(now),
          actionableMs: now,
          expiresMs: now + 90 * 1000,
          bypass_entry_timing: true,
          signal_source: "llm_consensus",
          reason: prediction.reason,
          votes: prediction.votes,
          failed: prediction.failed,
          entry_price: prediction.entryPrice
        };
        const activeTrade = {
          direction: prediction.direction,
          amount,
          openSignalTimeMs: now,
          settleTimeMs: now + 600_000,
          entryPrice: prediction.entryPrice,
          votes: prediction.votes,
          dataTimeMs: predictionDataMs,
          dataAgeMs: predictionDataAgeMs,
          modelLatencyMs: predictionLatencyMs
        };
        writeLlmStatus({
          ...base,
          state: "signal_ready",
          lastSignal: signal,
          activeTrade,
          nextPredictionAt: activeTrade.settleTimeMs
        });
        nextDelayMs = activeTrade.settleTimeMs - now;
        appendJsonl(TRADE_AUDIT_FILE, {
          serverTime: now,
          event: "llm_consensus_signal",
          strategyId: LLM_STRATEGY_ID,
          direction: prediction.direction,
          confidence: prediction.confidence,
          amount,
          votes: prediction.votes,
          failed: prediction.failed,
          latencyMs: predictionLatencyMs,
          dataAgeMs: predictionDataAgeMs
        });
        broadcastState();
      } else {
        writeLlmStatus({
          ...base,
          state: "no_consensus",
          lastSignal: null,
          lastError: null
        });
      }
    } catch (e) {
      writeLlmStatus({ state: "error", lastSignal: null, lastError: String(e && e.message ? e.message : e).slice(0, 1000) });
    }
    scheduleLlmPrediction(nextDelayMs);
  });
}

function readProdConfig() {
  try {
    if (fs.existsSync(PROD_CONFIG_FILE)) {
      return JSON.parse(fs.readFileSync(PROD_CONFIG_FILE, "utf8"));
    }
  } catch (e) {}
  return {};
}

function applyProdStrategyParams(baseConfig, config) {
  const out = baseConfig && typeof baseConfig === "object" ? { ...baseConfig } : {};
  const variants = strategyVariants(config);
  // Remove managed 30m/10m entries that are no longer configured.
  for (const key of Object.keys(out)) {
    const managed30m = key === "BTC_30min" || key.startsWith("BTC_30min_");
    const managed10m = key === "BTC_10min" || key.startsWith("BTC_10min_");
    if ((managed30m || managed10m) && !variants.some(v => v.id === key)) delete out[key];
  }
  const safeTemplate = out.BTC_10min_SAFE || {};
  const takerTemplate = out.BTC_10min_TAKER || {};
  for (const key of Object.keys(out)) {
    if ((key.startsWith("BTC_10min_TAKER") || key.startsWith("BTC_10min_SAFE") || key.startsWith("BTC_10min_SECOND") || key.startsWith("BTC_10min_SMART") || key.startsWith("BTC_10min_NORMAL_STATE") || key.startsWith("BTC_10min_NORMAL_LIQ") || key.startsWith("BTC_10min_BRANCH") || key.startsWith("BTC_10min_MULTI") || key.startsWith("BTC_10min_V22")) && !variants.some(v => v.id === key)) delete out[key];
  }
  for (const variant of variants) {
    const current = out[variant.id] && typeof out[variant.id] === "object" ? out[variant.id] : {};
    const template = variant.base === "SAFE" ? safeTemplate : takerTemplate;
    if (variant.base === "POC_NORMAL") {
      // 30m 策略必须写成三模型 ML 配置；显式剔除旧 poc_normal/norm_* 字段，避免保存配置时回退旧算法。
      const {
        model_type: _oldModelType,
        norm_window: _oldNormWindow,
        norm_tail_pct: _oldNormTailPct,
        norm_mode: _oldNormMode,
        norm_use_rsi: _oldNormUseRsi,
        ...cleanCurrent
      } = current;
      out[variant.id] = {
        ...cleanCurrent,
        enabled: variant.enabled,
        trade_enabled: variant.tradeEnabled === true,
        symbol: "btcusdt",
        interval_min: 30,
        horizon: 6,
        threshold: variant.threshold,
        rsi_lo: variant.rsiLo,
        rsi_hi: variant.rsiHi,
        agree_mode: variant.agreeMode,
        model_label: "BTC_30min",
        observation_mode: variant.observationMode,
        strategy_role: variant.role
      };
      continue;
    }
    if (variant.base === "SECOND_VW_CONFIRM") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        model_type: "second_normal_vw_confirm",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_lookback_sec: variant.lookbackSec || 2700,
        second_horizon_sec: variant.horizonSec || 600,
        second_min_gap_sec: variant.gapSec || variant.horizonSec || 600,
        second_tail_pct: variant.tailPct,
        second_zone_filter: variant.zoneFilter || "none",
        eta_target_bps: variant.etaTargetBps || 2,
        eta_max_wait_sec: variant.etaMaxWaitSec || 45,
        up_reversal_confirm_bps: variant.upReversalConfirmBps ?? 0.0,
        up_reversal_confirm_max_sec: variant.upReversalConfirmMaxSec ?? 20,
        incident_filter_enabled: variant.incidentFilterEnabled !== false,
        incident_filter_mode: variant.incidentFilterMode || "directional_only",
        incident_window_sec: variant.incidentWindowSec || 10,
        incident_min_move_bps: variant.incidentMinMoveBps ?? 10,
        incident_min_volume_quantile: variant.incidentMinVolumeQuantile ?? 0.99,
        incident_min_flow_imbalance: variant.incidentMinFlowImbalance ?? 0.8,
        incident_cooldown_sec: variant.incidentCooldownSec ?? 10,
        model_label: variant.label || `SECOND_VW_CONFIRM_${variant.lookbackSec || 2700}_${Math.round(Number(variant.tailPct || 0.2) * 100)}_ETA${variant.etaTargetBps || 2}`
      };
      continue;
    }
    if (variant.base === "SECOND") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        model_type: "second_normal",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_lookback_sec: variant.lookbackSec || 1800,
        second_horizon_sec: variant.horizonSec || 600,
        second_min_gap_sec: variant.gapSec || variant.horizonSec || 600,
        second_tail_pct: variant.tailPct,
        second_filter: variant.secondFilter || "none",
        second_zone_filter: variant.zoneFilter || "none",
        second_sigma_min_bps: variant.sigmaMinBps ?? 0,
        second_sigma_max_bps: variant.sigmaMaxBps ?? 9999,
        model_label: `SECOND_${variant.lookbackSec || 1800}_${Math.round(variant.tailPct * 100)}_${100 - Math.round(variant.tailPct * 100)}`
      };
      continue;
    }
    if (variant.base === "SECOND_CHIP") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        model_type: "second_chip",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_chip_lookback_sec: variant.lookbackSec || 3600,
        second_chip_horizon_sec: variant.horizonSec || 600,
        second_chip_min_gap_sec: variant.gapSec || variant.horizonSec || 600,
        second_chip_target_share: variant.chipTargetShare,
        second_chip_bin_mode: variant.chipBinMode || "fixed",
        second_chip_bin_size: variant.chipBinSize || 20,
        second_chip_bin_pct: variant.chipBinPct,
        second_chip_break_pct: variant.chipBreakPct,
        second_chip_direction_filter: variant.chipDirectionFilter || "breakout_up_only",
        second_chip_filter: variant.chipFilter || "none",
        model_label: `SECOND_CHIP_${variant.lookbackSec || 3600}_${Math.round(Number(variant.chipTargetShare || 0.2) * 100)}_${Math.round(Number(variant.chipBreakPct || 0.0023) * 10000)}`
      };
      continue;
    }
    if (variant.base === "SECOND_VALUE_AREA_SMART") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        model_type: "second_value_area_smart",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_va_lookback_sec: variant.lookbackSec || 4200,
        second_va_horizon_sec: variant.horizonSec || 600,
        second_va_signal_gap_sec: variant.gapSec || variant.horizonSec || 600,
        second_va_tail_pct: variant.tailPct ?? 0.20,
        second_va_sigma_min_bps: variant.sigmaMinBps ?? 8,
        second_va_sigma_max_bps: variant.sigmaMaxBps ?? 80,
        second_va_value_area_sec: variant.valueAreaSec || 3600,
        second_va_bin_size: variant.binSize ?? 10,
        second_va_value_pct: variant.valuePct ?? 0.70,
        second_va_normal_window_sec: variant.normalWindowSec || 600,
        second_va_normal_coverage: variant.normalCoverage ?? 0.70,
        second_va_mode: variant.mode || "failed_break_fade",
        second_va_min_edge_bps: variant.minEdgeBps ?? 1,
        second_va_min_flow: variant.minFlow ?? 0.05,
        second_va_min_trend_bps: variant.minTrendBps ?? 1.0,
        second_va_min_volume_ratio: variant.minVolumeRatio ?? 1.15,
        second_va_min_ob_imbalance: variant.minObImbalance ?? 0.05,
        second_va_min_micro_bps: variant.minMicroBps ?? 0.001,
        second_va_max_against_ob_imbalance: variant.maxAgainstObImbalance ?? 0.25,
        second_va_max_against_flow: variant.maxAgainstFlow ?? 0.35,
        second_va_retest_sec: variant.retestSec || 180,
        second_va_retest_bps: variant.retestBps ?? 4.0,
        second_va_break_hold_sec: variant.breakHoldSec || 30,
        second_va_reclaim_bps: variant.reclaimBps ?? 0.8,
        second_va_absorption_max_progress_bps: variant.absorptionMaxProgressBps ?? 1.5,
        second_va_loss_pause_after: variant.lossPauseAfter ?? 2,
        second_va_loss_pause_sec: variant.lossPauseSec ?? 1800,
        model_label: variant.label || "SMART_OBSAFE_LOSS2_VA3600_E1_R180_CD600"
      };
      continue;
    }
    if (variant.base === "SECOND_NORMAL_STATE_V11") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        model_type: "normal_state_v11",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        normal_state_lookback_sec: variant.lookbackSec || 10800,
        normal_state_horizon_sec: variant.horizonSec || 600,
        normal_state_min_gap_sec: variant.gapSec || variant.horizonSec || 600,
        normal_state_confirm_delay_sec: variant.confirmDelaySec ?? 5,
        normal_state_max_adverse_bps: variant.maxAdverseBps ?? 5,
        normal_state_signal_hold_sec: variant.signalHoldSec ?? 55,
        normal_state_bandwalk_max: variant.bandwalkMax ?? 6,
        normal_state_min_consensus_votes: variant.minConsensusVotes ?? 2,
        normal_state_state_gate: variant.stateGate || "edge_persistence_lt6",
        normal_state_confirmation_veto: variant.confirmationVeto || "none",
        normal_state_loss_density_enabled: variant.lossDensityEnabled === true,
        normal_state_loss_density_window: variant.lossDensityWindow || 6,
        normal_state_loss_density_losses: variant.lossDensityLosses || 3,
        normal_state_loss_density_min_trades: variant.lossDensityMinTrades || 4,
        normal_state_loss_density_cooldown_sec: variant.lossDensityCooldownSec || 28800,
        normal_state_loss_density_lookback_hours: variant.lossDensityLookbackHours || 72,
        model_label: variant.label || "BTC_10min_NORMAL_STATE_V11_BANDWALK_2OF5_D5A5"
      };
      continue;
    }
    if (variant.base === "SECOND_NORMAL_ROUTER_V21") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        model_type: "second_normal_router_v21",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_lookback_sec: variant.lookbackSec || 4200,
        second_horizon_sec: variant.horizonSec || 600,
        second_min_gap_sec: variant.gapSec || variant.horizonSec || 600,
        second_router_route_lookback_sec: variant.routeLookbackSec || 4200,
        second_router_r10_window_sec: variant.r10WindowSec || 600,
        second_router_r10_cap_bps: variant.r10CapBps ?? 42,
        second_router_down_r10_cap_bps: variant.downR10CapBps ?? 35,
        second_router_mid_route_sigma_cap_bps: variant.midRouteSigmaCapBps ?? 20,
        second_router_min_observed_pct: variant.minObservedPct ?? 88,
        second_router_veto_low_up: variant.vetoLowUp !== false,
        normal_state_loss_density_enabled: variant.lossDensityEnabled !== false,
        normal_state_loss_density_window: variant.lossDensityWindow || 6,
        normal_state_loss_density_losses: variant.lossDensityLosses || 3,
        normal_state_loss_density_min_trades: variant.lossDensityMinTrades || 4,
        normal_state_loss_density_cooldown_sec: variant.lossDensityCooldownSec || 28800,
        normal_state_loss_density_lookback_hours: variant.lossDensityLookbackHours || 72,
        normal_state_loss_streak_enabled: variant.lossStreakEnabled !== false,
        normal_state_loss_streak_count: variant.lossStreakCount || 2,
        normal_state_loss_streak_cooldown_sec: variant.lossStreakCooldownSec || 3600,
        model_label: variant.label || "BTC_10min_NORMAL_STATE_V21_LOSS_DENSITY_3OF6_8H"
      };
      continue;
    }
    if (variant.base === "SECOND_NORMAL_LOWVOL_V22") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        trade_enabled: variant.tradeEnabled === true,
        model_type: "second_normal_lowvol_v22",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_lookback_sec: variant.lookbackSec || 4200,
        second_horizon_sec: variant.horizonSec || 600,
        second_min_gap_sec: variant.gapSec || variant.horizonSec || 600,
        second_router_route_lookback_sec: variant.routeLookbackSec || 4200,
        second_router_r10_window_sec: variant.r10WindowSec || 600,
        second_router_r10_cap_bps: variant.r10CapBps ?? 42,
        second_router_down_r10_cap_bps: variant.downR10CapBps ?? 35,
        second_router_mid_route_sigma_cap_bps: variant.midRouteSigmaCapBps ?? 20,
        second_router_min_observed_pct: variant.minObservedPct ?? 88,
        second_router_veto_low_up: variant.vetoLowUp === true,
        second_lowvol_route_sigma_max_bps: variant.lowVolRouteSigmaMaxBps ?? 10,
        second_lowvol_confirm_sec: variant.lowVolConfirmSec ?? 15,
        second_lowvol_reversion_bps: variant.lowVolReversionBps ?? 0.5,
        second_lowvol_breakout_bps: variant.lowVolBreakoutBps ?? 1.5,
        normal_state_loss_density_enabled: false,
        normal_state_loss_streak_enabled: false,
        model_label: variant.label || "正态V22 低波动确认影子"
      };
      continue;
    }
    if (variant.base === "SECOND_NORMAL_LIQUIDITY_ORDERBOOK_V1") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        trade_enabled: variant.tradeEnabled === true,
        model_type: variant.v9AugmentedEnabled === true
          ? "second_normal_liquidity_orderbook_v1"
          : "second_normal_trend_orderbook_latch_v2",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_liq_normal_window_sec: variant.normalWindowSec || 600,
        second_liq_horizon_sec: variant.horizonSec || 600,
        second_liq_signal_gap_sec: variant.gapSec || variant.horizonSec || 600,
        second_liq_z_entry: variant.zEntry ?? 1.2,
        second_liq_z_reclaim: variant.zReclaim ?? 0.85,
        second_liq_retest_sec: variant.retestSec || 120,
        second_liq_inside_min: variant.insideMin ?? 0.55,
        second_liq_observed_min_pct: variant.observedMinPct ?? 88,
        second_liq_center_slope_sec: variant.centerSlopeSec || 300,
        second_liq_center_slope_max_bps: variant.centerSlopeMaxBps ?? 8,
        second_liq_sigma_min_bps: variant.sigmaMinBps ?? 5.8,
        second_liq_sigma_max_bps: variant.sigmaMaxBps ?? 55,
        second_liq_sigma_expand_max: variant.sigmaExpandMax ?? 1.9,
        second_liq_orderbook_max_age_sec: variant.orderbookMaxAgeSec || 3,
        second_liq_ob_imbalance_min: variant.obImbalanceMin ?? 0.08,
        second_liq_micro_min_bps: variant.microMinBps ?? 0.001,
        second_liq_wall_ratio_min: variant.wallRatioMin ?? 1.0,
        second_liq_flow_guard: variant.flowGuard ?? 0.12,
        second_liq_true_break_flow: variant.trueBreakFlow ?? 0.28,
        second_liq_true_break_imbalance: variant.trueBreakImbalance ?? 0.28,
        second_liq_bidwall_trap_enabled: variant.bidwallTrapEnabled !== false,
        second_liq_bidwall_trap_ret300_max_bps: variant.bidwallTrapRet300MaxBps ?? -5,
        second_liq_bidwall_trap_bid20_chg60_min: variant.bidwallTrapBid20Chg60Min ?? 2,
        second_liq_bidwall_trap_ret600_min_bps: variant.bidwallTrapRet600MinBps ?? -20,
        second_liq_quality_v2_enabled: variant.qualityV2Enabled !== false,
        second_liq_quality_v2_down_bid20_chg60_min: variant.qualityV2DownBid20Chg60Min ?? -0.7,
        second_liq_quality_v2_up_flow60_min: variant.qualityV2UpFlow60Min ?? -0.063,
        second_liq_trend_space_enabled: variant.trendSpaceEnabled === true,
        second_liq_trend_space_sigma_expand_max: variant.trendSpaceSigmaExpandMax ?? 1.6,
        second_liq_trend_space_center_slope_abs_max_bps: variant.trendSpaceCenterSlopeAbsMaxBps ?? 6,
        second_liq_trend_space_inside_max: variant.trendSpaceInsideMax ?? 0.75,
        second_liq_trend_space_trend_ret_1800_bps: variant.trendSpaceTrendRet1800Bps ?? 15,
        second_liq_trend_space_up_pos_1800_min: variant.trendSpaceUpPos1800Min ?? 0.72,
        second_liq_trend_space_down_pos_1800_max: variant.trendSpaceDownPos1800Max ?? 0.28,
        second_liq_trend_space_block_countertrend: variant.trendSpaceBlockCountertrend !== false,
        second_liq_trend_space_block_upper_fade_pullback: variant.trendSpaceBlockUpperFadePullback !== false,
        second_liq_trend_space_short_ret_600_up_bps: variant.trendSpaceShortRet600UpBps ?? 12,
        second_liq_trend_space_short_pos_600_min: variant.trendSpaceShortPos600Min ?? 0.65,
        second_liq_mode: variant.liquidityMode || "reclaim",
        v9_augmented_enabled: variant.v9AugmentedEnabled === true,
        v9_efficiency_min: variant.v9EfficiencyMin ?? 0.60,
        v9_trend_strength_min: variant.v9TrendStrengthMin ?? 1.25,
        v9_opposing_min_bps: variant.v9OpposingMinBps ?? 2.0,
        v9_z30_min: variant.v9Z30Min ?? 1.0,
        v9_volume_ratio_min: variant.v9VolumeRatioMin ?? 0.80,
        v9_book_coverage_min: variant.v9BookCoverageMin ?? 0.90,
        v9_book_votes_min: variant.v9BookVotesMin ?? 2,
        v9_max_emit_age_sec: variant.v9MaxEmitAgeSec ?? 8,
        v9_supplement_min_abs_normal_z: variant.v9SupplementMinAbsNormalZ ?? 0,
        v9_original_regime_veto_enabled: variant.v9OriginalRegimeVetoEnabled === true,
        v9_original_veto_mature_downtrend: variant.v9OriginalVetoMatureDowntrend !== false,
        v9_original_veto_short_migration_up_down: variant.v9OriginalVetoShortMigrationUpDown !== false,
        v9_original_allow_mature_downtrend_down_flow_min: variant.v9OriginalAllowMatureDowntrendDownFlowMin ?? null,
        v9_supplement_loose_short_migration_reversion_enabled: variant.v9SupplementLooseShortMigrationReversionEnabled === true,
        v9_supplement_loose_mature_uptrend_down_enabled: variant.v9SupplementLooseMatureUptrendDownEnabled === true,
        v9_supplement_mature_uptrend_down_flow_min: variant.v9SupplementMatureUptrendDownFlowMin ?? -0.3,
        router_latch_sec: 6,
        router_execution_interval_sec: 5,
        router_execution_phase: 0,
        router_max_emit_age_sec: 3,
        router_data_observed_min_pct: 90,
        router_orderbook_coverage_min: 0.9,
        router_trend_confirm_sec: 20,
        router_startup_skip_enabled: variant.startupSkipEnabled === true,
        router_startup_skip_threshold: variant.startupSkipThreshold ?? 4,
        router_band_ultra_low_z_entry: 0.8,
        router_band_ultra_low_z_reclaim: 0.8,
        router_band_ultra_low_confirm_hits: 2,
        router_band_ultra_low_confirm_span_sec: 5,
        router_band_ultra_low_ret600_min_bps: -15,
        router_band_ultra_low_flow120_min: -0.12,
        router_band_low_z_entry: 0.9,
        router_band_low_z_reclaim: 0.85,
        router_band_low_confirm_hits: 2,
        router_band_low_confirm_span_sec: 5,
        router_band_low_ret600_min_bps: -15,
        router_band_low_flow120_min: -0.12,
        router_band_mid_z_entry: 1.0,
        router_band_mid_z_reclaim: 0.9,
        router_band_mid_confirm_hits: 2,
        router_band_mid_confirm_span_sec: 5,
        router_band_mid_ret600_min_bps: -12,
        router_band_mid_flow120_min: -0.08,
        router_band_elevated_z_entry: 1.2,
        router_band_elevated_z_reclaim: 0.85,
        router_band_elevated_confirm_hits: 3,
        router_band_elevated_confirm_span_sec: 8,
        router_band_elevated_ret600_min_bps: -10,
        router_band_elevated_flow120_min: -0.08,
        router_band_high_enabled: false,
        normal_state_loss_density_enabled: false,
        normal_state_loss_streak_enabled: false,
        model_label: variant.label || "正态流动性订单薄V1 影子"
      };
      continue;
    }
    if (variant.base === "SECOND_BRANCH_VOTE_STARTUP_V1") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        trade_enabled: variant.tradeEnabled === true,
        model_type: "second_branch_vote_startup_v1",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_lookback_sec: variant.lookbackSec || 7200,
        second_horizon_sec: variant.horizonSec || 600,
        second_min_gap_sec: variant.gapSec || variant.horizonSec || 600,
        branch_vote_normal_window_sec: variant.normalWindowSec || 600,
        branch_vote_horizon_sec: variant.horizonSec || 600,
        branch_vote_signal_gap_sec: variant.gapSec || variant.horizonSec || 600,
        branch_vote_orderbook_max_age_sec: variant.orderbookMaxAgeSec || 3,
        branch_vote_min_votes: variant.minVotes || 2,
        branch_vote_startup_skip_threshold: variant.startupSkipThreshold || 4,
        branch_vote_rule_path: variant.rulePath || "data/branch_vote_startup_rules.json",
        normal_state_loss_density_enabled: false,
        normal_state_loss_streak_enabled: false,
        model_label: variant.label || "分支投票趋势启动V1"
      };
      continue;
    }
    if (variant.base === "SECOND_MULTI_NORMAL_HF_STABLE_V1") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        trade_enabled: variant.tradeEnabled === true,
        model_type: "second_multi_normal_hf_stable_v1",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_lookback_sec: variant.lookbackSec || 7200,
        second_horizon_sec: variant.horizonSec || 600,
        second_min_gap_sec: variant.gapSec || variant.horizonSec || 600,
        multi_normal_window_sec: variant.normalWindowSec || 600,
        multi_normal_horizon_sec: variant.horizonSec || 600,
        multi_normal_signal_gap_sec: variant.gapSec || variant.horizonSec || 600,
        multi_normal_orderbook_max_age_sec: variant.orderbookMaxAgeSec || 3,
        multi_normal_lowvol_sigma_max_bps: variant.lowVolSigmaMaxBps ?? 3,
        multi_normal_lowvol_range_max_bps: variant.lowVolRangeMaxBps ?? 20,
        multi_normal_lowvol_abs_ret10_max_bps: variant.lowVolAbsRet10MaxBps ?? 5,
        multi_normal_lowvol_z_min: variant.lowVolZMin ?? 1.2,
        multi_normal_lowvol_z_max: variant.lowVolZMax ?? 1.8,
        multi_normal_lowvol_min_signed_flow: variant.lowVolMinSignedFlow ?? 0,
        multi_normal_lowvol_max_adverse_ret30_sigma: variant.lowVolMaxAdverseRet30Sigma ?? 0.5,
        multi_normal_trend_base_z_min: variant.trendBaseZMin ?? 1.2,
        multi_normal_trend_high_vol_sigma_min_bps: variant.trendHighVolSigmaMinBps ?? 8,
        multi_normal_trend_high_vol_z_min: variant.trendHighVolZMin ?? 0.5,
        multi_normal_trend_min_signed_flow: variant.trendMinSignedFlow ?? 0.12,
        multi_normal_trend_max_signed_book: variant.trendMaxSignedBook ?? 0.08,
        incident_filter_enabled: false,
        normal_state_loss_density_enabled: false,
        normal_state_loss_streak_enabled: false,
        model_label: variant.label || "多周期动态正态高频稳定V1"
      };
      continue;
    }
    if (variant.base === "SECOND_MULTISCALE_PHASE_GATE_V1") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        trade_enabled: variant.tradeEnabled === true,
        model_type: "second_multiscale_phase_gate_v1",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_lookback_sec: variant.lookbackSec || 7800,
        second_horizon_sec: variant.horizonSec || 600,
        second_min_gap_sec: variant.gapSec || variant.horizonSec || 600,
        phase_gate_horizon_sec: variant.horizonSec || 600,
        phase_gate_signal_gap_sec: variant.gapSec || variant.horizonSec || 600,
        phase_gate_orderbook_max_age_sec: variant.orderbookMaxAgeSec || 3,
        phase_gate_max_emit_age_sec: variant.maxEmitAgeSec || 8,
        phase_gate_lookback_sec: variant.phaseLookbackSec || 3600,
        phase_gate_maturity_history_sec: variant.maturityHistorySec || 3600,
        phase_gate_maturity_min_periods: variant.maturityMinPeriods || 1800,
        phase_gate_maturity_quantile: variant.maturityQuantile ?? 0.75,
        phase_gate_min_flow60: variant.minFlow60 ?? 0.08,
        phase_gate_min_imbalance20: variant.minImbalance20 ?? 0.05,
        phase_gate_min_microprice_bps: variant.minMicropriceBps ?? 0,
        phase_gate_min_volume_ratio: variant.minVolumeRatio ?? 0.8,
        incident_filter_enabled: false,
        normal_state_loss_density_enabled: false,
        normal_state_loss_streak_enabled: false,
        model_label: variant.label || "多周期迁移阶段 V1"
      };
      continue;
    }
    if (variant.base === "SECOND_RANGE_BREAKOUT_CONFIRM") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        trade_enabled: variant.tradeEnabled === true,
        model_type: "second_range_breakout_confirm",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_range_lookback_sec: variant.lookbackSec || 1800,
        second_range_horizon_sec: variant.horizonSec || 600,
        second_range_signal_gap_sec: variant.gapSec || variant.horizonSec || 600,
        second_range_z_entry: variant.rangeZEntry ?? 2.2,
        second_range_confirm_sec: variant.rangeConfirmSec ?? 60,
        second_range_hold_z: variant.rangeHoldZ ?? 1.0,
        second_range_min_hold_ratio: variant.rangeMinHoldRatio ?? 0.75,
        second_range_pre_slope_sec: variant.rangePreSlopeSec ?? 300,
        second_range_confirm_slope_sec: variant.rangeConfirmSlopeSec ?? 60,
        second_range_min_pre_slope_bps: variant.rangeMinPreSlopeBps ?? 8,
        second_range_min_confirm_slope_bps: variant.rangeMinConfirmSlopeBps ?? 4,
        second_range_min_flow_imbalance: variant.rangeMinFlowImbalance ?? 0.12,
        second_range_min_confirm_flow_imbalance: variant.rangeMinConfirmFlowImbalance ?? 0.08,
        second_range_min_volume_ratio: variant.rangeMinVolumeRatio ?? 0.45,
        second_range_min_volatility_ratio: variant.rangeMinVolatilityRatio ?? 0.55,
        second_range_max_age_beyond_sec: variant.rangeMaxAgeBeyondSec ?? 180,
        model_label: variant.label || "range breakout confirm shadow"
      };
      continue;
    }
    if (variant.base === "SECOND_TREND_DOWN") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        model_type: "second_trend_pullback_down",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_trend_regime_lookback_sec: variant.regimeLookbackSec || 7200,
        second_trend_regime_drop_pct: variant.regimeDropPct || 0.004,
        second_trend_pullback_sec: variant.pullbackSec || 300,
        second_trend_pullback_pct: variant.pullbackPct || 0.001,
        second_trend_horizon_sec: variant.horizonSec || 600,
        second_trend_min_gap_sec: variant.gapSec || variant.horizonSec || 600,
        second_trend_suppress_reversal: variant.suppressReversal !== false,
        model_label: "SECOND_TREND_DOWN_7200_04_300_10"
      };
      continue;
    }
    out[variant.id] = {
      ...template,
      ...current,
      enabled: variant.enabled,
      model_type: "poc_normal",
      norm_tail_pct: variant.tailPct,
      norm_taker_filter: variant.base === "TAKER" ? "align" : "none",
      model_label: `${variant.base}_${Math.round(variant.tailPct * 100)}_${100 - Math.round(variant.tailPct * 100)}`
    };
  }
  return out;
}

function saveProdStrategyParams(config) {
  const next = applyProdStrategyParams(readProdConfig(), config);
  fs.writeFileSync(PROD_CONFIG_FILE, JSON.stringify(next, null, 2));
}

function strategyRestartFingerprint(config) {
  return JSON.stringify(strategyVariants(config).map(v => ({
    id: v.id,
    base: v.base,
    amount: v.amount,
    tailPct: v.tailPct,
    enabled: v.enabled,
    tradeEnabled: v.tradeEnabled,
    lookbackSec: v.lookbackSec,
    horizonSec: v.horizonSec,
    gapSec: v.gapSec,
    secondFilter: v.secondFilter,
    zoneFilter: v.zoneFilter,
    sigmaMinBps: v.sigmaMinBps,
    sigmaMaxBps: v.sigmaMaxBps,
    ...(v.base === "SECOND_VW_CONFIRM" ? {
      etaTargetBps: v.etaTargetBps,
      etaMaxWaitSec: v.etaMaxWaitSec
    } : {}),
    ...(v.base === "SECOND_VALUE_AREA_SMART" ? {
      valueAreaSec: v.valueAreaSec,
      binSize: v.binSize,
      valuePct: v.valuePct,
      normalWindowSec: v.normalWindowSec,
      normalCoverage: v.normalCoverage,
      mode: v.mode,
      minEdgeBps: v.minEdgeBps,
      minFlow: v.minFlow,
      minTrendBps: v.minTrendBps,
      minVolumeRatio: v.minVolumeRatio,
      minObImbalance: v.minObImbalance,
      minMicroBps: v.minMicroBps,
      maxAgainstObImbalance: v.maxAgainstObImbalance,
      maxAgainstFlow: v.maxAgainstFlow,
      retestSec: v.retestSec,
      retestBps: v.retestBps,
      breakHoldSec: v.breakHoldSec,
      reclaimBps: v.reclaimBps,
      absorptionMaxProgressBps: v.absorptionMaxProgressBps,
      lossPauseAfter: v.lossPauseAfter,
      lossPauseSec: v.lossPauseSec
    } : {}),
    ...(v.base === "SECOND_NORMAL_STATE_V11" ? {
      confirmDelaySec: v.confirmDelaySec,
      maxAdverseBps: v.maxAdverseBps,
      signalHoldSec: v.signalHoldSec,
      bandwalkMax: v.bandwalkMax,
      minConsensusVotes: v.minConsensusVotes,
      stateGate: v.stateGate,
      confirmationVeto: v.confirmationVeto
    } : {}),
    ...(v.base === "SECOND_NORMAL_ROUTER_V21" || v.base === "SECOND_NORMAL_LOWVOL_V22" ? {
      routeLookbackSec: v.routeLookbackSec,
      r10WindowSec: v.r10WindowSec,
      r10CapBps: v.r10CapBps,
      downR10CapBps: v.downR10CapBps,
      midRouteSigmaCapBps: v.midRouteSigmaCapBps,
      minObservedPct: v.minObservedPct,
      lossDensityEnabled: v.lossDensityEnabled,
      lossDensityWindow: v.lossDensityWindow,
      lossDensityLosses: v.lossDensityLosses,
      lossDensityMinTrades: v.lossDensityMinTrades,
      lossDensityCooldownSec: v.lossDensityCooldownSec,
      lossDensityLookbackHours: v.lossDensityLookbackHours,
      lossStreakEnabled: v.lossStreakEnabled,
      lossStreakCount: v.lossStreakCount,
      lossStreakCooldownSec: v.lossStreakCooldownSec,
      vetoLowUp: v.vetoLowUp
    } : {}),
    ...(v.base === "SECOND_NORMAL_LOWVOL_V22" ? {
      lowVolRouteSigmaMaxBps: v.lowVolRouteSigmaMaxBps,
      lowVolConfirmSec: v.lowVolConfirmSec,
      lowVolReversionBps: v.lowVolReversionBps,
      lowVolBreakoutBps: v.lowVolBreakoutBps
    } : {}),
    ...(v.base === "SECOND_NORMAL_LIQUIDITY_ORDERBOOK_V1" ? {
      normalWindowSec: v.normalWindowSec,
      zEntry: v.zEntry,
      zReclaim: v.zReclaim,
      retestSec: v.retestSec,
      insideMin: v.insideMin,
      observedMinPct: v.observedMinPct,
      centerSlopeSec: v.centerSlopeSec,
      centerSlopeMaxBps: v.centerSlopeMaxBps,
      sigmaExpandMax: v.sigmaExpandMax,
      orderbookMaxAgeSec: v.orderbookMaxAgeSec,
      obImbalanceMin: v.obImbalanceMin,
      microMinBps: v.microMinBps,
      wallRatioMin: v.wallRatioMin,
      flowGuard: v.flowGuard,
      trueBreakFlow: v.trueBreakFlow,
      trueBreakImbalance: v.trueBreakImbalance,
      bidwallTrapEnabled: v.bidwallTrapEnabled !== false,
      bidwallTrapRet300MaxBps: v.bidwallTrapRet300MaxBps,
      bidwallTrapBid20Chg60Min: v.bidwallTrapBid20Chg60Min,
      bidwallTrapRet600MinBps: v.bidwallTrapRet600MinBps,
      qualityV2Enabled: v.qualityV2Enabled !== false,
      qualityV2DownBid20Chg60Min: v.qualityV2DownBid20Chg60Min,
      qualityV2UpFlow60Min: v.qualityV2UpFlow60Min,
      startupSkipEnabled: v.startupSkipEnabled === true,
      startupSkipThreshold: v.startupSkipThreshold,
      liquidityMode: v.liquidityMode
    } : {}),
    ...(v.base === "SECOND_BRANCH_VOTE_STARTUP_V1" ? {
      normalWindowSec: v.normalWindowSec,
      orderbookMaxAgeSec: v.orderbookMaxAgeSec,
      minVotes: v.minVotes,
      startupSkipThreshold: v.startupSkipThreshold,
      rulePath: v.rulePath
    } : {}),
    ...(v.base === "SECOND_MULTI_NORMAL_HF_STABLE_V1" ? {
      normalWindowSec: v.normalWindowSec,
      orderbookMaxAgeSec: v.orderbookMaxAgeSec,
      lowVolSigmaMaxBps: v.lowVolSigmaMaxBps,
      lowVolRangeMaxBps: v.lowVolRangeMaxBps,
      lowVolAbsRet10MaxBps: v.lowVolAbsRet10MaxBps,
      lowVolZMin: v.lowVolZMin,
      lowVolZMax: v.lowVolZMax,
      lowVolMinSignedFlow: v.lowVolMinSignedFlow,
      lowVolMaxAdverseRet30Sigma: v.lowVolMaxAdverseRet30Sigma,
      trendBaseZMin: v.trendBaseZMin,
      trendHighVolSigmaMinBps: v.trendHighVolSigmaMinBps,
      trendHighVolZMin: v.trendHighVolZMin,
      trendMinSignedFlow: v.trendMinSignedFlow,
      trendMaxSignedBook: v.trendMaxSignedBook
    } : {}),
    ...(v.base === "SECOND_MULTISCALE_PHASE_GATE_V1" ? {
      orderbookMaxAgeSec: v.orderbookMaxAgeSec,
      maxEmitAgeSec: v.maxEmitAgeSec,
      phaseLookbackSec: v.phaseLookbackSec,
      maturityHistorySec: v.maturityHistorySec,
      maturityMinPeriods: v.maturityMinPeriods,
      maturityQuantile: v.maturityQuantile,
      minFlow60: v.minFlow60,
      minImbalance20: v.minImbalance20,
      minMicropriceBps: v.minMicropriceBps,
      minVolumeRatio: v.minVolumeRatio
    } : {}),
    upReversalConfirmBps: v.upReversalConfirmBps,
    upReversalConfirmMaxSec: v.upReversalConfirmMaxSec,
    incidentFilterEnabled: v.incidentFilterEnabled,
    incidentFilterMode: v.incidentFilterMode,
    incidentWindowSec: v.incidentWindowSec,
    incidentMinMoveBps: v.incidentMinMoveBps,
    incidentMinVolumeQuantile: v.incidentMinVolumeQuantile,
    incidentMinFlowImbalance: v.incidentMinFlowImbalance,
    incidentCooldownSec: v.incidentCooldownSec,
    chipTargetShare: v.chipTargetShare,
    chipBinMode: v.chipBinMode,
    chipBinSize: v.chipBinSize,
    chipBinPct: v.chipBinPct,
    chipBreakPct: v.chipBreakPct,
    chipDirectionFilter: v.chipDirectionFilter,
    chipFilter: v.chipFilter,
    regimeLookbackSec: v.regimeLookbackSec,
    regimeDropPct: v.regimeDropPct,
    pullbackSec: v.pullbackSec,
    pullbackPct: v.pullbackPct,
    suppressReversal: v.suppressReversal
  })));
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
  const result = applyTradeConfigPatch(tradeConfig, req.body, { autoTradeSafetyGate });
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

app.get("/api/llm-config", (req, res) => {
  res.status(410).json(LLM_REMOVED_RESPONSE);
});

app.post("/api/llm-config", requireApiToken, express.json({ limit: "200kb" }), (req, res) => {
  writeLlmStatus({ state: "removed", lastSignal: null, activeTrade: null, lastError: null });
  res.status(410).json(LLM_REMOVED_RESPONSE);
});

app.get("/api/llm-status", (req, res) => {
  res.status(410).json({
    ...LLM_REMOVED_RESPONSE,
    status: {
      strategyId: LLM_STRATEGY_ID,
      state: "removed",
      activeTrade: null,
      lastPrediction: null,
      lastSignal: null,
      lastError: null
    },
    signal: { strategy_id: LLM_STRATEGY_ID, signal: null, confidence: null, reason: "llm_removed" }
  });
});

app.post("/api/llm-predict-now", requireApiToken, (req, res) => {
  writeLlmStatus({ state: "removed", lastSignal: null, activeTrade: null, lastError: null });
  res.status(410).json({ ok: false, reason: "llm_removed", ...LLM_REMOVED_RESPONSE });
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
  const gate = autoTradeSafetyGate(tradeConfig);
  // 手动单也会被 AutoJS 真实点击执行，因此必须受全局实盘总闸保护。
  // 如果总闸关闭，立即清掉遗留手动命令，避免 AutoJS 主循环在检查自动开关前先执行它。
  if (manualTrade && !gate.allow) {
    appendJsonl(TRADE_AUDIT_FILE, {
      serverTime: Date.now(),
      event: "manual_order_cleared",
      reason: "real_trading_disabled",
      verdict: gate.verdict,
      manualTrade
    });
    manualTrade = null;
  }
  res.json(manualTrade);
});

app.post('/api/manual', requireApiToken, express.json(), (req, res) => {
  const { direction, amount, duration } = req.body;
  if (direction !== 'UP' && direction !== 'DOWN') { res.json({ error: 'invalid direction' }); return; }
  const gate = autoTradeSafetyGate(tradeConfig);
  // 修复：以前手动下单接口绕过 realTradingEnabled，AutoJS 又会在自动实盘开关判断前优先执行手动命令。
  // 现在手动单必须和自动单一样经过全局实盘总闸，避免页面/配置显示关闭时仍能真实下单。
  if (!gate.allow) {
    manualTrade = null;
    appendJsonl(TRADE_AUDIT_FILE, {
      serverTime: Date.now(),
      event: "manual_order_rejected",
      reason: "real_trading_disabled",
      verdict: gate.verdict,
      requestedDirection: direction,
      requestedAmount: amount,
      requestedDuration: duration
    });
    res.status(409).json({
      error: "real_trading_disabled",
      message: "全局实盘未开启，已拒绝手动真实下单。",
      verdict: gate.verdict,
      required: gate.requiredVerdict
    });
    return;
  }
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
  for (const item of items) invalidateTradeDerivedCaches(item && item.event);
  res.json({ ok: true, ...result });
});

app.post('/api/trade-audit', requireApiToken, (req, res) => {
  readRawJson(req, (payload, raw) => {
    if (!payload || typeof payload !== 'object') {
      res.status(400).json({ error: 'invalid body', raw: raw.substring(0, 200) });
      return;
    }
    updateRealBalanceFromPayload(payload, 'trade-audit');
    const item = {
      serverTime: Date.now(),
      price: currentPrice,
      ...payload,
      realBalance
    };
    const written = appendJsonl(TRADE_AUDIT_FILE, item);
    invalidateTradeDerivedCaches((written || item).event);
    res.json({ ok: true, item: written || item });
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
