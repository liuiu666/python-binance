const express = require("express");
const http = require("http");
const { WebSocketServer, WebSocket } = require("ws");
const path = require("path");
const fs = require("fs");
const os = require("os");
const { spawn } = require("child_process");

const app = express();
const server = http.createServer(app);
const wss = new WebSocketServer({ server, path: "/ws" });
app.use(express.static(path.join(__dirname, "public")));
const PORT = process.env.PORT || 3000;

const SIGNAL_FILE = path.join(__dirname, "data", "live_signals.json");
const SIGNAL_SCRIPT_FILE = path.join(__dirname, "py", "signal_btc.py");
const SIGNAL_STDOUT_FILE = path.join(__dirname, ".sig.out");
const SIGNAL_STDERR_FILE = path.join(__dirname, ".sig.err");
const REPORT_STDOUT_FILE = path.join(__dirname, ".reports.out");
const REPORT_STDERR_FILE = path.join(__dirname, ".reports.err");
const PRICE_FILE = path.join(__dirname, "data", "current_price.json");
const CONFIG_FILE = path.join(__dirname, "data", "trade_config.json");
const TRADE_AUDIT_FILE = path.join(__dirname, "data", "trade_audit.jsonl");
const AUTO_SCRIPT_FILE = path.join(__dirname, "auto_btc.js");
const PRICE_TICKS_FILE = path.join(__dirname, "data", "price_ticks.jsonl");
const REPORT_FILES = {
  decision: path.join(__dirname, "data", "strategy_decision_report.json"),
  health: path.join(__dirname, "data", "strategy_health_report.json"),
  signalAudit: path.join(__dirname, "data", "signal_audit_report.json"),
  liveBacktestGap: path.join(__dirname, "data", "live_backtest_gap_report.json"),
  tenMinRegimeFilter: path.join(__dirname, "data", "ten_min_regime_filter_search.json"),
  liveAudit: path.join(__dirname, "data", "live_trade_audit_report.json"),
  shadowDecision: path.join(__dirname, "data", "shadow_decision_report.json"),
  latency: path.join(__dirname, "data", "execution_latency_validation.json")
};
const PYTHON_EXE = process.env.PYTHON_EXE || "python";
const REPORT_REFRESH_INTERVAL_MS = 60 * 1000;
const LIGHT_REPORT_SCRIPTS = [
  path.join(__dirname, "py", "analyze_signal_audit.py"),
  path.join(__dirname, "py", "analyze_live_backtest_gap.py"),
  path.join(__dirname, "py", "shadow_decision_report.py"),
  path.join(__dirname, "py", "strategy_decision_report.py")
];

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
  try { fs.appendFileSync(file, JSON.stringify(obj) + "\n"); } catch (e) {}
}

function tailJsonl(file, limit) {
  try {
    if (!fs.existsSync(file)) return [];
    return fs.readFileSync(file, "utf8").trim().split(/\r?\n/).filter(Boolean).slice(-limit).map(line => {
      try { return JSON.parse(line); } catch (e) { return { raw: line }; }
    });
  } catch (e) { return []; }
}

function readJsonFile(file, fallback = null) {
  try { return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : fallback; }
  catch (e) { return fallback; }
}

function autoTradeSafetyGate() {
  const report = readJsonFile(REPORT_FILES.shadowDecision, null);
  const verdict = report && report.safety ? report.safety.verdict : null;
  const allow = verdict === "allow_real_auto_trading";
  return {
    allow,
    blocked: !allow,
    verdict: verdict || "missing_shadow_decision",
    requiredVerdict: "allow_real_auto_trading"
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
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
    stdio: ["ignore", out, err]
  });
  child.on("exit", (code, signal) => finish(code, signal));
  child.on("error", (e) => finish(null, null, e));
}

