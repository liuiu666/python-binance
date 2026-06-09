const express = require("express");
const http = require("http");
const { WebSocketServer, WebSocket } = require("ws");
const path = require("path");
const fs = require("fs");
const os = require("os");
const { spawn } = require("child_process");
const { EventStore } = require("./lib/event_store");
const { createApiAuth } = require("./lib/auth");

const app = express();
const server = http.createServer(app);
const wss = new WebSocketServer({ server, path: "/ws" });
const PUBLIC_DIR = path.join(__dirname, "public");
const DASHBOARD_DIR = path.join(PUBLIC_DIR, "dashboard");
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
const PRICE_FILE = path.join(DATA_DIR, "current_price.json");
const CONFIG_FILE = path.join(DATA_DIR, "trade_config.json");
const TRADE_AUDIT_FILE = path.join(DATA_DIR, "trade_audit.jsonl");
const REAL_BALANCE_FILE = path.join(DATA_DIR, "real_balance.json");
const AUTO_SCRIPT_FILE = path.join(__dirname, "auto_btc.js");
const PRICE_TICKS_FILE = path.join(DATA_DIR, "price_ticks.jsonl");
const REPORT_FILES = {
  decision: path.join(DATA_DIR, "strategy_decision_report.json"),
  health: path.join(DATA_DIR, "strategy_health_report.json"),
  signalAudit: path.join(DATA_DIR, "signal_audit_report.json"),
  liveBacktestGap: path.join(DATA_DIR, "live_backtest_gap_report.json"),
  tenMinRegimeFilter: path.join(DATA_DIR, "ten_min_regime_filter_search.json"),
  tenMinStatefulPolicyFilter: path.join(DATA_DIR, "ten_min_stateful_policy_filter_search.json"),
  thirtyMinRegimeFilter: path.join(DATA_DIR, "thirty_min_regime_filter_search.json"),
  regimePattern: path.join(DATA_DIR, "regime_pattern_report.json"),
  liveAudit: path.join(DATA_DIR, "live_trade_audit_report.json"),
  shadowDecision: path.join(DATA_DIR, "shadow_decision_report.json"),
  latency: path.join(DATA_DIR, "execution_latency_validation.json")
};
const PYTHON_EXE = process.env.PYTHON_EXE || "python";
const SERVER_SIM_TRADING_ENABLED = process.env.SERVER_SIM_TRADING_ENABLED === "1";
const MANAGED_PROCESSES_ENABLED = process.env.DISABLE_MANAGED_PROCESSES !== "1";
const DATA_UPDATE_INTERVAL_MS = Math.max(
  60 * 1000,
  Number(process.env.DATA_UPDATE_INTERVAL_MS || 5 * 60 * 1000)
);
const REPORT_REFRESH_INTERVAL_MS = Math.max(
  60 * 1000,
  Number(process.env.REPORT_REFRESH_INTERVAL_MS || 10 * 60 * 1000)
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
  OMP_NUM_THREADS: "1",
  OPENBLAS_NUM_THREADS: "1",
  MKL_NUM_THREADS: "1",
  NUMEXPR_NUM_THREADS: "1"
};
const LIGHT_REPORT_SCRIPTS = [
  path.join(__dirname, "py", "analyze_signal_audit.py"),
  path.join(__dirname, "py", "analyze_live_backtest_gap.py"),
  path.join(__dirname, "py", "shadow_decision_report.py"),
  path.join(__dirname, "py", "strategy_health_report.py"),
  path.join(__dirname, "py", "strategy_decision_report.py")
];
const HEAVY_REPORT_SCRIPT_NAMES = new Set([
  "analyze_live_backtest_gap.py"
]);

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
  runs: 0
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
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
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

