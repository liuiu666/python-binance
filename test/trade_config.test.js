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
  assert.equal(result.tradeConfig.strategyVariants.length, 6);
  assert.ok(result.tradeConfig.strategyVariants.some(v => v.base === "SECOND"));
  assert.ok(result.tradeConfig.strategyVariants.some(v => v.base === "SECOND_CHIP"));
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
  assert.equal(result.tradeConfig.strategyVariants.length, 6);
  assert.ok(result.tradeConfig.strategyVariants.some(v => v.base === "SECOND"));
  assert.ok(result.tradeConfig.strategyVariants.some(v => v.base === "SECOND_CHIP"));
});

test("trade config separates observation from real execution", () => {
  const cfg = normalizeTradeConfig({
    strategyVariants: [
      { id: "BTC_10min_SAFE", base: "SAFE", amount: "5", tailPct: 0.2, enabled: true, tradeEnabled: false },
      { id: "BTC_10min_TAKER", base: "TAKER", amount: "10", tailPct: 0.2, enabled: true, tradeEnabled: true },
      { id: "BTC_10min_SECOND_1800_20", base: "SECOND", amount: "5", tailPct: 0.2, enabled: true, tradeEnabled: false }
    ]
  });
  assert.deepEqual(observedStrategyIds(cfg), ["BTC_10min_SAFE", "BTC_10min_TAKER", "BTC_10min_SECOND_1800_20", "BTC_10min_SECOND_CHIP_1800_OPT", "BTC_10min_SECOND_CHIP_3600_WIDE_FLOW"]);
  assert.deepEqual(liveStrategyIds(cfg), ["BTC_10min_TAKER", "BTC_10min_SECOND_CHIP_3600_WIDE_FLOW"]);
});

test("trade config preserves custom second normal variants", () => {
  const cfg = normalizeTradeConfig({
    strategyVariants: [
      { id: "BTC_10min_SAFE", base: "SAFE", amount: "5", tailPct: 0.2 },
      { id: "BTC_10min_TAKER", base: "TAKER", amount: "10", tailPct: 0.2 },
      {
        id: "BTC_10min_SECOND_900_27",
        base: "SECOND",
        amount: "7",
        tailPct: 0.27,
        enabled: false,
        tradeEnabled: true,
        lookbackSec: 900,
        horizonSec: 600,
        gapSec: 300,
        secondFilter: "flow_align"
      }
    ]
  });
  const second = cfg.strategyVariants.find(v => v.id === "BTC_10min_SECOND_900_27");
  assert.equal(second.amount, "7");
  assert.equal(second.enabled, false);
  assert.equal(second.tradeEnabled, true);
  assert.equal(second.lookbackSec, 900);
  assert.equal(second.gapSec, 300);
  assert.equal(second.secondFilter, "flow_align");
  assert.equal(second.duration, "10");

  const pub = publicTradeConfig(cfg);
  const saved = normalizeTradeConfig(pub);
  const restored = saved.strategyVariants.find(v => v.id === "BTC_10min_SECOND_900_27");
  assert.equal(restored.amount, "7");
  assert.equal(restored.lookbackSec, 900);
  assert.equal(restored.secondFilter, "flow_align");
});

test("second chip default keeps the backtested fixed 20U bin", () => {
  const cfg = normalizeTradeConfig({
    strategyVariants: [
      { id: "BTC_10min_SAFE", base: "SAFE", amount: "5", tailPct: 0.2 },
      { id: "BTC_10min_TAKER", base: "TAKER", amount: "10", tailPct: 0.2 },
      {
        id: "BTC_10min_SECOND_CHIP_3600_20",
        base: "SECOND_CHIP",
        amount: "5",
        enabled: true,
        tradeEnabled: false,
        lookbackSec: 3600,
        horizonSec: 600,
        gapSec: 600,
        chipTargetShare: 0.2,
        chipBinMode: "percent",
        chipBinPct: 0.0003,
        chipBreakPct: 0.0023,
        chipDirectionFilter: "breakout_up_only"
      }
    ]
  });
  const chip = cfg.strategyVariants.find(v => v.base === "SECOND_CHIP");
  assert.equal(chip.chipBinMode, "fixed");
  assert.equal(chip.chipBinSize, 20);
});

test("second chip optimized variant keeps width filter and matching backtest", () => {
  const cfg = normalizeTradeConfig({
    strategyVariants: [
      { id: "BTC_10min_SAFE", base: "SAFE", amount: "5", tailPct: 0.2 },
      { id: "BTC_10min_TAKER", base: "TAKER", amount: "10", tailPct: 0.2 },
      {
        id: "BTC_10min_SECOND_CHIP_1800_OPT",
        base: "SECOND_CHIP",
        amount: "5",
        enabled: true,
        tradeEnabled: false,
        lookbackSec: 1800,
        horizonSec: 600,
        gapSec: 300,
        chipTargetShare: 0.2,
        chipBinMode: "fixed",
        chipBinSize: 20,
        chipBreakPct: 0.004,
        chipDirectionFilter: "all",
        chipFilter: "width_lte_3"
      }
    ]
  });
  const chip = cfg.strategyVariants.find(v => v.id === "BTC_10min_SECOND_CHIP_1800_OPT");
  assert.equal(chip.chipFilter, "width_lte_3");
  assert.equal(chip.backtest.wr, 73.91);
  const pub = publicTradeConfig(cfg);
  assert.equal(pub.strategyParams.BTC_10min_SECOND_CHIP_1800_OPT.chipFilter, "width_lte_3");
});

test("second chip flow variant keeps matching latest backtest and 10U amount", () => {
  const cfg = normalizeTradeConfig({
    strategyVariants: [
      { id: "BTC_10min_SAFE", base: "SAFE", amount: "5", tailPct: 0.2 },
      { id: "BTC_10min_TAKER", base: "TAKER", amount: "10", tailPct: 0.2 },
      {
        id: "BTC_10min_SECOND_CHIP_3600_FLOW",
        base: "SECOND_CHIP",
        label: "秒级筹码区 60m 资金流",
        amount: "10",
        enabled: true,
        tradeEnabled: true,
        lookbackSec: 3600,
        horizonSec: 600,
        gapSec: 1800,
        chipTargetShare: 0.5,
        chipBinMode: "fixed",
        chipBinSize: 50,
        chipBreakPct: 0.003,
        chipDirectionFilter: "all",
        chipFilter: "flow_reversal"
      }
    ]
  });
  const chip = cfg.strategyVariants.find(v => v.id === "BTC_10min_SECOND_CHIP_3600_FLOW");
  assert.equal(chip.amount, "10");
  assert.equal(chip.tradeEnabled, true);
  assert.equal(chip.backtest.wr, 84.21);
  assert.equal(chip.backtest.tradesPerDay, 9.41);
  const pub = publicTradeConfig(cfg);
  assert.equal(pub.strategyAmounts.BTC_10min_SECOND_CHIP_3600_FLOW, "10");
  assert.equal(pub.strategyParams.BTC_10min_SECOND_CHIP_3600_FLOW.chipFilter, "flow_reversal");
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
  assert.equal(amountForStrategyConfig("BTC_10min_TAKER", result.tradeConfig), "5");
});
