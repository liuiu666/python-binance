const assert = require("node:assert");
const test = require("node:test");
const {
  buildLiveOrderHistory,
  fallbackSettlePrice,
  payoutRateForDuration,
  priceAtOrAfter,
  settleStatus,
  statusPnl
} = require("../lib/trade_history");

test("priceAtOrAfter returns the first tick at or after target", () => {
  const ticks = [{ time: 10, price: 100 }, { time: 20, price: 101 }];
  assert.equal(priceAtOrAfter(ticks, 1), 100);
  assert.equal(priceAtOrAfter(ticks, 20), 101);
  assert.equal(priceAtOrAfter(ticks, 30), null);
});

test("expired orders settle with current price when tick history is missing", () => {
  assert.equal(fallbackSettlePrice([], 1000, 2000, 101), 101);
  assert.equal(fallbackSettlePrice([], 3000, 2000, 101), null);
  const history = buildLiveOrderHistory({
    now: 601000,
    currentPrice: 101,
    auditRows: [
      { event: "order_done", serverTime: 1000, duration: 10, direction: "UP", amount: 5, price: 100, strategyId: "BTC_10min_SAFE" },
      {
        event: "shadow_trade_open",
        serverTime: 1000,
        tradeId: 1,
        source: "shadow:BTC_10min_SAFE",
        strategyId: "BTC_10min_SAFE",
        direction: "UP",
        amount: 5,
        duration: 10,
        openTime: 1000,
        strikePrice: 100
      }
    ],
    priceTicks: []
  });

  assert.equal(history.summary.real.wins, 1);
  assert.equal(history.summary.shadow.wins, 1);
  assert.equal(history.summary.pending, 0);
});

test("settle status and pnl match binary option rules", () => {
  assert.equal(settleStatus("UP", 100, 101), "won");
  assert.equal(settleStatus("UP", 100, 99), "lost");
  assert.equal(settleStatus("DOWN", 100, 99), "won");
  assert.equal(settleStatus("DOWN", 100, 100), "tie");
  assert.equal(statusPnl("won", 10), 8.5);
  assert.equal(statusPnl("lost", 10), -10);
  assert.equal(statusPnl("tie", 10), 0);
  assert.equal(payoutRateForDuration(10), 0.8);
  assert.equal(payoutRateForDuration(30), 0.85);
});

test("live order history uses duration-specific payout rates", () => {
  const history = buildLiveOrderHistory({
    now: 1710000000000,
    limit: 10,
    auditRows: [
      { event: "order_done", serverTime: 1000, duration: 10, direction: "UP", amount: 10, price: 100, strategyId: "BTC_10min_SAFE" },
      { event: "order_done", serverTime: 2000, duration: 30, direction: "UP", amount: 10, price: 100, strategyId: "manual" }
    ],
    priceTicks: [
      { time: 601000, price: 101 },
      { time: 1802000, price: 101 }
    ]
  });

  assert.equal(history.summary.wins, 2);
  assert.equal(history.summary.pnl, 16.5);
  const tenMin = history.recent.find(row => row.duration === "10");
  const thirtyMin = history.recent.find(row => row.duration === "30");
  assert.equal(tenMin.pnl, 8);
  assert.equal(thirtyMin.pnl, 8.5);
});

test("live order history summarizes settled, pending, aborted, and server rows", () => {
  const history = buildLiveOrderHistory({
    now: 1710000000000,
    limit: 10,
    auditRows: [
      { event: "order_done", serverTime: 1000, duration: 1, direction: "UP", amount: 10, price: 100, strategyId: "BTC_10min_SAFE" },
      { event: "order_done", serverTime: 2000, duration: 1, direction: "DOWN", amount: 5, price: 100, strategyId: "BTC_10min_TAKER" },
      { event: "order_abort", serverTime: 3000, direction: "UP", amount: 5, reason: "button_not_found" }
    ],
    priceTicks: [
      { time: 61000, price: 101 },
      { time: 62000, price: 100 }
    ],
    serverTrades: [
      { id: 1, source: "server", direction: "UP", amount: 2, duration: "30", openTime: 4000, settleTime: 5000, strikePrice: 100, settlePrice: null, status: "active" }
    ]
  });

  assert.equal(history.updatedAt, 1710000000000);
  assert.equal(history.summary.total, 4);
  assert.equal(history.summary.settled, 2);
  assert.equal(history.summary.wins, 1);
  assert.equal(history.summary.ties, 1);
  assert.equal(history.summary.pending, 1);
  assert.equal(history.summary.winRate, 100);
  assert.equal(history.summary.pnl, 8.5);
  assert.equal(history.active.length, 1);
  assert.equal(history.recent[0].event, "server_trade");
});

test("shadow audit rows do not merge reused trade ids after restart", () => {
  const history = buildLiveOrderHistory({
    now: 1710000000000,
    limit: 10,
    auditRows: [
      {
        event: "shadow_trade_open",
        serverTime: 1000,
        tradeId: 1,
        source: "shadow:BTC_10min_SAFE",
        strategyId: "BTC_10min_SAFE",
        direction: "UP",
        amount: 5,
        duration: 10,
        openTime: 1000,
        strikePrice: 100
      },
      {
        event: "shadow_trade_settle",
        serverTime: 601000,
        tradeId: 1,
        source: "shadow:BTC_10min_SAFE",
        strategyId: "BTC_10min_SAFE",
        openTime: 1000,
        settleTime: 601000,
        settlePrice: 101,
        status: "won"
      },
      {
        event: "shadow_trade_open",
        serverTime: 2000,
        tradeId: 1,
        source: "shadow:BTC_10min_SAFE",
        strategyId: "BTC_10min_SAFE",
        direction: "DOWN",
        amount: 5,
        duration: 10,
        openTime: 2000,
        strikePrice: 100
      },
      {
        event: "shadow_trade_settle",
        serverTime: 602000,
        tradeId: 1,
        source: "shadow:BTC_10min_SAFE",
        strategyId: "BTC_10min_SAFE",
        openTime: 2000,
        settleTime: 602000,
        settlePrice: 101,
        status: "lost"
      }
    ]
  });

  assert.equal(history.summary.shadow.total, 2);
  assert.equal(history.summary.shadow.wins, 1);
  assert.equal(history.summary.shadow.losses, 1);
  assert.equal(history.summary.shadow.pnl, -1);
});
