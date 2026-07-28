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
      SHADOW_EXECUTION_DELAY_MS: "20",
      API_TOKEN: token,
      CODEX_API_TOKEN: "",
      TRADE_API_TOKEN: ""
    },
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"]
  });
}

function writeFreshMinuteCandles(dataDir, now = Date.now()) {
  let csv = "open_time,open,high,low,close,volume\n";
  for (let i = 299; i >= 0; i -= 1) {
    csv += `${new Date(now - i * 60_000).toISOString()},1,1,1,1,1\n`;
  }
  fs.writeFileSync(path.join(dataDir, "btcusdt_1m.csv"), csv, "utf8");
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

test("data health uses signal snapshot time instead of stale strategy candle time", async () => {
  await withServer({}, async ({ baseUrl, dataDir }) => {
    const now = Date.now();
    writeFreshMinuteCandles(dataDir, now);
    fs.writeFileSync(path.join(dataDir, "live_signals.json"), JSON.stringify({
      TEST_OLD_STRATEGY_TIME: {
        strategy_id: "TEST_OLD_STRATEGY_TIME",
        time: "2020-01-01T00:00:00Z",
        signal: null,
        reason: "test_old_strategy_time"
      },
      _snapshot_time_ms: now,
      _snapshot_time: new Date(now).toISOString(),
      _snapshot_strategy_count: 1
    }, null, 2), "utf8");

    const health = await requestJson(baseUrl, "/api/data-health");
    assert.equal(health.status, 200);
    assert.equal(health.json.blocked, false);
    assert.equal(health.json.signal.freshnessSource, "snapshot_time");
    assert.match(health.json.signal.latestStrategyTime, /^2020-01-01T00:00:00/);
    assert.ok(!health.json.reasons.includes("signal_snapshot_stale"));
    assert.ok(health.json.files.openInterest);
    assert.ok(health.json.files.globalLsratio);
    assert.ok(health.json.files.topAccountLsratio);
  });
});

test("auction data health requires fresh collector status and depth stream", async () => {
  await withServer({}, async ({ baseUrl, dataDir }) => {
    const now = Date.now();
    const statusPath = path.join(dataDir, "auction_data_status.json");
    fs.writeFileSync(statusPath, JSON.stringify({
      ok: true,
      updated_at: new Date(now).toISOString(),
      event_age_ms: 20,
      streams: { depth_updates: { age_ms: 20 } },
      trades: 12,
      depth_updates: 9
    }), "utf8");

    const healthy = await requestJson(baseUrl, "/api/auction-data-health");
    assert.equal(healthy.status, 200);
    assert.equal(healthy.json.ok, true);
    assert.equal(healthy.json.depthAgeMs, 20);

    fs.writeFileSync(statusPath, JSON.stringify({
      ok: true,
      updated_at: new Date(now - 60_000).toISOString(),
      event_age_ms: 0,
      streams: { depth_updates: { age_ms: 0 } }
    }), "utf8");
    const staleStatus = await requestJson(baseUrl, "/api/auction-data-health");
    assert.equal(staleStatus.status, 200);
    assert.equal(staleStatus.json.ok, false);
  });
});

test("tablet signal mirror records one shadow trade for repeated signal polls", async () => {
  await withServer({}, async ({ baseUrl, dataDir }) => {
    const now = Date.now();
    writeFreshMinuteCandles(dataDir, now);
    fs.writeFileSync(path.join(dataDir, "current_price.json"), JSON.stringify({ price: "100", time: now }), "utf8");

    const current = await requestJson(baseUrl, "/api/config");
    assert.equal(current.status, 200);
    const strategy = current.json.strategyVariants[0];
    const config = {
      ...current.json,
      realTradingEnabled: true,
      autoTrade_10m: true,
      forceAutoTrade: true,
      shadowTradingEnabled: true,
      strategyVariants: [strategy]
    };
    const saved = await requestJson(baseUrl, "/api/config", { method: "POST", body: config });
    assert.equal(saved.status, 200);

    const signalTime = new Date(Date.now() - 1000).toISOString();
    fs.writeFileSync(path.join(dataDir, "live_signals.json"), JSON.stringify({
      [strategy.id]: {
        strategy_id: strategy.id,
        time: signalTime,
        actionable_time: signalTime,
        signal: "UP",
        price: 100,
        entry: 100,
        duration: "10",
        confidence: 80,
        bypass_entry_timing: true
      },
      _snapshot_time_ms: Date.now(),
      _snapshot_time: new Date().toISOString(),
      _snapshot_strategy_count: 1
    }, null, 2), "utf8");

    await new Promise(resolve => setTimeout(resolve, 2300));
    const first = await requestJson(baseUrl, "/api/signal?source=autojs");
    const second = await requestJson(baseUrl, "/api/signal?source=autojs");
    assert.equal(first.status, 200);
    assert.equal(second.status, 200);
    assert.equal(first.json[strategy.id].signal, "UP");
    assert.equal(second.json[strategy.id].signal, "UP");
    await new Promise(resolve => setTimeout(resolve, 50));

    const audit = await requestJson(baseUrl, "/api/trade-audit?limit=100");
    const mirrors = audit.json.items.filter(item => (
      item.event === "shadow_trade_open"
      && item.strategyId === strategy.id
      && item.shadowType === "tablet_signal_mirror"
    ));
    assert.equal(mirrors.length, 1);
    assert.equal(mirrors[0].direction, "UP");
    assert.equal(mirrors[0].amount, Number(strategy.amount));
    assert.ok(mirrors[0].shadowQueueDelayMs >= 20);
    assert.equal(mirrors[0].strikePrice, mirrors[0].executionStrikePrice);
  });
});

test("shadow-only strategy records a trade while global real trading is disabled", async () => {
  await withServer({}, async ({ baseUrl, dataDir }) => {
    const now = Date.now();
    writeFreshMinuteCandles(dataDir, now);
    fs.writeFileSync(path.join(dataDir, "current_price.json"), JSON.stringify({ price: "100", time: now }), "utf8");

    const current = await requestJson(baseUrl, "/api/config");
    const strategy = { ...current.json.strategyVariants[0], enabled: true, tradeEnabled: false };
    const saved = await requestJson(baseUrl, "/api/config", {
      method: "POST",
      body: {
        ...current.json,
        realTradingEnabled: false,
        autoTrade_10m: false,
        shadowTradingEnabled: true,
        strategyVariants: [strategy]
      }
    });
    assert.equal(saved.status, 200);
    assert.equal(saved.json.realTradingEnabled, false);
    assert.equal(saved.json.strategyVariants[0].tradeEnabled, false);

    const signalTime = new Date(Date.now() - 1000).toISOString();
    fs.writeFileSync(path.join(dataDir, "live_signals.json"), JSON.stringify({
      [strategy.id]: {
        strategy_id: strategy.id,
        time: signalTime,
        actionable_time: signalTime,
        signal: "UP",
        price: 100,
        entry: 100,
        duration: "10",
        confidence: 80,
        bypass_entry_timing: true,
        shadow_only: true,
        trade_enabled: false
      },
      _snapshot_time_ms: Date.now(),
      _snapshot_time: new Date().toISOString(),
      _snapshot_strategy_count: 1
    }, null, 2), "utf8");

    await new Promise(resolve => setTimeout(resolve, 3300));
    const audit = await requestJson(baseUrl, "/api/trade-audit?limit=100");
    const opens = audit.json.items.filter(item => (
      item.event === "shadow_trade_open" && item.strategyId === strategy.id
    ));
    assert.equal(opens.length, 1);
    assert.equal(opens[0].tradeEnabled, false);

    const configAfter = await requestJson(baseUrl, "/api/config");
    assert.equal(configAfter.json.realTradingEnabled, false);
  });
});

test("multi-normal config replaces branch vote and writes the shared-core parameters", async () => {
  await withServer({}, async ({ baseUrl, dataDir }) => {
    fs.writeFileSync(path.join(dataDir, "prod_config.json"), JSON.stringify({
      BTC_10min_BRANCH_VOTE_STARTUP_V1: {
        enabled: true,
        model_type: "second_branch_vote_startup_v1"
      }
    }), "utf8");
    const strategyId = "BTC_10min_MULTI_NORMAL_HF_STABLE_V1";
    const response = await requestJson(baseUrl, "/api/config", {
      method: "POST",
      body: {
        realTradingEnabled: true,
        autoTrade_10m: true,
        forceAutoTrade: true,
        shadowTradingEnabled: true,
        strategyVariants: [{
          id: strategyId,
          base: "SECOND_MULTI_NORMAL_HF_STABLE_V1",
          label: "多周期动态正态高频稳定V1",
          amount: "5",
          enabled: true,
          tradeEnabled: true,
          lookbackSec: 7200,
          horizonSec: 600,
          gapSec: 600,
          normalWindowSec: 600,
          orderbookMaxAgeSec: 3,
          lowVolSigmaMaxBps: 3,
          lowVolRangeMaxBps: 20,
          lowVolAbsRet10MaxBps: 5,
          lowVolZMin: 1.2,
          lowVolZMax: 1.8,
          trendBaseZMin: 1.2,
          trendHighVolSigmaMinBps: 8,
          trendHighVolZMin: 0.5,
          trendMinSignedFlow: 0.12,
          trendMaxSignedBook: 0.08,
          incidentFilterEnabled: true
        }]
      }
    });
    assert.equal(response.status, 200);
    assert.deepEqual(response.json.strategyVariants.map(item => item.id), [strategyId]);
    assert.equal(response.json.strategyVariants[0].incidentFilterEnabled, false);

    const prod = JSON.parse(fs.readFileSync(path.join(dataDir, "prod_config.json"), "utf8"));
    assert.equal(prod.BTC_10min_BRANCH_VOTE_STARTUP_V1, undefined);
    assert.equal(prod[strategyId].model_type, "second_multi_normal_hf_stable_v1");
    assert.equal(prod[strategyId].multi_normal_trend_high_vol_sigma_min_bps, 8);
    assert.equal(prod[strategyId].multi_normal_trend_high_vol_z_min, 0.5);
    assert.equal(prod[strategyId].incident_filter_enabled, false);
  });
});

test("multiscale phase gate replaces old high-frequency strategy", async () => {
  await withServer({}, async ({ baseUrl, dataDir }) => {
    fs.writeFileSync(path.join(dataDir, "prod_config.json"), JSON.stringify({
      BTC_10min_MULTI_NORMAL_HF_STABLE_V1: {
        enabled: true,
        model_type: "second_multi_normal_hf_stable_v1"
      }
    }), "utf8");
    const strategyId = "BTC_10min_MULTISCALE_PHASE_GATE_V1";
    const response = await requestJson(baseUrl, "/api/config", {
      method: "POST",
      body: {
        realTradingEnabled: true,
        autoTrade_10m: true,
        forceAutoTrade: true,
        shadowTradingEnabled: true,
        strategyVariants: [{
          id: strategyId,
          base: "SECOND_MULTISCALE_PHASE_GATE_V1",
          label: "多周期迁移阶段 V1",
          amount: "5",
          enabled: true,
          tradeEnabled: true,
          lookbackSec: 7800,
          horizonSec: 600,
          gapSec: 600,
          orderbookMaxAgeSec: 3,
          maxEmitAgeSec: 8,
          phaseLookbackSec: 3600,
          maturityHistorySec: 3600,
          maturityMinPeriods: 1800,
          maturityQuantile: 0.75,
          minFlow60: 0.08,
          minImbalance20: 0.05,
          minMicropriceBps: 0,
          minVolumeRatio: 0.8
        }]
      }
    });
    assert.equal(response.status, 200);
    assert.deepEqual(response.json.strategyVariants.map(item => item.id), [strategyId]);
    const prod = JSON.parse(fs.readFileSync(path.join(dataDir, "prod_config.json"), "utf8"));
    assert.equal(prod.BTC_10min_MULTI_NORMAL_HF_STABLE_V1, undefined);
    assert.equal(prod[strategyId].model_type, "second_multiscale_phase_gate_v1");
    assert.equal(prod[strategyId].phase_gate_maturity_quantile, 0.75);
    assert.equal(prod[strategyId].phase_gate_max_emit_age_sec, 8);
    assert.equal(prod[strategyId].trade_enabled, true);
  });
});

test("augmented V9 replaces old strategies and maps to the shared liquidity core", async () => {
  await withServer({}, async ({ baseUrl, dataDir }) => {
    fs.writeFileSync(path.join(dataDir, "prod_config.json"), JSON.stringify({
      BTC_10min_NORMAL_LIQ_OB_V2_QUALITY: { enabled: true, model_type: "second_normal_trend_orderbook_latch_v2" },
      BTC_10min_MULTISCALE_PHASE_GATE_V1: { enabled: true, model_type: "second_multiscale_phase_gate_v1" }
    }), "utf8");
    const strategyId = "BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V9";
    const response = await requestJson(baseUrl, "/api/config", {
      method: "POST",
      body: {
        realTradingEnabled: false,
        autoTrade_10m: false,
        shadowTradingEnabled: true,
        strategyVariants: [{
          id: strategyId,
          base: "SECOND_NORMAL_LIQUIDITY_ORDERBOOK_V1",
          label: "当前V2增强 V9（影子观察）",
          amount: "5",
          enabled: true,
          tradeEnabled: false,
          lookbackSec: 7200,
          horizonSec: 600,
          gapSec: 600,
          v9AugmentedEnabled: true,
          v9EfficiencyMin: 0.6,
          v9TrendStrengthMin: 1.25,
          v9OpposingMinBps: 2,
          v9Z30Min: 1,
          v9VolumeRatioMin: 0.8,
          v9BookCoverageMin: 0.9,
          v9BookVotesMin: 2,
          v9MaxEmitAgeSec: 8
        }]
      }
    });
    assert.equal(response.status, 200);
    assert.deepEqual(response.json.strategyVariants.map(item => item.id), [strategyId]);
    assert.equal(response.json.strategyVariants[0].tradeEnabled, false);
    assert.equal(response.json.strategyVariants[0].v9AugmentedEnabled, true);
    const prod = JSON.parse(fs.readFileSync(path.join(dataDir, "prod_config.json"), "utf8"));
    assert.equal(prod.BTC_10min_NORMAL_LIQ_OB_V2_QUALITY, undefined);
    assert.equal(prod.BTC_10min_MULTISCALE_PHASE_GATE_V1, undefined);
    assert.equal(prod[strategyId].model_type, "second_normal_liquidity_orderbook_v1");
    assert.equal(prod[strategyId].v9_augmented_enabled, true);
    assert.equal(prod[strategyId].v9_book_votes_min, 2);
    assert.equal(prod[strategyId].trade_enabled, false);
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

test("manual orders are accepted even when strategy real trading is disabled", async () => {
  await withServer({}, async ({ baseUrl }) => {
    const manual = await requestJson(baseUrl, "/api/manual", {
      method: "POST",
      body: { direction: "UP", amount: "5", duration: "10" }
    });
    assert.equal(manual.status, 200);
    assert.equal(manual.json.direction, "UP");
    assert.equal(manual.json.amount, "5");

    const pending = await requestJson(baseUrl, "/api/manual");
    assert.equal(pending.status, 200);
    assert.equal(pending.json.direction, "UP");
  });
});

test("manual order command survives retryable tablet execution failure", async () => {
  await withServer({}, async ({ baseUrl }) => {
    const manual = await requestJson(baseUrl, "/api/manual", {
      method: "POST",
      body: { direction: "UP", amount: "5", duration: "10" }
    });
    assert.equal(manual.status, 200);

    const attempt = await requestJson(baseUrl, "/api/trade-audit", {
      method: "POST",
      body: {
        event: "order_attempt",
        clientTime: manual.json.time + 1000,
        device: "tablet",
        strategyId: "manual",
        direction: "UP",
        amount: "5",
        duration: "10"
      }
    });
    assert.equal(attempt.status, 200);

    const abort = await requestJson(baseUrl, "/api/trade-audit", {
      method: "POST",
      body: {
        event: "order_abort",
        clientTime: manual.json.time + 2000,
        device: "tablet",
        strategyId: "manual",
        direction: "UP",
        amount: "5",
        duration: "10",
        reason: "amount_failed"
      }
    });
    assert.equal(abort.status, 200);

    const deleted = await requestJson(baseUrl, "/api/manual", { method: "DELETE" });
    assert.equal(deleted.status, 200);
    assert.equal(deleted.json.cleared, false);
    assert.equal(deleted.json.retry, true);
    assert.equal(deleted.json.reason, "amount_failed");

    const pending = await requestJson(baseUrl, "/api/manual");
    assert.equal(pending.status, 200);
    assert.equal(pending.json.direction, "UP");
    assert.equal(pending.json.lastAbortReason, "amount_failed");
  });
});

test("manual order command retries when confirm button is not found", async () => {
  await withServer({}, async ({ baseUrl }) => {
    const manual = await requestJson(baseUrl, "/api/manual", {
      method: "POST",
      body: { direction: "DOWN", amount: "5", duration: "10" }
    });
    assert.equal(manual.status, 200);

    await requestJson(baseUrl, "/api/trade-audit", {
      method: "POST",
      body: {
        event: "order_attempt",
        clientTime: manual.json.time + 1000,
        device: "tablet",
        strategyId: "manual",
        direction: "DOWN",
        amount: "5",
        duration: "10"
      }
    });

    await requestJson(baseUrl, "/api/trade-audit", {
      method: "POST",
      body: {
        event: "order_abort",
        clientTime: manual.json.time + 2000,
        device: "tablet",
        strategyId: "manual",
        direction: "DOWN",
        amount: "5",
        duration: "10",
        reason: "confirm_not_found"
      }
    });

    const deleted = await requestJson(baseUrl, "/api/manual", { method: "DELETE" });
    assert.equal(deleted.status, 200);
    assert.equal(deleted.json.cleared, false);
    assert.equal(deleted.json.retry, true);
    assert.equal(deleted.json.reason, "confirm_not_found");
  });
});

test("manual order command clears after tablet reports order done", async () => {
  await withServer({}, async ({ baseUrl }) => {
    const manual = await requestJson(baseUrl, "/api/manual", {
      method: "POST",
      body: { direction: "DOWN", amount: "5", duration: "10" }
    });
    assert.equal(manual.status, 200);

    const done = await requestJson(baseUrl, "/api/trade-audit", {
      method: "POST",
      body: {
        event: "order_done",
        clientTime: manual.json.time + 2000,
        device: "tablet",
        strategyId: "manual",
        direction: "DOWN",
        amount: "5",
        duration: "10"
      }
    });
    assert.equal(done.status, 200);

    const deleted = await requestJson(baseUrl, "/api/manual", { method: "DELETE" });
    assert.equal(deleted.status, 200);
    assert.equal(deleted.json.cleared, true);

    const pending = await requestJson(baseUrl, "/api/manual");
    assert.equal(pending.status, 200);
    assert.equal(pending.json, null);
  });
});

test("strategy loss density gate blocks after dense losses", async () => {
  await withServer({}, async ({ baseUrl }) => {
    const strategyId = "BTC_10min_NORMAL_STATE_V21_LOSS_DENSITY_3OF6_8H";
    const cfg = await requestJson(baseUrl, "/api/config", {
      method: "POST",
      body: {
        realTradingEnabled: false,
        autoTrade_10m: false,
        shadowTradingEnabled: true,
        strategyVariants: [{
          id: strategyId,
          base: "SECOND_NORMAL_ROUTER_V21",
          label: "normal router v21 loss density",
          amount: "5",
          tailPct: 0.25,
          enabled: true,
          tradeEnabled: false,
          lookbackSec: 4200,
          horizonSec: 600,
          gapSec: 600,
          routeLookbackSec: 4200,
          r10WindowSec: 600,
          r10CapBps: 42,
          downR10CapBps: 35,
          midRouteSigmaCapBps: 20,
          minObservedPct: 88,
          lossDensityEnabled: true,
          lossDensityWindow: 6,
          lossDensityLosses: 3,
          lossDensityCooldownSec: 28800,
          lossDensityLookbackHours: 72
        }]
      }
    });
    assert.equal(cfg.status, 200);
    const start = Date.now() - 60 * 60 * 1000;
    const items = [];
    const outcomes = ["lost", "lost", "lost", "won"];
    outcomes.forEach((status, index) => {
      const openTime = start + index * 11 * 60 * 1000;
      const settleTime = openTime + 10 * 60 * 1000;
      items.push({
        event: "shadow_trade_open",
        serverTime: openTime,
        tradeId: index + 1,
        source: `shadow:${strategyId}`,
        strategyId,
        direction: "UP",
        amount: 5,
        duration: 10,
        openTime,
        strikePrice: 100
      });
      items.push({
        event: "shadow_trade_settle",
        serverTime: settleTime,
        tradeId: index + 1,
        source: `shadow:${strategyId}`,
        strategyId,
        openTime,
        settleTime,
        settlePrice: status === "won" ? 101 : 99,
        status
      });
    });

    const imported = await requestJson(baseUrl, "/api/trade-audit/import", {
      method: "POST",
      body: { source: "shadow-circuit-test", items }
    });
    assert.equal(imported.status, 200);
    assert.equal(imported.json.imported, 8);

    const signal = await requestJson(baseUrl, "/api/signal?source=dashboard");
    assert.equal(signal.status, 200);
    assert.equal(signal.json._shadowCircuitGate, undefined);
    assert.equal(signal.json._lossDensityGate.strategies[strategyId].blocked, true);
    assert.equal(signal.json._lossDensityGate.strategies[strategyId].policy.minTrades, 4);
    assert.equal(signal.json._lossDensityGate.strategies[strategyId].lastTrigger.lossCount, 3);
  });
});

test("recent real execution failure blocks the next live signal", async () => {
  await withServer({}, async ({ baseUrl, dataDir }) => {
    const strategyId = "BTC_10min_SECOND_VW_STABLE_2700_20_ETA2";
    const now = Date.now();
    writeFreshMinuteCandles(dataDir, now);

    const cfg = await requestJson(baseUrl, "/api/config", {
      method: "POST",
      body: {
        realTradingEnabled: true,
        autoTrade_10m: true,
        forceAutoTrade: true,
        shadowTradingEnabled: false,
        strategyVariants: [
          {
            id: strategyId,
            base: "SECOND_VW_CONFIRM",
            amount: "5",
            tailPct: 0.2,
            enabled: true,
            tradeEnabled: true,
            lookbackSec: 2700,
            horizonSec: 600,
            gapSec: 600,
            etaTargetBps: 2,
            etaMaxWaitSec: 45
          }
        ]
      }
    });
    assert.equal(cfg.status, 200);
    assert.equal(cfg.json.realTradingEnabled, true);
    assert.equal(cfg.json.autoTrade_10m, true);

    fs.writeFileSync(path.join(dataDir, "live_signals.json"), JSON.stringify({
      [strategyId]: {
        strategy_id: strategyId,
        model_type: "test",
        signal: "UP",
        confidence: 88,
        time: new Date(now).toISOString(),
        actionable_time: new Date(now).toISOString(),
        price: 100,
        entry: 100,
        duration: "10"
      },
      _snapshot_time_ms: now,
      _snapshot_time: new Date(now).toISOString(),
      _snapshot_strategy_count: 1
    }, null, 2), "utf8");

    const before = await requestJson(baseUrl, "/api/signal");
    assert.equal(before.status, 200);
    assert.equal(before.json[strategyId].signal, "UP");

    const abort = await requestJson(baseUrl, "/api/trade-audit", {
      method: "POST",
      body: {
        event: "order_abort",
        clientTime: now + 1000,
        device: "tablet",
        strategyId,
        direction: "UP",
        amount: 5,
        duration: "10",
        reason: "amount_failed"
      }
    });
    assert.equal(abort.status, 200);
    assert.equal(abort.json.ok, true);

    const after = await requestJson(baseUrl, "/api/signal");
    assert.equal(after.status, 200);
    assert.equal(after.json[strategyId].signal, null);
    assert.equal(after.json[strategyId].reason, "recent_order_failure_cooldown");
    assert.equal(after.json[strategyId].blocked_signal, "UP");
    assert.equal(after.json[strategyId].execution_failure.blocked, true);
    assert.equal(after.json[strategyId].execution_failure.mode, "amount_failed");
    assert.equal(after.json[strategyId].execution_failure.lastReasonLabel, "金额输入失败");
    assert.equal(after.json[strategyId].execution_failure_label, "金额输入失败");
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
      body: {
        amount: "9",
        duration: "10",
        strategyVariants: [
          { id: "BTC_10min_SECOND_VW_STABLE_2700_20_ETA2", base: "SECOND_VW_CONFIRM", amount: "5", tailPct: 0.2, etaTargetBps: 2, etaMaxWaitSec: 45 },
          { id: "BTC_10min_SECOND_VW_FAST_2700_27_ETA3", base: "SECOND_VW_CONFIRM", amount: "5", tailPct: 0.27, etaTargetBps: 3, etaMaxWaitSec: 45 }
        ]
      }
    });
    assert.equal(allowed.status, 200);
    assert.equal(allowed.json.amount, "9");
    assert.equal(allowed.json.duration, "10");
    assert.equal(allowed.json.strategyAmounts.BTC_10min_SECOND_VW_STABLE_2700_20_ETA2, "5");
    assert.equal(allowed.json.strategyAmounts.BTC_10min_SECOND_VW_FAST_2700_27_ETA3, "5");
    assert.equal(allowed.json.queueOrderPolicy, undefined);
  });
});
