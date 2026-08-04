const assert = require("node:assert");
const test = require("node:test");
const {
  buildLiveOrderHistory,
  deriveOrderLifecycleGate,
  fallbackSettlePrice,
  payoutRateForDuration,
  priceAtOrAfter,
  realExecutionTime,
  settleStatus,
  statusPnl
} = require("../lib/trade_history");

test("priceAtOrAfter returns the first tick at or after target", () => {
  const ticks = [{ time: 10, price: 100 }, { time: 20, price: 101 }];
  assert.equal(priceAtOrAfter(ticks, 1), 100);
  assert.equal(priceAtOrAfter(ticks, 20), 101);
  assert.equal(priceAtOrAfter(ticks, 30), null);
});

test("real execution time uses confirm dispatch and corrects the tablet clock", () => {
  const row = {
    serverTime: 9_000,
    clientTime: 10_200,
    executionTime: 6_000,
    confirmProbe: { dispatchedAt: 7_000 }
  };
  assert.deepEqual(realExecutionTime(row), { time: 5_800, source: "confirm_probe" });
});

test("real history opens at corrected confirmation time and settles 10 minutes later", () => {
  const signalTime = new Date(1_000).toISOString();
  const history = buildLiveOrderHistory({
    now: 700_000,
    limit: 10,
    auditRows: [{
      event: "order_done",
      serverTime: 8_000,
      clientTime: 9_000,
      executionTime: 5_000,
      confirmProbe: { dispatchedAt: 6_000 },
      actionableTime: signalTime,
      duration: 10,
      direction: "UP",
      amount: 5,
      price: 110,
      strategyId: "TEST"
    }],
    priceTicks: [
      { time: 5_000, price: 100 },
      { time: 605_000, price: 101 }
    ]
  });

  const row = history.recent[0];
  assert.equal(row.openTime, 5_000);
  assert.equal(row.openTimeSource, "confirm_probe");
  assert.equal(row.openPrice, 100);
  assert.equal(row.settleTime, 605_000);
  assert.equal(row.closePrice, 101);
  assert.equal(row.signalToOpenMs, 4_000);
  assert.equal(row.status, "won");
});

