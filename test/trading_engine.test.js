"use strict";

const assert = require("node:assert");
const test = require("node:test");
const { createTradingEngine } = require("../lib/trading_engine");

const STRATEGY_ID = "BTC_10min_TEST_LIVE";

/**
 * 构造交易引擎运行影子单流程所需的完整最小依赖，并暴露可控时钟与定时器记录。
 */
function createHarness() {
  let currentTime = 1_000;
  let currentPrice = 100;
  const audits = [];
  const invalidatedEvents = [];
  const oneShotTimers = [];
  const repeatingTimers = [];
  const clearedTimers = [];
  const clearedRepeatingTimers = [];
  const variant = { id: STRATEGY_ID, duration: 1, tradeEnabled: true };

  const engine = createTradingEngine({
    getTradeConfig: () => ({
      shadowTradingEnabled: true,
      realTradingEnabled: true,
      autoTrade_10m: true,
      duration: 1
    }),
    getCurrentPrice: () => currentPrice,
    getRealBalance: () => 0,
    buildSignalResponse: () => ({}),
    signalIsActionableNow: () => true,
    signalActionableMs: sig => Date.parse(sig.actionable_time),
    signalActionableTime: sig => sig.actionable_time,
    signalReferencePrice: sig => sig.entry_price,
    llmLogSnapshotForDecision: () => null,
    currentStrategyVariants: () => [variant],
    currentLiveStrategyIds: () => [STRATEGY_ID],
    amountForStrategy: () => "5",
    orderbookConfirmForSignal: () => ({ ok: false }),
    appendTradeAudit: item => {
      audits.push(item);
      return item;
    },
    tailTradeAudit: () => [],
    invalidateTradeEvent: eventName => invalidatedEvents.push(eventName),
    publish: () => {},
    getMarketSnapshot: () => ({
      price: currentPrice,
      priceHistory: [],
      candles: [],
      realBalance: 0
    }),
    now: () => currentTime,
    setTimer: (callback, delay) => {
      const handle = { callback, delay };
      oneShotTimers.push(handle);
      return handle;
    },
    clearTimer: handle => clearedTimers.push(handle),
    setRepeatingTimer: (callback, delay) => {
      const handle = { callback, delay };
      repeatingTimers.push(handle);
      return handle;
    },
    clearRepeatingTimer: handle => clearedRepeatingTimers.push(handle),
    payoutRate: 0.8,
    shadowExecutionDelayMs: 5_000
  });

  return {
    engine,
    audits,
    invalidatedEvents,
    oneShotTimers,
    repeatingTimers,
    clearedTimers,
    clearedRepeatingTimers,
    setCurrentTime: value => { currentTime = value; },
    setCurrentPrice: value => { currentPrice = value; }
  };
}

/**
 * 按 mirrorTabletSignalsToShadow 的实际约定，以策略 ID 为键提供实盘变体信号。
 */
function tabletSignals() {
  return {
    [STRATEGY_ID]: {
      signal: "UP",
      time: "1970-01-01T00:00:01.000Z",
      actionable_time: "1970-01-01T00:00:01.000Z",
      entry_price: 100,
      duration: 1,
      confidence: 80,
      avg_prob: 0.8
    }
  };
}

test("stop cancels a pending mirrored shadow trade and blocks its saved timer", () => {
  const harness = createHarness();

  harness.engine.mirrorTabletSignalsToShadow(tabletSignals());

  const trade = harness.engine.getShadowTrades()[0];
  assert.equal(trade.status, "pending");
  assert.equal(harness.oneShotTimers.length, 1);
  const pendingTimer = harness.oneShotTimers[0];
  assert.equal(pendingTimer.delay, 5_000);

  harness.engine.stop();

  assert.deepEqual(harness.clearedTimers, [pendingTimer]);
  assert.equal(trade.status, "cancelled");
  assert.equal(trade.cancelReason, "engine_stopped");

  // 即使底层定时器竞态地回调，已取消订单也不得再次开仓或写入审计。
  pendingTimer.callback();
  assert.equal(trade.status, "cancelled");
  assert.equal(harness.audits.some(row => row.event === "shadow_trade_open"), false);
});

test("mirrored shadow trade activation audits and invalidates shadow_trade_open", () => {
  const harness = createHarness();

  harness.engine.mirrorTabletSignalsToShadow(tabletSignals());
  harness.setCurrentTime(6_000);
  harness.oneShotTimers[0].callback();

  const trade = harness.engine.getShadowTrades()[0];
  assert.equal(trade.status, "active");
  assert.equal(trade.openTime, 6_000);
  assert.equal(trade.strikePrice, 100);
  assert.equal(harness.audits.length, 1);
  assert.equal(harness.audits[0].event, "shadow_trade_open");
  assert.equal(harness.audits[0].strategyId, STRATEGY_ID);
  assert.deepEqual(harness.invalidatedEvents, ["shadow_trade_open"]);
});

test("repeating settlement timer audits and invalidates an expired shadow trade", () => {
  const harness = createHarness();

  harness.engine.mirrorTabletSignalsToShadow(tabletSignals());
  harness.setCurrentTime(6_000);
  harness.oneShotTimers[0].callback();
  harness.engine.start();

  // start() 注册多个循环任务；按源码函数名定位每秒执行的影子单结算任务。
  const settlementTimer = harness.repeatingTimers.find(
    handle => handle.delay === 1_000 && handle.callback.name === "settleTrades"
  );
  assert.ok(settlementTimer);

  harness.setCurrentTime(66_000);
  harness.setCurrentPrice(101);
  settlementTimer.callback();

  const trade = harness.engine.getShadowTrades()[0];
  assert.equal(trade.status, "won");
  assert.equal(trade.settlePrice, 101);
  assert.equal(harness.audits.at(-1).event, "shadow_trade_settle");
  assert.equal(harness.audits.at(-1).strategyId, STRATEGY_ID);
  assert.deepEqual(harness.invalidatedEvents, [
    "shadow_trade_open",
    "shadow_trade_settle"
  ]);

  harness.engine.stop();
});