function refreshLightReports(reason = "timer") {
  if (reportRefresh.running) return;
  reportRefresh.running = true;
  reportRefresh.lastStart = Date.now();
  reportRefresh.lastError = null;
  reportRefresh.runs += 1;
  appendJsonl(TRADE_AUDIT_FILE, { serverTime: Date.now(), event: "report_refresh_start", reason });
  let idx = 0;
  const next = () => {
    if (idx >= LIGHT_REPORT_SCRIPTS.length) {
      reportRefresh.running = false;
      reportRefresh.lastFinish = Date.now();
      reportRefresh.lastExitCode = 0;
      appendJsonl(TRADE_AUDIT_FILE, { serverTime: Date.now(), event: "report_refresh_done", reason });
      return;
    }
    const script = LIGHT_REPORT_SCRIPTS[idx++];
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

function buildSignalResponse() {
  const signals = fs.existsSync(SIGNAL_FILE) ? JSON.parse(fs.readFileSync(SIGNAL_FILE, "utf8")) : {};
  const strategyAmounts = {};
  for (const [strategyId, sig] of Object.entries(signals)) {
    strategyAmounts[strategyId] = amountForStrategy(strategyId, sig);
  }
  const legacySig = signals.BTC_30min || signals.BTC_10min;
  const legacyAmount = legacySig ? amountForStrategy(legacySig.strategy_id || "BTC_30min", legacySig) : String(tradeConfig.amount);
  return { ...signals, _config: tradeConfig, _strategyAmounts: strategyAmounts, _signalAmount: legacyAmount };
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
    liveAudit: readJsonFile(REPORT_FILES.liveAudit),
    shadowDecision: readJsonFile(REPORT_FILES.shadowDecision),
    latency: readJsonFile(REPORT_FILES.latency),
    reportRefresh
  });
});

