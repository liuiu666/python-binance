const assert = require("node:assert");
const test = require("node:test");
const {
  DEFAULT_TRADE_CONFIG,
  amountForConfidence,
  applyTradeConfigPatch,
  normalizeTradeConfig
} = require("../lib/trade_config");

test("trade config normalizes tiers and preserves defaults", () => {
  const cfg = normalizeTradeConfig({
    amount: 7,
    tiers: [{ min: 60, amount: 12 }, { min: 120, amount: 50 }, { min: 80, amount: 20 }],
    queueOrderPolicy: "bad"
  });
  assert.equal(cfg.amount, "7");
  assert.equal(cfg.queueOrderPolicy, DEFAULT_TRADE_CONFIG.queueOrderPolicy);
  assert.deepEqual(cfg.tiers, [{ min: 80, amount: 20 }, { min: 60, amount: 12 }]);
});

test("tiered amount uses highest matching confidence tier", () => {
  const cfg = normalizeTradeConfig({
    amount: "5",
    tiersEnabled: true,
    tiers: [{ min: 80, amount: 25 }, { min: 60, amount: 12 }, { min: 40, amount: 6 }]
  });
  assert.equal(amountForConfidence(85, cfg), "25");
  assert.equal(amountForConfidence(65, cfg), "12");
  assert.equal(amountForConfidence(35, cfg), "5");
});

test("auto trade patch records blocked and forced transitions", () => {
  const blocked = applyTradeConfigPatch(DEFAULT_TRADE_CONFIG, { autoTrade: true }, {
    autoTradeSafetyGate: () => ({ blocked: true, verdict: "missing_shadow_decision" })
  });
  assert.equal(blocked.tradeConfig.autoTrade_10m, false);
  assert.equal(blocked.tradeConfig.autoTrade_30m, false);
  assert.equal(blocked.auditEvents[0].event, "auto_trade_safety_block");

  const forced = applyTradeConfigPatch(DEFAULT_TRADE_CONFIG, { autoTrade: true, forceAutoTrade: true }, {
    autoTradeSafetyGate: () => ({ blocked: true, verdict: "missing_shadow_decision" })
  });
  assert.equal(forced.tradeConfig.autoTrade_10m, true);
  assert.equal(forced.tradeConfig.autoTrade_30m, true);
  assert.equal(forced.tradeConfig.realTradingEnabled, true);
  assert.equal(forced.auditEvents[0].event, "auto_trade_force_enabled");
});

test("trade config patch validates queue policy and actionable lag", () => {
  const result = applyTradeConfigPatch(DEFAULT_TRADE_CONFIG, {
    queueOrderPolicy: "10_then_30",
    maxActionableLagMs: 4500,
    tiers: [{ min: 70, amount: 11 }]
  });
  assert.equal(result.tradeConfig.queueOrderPolicy, "10_then_30");
  assert.equal(result.tradeConfig.maxActionableLagMs, DEFAULT_TRADE_CONFIG.maxActionableLagMs);
  assert.deepEqual(result.tradeConfig.tiers, [{ min: 70, amount: 11 }]);
});
