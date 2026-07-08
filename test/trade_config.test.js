const assert = require("node:assert");
const test = require("node:test");
const {
  DEFAULT_TRADE_CONFIG,
  applyTradeConfigPatch,
  amountForStrategyConfig,
  normalizeTradeConfig,
  publicTradeConfig,
  liveStrategyIds,
  observedStrategyIds
} = require("../lib/trade_config");

test("trade config defaults to the two normal plus volume confirmation strategies", () => {
  const cfg = normalizeTradeConfig({});
  assert.deepEqual(cfg.strategyVariants.map(v => v.id), [
    "BTC_10min_SECOND_VW_STABLE_2700_20_ETA2",
    "BTC_10min_SECOND_VW_FAST_2700_27_ETA3"
  ]);
  assert.equal(cfg.strategyVariants[0].tailPct, 0.2);
  assert.equal(cfg.strategyVariants[0].etaTargetBps, 2);
  assert.equal(cfg.strategyVariants[1].tailPct, 0.27);
  assert.equal(cfg.strategyVariants[1].etaTargetBps, 3);
});

test("trade config drops legacy strategies instead of re-adding them", () => {
  const cfg = normalizeTradeConfig({
    strategyVariants: [
      { id: "BTC_10min_SAFE", base: "SAFE", amount: "5", tailPct: 0.2 },
      { id: "BTC_10min_TAKER", base: "TAKER", amount: "10", tailPct: 0.27 },
      { id: "BTC_10min_SECOND_CHIP_3600_WIDE_FLOW", base: "SECOND_CHIP", amount: "15" }
    ],
    autoTrade: true,
    realTradingOverride: true,
    queueOrderPolicy: "legacy"
  });
  assert.deepEqual(cfg.strategyVariants.map(v => v.base), ["SECOND_VW_CONFIRM", "SECOND_VW_CONFIRM"]);
  assert.equal(cfg.autoTrade_10m, true);
  assert.equal(cfg.realTradingEnabled, true);
  assert.equal(cfg.queueOrderPolicy, undefined);
});

test("trade config preserves current strategy params and independent amounts", () => {
  const result = applyTradeConfigPatch(DEFAULT_TRADE_CONFIG, {
    strategyVariants: [
      {
        id: "BTC_10min_SECOND_VW_STABLE_2700_20_ETA2",
        base: "SECOND_VW_CONFIRM",
        label: "正态成交量确认 稳健",
        amount: "6",
        tailPct: 0.2,
        enabled: true,
        tradeEnabled: false,
        lookbackSec: 2700,
        horizonSec: 600,
        gapSec: 600,
        etaTargetBps: 2,
        etaMaxWaitSec: 45
      },
      {
        id: "BTC_10min_SECOND_VW_FAST_2700_27_ETA3",
        base: "SECOND_VW_CONFIRM",
        label: "正态成交量确认 高频",
        amount: "10",
        tailPct: 0.27,
        enabled: true,
        tradeEnabled: true,
        lookbackSec: 2700,
        horizonSec: 600,
        gapSec: 600,
        etaTargetBps: 3,
        etaMaxWaitSec: 45
      }
    ]
  });
  assert.equal(amountForStrategyConfig("BTC_10min_SECOND_VW_STABLE_2700_20_ETA2", result.tradeConfig), "6");
  assert.equal(amountForStrategyConfig("BTC_10min_SECOND_VW_FAST_2700_27_ETA3", result.tradeConfig), "10");
  assert.deepEqual(observedStrategyIds(result.tradeConfig), [
    "BTC_10min_SECOND_VW_STABLE_2700_20_ETA2",
    "BTC_10min_SECOND_VW_FAST_2700_27_ETA3"
  ]);
  assert.deepEqual(liveStrategyIds(result.tradeConfig), [
    "BTC_10min_SECOND_VW_FAST_2700_27_ETA3"
  ]);
});

test("public config exposes current backtest and eta params", () => {
  const cfg = normalizeTradeConfig(DEFAULT_TRADE_CONFIG);
  const pub = publicTradeConfig(cfg);
  assert.equal(pub.strategyAmounts.BTC_10min_SECOND_VW_STABLE_2700_20_ETA2, "5");
  assert.equal(pub.strategyParams.BTC_10min_SECOND_VW_STABLE_2700_20_ETA2.etaTargetBps, 2);
  assert.equal(pub.strategyParams.BTC_10min_SECOND_VW_FAST_2700_27_ETA3.etaMaxWaitSec, 45);
  assert.equal(pub.strategyVariants[0].backtest.wr, 68.66);
  assert.equal(pub.strategyVariants[1].backtest.tradesPerDay, 13.27);
});