function readLastCsvRows(file, limit = 200, bytes = 65536) {
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
    const lines = buf.toString("utf8").split(/\r?\n/).filter(Boolean);
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

function configuredMaxActionableLagMs() {
  const value = Number(tradeConfig && tradeConfig.maxActionableLagMs);
  return Number.isFinite(value) && value > 0 ? value : 60 * 1000;
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
    reasons.push(...files[name].reasons);
  }

  const updateStatus = readJsonFile(DATA_UPDATE_STATUS_FILE, null);
  if (updateStatus && updateStatus.ok === false) reasons.push("data_update_failed");

  const realSignals = Object.entries(signals || {})
    .filter(([key, sig]) => !key.startsWith("_") && sig && typeof sig === "object" && !sig.shadow);
  const signalTimes = realSignals
    .map(([strategyId, sig]) => ({ strategyId, ms: signalTimeMs(sig), blocked: !!sig.data_health_blocked }))
    .filter(row => Number.isFinite(row.ms));
  if (!fs.existsSync(SIGNAL_FILE)) {
    reasons.push("signal_file_missing");
  } else if (!realSignals.length || !signalTimes.length) {
    reasons.push("signal_snapshot_missing");
  }
  const latestSignal = signalTimes.sort((a, b) => b.ms - a.ms)[0] || null;
  const signalAgeMs = latestSignal ? now - latestSignal.ms : null;
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
      status: updateStatus
    },
    signal: {
      strategies: realSignals.map(([strategyId]) => strategyId),
      latestTime: latestSignal ? new Date(latestSignal.ms).toISOString() : null,
      ageMs: signalAgeMs,
      maxAgeMs: SIGNAL_SNAPSHOT_MAX_AGE_MS
    }
  };
}

function autoTradeSafetyGate() {
  const report = readJsonFile(REPORT_FILES.shadowDecision, null);
  const verdict = report && report.safety ? report.safety.verdict : null;
  const manualOverride = !!(tradeConfig && tradeConfig.realTradingOverride);
  const allow = verdict === "allow_real_auto_trading" || manualOverride;
  return {
    allow,
    blocked: !allow,
    verdict: verdict || "missing_shadow_decision",
    requiredVerdict: "allow_real_auto_trading",
    manualOverride,
    overrideSource: manualOverride ? "trade_config.realTradingOverride" : null
  };
}