app.post("/api/reports/refresh", (req, res) => {
  refreshLightReports("manual_api");
  res.json({ ok: true, reportRefresh });
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
  const base = preferred ? preferred.url : `http://127.0.0.1:${PORT}`;
  return {
    port: Number(PORT),
    urls,
    tabletUrl: base,
    tabletPageUrl: `${base}/tablet.html`,
    auditUrl: `${base}/api/trade-audit`,
    signalUrl: `${base}/api/signal`,
    scriptUrl: `${base}/auto_btc.js`,
    loaderUrl: `${base}/auto_btc_loader.js`,
    bootstrapUrl: `${base}/auto_btc_bootstrap.js`,
    scriptVersion: autoScriptVersion()
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

app.get("/api/runtime", (req, res) => {
  res.json(runtimeInfo());
});

app.get("/api/signal-service", (req, res) => {
  res.json({
    ...signalService,
    running: !!signalService.pid,
    python: PYTHON_EXE,
    script: SIGNAL_SCRIPT_FILE
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
const AUTO_TRADE_ENABLED = true;
const SIGNAL_EXPIRY_MS = 120 * 1000;  // Signal valid for 2 minutes

let currentPrice = null;
let priceHistory = [];
const MAX_HISTORY = 600;
let trades = [];
let nextTradeId = 1;
let account = { balance: 10000.0, totalTrades: 0, wins: 0, losses: 0, totalPnl: 0 };
let lastSignals = {};
let autoTradeLog = [];
let realBalance = { amount: null, time: null, device: null };

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
  if (!AUTO_TRADE_ENABLED || !tradeConfig.autoTrade || !currentPrice) return;
  const status = getTradeWindowStatus();
  if (!status.inWindow) return;
  
  // Read current signal
  try {
    if (!fs.existsSync(SIGNAL_FILE)) return;
    const signals = JSON.parse(fs.readFileSync(SIGNAL_FILE, "utf8"));
    for (const strategyId of ["BTC_30min", "BTC_10min"]) {
      const sig = signals[strategyId];
      if (!sig || !sig.signal || !sig.confidence) continue;
      if (Number(sig.confidence) < Number(tradeConfig.minConfidence || 0)) continue;
      if (tradeConfig.preventOverlapOrders && trades.some(t => t.status === "active" && t.source === "auto:" + strategyId)) continue;

      // Check if signal is fresh (within last 2 minutes)
      const sigTime = new Date(sig.actionable_time || sig.candle_close_time || sig.time).getTime();
      if (Date.now() - sigTime > SIGNAL_EXPIRY_MS) continue;

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
  preventOverlapOrders: true
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

app.post("/api/config", express.json(), (req, res) => {
  let safetyBlocked = null;
  if (req.body.amount !== undefined) tradeConfig.amount = String(req.body.amount);
  if (req.body.duration !== undefined) tradeConfig.duration = String(req.body.duration);
  if (req.body.autoTrade !== undefined) {
    const requestedAutoTrade = !!req.body.autoTrade;
    if (requestedAutoTrade) {
      const gate = autoTradeSafetyGate();
      if (gate.blocked) {
        tradeConfig.autoTrade = false;
        safetyBlocked = gate;
        appendJsonl(TRADE_AUDIT_FILE, {
          serverTime: Date.now(),
          event: "auto_trade_safety_block",
          gate
        });
      } else {
        tradeConfig.autoTrade = true;
      }
    } else {
      tradeConfig.autoTrade = false;
    }
  }
  if (req.body.minConfidence !== undefined) tradeConfig.minConfidence = Number(req.body.minConfidence);
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
  res.json({ ...tradeConfig, safetyBlocked });
});


// --- Manual Trade Command ---
let manualTrade = null; // { direction: 'UP'|'DOWN', amount: '5', duration: '30', time: Date.now() }

app.get('/api/manual', (req, res) => {
  res.json(manualTrade);
});

app.post('/api/manual', express.json(), (req, res) => {
  const { direction, amount, duration } = req.body;
  if (direction !== 'UP' && direction !== 'DOWN') { res.json({ error: 'invalid direction' }); return; }
  manualTrade = { direction, amount: amount || tradeConfig.amount, duration: duration || tradeConfig.duration, time: Date.now() };
  console.log('[Manual] Trade command:', JSON.stringify(manualTrade));
  res.json(manualTrade);
});

app.delete('/api/manual', (req, res) => {
  manualTrade = null;
  res.json({ cleared: true });
});

// --- Trade audit reported by AutoJS tablet and server simulator ---
app.get('/api/trade-audit', (req, res) => {
  const limit = Math.min(500, Math.max(1, Number(req.query.limit) || 100));
  res.json({ items: tailJsonl(TRADE_AUDIT_FILE, limit) });
});

app.post('/api/trade-audit', (req, res) => {
  readRawJson(req, (payload, raw) => {
    if (!payload || typeof payload !== 'object') {
      res.status(400).json({ error: 'invalid body', raw: raw.substring(0, 200) });
      return;
    }
    const item = {
      serverTime: Date.now(),
      price: currentPrice,
      realBalance,
      ...payload
    };
    appendJsonl(TRADE_AUDIT_FILE, item);
    res.json({ ok: true, item });
  });
});

// --- Real balance (reported by auto_btc.js on tablet) ---
app.get('/api/balance', (req, res) => {
  res.json(realBalance);
});

app.post('/api/balance', (req, res) => {
  readRawJson(req, (payload, raw) => {
    if (!payload) {
      console.log('[Balance POST] failed to parse body');
      res.status(400).json({ error: 'invalid body', raw: raw.substring(0, 200) });
      return;
    }
    const amt = parseFloat(payload.amount);
    if (isNaN(amt) || amt < 0) {
      console.log('[Balance POST] invalid amount:', payload.amount);
      res.status(400).json({ error: 'invalid amount' });
      return;
    }
    realBalance = {
      amount: amt,
      time: Number(payload.time) || Date.now(),
      device: payload.device || 'unknown'
    };
    console.log('[Balance] ' + realBalance.device + ': ' + amt + ' USDT');
    wss.clients.forEach(cl => { if (cl.readyState === WebSocket.OPEN) cl.send(JSON.stringify({ type: 'balance', ...realBalance })); });
    res.json(realBalance);
  });
});

process.on("SIGINT", stopSignalService);
process.on("SIGTERM", stopSignalService);
process.on("exit", stopSignalService);

server.listen(PORT, '0.0.0.0', () => {
  startSignalService("server_listen");
  refreshLightReports("server_listen");
  setInterval(() => refreshLightReports("timer"), REPORT_REFRESH_INTERVAL_MS);
  console.log(`BTC 二元期权 http://localhost:${PORT} | 自动交易: ${AUTO_TRADE_ENABLED}`);
});