test("shadow trading switch can be enabled without enabling real trading", () => {
  const result = applyTradeConfigPatch(DEFAULT_TRADE_CONFIG, {
    shadowTradingEnabled: true,
    realTradingEnabled: false,
    autoTrade_10m: false
  });
  assert.equal(result.tradeConfig.shadowTradingEnabled, true);
  assert.equal(result.tradeConfig.realTradingEnabled, false);
  assert.equal(result.tradeConfig.autoTrade_10m, false);
  const pub = publicTradeConfig(result.tradeConfig);
  assert.equal(pub.shadowTradingEnabled, true);
});

test("liquidity orderbook strategy is preserved as shadow by default", () => {
  const cfg = normalizeTradeConfig({
    strategyVariants: [{
      id: "BTC_10min_NORMAL_LIQUIDITY_ORDERBOOK_V1_SHADOW",
      base: "SECOND_NORMAL_LIQUIDITY_ORDERBOOK_V1",
      enabled: true,
      tradeEnabled: false,
      normalWindowSec: 600,
      zEntry: 1.2,
      obImbalanceMin: 0.08
    }]
  });
  assert.equal(cfg.strategyVariants.length, 1);
  assert.equal(cfg.strategyVariants[0].base, "SECOND_NORMAL_LIQUIDITY_ORDERBOOK_V1");
  assert.equal(cfg.strategyVariants[0].tradeEnabled, false);
  assert.equal(cfg.strategyVariants[0].normalWindowSec, 600);
  assert.equal(cfg.strategyVariants[0].zEntry, 1.2);
  assert.equal(cfg.strategyVariants[0].obImbalanceMin, 0.08);
  assert.equal(cfg.strategyVariants[0].bidwallTrapEnabled, true);
  assert.equal(cfg.strategyVariants[0].bidwallTrapRet300MaxBps, -5);
  assert.equal(cfg.strategyVariants[0].bidwallTrapBid20Chg60Min, 2);
  assert.equal(cfg.strategyVariants[0].qualityV2Enabled, true);
  assert.equal(cfg.strategyVariants[0].qualityV2DownBid20Chg60Min, -0.7);
  assert.equal(cfg.strategyVariants[0].qualityV2UpFlow60Min, -0.063);
  assert.deepEqual(liveStrategyIds(cfg), []);
  assert.deepEqual(observedStrategyIds(cfg), ["BTC_10min_NORMAL_LIQUIDITY_ORDERBOOK_V1_SHADOW"]);
  assert.equal(publicTradeConfig(cfg).strategyVariants[0].backtest.wr, 77.78);
});

test("auto trade patch records blocked and forced transitions", () => {
  const blocked = applyTradeConfigPatch(DEFAULT_TRADE_CONFIG, { autoTrade: true }, {
    autoTradeSafetyGate: () => ({ blocked: true, verdict: "missing_shadow_decision" })
  });
  assert.equal(blocked.tradeConfig.autoTrade_10m, false);
  assert.equal(blocked.auditEvents[0].event, "auto_trade_safety_block_10m");

  const forced = applyTradeConfigPatch(DEFAULT_TRADE_CONFIG, { autoTrade: true, forceAutoTrade: true }, {
    autoTradeSafetyGate: () => ({ blocked: true, verdict: "missing_shadow_decision" })
  });
  assert.equal(forced.tradeConfig.autoTrade_10m, true);
  assert.equal(forced.tradeConfig.realTradingEnabled, true);
  assert.equal(forced.auditEvents[0].event, "auto_trade_force_enabled_10m");
});

test("auto trade can be enabled in the same save as real trading", () => {
  const result = applyTradeConfigPatch(DEFAULT_TRADE_CONFIG, {
    realTradingEnabled: true,
    autoTrade_10m: true
  }, {
    autoTradeSafetyGate: config => ({
      blocked: !(config.realTradingEnabled && config.autoTrade_10m),
      verdict: config.realTradingEnabled ? "ok" : "real_disabled"
    })
  });

  assert.equal(result.tradeConfig.realTradingEnabled, true);
  assert.equal(result.tradeConfig.autoTrade_10m, true);
  assert.equal(result.safetyBlocked, null);
});
