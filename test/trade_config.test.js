const assert = require("node:assert");
const test = require("node:test");
const {
  DEFAULT_TRADE_CONFIG,
  applyTradeConfigPatch,
  amountForStrategyConfig,
  normalizeTradeConfig,
  publicTradeConfig
} = require("../lib/trade_config");

test("trade config normalizes only current two-strategy fields", () => {
  const cfg = normalizeTradeConfig({
    amount: 7,
    strategyAmounts: {
      BTC_10min_SAFE: 5,
      BTC_10min_TAKER: 10,
      OLD_STRATEGY: 0
    },
    autoTrade: true,
    realTradingOverride: true,
    minConfidence: "40",
    tiersEnabled: true,
    queueOrderPolicy: "bad"
  });
  assert.equal(cfg.amount, "7");
  assert.equal(cfg.strategyAmounts.BTC_10min_SAFE, "5");
  assert.equal(cfg.strategyAmounts.BTC_10min_TAKER, "10");
  assert.equal(cfg.strategyAmounts.OLD_STRATEGY, undefined);
  assert.equal(cfg.autoTrade_10m, true);
  assert.equal(cfg.realTradingEnabled, true);
  assert.equal(cfg.minConfidence, 40);
  assert.equal(cfg.queueOrderPolicy, undefined);
  assert.equal(cfg.tiersEnabled, undefined);
  assert.deepEqual(publicTradeConfig(cfg), {
    amount: "7",
    strategyAmounts: {
      BTC_10min_SAFE: "5",
      BTC_10min_TAKER: "10"
    },
    duration: "10",
    autoTrade_10m: true,
    realTradingEnabled: true,
    shadowTradingEnabled: true,
    minConfidence: 40
  });
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