test("real history does not settle from a price far beyond expiry", () => {
  const history = buildLiveOrderHistory({
    now: 700_000,
    auditRows: [{
      event: "order_done",
      serverTime: 1_000,
      duration: 10,
      direction: "UP",
      amount: 5,
      price: 100,
      strategyId: "TEST"
    }],
    priceTicks: [{ time: 650_000, price: 101 }],
    currentPrice: 102
  });

  assert.equal(history.recent[0].closePrice, null);
  assert.equal(history.recent[0].status, "pending");
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

test("order lifecycle gate locks done and unverified orders by corrected confirmation time", () => {
  const gate = deriveOrderLifecycleGate({
    now: 500_000,
    auditRows: [
      {
        event: "order_done",
        serverTime: 8_000,
        clientTime: 9_000,
        confirmProbe: { dispatchedAt: 6_000 },
        duration: 10,
        direction: "UP",
        price: 100,
        strategyId: "DONE"
      },
      {
        event: "order_unverified",
        serverTime: 20_000,
        duration: 30,
        direction: "DOWN",
        price: 100,
        strategyId: "UNVERIFIED"
      }
    ]
  });

  assert.equal(gate.strategies.DONE.openTime, 5_000);
  assert.equal(gate.strategies.DONE.settleTime, 605_000);
  assert.equal(gate.strategies.DONE.reason, "order_duration_pending");
  assert.equal(gate.strategies.UNVERIFIED.settleTime, 1_820_000);
});

test("order lifecycle gate remains locked after expiry until a valid settlement tick exists", () => {
  const order = {
    event: "order_done",
    serverTime: 1_000,
    duration: 10,
    direction: "UP",
    price: 100,
    strategyId: "WAIT_PRICE"
  };
  const expired = deriveOrderLifecycleGate({
    now: 700_000,
    auditRows: [order],
    priceTicks: [{ time: 650_000, price: 101 }],
    currentPrice: 102
  });
  assert.equal(expired.strategies.WAIT_PRICE.expired, true);
  assert.equal(expired.strategies.WAIT_PRICE.reason, "settlement_price_pending");

  const settled = deriveOrderLifecycleGate({
    now: 700_000,
    auditRows: [order],
    priceTicks: [{ time: 601_000, price: 101 }]
  });
  assert.equal(settled.strategies.WAIT_PRICE, undefined);
});

test("order lifecycle gate does not revive an older order after the latest order settled", () => {
  const gate = deriveOrderLifecycleGate({
    now: 2_000_000,
    auditRows: [
      { event: "order_done", serverTime: 1_000, duration: 10, direction: "UP", price: 100, strategyId: "SAME" },
      { event: "order_done", serverTime: 1_000_000, duration: 10, direction: "UP", price: 100, strategyId: "SAME" }
    ],
    // 价格日志只保留最新订单的结算点；旧订单不应因此被重新锁定。
    priceTicks: [{ time: 1_600_000, price: 101 }]
  });

  assert.equal(gate.strategies.SAME, undefined);
});

test("order lifecycle gate restores from persisted rows and isolates strategies", () => {
  const auditRows = [{
    event: "order_unverified",
    serverTime: 1_000,
    direction: "DOWN",
    price: 100,
    strategyId: "RESTORED"
  }];
  const options = {
    now: 100_000,
    auditRows: JSON.parse(JSON.stringify(auditRows)),
    durationForStrategy: { RESTORED: 15 }
  };
  const firstProcess = deriveOrderLifecycleGate(options);
  const restartedProcess = deriveOrderLifecycleGate({
    ...options,
    auditRows: JSON.parse(JSON.stringify(auditRows))
  });

  assert.deepEqual(restartedProcess, firstProcess);
  assert.equal(restartedProcess.strategies.RESTORED.duration, "15");
  assert.equal(restartedProcess.strategies.OTHER, undefined);
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

test("live order history restores amount from signal payload when audit amount is missing", () => {
  const history = buildLiveOrderHistory({
    now: 1710000000000,
    limit: 10,
    auditRows: [
      {
        event: "order_done",
        serverTime: 1000,
        duration: 10,
        direction: "UP",
        price: 100,
        strategyId: "BTC_10min_SAFE",
        signal: { amount: "12", fixed_amount: true }
      }
    ],
    priceTicks: [
      { time: 601000, price: 101 }
    ]
  });

  assert.equal(history.recent[0].amount, 12);
  assert.equal(history.recent[0].openAmount, 12);
  assert.equal(history.recent[0].settleAmount, 21.6);
  assert.equal(history.recent[0].pnl, 9.6);
  assert.match(history.recent[0].amountReason, /12U/);
});

test("live order history preserves the exact LLM prompt and response for the order", () => {
  const prompt = "=== MARKET DATA ===\nBTC/USDT | Price: $64000\n=== OUTPUT FORMAT ===";
  const response = '{"direction":"UP","confidence":0.72,"reason":"订单流增强"}';
  const history = buildLiveOrderHistory({
    now: 1710000000000,
    limit: 10,
    auditRows: [{
      event: "order_done",
      serverTime: 1000,
      duration: 10,
      direction: "UP",
      amount: 5,
      price: 100,
      strategyId: "BTC_10min_LLM_GLM52",
      llm_decision_id: "decision-1",
      llm_model: "glm-5.2",
      llm_prompt: prompt,
      llm_response: response
    }],
    priceTicks: [{ time: 601000, price: 101 }]
  });

  assert.equal(history.recent[0].llm_decision_id, "decision-1");
  assert.equal(history.recent[0].llm_model, "glm-5.2");
  assert.equal(history.recent[0].llm_prompt, prompt);
  assert.equal(history.recent[0].llm_response, response);
  assert.equal(JSON.stringify(history).includes("Authorization"), false);
});

test("live order history fills a missing shadow LLM log from its immutable decision id", () => {
  const history = buildLiveOrderHistory({
    now: 1710000000000,
    kind: "shadow",
    llmLogsByDecisionId: {
      "shadow-decision": {
        llm_model: "glm-5.2",
        llm_prompt: "archived prompt",
        llm_response: '{"direction":"UP"}'
      }
    },
    auditRows: [{
      event: "shadow_trade_open",
      serverTime: 1000,
      tradeId: 1,
      source: "shadow:BTC_10min_LLM_GLM52",
      strategyId: "BTC_10min_LLM_GLM52",
      direction: "UP",
      amount: 5,
      duration: 10,
      openTime: 1000,
      strikePrice: 100,
      llm_decision_id: "shadow-decision"
    }]
  });

  assert.equal(history.recent[0].llm_prompt, "archived prompt");
  assert.equal(history.recent[0].llm_response, '{"direction":"UP"}');
});

test("live order history exposes open and settlement amounts", () => {
  const history = buildLiveOrderHistory({
    now: 1710000000000,
    limit: 10,
    auditRows: [
      { event: "order_done", serverTime: 1000, duration: 10, direction: "UP", amount: 10, price: 100, strategyId: "WIN" },
      { event: "order_done", serverTime: 2000, duration: 10, direction: "DOWN", amount: 10, price: 100, strategyId: "LOSS" },
      { event: "order_done", serverTime: 3000, duration: 10, direction: "UP", amount: 10, price: 100, strategyId: "TIE" }
    ],
    priceTicks: [
      { time: 601000, price: 101 },
      { time: 602000, price: 101 },
      { time: 603000, price: 100 }
    ]
  });

  const byStrategy = Object.fromEntries(history.recent.map(row => [row.strategyId, row]));
  assert.equal(byStrategy.WIN.openAmount, 10);
  assert.equal(byStrategy.WIN.settleAmount, 18);
  assert.equal(byStrategy.LOSS.openAmount, 10);
  assert.equal(byStrategy.LOSS.settleAmount, 0);
  assert.equal(byStrategy.TIE.openAmount, 10);
  assert.equal(byStrategy.TIE.settleAmount, 10);
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

test("live order history can scope records by Shanghai day", () => {
  const dayOne = Date.parse("2026-07-04T12:00:00+08:00");
  const dayTwo = Date.parse("2026-07-05T12:00:00+08:00");
  const history = buildLiveOrderHistory({
    now: Date.parse("2026-07-05T13:00:00+08:00"),
    mode: "day",
    day: "2026-07-05",
    kind: "real",
    auditRows: [
      { event: "order_done", serverTime: dayOne, duration: 10, direction: "UP", amount: 5, price: 100, strategyId: "OLD_DAY" },
      { event: "order_done", serverTime: dayTwo, duration: 10, direction: "DOWN", amount: 5, price: 100, strategyId: "TARGET_DAY" }
    ],
    priceTicks: [
      { time: dayOne + 600000, price: 101 },
      { time: dayTwo + 600000, price: 99 }
    ],
    availableDays: ["2026-07-05", "2026-07-04"]
  });

  assert.equal(history.pagination.mode, "day");
  assert.equal(history.pagination.day, "2026-07-05");
  assert.equal(history.pagination.total, 1);
  assert.deepEqual(history.pagination.availableDays, ["2026-07-05", "2026-07-04"]);
  assert.equal(history.summary.total, 1);
  assert.equal(history.summary.real.wins, 1);
  assert.equal(history.recent[0].strategyId, "TARGET_DAY");
});

test("unverified autojs orders are visible but excluded from win rate and pnl", () => {
  const history = buildLiveOrderHistory({
    now: 1710000000000,
    limit: 10,
    auditRows: [
      { event: "order_done", serverTime: 1000, duration: 10, direction: "UP", amount: 10, price: 100, strategyId: "BTC_10min_SAFE" },
      {
        event: "order_unverified",
        serverTime: 2000,
        duration: 10,
        direction: "UP",
        amount: 15,
        reason: "balance_not_decreased",
        beforeBalance: 110.58,
        afterBalance: 110.58,
        balanceDelta: 0,
        strategyId: "BTC_10min_SECOND_CHIP_3600_FLOW"
      }
    ],
    priceTicks: [
      { time: 601000, price: 101 }
    ]
  });

  assert.equal(history.summary.total, 2);
  assert.equal(history.summary.settled, 1);
  assert.equal(history.summary.wins, 1);
  assert.equal(history.summary.pending, 0);
  assert.equal(history.summary.pnl, 8);
  const unverified = history.recent.find(row => row.event === "order_unverified");
  assert.equal(unverified.status, "unverified");
  assert.equal(unverified.pnl, 0);
  assert.equal(unverified.balanceDelta, 0);
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

test("shadow audit rows expose strategy and execution prices separately", () => {
  const openTime = Date.parse("2026-07-09T09:17:43+08:00");
  const executionOpenTime = openTime + 20000;
  const history = buildLiveOrderHistory({
    now: openTime + 11 * 60 * 1000,
    kind: "shadow",
    auditRows: [
      {
        event: "shadow_trade_open",
        serverTime: executionOpenTime,
        tradeId: 9,
        source: "shadow:BTC_10min_NORMAL_LIQ_OB_V2_QUALITY",
        strategyId: "BTC_10min_NORMAL_LIQ_OB_V2_QUALITY",
        direction: "UP",
        amount: 5,
        duration: 10,
        openTime,
        strikePrice: 62393.4,
        signalEntryPrice: 62393.4,
        executionStrikePrice: 62452,
        executionOpenTime,
        executionDelayMs: 20000,
        llm_decision_id: "shadow-decision-1",
        llm_model: "glm-5.2",
        llm_prompt: "shadow prompt",
        llm_response: '{"direction":"UP"}'
      },
      {
        event: "shadow_trade_settle",
        serverTime: openTime + 10 * 60 * 1000,
        tradeId: 9,
        source: "shadow:BTC_10min_NORMAL_LIQ_OB_V2_QUALITY",
        strategyId: "BTC_10min_NORMAL_LIQ_OB_V2_QUALITY",
        openTime,
        settleTime: executionOpenTime + 10 * 60 * 1000,
        settlePrice: 62398.7,
        status: "won"
      }
    ]
  });

  assert.equal(history.summary.shadow.total, 1);
  assert.equal(history.recent[0].openTime, executionOpenTime);
  assert.equal(history.recent[0].openTimeSource, "shadow_execution");
  assert.equal(history.recent[0].settleTime, executionOpenTime + 10 * 60 * 1000);
  assert.equal(history.recent[0].openPrice, 62452);
  assert.equal(history.recent[0].signalEntryPrice, 62393.4);
  assert.equal(history.recent[0].executionStrikePrice, 62452);
  assert.equal(history.recent[0].executionDelayMs, 20000);
  assert.equal(history.recent[0].llm_decision_id, "shadow-decision-1");
  assert.equal(history.recent[0].llm_model, "glm-5.2");
  assert.equal(history.recent[0].llm_prompt, "shadow prompt");
  assert.equal(history.recent[0].llm_response, '{"direction":"UP"}');
  assert.equal(history.recent[0].status, "won");
});

test("real history summary excludes shadow losses when viewing real orders", () => {
  const history = buildLiveOrderHistory({
    now: 1710000000000,
    mode: "day",
    day: "2026-07-05",
    kind: "real",
    auditRows: [
      {
        event: "order_abort",
        serverTime: Date.parse("2026-07-05T12:00:00+08:00"),
        duration: 10,
        direction: "UP",
        amount: 10,
        reason: "amount_failed",
        strategyId: "BTC_10min_NORMAL_STATE_V21_LOSS_DENSITY_3OF6_8H"
      },
      {
        event: "shadow_trade_open",
        serverTime: Date.parse("2026-07-05T12:01:00+08:00"),
        tradeId: 1,
        source: "shadow:BTC_10min_NORMAL_STATE_V21_LOSS_DENSITY_3OF6_8H",
        strategyId: "BTC_10min_NORMAL_STATE_V21_LOSS_DENSITY_3OF6_8H",
        direction: "DOWN",
        amount: 5,
        duration: 10,
        openTime: Date.parse("2026-07-05T12:01:00+08:00"),
        strikePrice: 100
      },
      {
        event: "shadow_trade_settle",
        serverTime: Date.parse("2026-07-05T12:11:00+08:00"),
        tradeId: 1,
        source: "shadow:BTC_10min_NORMAL_STATE_V21_LOSS_DENSITY_3OF6_8H",
        strategyId: "BTC_10min_NORMAL_STATE_V21_LOSS_DENSITY_3OF6_8H",
        openTime: Date.parse("2026-07-05T12:01:00+08:00"),
        settleTime: Date.parse("2026-07-05T12:11:00+08:00"),
        settlePrice: 101,
        status: "lost"
      }
    ]
  });

  assert.equal(history.summary.total, 1);
  assert.equal(history.summary.losses, 0);
  assert.equal(history.summary.aborted, 1);
  assert.equal(history.summary.executionFailed, 1);
  assert.equal(history.summary.pnl, 0);
  assert.equal(history.summary.shadow.losses, 1);
  assert.equal(history.breakdown.shadow.total.losses, 1);
  assert.equal(history.recent[0].status, "aborted");
});
