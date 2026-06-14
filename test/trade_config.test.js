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

test("trade config normalizes only current two-strategy fields", () => {
  const cfg = normalizeTradeConfig({
    amount: 7,
    strategyAmounts: {
      BTC_10min_SAFE: 5,
      BTC_10min_TAKER: 10,
      OLD_STRATEGY: 0
    },
    strategyParams: {
      BTC_10min_SAFE: { tailPct: 0.22 },
      BTC_10min_TAKER: { tailPct: 0.27 },
      OLD_STRATEGY: { tailPct: 0.4 }
    },
    autoTrade: true,
    realTradingOverride: true,
    tiersEnabled: true,
    queueOrderPolicy: "bad"
  });
  assert.equal(cfg.amount, "7");
  assert.equal(cfg.strategyVariants[0].id, "BTC_10min_SAFE");
  assert.equal(cfg.strategyVariants[0].amount, "5");
  assert.equal(cfg.strategyVariants[0].tailPct, 0.22);
  assert.equal(cfg.strategyVariants[1].id, "BTC_10min_TAKER");
  assert.equal(cfg.strategyVariants[1].amount, "10");
  assert.equal(cfg.strategyVariants[1].tailPct, 0.27);
  assert.equal(cfg.autoTrade_10m, true);
  assert.equal(cfg.realTradingEnabled, true);
  assert.equal(cfg.queueOrderPolicy, undefined);
  assert.equal(cfg.tiersEnabled, undefined);
  const pub = publicTradeConfig(cfg);
  assert.equal(pub.strategyAmounts.BTC_10min_SAFE, "5");
  assert.equal(pub.strategyAmounts.BTC_10min_TAKER, "10");
  assert.equal(pub.strategyParams.BTC_10min_TAKER.tailPct, 0.27);
  assert.equal(pub.autoTrade_10m, true);
  assert.equal(pub.realTradingEnabled, true);
  assert.equal(pub.shadowTradingEnabled, false);
});

test("trade config supports multiple taker variants with independent amounts", () => {
  const result = applyTradeConfigPatch(DEFAULT_TRADE_CONFIG, {
    strategyVariants: [
      { id: "BTC_10min_SAFE", base: "SAFE", amount: "5", tailPct: 0.2 },
      { id: "BTC_10min_TAKER", base: "TAKER", amount: "10", tailPct: 0.2 },
      { id: "BTC_10min_TAKER_27", base: "TAKER", amount: "8", tailPct: 0.27 }
    ]
  });
  assert.equal(amountForStrategyConfig("BTC_10min_TAKER", result.tradeConfig), "10");
  assert.equal(amountForStrategyConfig("BTC_10min_TAKER_27", result.tradeConfig), "8");
  assert.equal(result.tradeConfig.strategyVariants.length, 4);
  assert.ok(result.tradeConfig.strategyVariants.some(v => v.base === "SECOND"));
});

test("trade config supports multiple safe variants with independent amounts", () => {
  const result = applyTradeConfigPatch(DEFAULT_TRADE_CONFIG, {
    strategyVariants: [
      { id: "BTC_10min_SAFE", base: "SAFE", amount: "5", tailPct: 0.2 },
      { id: "BTC_10min_SAFE_22", base: "SAFE", amount: "3", tailPct: 0.22 },
      { id: "BTC_10min_TAKER", base: "TAKER", amount: "10", tailPct: 0.2 }
    ]
  });
  assert.equal(amountForStrategyConfig("BTC_10min_SAFE", result.tradeConfig), "5");
  assert.equal(amountForStrategyConfig("BTC_10min_SAFE_22", result.tradeConfig), "3");
  assert.equal(result.tradeConfig.strategyVariants.length, 4);
  assert.ok(result.tradeConfig.strategyVariants.some(v => v.base === "SECOND"));
});

test("trade config separates observation from real execution", () => {
  const cfg = normalizeTradeConfig({
    strategyVariants: [
      { id: "BTC_10min_SAFE", base: "SAFE", amount: "5", tailPct: 0.2, enabled: true, tradeEnabled: false },
      { id: "BTC_10min_TAKER", base: "TAKER", amount: "10", tailPct: 0.2, enabled: true, tradeEnabled: true },
      { id: "BTC_10min_SECOND_1800_20", base: "SECOND", amount: "5", tailPct: 0.2, enabled: true, tradeEnabled: false }
    ]
  });
  assert.deepEqual(observedStrategyIds(cfg), ["BTC_10min_SAFE", "BTC_10min_TAKER", "BTC_10min_SECOND_1800_20"]);
  assert.deepEqual(liveStrategyIds(cfg), ["BTC_10min_TAKER"]);
});

test("auto trade patch records blocked and forced transitions", () => {
  const blocked = applyTradeConfigPatch(DEFAULT_TRADE_CONFIG, { autoTrade: true }, {
    autoTradeSafetyGate: () => ({ blocked: true, verdict: "missing_shadow_decision" })
  });
  assert.equal(blocked.tradeConfig.autoTrade_10m, false);
  assert.equal(blocked.auditEvents[0].event, "auto_trade_safety_block");

  const forced = applyTradeConfigPatch(DEFAULT_TRADE_CONFIG, { autoTrade: true, forceAutoTrade: true }, {
    autoTradeSafetyGate: () => ({ blocked: true, verdict: "missing_shadow_decision" })
  });
  assert.equal(forced.tradeConfig.autoTrade_10m, true);
  assert.equal(forced.tradeConfig.realTradingEnabled, true);
  assert.equal(forced.auditEvents[0].event, "auto_trade_force_enabled");
});

test("trade config ignores legacy knobs and keeps fixed strategy amounts", () => {
  const result = applyTradeConfigPatch(DEFAULT_TRADE_CONFIG, {
    queueOrderPolicy: "taker_then_safe",
    maxActionableLagMs: 4500,
    tiers: [{ min: 70, amount: 11 }],
    strategyAmounts: {
      BTC_10min_SAFE: "5",
      BTC_10min_TAKER: "10"
    }
  });
  assert.equal(result.tradeConfig.queueOrderPolicy, undefined);
  assert.equal(result.tradeConfig.maxActionableLagMs, undefined);
  assert.equal(result.tradeConfig.tiers, undefined);
  assert.equal(amountForStrategyConfig("BTC_10min_SAFE", result.tradeConfig), "5");
  assert.equal(amountForStrategyConfig("BTC_10min_TAKER", result.tradeConfig), "10");
});