function runScript(script, cb) {
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
  const child = spawn(PYTHON_EXE, [script], {
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
    const finish = (code, signal, error) => {
      try { if (out !== null) fs.closeSync(out); } catch (e) {}
      try { if (err !== null) fs.closeSync(err); } catch (e) {}
      dataUpdate.running = false;
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
  const scripts = (
    process.env.ENABLE_HEAVY_REPORTS === "1" || String(reason).startsWith("manual")
  ) ? LIGHT_REPORT_SCRIPTS : LIGHT_REPORT_SCRIPTS.filter(script => !HEAVY_REPORT_SCRIPT_NAMES.has(path.basename(script)));
  reportRefresh.running = true;
  reportRefresh.lastStart = Date.now();
  reportRefresh.lastError = null;
  reportRefresh.runs += 1;
  appendJsonl(TRADE_AUDIT_FILE, {
    serverTime: Date.now(),
    event: "report_refresh_start",
    reason,
    scripts: scripts.map(script => path.basename(script))
  });
  let idx = 0;
  const next = () => {
    if (idx >= scripts.length) {
      reportRefresh.running = false;
      reportRefresh.lastFinish = Date.now();
      reportRefresh.lastExitCode = 0;
      appendJsonl(TRADE_AUDIT_FILE, { serverTime: Date.now(), event: "report_refresh_done", reason });
      return;
    }
    const script = scripts[idx++];
    runScript(script, (code, signal, err) => {
      if (err || code !== 0) {
        reportRefresh.running = false;
        reportRefresh.lastFinish = Date.now();
        reportRefresh.lastExitCode = code;
        reportRefresh.lastError = err ? String(err.message || err) : `code=${code} signal=${signal || ""}`;
        appendJsonl(TRADE_AUDIT_FILE, {
          serverTime: Date.now(),
          event: "report_refresh_error",
          reason,
          script,
          error: reportRefresh.lastError
        });
        return;
      }
      next();
    });
  };
  next();
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
  if (sig && sig.confidence != null) return amountForConfidence(sig.confidence, tradeConfig);
  if (sig && sig.amount && sig.fixed_amount !== true) return String(sig.amount);
  return String(tradeConfig.amount);
}

const ENTRY_TIMING_ENABLED = true;
const ENTRY_TIMING_POLICIES = {
  BTC_10min: {
    name: "pullback_0bp_then_confirm_5m",
    type: "pullback_then_confirm",
    pullbackBps: 0,
    maxWaitMin: 5,
    minPullbackDelayMs: 60000,
    minConfirmDelayMs: 60000
  },
  BTC_30min: {
    name: "pullback_5bp_within_3m",
    type: "pullback_within",
    pullbackBps: 5,
    maxWaitMin: 3,
    minPullbackDelayMs: 60000,
    minConfirmDelayMs: 0
  }
};
const entryTimingState = {};

function isoTime(ms) {
  return new Date(ms).toISOString();
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

function allowSignalForEntryTiming(sig, state, reason) {
  const now = Date.now();
  if (!state.allowedAt) {
    state.allowedAt = now;
    state.allowedActionableTime = isoTime(now);
  } else if (now - state.allowedAt > SIGNAL_EXPIRY_MS) {
    delete entryTimingState[state.strategyId];
    return blockSignalForEntryTiming(sig, state, "entry_timing_entry_window_elapsed");
  }
  return {
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
}

function applyEntryTimingForSignal(strategyId, sig) {
  const policy = ENTRY_TIMING_POLICIES[strategyId];
  if (sig && sig.bypass_entry_timing) return sig;
  if (!ENTRY_TIMING_ENABLED || !policy || !sig || !sig.signal) return sig;

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
  for (const strategyId of Object.keys(ENTRY_TIMING_POLICIES)) {
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
  const out = { ...signals };
  for (const [strategyId, sig] of Object.entries(signals)) {
    if (!sig || typeof sig !== "object" || sig.shadow) continue;
    out[strategyId] = {
      ...sig,
      signal: null,
      confidence: null,
      data_health_blocked: true,
      data_health_block_reasons: gate.reasons,
      blocked_signal: sig.blocked_signal || sig.signal || null,
      blocked_confidence: sig.blocked_confidence || (sig.confidence == null ? null : sig.confidence)
    };
  }
  return { signals: out, gate };
}

function buildSignalResponse() {
  const rawSignals = fs.existsSync(SIGNAL_FILE) ? JSON.parse(fs.readFileSync(SIGNAL_FILE, "utf8")) : {};
  const timedSignals = applyEntryTiming(rawSignals);
  const freshSignals = applyExecutionFreshnessGate(timedSignals);
  const health = applyDataHealthGate(freshSignals, dataHealthGate(freshSignals));
  const safety = applyAutoTradeSafetyGate(health.signals);
  const signals = safety.signals;
  const strategyAmounts = {};
  for (const [strategyId, sig] of Object.entries(signals)) {
    strategyAmounts[strategyId] = amountForStrategy(strategyId, sig);
  }
  const legacySig = signals.BTC_30min || signals.BTC_10min;
  const legacyAmount = legacySig ? amountForStrategy(legacySig.strategy_id || "BTC_30min", legacySig) : String(tradeConfig.amount);
  return {
    ...signals,
    _config: tradeConfig,
    _strategyAmounts: strategyAmounts,
    _signalAmount: legacyAmount,
    _entryTimingEnabled: ENTRY_TIMING_ENABLED,
    _entryTimingPolicies: ENTRY_TIMING_POLICIES,
    _execution: {
      serverTime: isoTime(Date.now()),
      currentPrice: Number.isFinite(Number(currentPrice)) ? Number(currentPrice) : null,
      maxActionableLagMs: configuredMaxActionableLagMs()
    },
    _dataHealthGate: health.gate,
    _autoTradeSafetyGate: safety.gate
  };
}

app.get("/api/signal", (req, res) => {
  try {
    res.json(buildSignalResponse());
  } catch (e) { res.json({ _config: tradeConfig, _signalAmount: String(tradeConfig.amount) }); }
});
app.get("/api/price", (req, res) => {
  try { res.json(fs.existsSync(PRICE_FILE) ? JSON.parse(fs.readFileSync(PRICE_FILE, "utf8")) : { price: null }); }
  catch (e) { res.json({ price: null }); }
});

app.get("/api/reports", (req, res) => {
  res.json({
    decision: readJsonFile(REPORT_FILES.decision),
    health: readJsonFile(REPORT_FILES.health),
    signalAudit: readJsonFile(REPORT_FILES.signalAudit),
    liveBacktestGap: readJsonFile(REPORT_FILES.liveBacktestGap),
    tenMinRegimeFilter: readJsonFile(REPORT_FILES.tenMinRegimeFilter),
    tenMinStatefulPolicyFilter: readJsonFile(REPORT_FILES.tenMinStatefulPolicyFilter),
    thirtyMinRegimeFilter: readJsonFile(REPORT_FILES.thirtyMinRegimeFilter),
    regimePattern: readJsonFile(REPORT_FILES.regimePattern),
    liveAudit: readJsonFile(REPORT_FILES.liveAudit),
    shadowDecision: readJsonFile(REPORT_FILES.shadowDecision),
    latency: readJsonFile(REPORT_FILES.latency),
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
    "autojs_heartbeat",
    "signal_tradeable",
    "signal_skipped",
    "order_attempt",
    "order_abort",
    "order_done"
  ]);
  const autojsRows = rows.filter(r => autojsEventNames.has(r.event));
  const latestTabletPagePing = [...rows].reverse().find(r => (
    r.event === "tablet_page_ping" && r.source !== "codex_local_probe"
  )) || null;
  const latestEvent = autojsRows.length ? autojsRows[autojsRows.length - 1] : null;
  const latestHeartbeat = [...autojsRows].reverse().find(r => r.event === "autojs_heartbeat") || null;
  const latestOrderDone = [...autojsRows].reverse().find(r => r.event === "order_done") || null;
  const now = Date.now();
  const ageOf = row => row && row.serverTime ? now - Number(row.serverTime) : null;
  const heartbeatAgeMs = ageOf(latestHeartbeat);
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
    tabletPageSeen: tabletPagePingAgeMs != null && tabletPagePingAgeMs <= 120000,
    loaderStarted: autojsRows.some(r => r.event === "autojs_loader_start"),
    loaderError: autojsRows.some(r => r.event === "autojs_loader_error"),
    autojsStarted: autojsRows.some(r => r.event === "autojs_start"),
    heartbeatOnline: heartbeatAgeMs != null && heartbeatAgeMs <= 120000,
    balanceRecent: balanceAgeMs != null && balanceAgeMs <= 120000,
    orderDoneSeen: !!latestOrderDone
  };
  const nextAction = !checks.tabletPageSeen
    ? `Open ${runtime.tabletPageUrl} on the tablet to confirm tablet network access.`
    : checks.loaderError && !checks.autojsStarted
      ? "Loader ran but failed; check AutoJS log for autojs_loader_error and retry the loader URL."
    : !checks.loaderStarted
      ? `Tablet browser reaches server; run loader ${runtime.loaderUrl} or bootstrap ${runtime.bootstrapUrl} in AutoJS.`
    : !checks.autojsStarted
      ? `Loader ran; wait for autojs_start or run latest auto_btc.js from ${runtime.scriptUrl} directly.`
    : !checks.heartbeatOnline
      ? "AutoJS was seen but heartbeat is stale; restart the tablet script and confirm it can POST /api/trade-audit."
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
    latestOrderDone,
    balance: realBalance,
    balanceAgeMs,
    recentAutojsEvents: autojsRows.slice(-20)
  };
}

function priceAtOrAfter(ticks, targetTime) {
  let lo = 0, hi = ticks.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (Number(ticks[mid].time) < targetTime) lo = mid + 1;
    else hi = mid;
  }
  return lo < ticks.length ? Number(ticks[lo].price) : null;
}

function settleStatus(direction, openPrice, closePrice) {
  if (openPrice == null || closePrice == null) return "pending";
  if (Number(closePrice) === Number(openPrice)) return "tie";
  if (direction === "UP") return Number(closePrice) > Number(openPrice) ? "won" : "lost";
  if (direction === "DOWN") return Number(closePrice) < Number(openPrice) ? "won" : "lost";
  return "pending";
}

function statusPnl(status, amount) {
  const stake = Number(amount) || 0;
  if (status === "won") return Number((stake * PAYOUT_RATE).toFixed(2));
  if (status === "lost") return -stake;
  return 0;
}

function liveOrderHistory(limit = 100) {
  const audit = readJsonl(TRADE_AUDIT_FILE);
  const ticks = readJsonl(PRICE_TICKS_FILE)
    .filter(t => Number.isFinite(Number(t.time)) && Number.isFinite(Number(t.price)))
    .map(t => ({ time: Number(t.time), price: Number(t.price) }))
    .sort((a, b) => a.time - b.time);

  const rows = [];
  for (const row of audit) {
    if (row.event !== "order_done") continue;
    const duration = Math.max(1, Number(row.duration) || 0);
    const openTime = Number(row.serverTime || row.clientTime || 0);
    if (!duration || !openTime) continue;
    const openPrice = row.price != null ? Number(row.price) : priceAtOrAfter(ticks, openTime);
    const settleTime = openTime + duration * 60 * 1000;
    const closePrice = priceAtOrAfter(ticks, settleTime);
    const status = settleStatus(row.direction, openPrice, closePrice);
    const amount = Number(row.amount) || 0;
    const id = [
      "autojs",
      row.strategyId || "manual",
      row.signalTime || "",
      row.queueBatchId || "",
      openTime
    ].join("|");
    rows.push({
      id,
      source: "autojs",
      event: row.event,
      strategyId: row.strategyId || "manual",
      direction: row.direction,
      amount,
      duration: String(duration),
      openTime,
      settleTime,
      openPrice,
      closePrice,
      status,
      pnl: statusPnl(status, amount),
      confidence: row.confidence,
      rsi_value: row.rsi_value,
      avg_prob: row.avg_prob,
      threshold: row.threshold,
      signalTime: row.signalTime,
      actionableTime: row.actionableTime,
      queueBatchId: row.queueBatchId,
      queuePosition: row.queuePosition,
      queueLength: row.queueLength,
      queueOrderPolicy: row.queueOrderPolicy,
      device: row.device,
      balance: row.balance,
      realBalance: row.realBalance
    });
  }

  for (const row of audit) {
    if (row.event !== "order_abort") continue;
    const openTime = Number(row.serverTime || row.clientTime || 0);
    if (!openTime) continue;
    const id = ["autojs_abort", row.strategyId || "manual", row.signalTime || "", openTime].join("|");
    rows.push({
      id,
      source: "autojs",
      event: row.event,
      strategyId: row.strategyId || "manual",
      direction: row.direction,
      amount: Number(row.amount) || 0,
      duration: String(row.duration || ""),
      openTime,
      settleTime: openTime,
      openPrice: row.price != null ? Number(row.price) : null,
      closePrice: null,
      status: "aborted",
      pnl: 0,
      reason: row.reason,
      confidence: row.confidence,
      rsi_value: row.rsi_value,
      signalTime: row.signalTime,
      queueBatchId: row.queueBatchId,
      queuePosition: row.queuePosition,
      queueLength: row.queueLength,
      device: row.device,
      balance: row.balance,
      realBalance: row.realBalance
    });
  }

  for (const t of trades) {
    rows.push({
      id: "server|" + t.id,
      source: t.source || "server",
      event: "server_trade",
      strategyId: String(t.source || "").replace(/^auto:/, "") || "manual",
      direction: t.direction,
      amount: Number(t.amount) || 0,
      duration: String(t.duration || ""),
      openTime: Number(t.openTime),
      settleTime: Number(t.settleTime),
      openPrice: t.strikePrice,
      closePrice: t.settlePrice,
      status: t.status === "active" ? "pending" : t.status,
      pnl: t.status === "won" ? Number(((Number(t.payout) || 0) - (Number(t.amount) || 0)).toFixed(2)) : statusPnl(t.status, t.amount),
      confidence: null,
      rsi_value: null
    });
  }

  rows.sort((a, b) => Number(b.openTime || 0) - Number(a.openTime || 0));

  const seen = new Set();
  const unique = rows.filter(row => {
    const key = row.id || JSON.stringify([row.source, row.strategyId, row.signalTime, row.openTime]);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });

  const settled = unique.filter(r => ["won", "lost", "tie"].includes(r.status));
  const wins = settled.filter(r => r.status === "won").length;
  const losses = settled.filter(r => r.status === "lost").length;
  const ties = settled.filter(r => r.status === "tie").length;
  const pnl = settled.reduce((sum, r) => sum + (Number(r.pnl) || 0), 0);
  const pending = unique.filter(r => r.status === "pending").length;
  const active = unique.filter(r => r.status === "pending").slice(0, limit);
  const recent = unique.slice(0, limit);
  return {
    updatedAt: Date.now(),
    summary: {
      total: unique.length,
      settled: settled.length,
      wins,
      losses,
      ties,
      pending,
      winRate: settled.length ? Number((wins / settled.length * 100).toFixed(2)) : null,
      pnl: Number(pnl.toFixed(2))
    },
    active,
    recent
  };
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
const PAYOUT_RATE = 0.85;
const DURATION_MS = 30 * 60 * 1000;  // 30 minutes
const WINDOW_SEC = 60;  // 60-second trading window (wider than before)
const AUTO_TRADE_AMOUNT = 100;  // Auto-trade 100 USDT per signal
const AUTO_TRADE_ENABLED = SERVER_SIM_TRADING_ENABLED;
const SIGNAL_EXPIRY_MS = 120 * 1000;  // Signal valid for 2 minutes

let currentPrice = null;
let priceHistory = [];
const MAX_HISTORY = 600;
let trades = [];
let nextTradeId = 1;
let account = { balance: 10000.0, totalTrades: 0, wins: 0, losses: 0, totalPnl: 0 };
let lastSignals = {};
let autoTradeLog = [];
let realBalance = normalizeRealBalance(readJsonFile(REAL_BALANCE_FILE, null));

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

function readPrice() {
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
          broadcastPrice();
        }
      }
    }
  } catch (e) {}
}
setInterval(readPrice, 2000);
readPrice();

function broadcastPrice() {
  const msg = JSON.stringify({ type: "price", price: currentPrice, time: Date.now(), history: priceHistory.slice(-300) });
  wss.clients.forEach(c => { if (c.readyState === WebSocket.OPEN) c.send(msg); });
}

function broadcastState() {
  const msg = JSON.stringify({
    type: "state",
    account: { ...account },
    activeTrades: trades.filter(t => t.status === "active"),
    recentTrades: trades.filter(t => t.status !== "active").slice(-20).reverse(),
    autoTradeLog: autoTradeLog.slice(-10).reverse(),
    autoTradeEnabled: AUTO_TRADE_ENABLED && tradeConfig.autoTrade,
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

// Auto-trade logic
function checkAutoTrade() {
  if (!SERVER_SIM_TRADING_ENABLED || !AUTO_TRADE_ENABLED || !tradeConfig.autoTrade || !currentPrice) return;
  const status = getTradeWindowStatus();
  if (!status.inWindow) return;
  
  // Read current signal
  try {
    if (!fs.existsSync(SIGNAL_FILE)) return;
    const signals = buildSignalResponse();
    for (const strategyId of ["BTC_30min", "BTC_10min"]) {
      const sig = signals[strategyId];
      if (!sig || !sig.signal || !sig.confidence) continue;
      if (!sig.bypass_min_confidence_filter && Number(sig.confidence) < Number(tradeConfig.minConfidence || 0)) continue;
      if (tradeConfig.preventOverlapOrders && trades.some(t => t.status === "active" && t.source === "auto:" + strategyId)) continue;

      const sigTime = signalActionableMs(sig);
      if (!sigTime || Date.now() - sigTime > configuredMaxActionableLagMs()) continue;

      const last = lastSignals[strategyId];
      if (last && last.signal === sig.signal && last.time === sig.time) continue;
      const autoAmt = Number(amountForStrategy(strategyId, sig));
      const trade = placeTrade(sig.signal, autoAmt, "auto:" + strategyId, sig.duration || sig.interval_min);
      if (trade) {
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
        console.log(`[Auto] #${trade.id} ${strategyId} ${sig.signal} ${sig.confidence}% @ ${currentPrice} (${autoAmt} USDT)`);
        broadcastTradeUpdate(trade);
      }
    }
  } catch (e) {}
}
setInterval(checkAutoTrade, 3000);

function settleTrades() {
  const now = Date.now();
  trades.filter(t => t.status === "active" && now >= t.settleTime).forEach(t => {
    const sp = currentPrice || t.strikePrice;
    let won = t.direction === "UP" ? sp > t.strikePrice : sp < t.strikePrice;
    const tie = sp === t.strikePrice;
    if (tie) { t.status = "tie"; t.settlePrice = sp; t.payout = t.amount; account.balance += t.amount; }
    else if (won) { t.status = "won"; t.settlePrice = sp; t.payout = t.amount + t.amount * PAYOUT_RATE; account.balance += t.payout; account.wins++; account.totalPnl += t.amount * PAYOUT_RATE; }
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
      payout: t.payout
    });
    console.log(`[Settle] #${t.id} ${t.status} ${t.direction} strike=${t.strikePrice} settle=${sp} pnl=${t.status === "won" ? "+" + (t.payout - t.amount).toFixed(2) : t.status === "lost" ? "-" + t.amount.toFixed(2) : "0"}`);
    broadcastTradeUpdate(t);
  });
  if (trades.length > 200) trades = trades.filter(t => t.status === "active" || trades.indexOf(t) > trades.length - 101);
}
setInterval(settleTrades, 1000);
setInterval(broadcastState, 2000);

wss.on("connection", (ws) => {
  const wsInit = {
    type: "init", price: currentPrice, time: Date.now(), history: priceHistory.slice(-300),
    account: { ...account }, activeTrades: trades.filter(t => t.status === "active"),
    recentTrades: trades.filter(t => t.status !== "active").slice(-20).reverse(),
    autoTradeLog: autoTradeLog.slice(-10).reverse(), autoTradeEnabled: AUTO_TRADE_ENABLED && tradeConfig.autoTrade,
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
const DEFAULT_TRADE_CONFIG = {
  amount: "5", duration: "30", autoTrade: false, minConfidence: 35,
  tiersEnabled: false, tiers: [{min:80,amount:20},{min:60,amount:10},{min:40,amount:5}],
  skipConflictSignals: false,
  queueOrderPolicy: "confidence_desc",
  preventOverlapOrders: true,
  realTradingOverride: false,
  maxActionableLagMs: 60000
};

let tradeConfig = (() => {
  try {
    if (fs.existsSync(CONFIG_FILE)) {
      const saved = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'));
      return { ...DEFAULT_TRADE_CONFIG, ...saved };
    }
  } catch (e) {}
  return { ...DEFAULT_TRADE_CONFIG };
})();

function saveTradeConfig() {
  try { fs.writeFileSync(CONFIG_FILE, JSON.stringify(tradeConfig, null, 2)); } catch (e) {}
}

function amountForConfidence(conf, cfg) {
  if (cfg.tiersEnabled && Array.isArray(cfg.tiers) && cfg.tiers.length) {
    const sorted = [...cfg.tiers].sort((a,b) => Number(b.min) - Number(a.min));
    for (const t of sorted) {
      if (Number(conf) >= Number(t.min)) return String(t.amount);
    }
  }
  return String(cfg.amount);
}

app.get("/api/config", (req, res) => {
  res.json(tradeConfig);
});

app.post("/api/config", requireApiToken, express.json(), (req, res) => {
  let safetyBlocked = null;
  let forceAutoTrade = false;
  if (req.body.amount !== undefined) tradeConfig.amount = String(req.body.amount);
  if (req.body.duration !== undefined) tradeConfig.duration = String(req.body.duration);
  if (req.body.autoTrade !== undefined) {
    const requestedAutoTrade = !!req.body.autoTrade;
    if (requestedAutoTrade) {
      const gate = autoTradeSafetyGate();
      forceAutoTrade = req.body.forceAutoTrade === true || req.body.forceAutoTrade === "true";
      if (gate.blocked && !forceAutoTrade) {
        tradeConfig.autoTrade = false;
        safetyBlocked = gate;
        appendJsonl(TRADE_AUDIT_FILE, {
          serverTime: Date.now(),
          event: "auto_trade_safety_block",
          gate
        });
      } else {
        tradeConfig.autoTrade = true;
        if (forceAutoTrade) tradeConfig.realTradingOverride = true;
        if (forceAutoTrade && gate.blocked) {
          appendJsonl(TRADE_AUDIT_FILE, {
            serverTime: Date.now(),
            event: "auto_trade_force_enabled",
            gate,
            config: {
              amount: tradeConfig.amount,
              minConfidence: tradeConfig.minConfidence,
              tiersEnabled: tradeConfig.tiersEnabled,
              tiers: tradeConfig.tiers,
              preventOverlapOrders: tradeConfig.preventOverlapOrders,
              queueOrderPolicy: tradeConfig.queueOrderPolicy
            }
          });
        }
      }
    } else {
      tradeConfig.autoTrade = false;
      tradeConfig.realTradingOverride = false;
    }
  }
  if (req.body.realTradingOverride !== undefined) {
    tradeConfig.realTradingOverride = req.body.realTradingOverride === true || req.body.realTradingOverride === "true";
  }
  if (req.body.minConfidence !== undefined) tradeConfig.minConfidence = Number(req.body.minConfidence);
  if (req.body.maxActionableLagMs !== undefined) {
    const lag = Number(req.body.maxActionableLagMs);
    if (Number.isFinite(lag) && lag >= 5000 && lag <= 10 * 60 * 1000) {
      tradeConfig.maxActionableLagMs = Math.round(lag);
    }
  }
  if (req.body.tiersEnabled !== undefined) tradeConfig.tiersEnabled = !!req.body.tiersEnabled;
  if (req.body.skipConflictSignals !== undefined) tradeConfig.skipConflictSignals = !!req.body.skipConflictSignals;
  if (req.body.preventOverlapOrders !== undefined) tradeConfig.preventOverlapOrders = !!req.body.preventOverlapOrders;
  if (req.body.queueOrderPolicy !== undefined) {
    const allowed = new Set(["confidence_desc", "30_then_10", "10_then_30"]);
    if (allowed.has(String(req.body.queueOrderPolicy))) tradeConfig.queueOrderPolicy = String(req.body.queueOrderPolicy);
  }
  if (Array.isArray(req.body.tiers)) {
    tradeConfig.tiers = req.body.tiers
      .map(t => ({ min: Number(t.min), amount: Number(t.amount) }))
      .filter(t => !isNaN(t.min) && !isNaN(t.amount) && t.min >= 0 && t.min <= 100 && t.amount > 0)
      .sort((a,b) => b.min - a.min);
  }
    saveTradeConfig();
  console.log("[Config] Updated:", JSON.stringify(tradeConfig));
  res.json({ ...tradeConfig, safetyBlocked, forceAutoTrade });
});


// --- Manual Trade Command ---
let manualTrade = null; // { direction: 'UP'|'DOWN', amount: '5', duration: '30', time: Date.now() }

app.get('/api/manual', (req, res) => {
  res.json(manualTrade);
});

app.post('/api/manual', requireApiToken, express.json(), (req, res) => {
  const { direction, amount, duration } = req.body;
  if (direction !== 'UP' && direction !== 'DOWN') { res.json({ error: 'invalid direction' }); return; }
  manualTrade = { direction, amount: amount || tradeConfig.amount, duration: duration || tradeConfig.duration, time: Date.now() };
  console.log('[Manual] Trade command:', JSON.stringify(manualTrade));
  res.json(manualTrade);
});

app.delete('/api/manual', requireApiToken, (req, res) => {
  manualTrade = null;
  res.json({ cleared: true });
});

// --- Trade audit reported by AutoJS tablet and server simulator ---
app.get('/api/trade-audit', (req, res) => {
  const limit = Math.min(500, Math.max(1, Number(req.query.limit) || 100));
  res.json({ serverId: eventStore.serverId, items: tailJsonl(TRADE_AUDIT_FILE, limit) });
});

app.get('/api/trade-history', (req, res) => {
  const limit = Math.min(300, Math.max(10, Number(req.query.limit) || 100));
  res.json(liveOrderHistory(limit));
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

process.on("SIGINT", stopSignalService);
process.on("SIGTERM", stopSignalService);
process.on("exit", stopSignalService);

server.listen(PORT, '0.0.0.0', () => {
  if (MANAGED_PROCESSES_ENABLED) {
    runDataUpdate("server_listen");
    startSignalService("server_listen");
    setInterval(() => runDataUpdate("timer"), DATA_UPDATE_INTERVAL_MS);
    refreshLightReports("server_listen");
    setInterval(() => refreshLightReports("timer"), REPORT_REFRESH_INTERVAL_MS);
  } else {
    console.log("[Server] Managed Python processes disabled by DISABLE_MANAGED_PROCESSES=1");
  }
  console.log(`BTC 二元期权 http://localhost:${PORT} | 自动交易: ${AUTO_TRADE_ENABLED}`);
});





