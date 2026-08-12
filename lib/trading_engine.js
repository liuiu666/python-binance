"use strict";

// =============================================================================
// 交易引擎模块 —— 服务器模拟交易 / 影子交易 / 结算 / 广播 / 交易窗口
// -----------------------------------------------------------------------------
// 本模块从 server.js 整体迁出，负责：
//   1. 交易窗口状态判定（getTradeWindowStatus）
//   2. WebSocket 状态广播（broadcastState / broadcastTradeUpdate）
//   3. 服务器模拟交易（placeTrade / checkAutoTrade）
//   4. 影子交易全流程（placeShadowTrade / checkShadowTrades /
//      checkOrderbookShadowTrades / mirrorTabletSignalsToShadow 等）
//   5. 结算（settleTrades，同时处理 trades 和 shadowTrades）
//   6. WebSocket 连接消息处理（handleWebSocketConnection）
//   7. 全部交易相关定时器的生命周期管理（start / stop）
//
// 设计要点：
//   - 采用工厂模式 createTradingEngine(deps)，全部交易状态由本模块独占持有。
//   - 动态状态（交易配置、现价、真实余额）通过 getter 注入，保证每次读取最新值。
//   - 信号服务接口（buildSignalResponse 等）以函数形式注入，解耦信号管线。
//   - WebSocket 广播通过 publish(type, payload) 注入，不直接依赖 wss。
//   - 定时器通过 now/setTimer/clearTimer/setRepeatingTimer/clearRepeatingTimer
//     注入，便于单元测试。
// =============================================================================

const { payoutRateForDuration } = require("./trade_history");

/**
 * 创建交易引擎实例。
 *
 * @param {Object} deps 依赖注入对象
 * @param {Function} deps.getTradeConfig              读取最新交易配置（动态 getter）
 * @param {Function} deps.getCurrentPrice             读取最新现价（动态 getter）
 * @param {Function} deps.getRealBalance              读取最新真实余额（动态 getter）
 * @param {Function} deps.buildSignalResponse         组装信号响应 (source) => signals
 * @param {Function} deps.signalIsActionableNow       信号当前是否可执行 (sig, now?) => bool
 * @param {Function} deps.signalActionableMs          信号可执行毫秒时间戳 (sig) => number
 * @param {Function} deps.signalActionableTime        信号可执行时间字符串 (sig) => string
 * @param {Function} deps.signalReferencePrice        信号参考价 (sig) => number|null
 * @param {Function} deps.llmLogSnapshotForDecision   LLM 决策日志快照 (decisionId) => object|null
 * @param {Function} deps.currentStrategyVariants     当前启用策略变体列表 () => variant[]
 * @param {Function} deps.currentLiveStrategyIds      当前实盘策略 ID 列表 () => string[]
 * @param {Function} deps.amountForStrategy           按策略计算下单金额 (strategyId, sig) => string
 * @param {Function} deps.orderbookConfirmForSignal   订单簿确认 (sig) => { ok, ... }
 * @param {Function} deps.appendTradeAudit            追加交易审计日志 (item) => void
 * @param {Function} deps.tailTradeAudit              读取交易审计尾部 (limit) => rows[]
 * @param {Function} deps.invalidateTradeEvent        失效交易派生缓存 (eventName) => void
 * @param {Function} deps.publish                     WebSocket 广播 (type, payload) => void
 * @param {Function} deps.getMarketSnapshot           市场快照 () => { price, priceHistory, candles, realBalance }
 * @param {Function} [deps.now]                       当前时间毫秒（默认 Date.now）
 * @param {Function} [deps.setTimer]                  单次定时器（默认 setTimeout）
 * @param {Function} [deps.clearTimer]                清除单次定时器（默认 clearTimeout）
 * @param {Function} [deps.setRepeatingTimer]         循环定时器（默认 setInterval）
 * @param {Function} [deps.clearRepeatingTimer]       清除循环定时器（默认 clearInterval）
 * @param {number}   deps.payoutRate                  赔付率常量（DEFAULT_PAYOUT_RATE）
 * @param {number}   [deps.windowSec]                 交易窗口秒数（默认 60）
 * @param {number}   [deps.autoTradeAmount]           自动交易默认金额（默认 100）
 * @param {boolean}  [deps.autoTradeEnabled]          自动交易是否启用
 * @param {boolean}  [deps.serverSimTradingEnabled]   服务器模拟交易是否启用
 * @param {boolean}  [deps.enableOrderbookShadowTrades] 订单簿影子交易是否启用
 * @param {number}   [deps.strategyCooldownMs]        策略冷却毫秒（默认 10 分钟）
 * @param {number}   [deps.shadowExecutionDelayMs]    影子交易执行延迟毫秒（默认 5000）
 */
function createTradingEngine(deps) {
  // ---- 注入：动态状态 getter ----
  const { getTradeConfig, getCurrentPrice, getRealBalance } = deps;

  // ---- 注入：信号服务接口 ----
  const {
    buildSignalResponse,
    signalIsActionableNow,
    signalActionableMs,
    signalActionableTime,
    signalReferencePrice,
    llmLogSnapshotForDecision,
    currentStrategyVariants,
    currentLiveStrategyIds,
    amountForStrategy
  } = deps;

  // ---- 注入：orderbook 确认 ----
  const { orderbookConfirmForSignal } = deps;

  // ---- 注入：审计与缓存 ----
  const { appendTradeAudit, tailTradeAudit, invalidateTradeEvent } = deps;

  // ---- 注入：WebSocket 广播 ----
  const { publish } = deps;

  // ---- 注入：市场快照 ----
  const { getMarketSnapshot } = deps;

  // ---- 注入：可测试定时器（提供默认值，便于生产直接使用） ----
  const {
    now = Date.now,
    setTimer = setTimeout,
    clearTimer = clearTimeout,
    setRepeatingTimer = setInterval,
    clearRepeatingTimer = clearInterval
  } = deps;

  // ---- 注入：常量配置 ----
  const {
    payoutRate,
    windowSec = 60,
    autoTradeAmount = 100,
    autoTradeEnabled = false,
    serverSimTradingEnabled = false,
    enableOrderbookShadowTrades = false,
    strategyCooldownMs = 10 * 60 * 1000,
    shadowExecutionDelayMs = Math.max(0, Number(process.env.SHADOW_EXECUTION_DELAY_MS || 5000))
  } = deps;

  // ===========================================================================
  // 模块独占交易状态（server.js 不再直接持有）
  // ===========================================================================

  // --- 服务端模拟交易 ---
  let trades = [];               // 活跃 + 历史模拟交易列表
  let nextTradeId = 1;           // 下一个模拟交易 ID
  let account = {                // 模拟账户
    balance: 10000.0,
    totalTrades: 0,
    wins: 0,
    losses: 0,
    totalPnl: 0
  };
  let lastSignals = {};          // 各策略最后一次成交信号快照

  // --- 影子交易 ---
  let shadowTrades = [];         // 活跃 + 历史影子交易列表
  let nextShadowTradeId = 1;     // 下一个影子交易 ID
  let lastShadowSignals = {};    // 各策略最后一次影子信号键
  let shadowSignalKeys = new Set(); // 已记录的影子信号键（去重）
  let shadowSignalKeysLoaded = false; // 影子信号键是否已从审计日志加载

  // --- 策略冷却 & 自动交易日志 ---
  let lastStrategyTradeAt = {};  // 策略 -> 最近成交毫秒时间戳
  let autoTradeLog = [];         // 自动/影子交易日志条目

  // --- 交易窗口广播状态 ---
  let lastWindowStatus = null;   // 上次窗口状态（用于变化检测）

  // --- 定时器句柄（start 时创建，stop 时统一清除） ---
  let repeatingTimers = [];
  const pendingShadowTimers = new Set();

  /**
   * 写入交易审计，并立即失效依赖该事件的信号门禁缓存。
   * 缓存服务会自行忽略与门禁无关的事件。
   */
  function writeTradeAudit(item) {
    const written = appendTradeAudit(item);
    invalidateTradeEvent(item && item.event);
    return written;
  }

  // ===========================================================================
  // 1. 交易窗口
  // ===========================================================================

  /**
   * 计算当前交易窗口状态。
   * 窗口规则：每 5 分钟边界（0/5/10/15...分）的前 windowSec 秒为可交易窗口。
   * @returns {{ inWindow: boolean, secUntilNext: number, windowClosesIn: number }}
   */
  function getTradeWindowStatus() {
    const date = new Date();
    const sec = date.getSeconds();
    const min = date.getMinutes();
    // 下一个 5 分钟边界
    const nextBoundary = (Math.floor(min / 5) + 1) * 5;
    const inWindow = sec < windowSec && min % 5 === 0;
    let secUntilNext;
    if (inWindow) {
      secUntilNext = 0;
    } else {
      secUntilNext = ((nextBoundary - min) * 60) - sec;
      if (secUntilNext < 0) secUntilNext += 300;
    }
    return {
      inWindow,
      secUntilNext: Math.max(0, secUntilNext),
      windowClosesIn: inWindow ? (windowSec - sec) : 0
    };
  }

  /**
   * 广播交易窗口状态：窗口状态变化或在窗口内时，每秒向所有客户端推送。
   */
  function broadcastWindowStatus() {
    const status = getTradeWindowStatus();
    if (status.inWindow !== lastWindowStatus || status.inWindow) {
      lastWindowStatus = status.inWindow;
      publish("window", status);
    }
  }

  // ===========================================================================
  // 2. 状态广播
  // ===========================================================================

  /**
   * 向所有 WebSocket 客户端广播完整交易状态快照。
   */
  function broadcastState() {
    const tc = getTradeConfig();
    publish("state", {
      account: { ...account },
      activeTrades: trades.filter(t => t.status === "active"),
      recentTrades: trades.filter(t => t.status !== "active").slice(-20).reverse(),
      autoTradeLog: autoTradeLog.slice(-10).reverse(),
      autoTradeEnabled: tc.realTradingEnabled && tc.autoTrade_10m,
      serverSimTradingEnabled: serverSimTradingEnabled,
      realBalance: getRealBalance()
    });
  }

  /**
   * 向所有 WebSocket 客户端广播单笔交易更新。
   * @param {Object} trade 交易对象
   */
  function broadcastTradeUpdate(trade) {
    publish("trade_update", { trade });
  }

  /**
   * 获取当前交易状态快照（用于 WebSocket init 消息和外部读取）。
   * @returns {Object} 状态快照
   */
  function getStateSnapshot() {
    const tc = getTradeConfig();
    return {
      account: { ...account },
      activeTrades: trades.filter(t => t.status === "active"),
      recentTrades: trades.filter(t => t.status !== "active").slice(-20).reverse(),
      autoTradeLog: autoTradeLog.slice(-10).reverse(),
      autoTradeEnabled: tc.realTradingEnabled && tc.autoTrade_10m,
      serverSimTradingEnabled: serverSimTradingEnabled
    };
  }

  // ===========================================================================
  // 3. 服务器模拟交易
  // ===========================================================================

  /**
   * 服务端模拟下单（供手动下单和自动交易调用）。
   * @param {string} direction   方向 "UP" | "DOWN"
   * @param {number} amount      下单金额（USDT）
   * @param {string} source      来源标识（"manual" / "auto:xxx"）
   * @param {number} durationMin 持仓周期（分钟）
   * @returns {Object|null} 成功返回交易对象，失败返回 null
   */
  function placeTrade(direction, amount, source, durationMin) {
    const price = getCurrentPrice();
    if (!price) return null;
    const amt = parseFloat(amount);
    if (isNaN(amt) || amt < 1 || amt > account.balance) return null;
    const dur = Math.max(1, Number(durationMin) || 30);
    const trade = {
      id: nextTradeId++,
      direction,
      amount: amt,
      strikePrice: price,
      openTime: now(),
      settleTime: now() + dur * 60 * 1000,
      duration: String(dur),
      status: "active",
      settlePrice: null,
      payout: null,
      source: source || "manual"
    };
    account.balance -= amt;
    trades.push(trade);
    // 写入审计日志
    writeTradeAudit({
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

  /**
   * 自动交易检查：在交易窗口内，对实盘策略信号自动下单。
   * 由 start() 注册为 3 秒循环定时器。
   */
  function checkAutoTrade() {
    const price = getCurrentPrice();
    const tc = getTradeConfig();
    // 三个开关全部打开且有现价时才执行
    if (!serverSimTradingEnabled || !autoTradeEnabled || !tc.shadowTradingEnabled || !price) return;
    const status = getTradeWindowStatus();
    if (!status.inWindow) return;

    try {
      // 使用 dashboard 来源获取完整未裁剪信号，用于影子执行
      const signals = buildSignalResponse("dashboard");
      for (const strategyId of currentLiveStrategyIds()) {
        if (!tc.autoTrade_10m) continue;
        const sig = signals[strategyId];
        if (!sig || !sig.signal) continue;
        if (hasStrategyCooldown(strategyId)) continue;
        // 同策略已有活跃模拟交易时跳过
        if (trades.some(t => t.status === "active" && t.source === "auto:" + strategyId)) continue;

        if (!signalIsActionableNow(sig)) continue;

        // 与上次成交信号完全一致时跳过（同方向同时间）
        const last = lastSignals[strategyId];
        if (last && last.signal === sig.signal && last.time === sig.time) continue;
        const autoAmt = Number(amountForStrategy(strategyId, sig));
        const trade = placeTrade(sig.signal, autoAmt, "auto:" + strategyId, sig.duration || sig.interval_min);
        if (trade) {
          markStrategyCooldown(strategyId);
          lastSignals[strategyId] = { signal: sig.signal, time: sig.time, confidence: sig.confidence };
          autoTradeLog.push({
            time: new Date().toISOString(),
            strategy: strategyId,
            signal: sig.signal,
            confidence: sig.confidence,
            price: price,
            amount: autoAmt,
            tradeId: trade.id
          });
          console.log(`[Shadow Auto] #${trade.id} ${strategyId} ${sig.signal} ${sig.confidence}% @ ${price} (${autoAmt} USDT)`);
          broadcastTradeUpdate(trade);
        }
      }
    } catch (e) {}
  }

  // ===========================================================================
  // 4. 影子交易
  // ===========================================================================

  /**
   * 影子下单：创建一笔延迟执行的影子交易。
   * 交易先进入 pending 状态，经过 shadowExecutionDelayMs 后以当时现价激活。
   * @param {string} strategyId 策略 ID
   * @param {Object} sig        信号对象
   * @param {Object} variant    策略变体配置
   * @param {Object} extra      额外字段 { tradeFields, auditFields }
   * @returns {Object|null} 成功返回交易对象，失败返回 null
   */
  function placeShadowTrade(strategyId, sig, variant, extra = {}) {
    const price = getCurrentPrice();
    if (!price || !sig || !sig.signal) return null;
    const llmLog = llmLogSnapshotForDecision(sig.llm_decision_id) || {};
    const amount = Number(amountForStrategy(strategyId, sig));
    if (!Number.isFinite(amount) || amount <= 0) return null;
    const tc = getTradeConfig();
    const dur = Math.max(1, Number(sig.duration || sig.interval_min || variant?.duration || tc.duration || 10));
    const ts = now();
    if (!signalIsActionableNow(sig, ts)) return null;
    const signalOpenTime = signalActionableMs(sig);
    const signalStrikePrice = signalReferencePrice(sig) || price;

    // 创建 pending 影子交易
    const trade = {
      id: nextShadowTradeId++,
      direction: sig.signal,
      amount,
      strikePrice: null,
      openTime: null,
      settleTime: null,
      duration: String(dur),
      status: "pending",
      settlePrice: null,
      payout: null,
      payoutRate: payoutRateForDuration(dur, payoutRate),
      source: "shadow:" + strategyId,
      confidence: sig.confidence,
      rsi_value: sig.rsi_value,
      avg_prob: sig.avg_prob,
      signalTime: sig.time,
      actionableTime: signalActionableTime(sig),
      signalEntryPrice: signalStrikePrice,
      executionStrikePrice: null,
      executionOpenTime: null,
      shadowRequestedAt: ts,
      shadowExecutionDelayMs: shadowExecutionDelayMs,
      ...extra.tradeFields
    };
    shadowTrades.push(trade);
    broadcastTradeUpdate(trade);

    // 延迟执行：保存句柄，确保 stop() 后不会继续激活影子交易。
    const timerHandle = setTimer(() => {
      pendingShadowTimers.delete(timerHandle);
      if (trade.status !== "pending") return;
      const execPrice = getCurrentPrice();
      if (!execPrice) {
        trade.status = "cancelled";
        trade.cancelReason = "shadow_execution_price_missing";
        broadcastTradeUpdate(trade);
        return;
      }
      const executionOpenTime = now();
      const executionStrikePrice = execPrice;
      trade.status = "active";
      trade.strikePrice = executionStrikePrice;
      trade.openTime = executionOpenTime;
      trade.settleTime = executionOpenTime + dur * 60 * 1000;
      trade.executionStrikePrice = executionStrikePrice;
      trade.executionOpenTime = executionOpenTime;
      // 写入影子交易开仓审计
      writeTradeAudit({
        event: "shadow_trade_open",
        serverTime: executionOpenTime,
        tradeId: trade.id,
        source: trade.source,
        strategyId,
        tradeEnabled: variant ? variant.tradeEnabled !== false : null,
        direction: trade.direction,
        amount: trade.amount,
        duration: trade.duration,
        openTime: trade.openTime,
        strikePrice: trade.strikePrice,
        signalEntryPrice: trade.signalEntryPrice,
        executionStrikePrice: trade.executionStrikePrice,
        executionOpenTime: trade.executionOpenTime,
        executionDelayMs: trade.executionOpenTime - signalOpenTime,
        shadowQueueDelayMs: trade.executionOpenTime - trade.shadowRequestedAt,
        confidence: trade.confidence,
        avg_prob: trade.avg_prob,
        signalTime: trade.signalTime,
        actionableTime: trade.actionableTime,
        llm_decision_id: sig.llm_decision_id || llmLog.llm_decision_id || null,
        llm_model: sig.llm_model || llmLog.llm_model || null,
        llm_prompt: typeof sig.llm_prompt === "string" ? sig.llm_prompt : llmLog.llm_prompt || null,
        llm_response: typeof sig.llm_response === "string" ? sig.llm_response : llmLog.llm_response || null,
        ...extra.auditFields
      });
      broadcastTradeUpdate(trade);
    }, shadowExecutionDelayMs);
    pendingShadowTimers.add(timerHandle);
    return trade;
  }

  /**
   * 生成影子信号去重键：策略 + 方向 + 可执行时间。
   */
  function shadowSignalKey(strategyId, sig) {
    return [
      strategyId,
      sig && sig.signal || "",
      sig && sig.time || "",
      signalActionableTime(sig) || ""
    ].join("|");
  }

  /**
   * 从审计行还原影子信号去重键（兼容旧审计行无 shadowSignalKey 字段的情况）。
   */
  function shadowAuditSignalKey(row) {
    if (!row || row.event !== "shadow_trade_open" || !row.strategyId) return "";
    if (row.shadowSignalKey) return String(row.shadowSignalKey);
    return [
      row.strategyId,
      row.direction || "",
      row.signalTime || "",
      row.actionableTime || row.signalTime || ""
    ].join("|");
  }

  /**
   * 从审计日志加载历史影子信号键（仅加载一次）。
   */
  function loadShadowSignalKeys() {
    if (shadowSignalKeysLoaded) return;
    shadowSignalKeysLoaded = true;
    for (const row of tailTradeAudit(2000)) {
      const key = shadowAuditSignalKey(row);
      if (key) shadowSignalKeys.add(key);
    }
  }

  /**
   * 判断影子信号是否已被记录（内存集合或当前会话去重表）。
   */
  function shadowSignalAlreadyRecorded(strategyId, sig) {
    loadShadowSignalKeys();
    const key = shadowSignalKey(strategyId, sig);
    return shadowSignalKeys.has(key) || lastShadowSignals[strategyId] === key;
  }

  /**
   * 记录影子信号键（写入内存集合和当前会话去重表）。
   */
  function rememberShadowSignal(strategyId, sig) {
    const key = shadowSignalKey(strategyId, sig);
    shadowSignalKeys.add(key);
    lastShadowSignals[strategyId] = key;
    return key;
  }

  /**
   * 将平板来源的信号镜像到影子交易。
   * 仅对实盘策略执行，跳过已记录或已有活跃持仓的策略。
   * @param {Object} signals 信号响应对象
   */
  function mirrorTabletSignalsToShadow(signals) {
    const price = getCurrentPrice();
    const tc = getTradeConfig();
    if (!tc.shadowTradingEnabled || !price) return;
    const variants = currentStrategyVariants();
    const liveIds = new Set(currentLiveStrategyIds());
    for (const variant of variants) {
      const strategyId = variant.id;
      if (!liveIds.has(strategyId)) continue;
      const sig = signals && signals[strategyId];
      if (!sig || !sig.signal || shadowSignalAlreadyRecorded(strategyId, sig)) continue;
      // 同策略已有 pending/active 影子交易时跳过
      if (shadowTrades.some(t => ["pending", "active"].includes(t.status) && t.source === "shadow:" + strategyId)) continue;
      if (!signalIsActionableNow(sig)) continue;
      const key = shadowSignalKey(strategyId, sig);
      const trade = placeShadowTrade(strategyId, sig, variant, {
        auditFields: {
          shadowType: "tablet_signal_mirror",
          shadowSignalKey: key,
          shadowSource: "api_signal_autojs"
        }
      });
      if (!trade) continue;
      rememberShadowSignal(strategyId, sig);
      autoTradeLog.push({
        time: new Date().toISOString(),
        strategy: strategyId,
        signal: sig.signal,
        confidence: sig.confidence,
        price: price,
        amount: trade.amount,
        tradeId: "shadow:" + trade.id,
        mode: "tablet_signal_mirror"
      });
    }
  }

  /**
   * 判断策略是否处于冷却期。
   */
  function hasStrategyCooldown(strategyId) {
    const last = Number(lastStrategyTradeAt[strategyId] || 0);
    return last && now() - last < strategyCooldownMs;
  }

  /**
   * 标记策略冷却（记录当前时间为最近成交时间）。
   */
  function markStrategyCooldown(strategyId) {
    if (strategyId) lastStrategyTradeAt[strategyId] = now();
  }

  /**
   * 影子交易检查：对非实盘策略的信号进行影子下单。
   * 由 start() 注册为 3 秒循环定时器。
   */
  function checkShadowTrades() {
    const price = getCurrentPrice();
    const tc = getTradeConfig();
    if (!tc.shadowTradingEnabled) return;
    if (!price) return;
    try {
      const signals = buildSignalResponse("dashboard");
      const variants = currentStrategyVariants();
      const liveIds = new Set(currentLiveStrategyIds());
      for (const variant of variants) {
        const strategyId = variant.id;
        // 实盘策略不在此处执行影子（实盘由 mirrorTabletSignalsToShadow 处理）
        if (liveIds.has(strategyId)) continue;
        const sig = signals[strategyId];
        if (!sig || !sig.signal) continue;
        if (hasStrategyCooldown(strategyId)) continue;
        if (shadowTrades.some(t => ["pending", "active"].includes(t.status) && t.source === "shadow:" + strategyId)) continue;
        if (!signalIsActionableNow(sig)) continue;
        const key = [sig.signal, sig.time || "", signalActionableTime(sig) || ""].join("|");
        if (lastShadowSignals[strategyId] === key) continue;
        const trade = placeShadowTrade(strategyId, sig, variant);
        if (trade) {
          markStrategyCooldown(strategyId);
          lastShadowSignals[strategyId] = key;
          autoTradeLog.push({
            time: new Date().toISOString(),
            strategy: strategyId,
            signal: sig.signal,
            confidence: sig.confidence,
            price: price,
            amount: trade.amount,
            tradeId: "shadow:" + trade.id,
            mode: "shadow"
          });
        }
      }
    } catch (e) {}
  }

  /**
   * 订单簿确认影子交易检查：结合订单簿预测对信号进行确认后影子下单。
   * 由 start() 注册为 3 秒循环定时器。
   */
  function checkOrderbookShadowTrades() {
    if (!enableOrderbookShadowTrades) return;
    const price = getCurrentPrice();
    const tc = getTradeConfig();
    if (!tc.shadowTradingEnabled || !price) return;
    try {
      const signals = buildSignalResponse("dashboard");
      const variants = currentStrategyVariants();
      for (const variant of variants) {
        const baseStrategyId = variant.id;
        const sig = signals[baseStrategyId];
        if (!sig || !sig.signal) continue;
        const confirm = orderbookConfirmForSignal(sig);
        if (!confirm.ok) continue;

        const strategyId = `OB_CONFIRM_${baseStrategyId}`;
        if (hasStrategyCooldown(strategyId)) continue;
        if (shadowTrades.some(t => ["pending", "active"].includes(t.status) && t.source === "shadow:" + strategyId)) continue;
        if (!signalIsActionableNow(sig)) continue;
        const key = [
          sig.signal,
          sig.time || "",
          sig.actionable_time || sig.candle_close_time || "",
          confirm.pred && confirm.pred.timestamp || ""
        ].join("|");
        if (lastShadowSignals[strategyId] === key) continue;
        // 融合订单簿预测信息构造增强信号
        const shadowSig = {
          ...sig,
          strategy_id: strategyId,
          confidence: Math.min(99, Math.round(((Number(sig.confidence) || 0) + confirm.confidence) / 2)),
          orderbook_confirmed: true,
          orderbook_direction: confirm.pred.direction,
          orderbook_confidence: confirm.confidence,
          orderbook_predicted_bps_10s: Number(confirm.predictedBps.toFixed(4)),
          orderbook_predicted_price_10s: confirm.target10 ? confirm.target10.predictedPrice : null,
        };
        const trade = placeShadowTrade(strategyId, shadowSig, { ...variant, tradeEnabled: false }, {
          tradeFields: {
            orderbookConfirmed: true,
            baseStrategyId,
            orderbookPrediction: {
              timestamp: confirm.pred.timestamp,
              direction: confirm.pred.direction,
              confidence: confirm.confidence,
              predictedBps10s: Number(confirm.predictedBps.toFixed(4)),
              predictedPrice10s: confirm.target10 ? confirm.target10.predictedPrice : null,
              mid: confirm.pred.mid
            }
          },
          auditFields: {
            shadowType: "orderbook_confirm",
            baseStrategyId,
            orderbookConfirmed: true,
            orderbookDirection: confirm.pred.direction,
            orderbookConfidence: confirm.confidence,
            orderbookPredictedBps10s: Number(confirm.predictedBps.toFixed(4)),
            orderbookPredictedPrice10s: confirm.target10 ? confirm.target10.predictedPrice : null,
            orderbookMid: confirm.pred.mid,
            orderbookTs: confirm.pred.timestamp
          }
        });
        if (trade) {
          markStrategyCooldown(strategyId);
          lastShadowSignals[strategyId] = key;
          autoTradeLog.push({
            time: new Date().toISOString(),
            strategy: strategyId,
            signal: sig.signal,
            confidence: shadowSig.confidence,
            price: price,
            amount: trade.amount,
            tradeId: "shadow:" + trade.id,
            mode: "orderbook_shadow"
          });
        }
      }
    } catch (e) {}
  }

  // ===========================================================================
  // 5. 结算
  // ===========================================================================

  /**
   * 结算到期的模拟交易和影子交易。
   * 同时处理 trades（服务端模拟）和 shadowTrades（影子），
   * 写入审计日志并广播每笔结算结果。
   * 由 start() 注册为 1 秒循环定时器。
   */
  function settleTrades() {
    const ts = now();
    const price = getCurrentPrice();

    // --- 结算服务端模拟交易 ---
    trades.filter(t => t.status === "active" && ts >= t.settleTime).forEach(t => {
      const sp = price || t.strikePrice;
      let won = t.direction === "UP" ? sp > t.strikePrice : sp < t.strikePrice;
      const tie = sp === t.strikePrice;
      if (tie) {
        t.status = "tie";
        t.settlePrice = sp;
        t.payout = t.amount;
        account.balance += t.amount;
      } else if (won) {
        const pr = payoutRateForDuration(t.duration, payoutRate);
        t.status = "won";
        t.settlePrice = sp;
        t.payoutRate = pr;
        t.payout = t.amount + t.amount * pr;
        account.balance += t.payout;
        account.wins++;
        account.totalPnl += t.amount * pr;
      } else {
        t.status = "lost";
        t.settlePrice = sp;
        t.payout = 0;
        account.losses++;
        account.totalPnl -= t.amount;
      }
      account.totalTrades++;
      writeTradeAudit({
        event: "server_trade_settle",
        tradeId: t.id,
        source: t.source,
        direction: t.direction,
        amount: t.amount,
        duration: t.duration,
        openTime: t.openTime,
        settleTime: ts,
        strikePrice: t.strikePrice,
        settlePrice: sp,
        status: t.status,
        payoutRate: payoutRateForDuration(t.duration, payoutRate),
        payout: t.payout
      });
      console.log(`[Settle] #${t.id} ${t.status} ${t.direction} strike=${t.strikePrice} settle=${sp} pnl=${t.status === "won" ? "+" + (t.payout - t.amount).toFixed(2) : t.status === "lost" ? "-" + t.amount.toFixed(2) : "0"}`);
      broadcastTradeUpdate(t);
    });

    // --- 结算影子交易 ---
    shadowTrades.filter(t => t.status === "active" && ts >= t.settleTime).forEach(t => {
      const sp = price || t.strikePrice;
      const tie = sp === t.strikePrice;
      const won = t.direction === "UP" ? sp > t.strikePrice : sp < t.strikePrice;
      if (tie) {
        t.status = "tie";
        t.settlePrice = sp;
        t.payout = t.amount;
      } else if (won) {
        t.status = "won";
        t.settlePrice = sp;
        t.payout = t.amount + t.amount * payoutRateForDuration(t.duration, payoutRate);
      } else {
        t.status = "lost";
        t.settlePrice = sp;
        t.payout = 0;
      }
      writeTradeAudit({
        event: "shadow_trade_settle",
        serverTime: ts,
        tradeId: t.id,
        source: t.source,
        strategyId: String(t.source || "").replace(/^shadow:/, ""),
        direction: t.direction,
        amount: t.amount,
        duration: t.duration,
        openTime: t.openTime,
        settleTime: ts,
        strikePrice: t.strikePrice,
        settlePrice: sp,
        status: t.status,
        payoutRate: payoutRateForDuration(t.duration, payoutRate),
        payout: t.payout
      });
      broadcastTradeUpdate(t);
    });

    // --- 清理过期交易条目，避免内存无限增长 ---
    if (trades.length > 200) trades = trades.filter(t => t.status === "active" || trades.indexOf(t) > trades.length - 101);
    if (shadowTrades.length > 500) shadowTrades = shadowTrades.filter(t => t.status === "active" || shadowTrades.indexOf(t) > shadowTrades.length - 301);
  }

  // ===========================================================================
  // 6. WebSocket 连接处理
  // ===========================================================================

  /**
   * 处理新的 WebSocket 连接：发送初始化快照并注册消息监听。
   * server.js 的 wss.on("connection") 委托到此方法。
   * @param {WebSocket} ws WebSocket 连接实例
   */
  function handleWebSocketConnection(ws) {
    // 组装并发送初始化消息：市场快照 + 交易状态 + 窗口状态
    const market = getMarketSnapshot();
    const state = getStateSnapshot();
    const wStatus = getTradeWindowStatus();
    ws.send(JSON.stringify({
      type: "init",
      price: market.price,
      time: now(),
      history: (market.priceHistory || []).slice(-300),
      candles: market.candles,
      ...state,
      realBalance: market.realBalance,
      ...wStatus
    }));

    // 注册消息监听
    ws.on("message", (raw) => {
      try {
        const msg = JSON.parse(raw);
        // 手动下单
        if (msg.type === "place_trade") {
          const status = getTradeWindowStatus();
          if (!status.inWindow) {
            ws.send(JSON.stringify({ type: "error", message: "不在交易窗口，下次窗口在 " + status.secUntilNext + " 秒后" }));
            return;
          }
          const { direction, amount } = msg;
          if (direction !== "UP" && direction !== "DOWN") {
            ws.send(JSON.stringify({ type: "error", message: "方向无效" }));
            return;
          }
          const trade = placeTrade(direction, amount || autoTradeAmount, "manual");
          if (trade) {
            ws.send(JSON.stringify({ type: "trade_placed", trade }));
          } else {
            ws.send(JSON.stringify({ type: "error", message: "下单失败（余额不足或价格未就绪）" }));
          }
        }
        // 自动交易开关切换（预留，暂不实现）
        if (msg.type === "toggle_auto") {
          // Allow toggling auto-trade from UI (for future use)
        }
      } catch (e) {}
    });
  }

  // ===========================================================================
  // 7. 定时器生命周期管理
  // ===========================================================================

  /**
   * 启动全部交易相关循环定时器。
   * 保持原有频率：checkAutoTrade(3s)、checkShadowTrades(3s)、
   * checkOrderbookShadowTrades(3s)、settleTrades(1s)、broadcastState(2s)、
   * broadcastWindowStatus(1s)。
   */
  function start() {
    if (repeatingTimers.length) return; // 防止重复启动
    repeatingTimers = [
      setRepeatingTimer(checkAutoTrade, 3000),
      setRepeatingTimer(checkShadowTrades, 3000),
      setRepeatingTimer(checkOrderbookShadowTrades, 3000),
      setRepeatingTimer(settleTrades, 1000),
      setRepeatingTimer(broadcastState, 2000),
      setRepeatingTimer(broadcastWindowStatus, 1000)
    ];
  }

  /**
   * 停止全部交易定时器，并取消尚未执行的影子订单。
   */
  function stop() {
    for (const handle of repeatingTimers) {
      clearRepeatingTimer(handle);
    }
    repeatingTimers = [];

    for (const handle of pendingShadowTimers) {
      clearTimer(handle);
    }
    pendingShadowTimers.clear();

    // 被取消的 pending 订单不能继续留在可执行状态。
    for (const trade of shadowTrades) {
      if (trade.status !== "pending") continue;
      trade.status = "cancelled";
      trade.cancelReason = "engine_stopped";
    }
  }

  // ===========================================================================
  // 导出
  // ===========================================================================

  return {
    // 生命周期
    start,
    stop,
    // 交易窗口
    getTradeWindowStatus,
    // 状态快照
    getStateSnapshot,
    // 交易列表读取
    getTrades: () => trades,
    getShadowTrades: () => shadowTrades,
    // 手动下单（内部等同于 placeTrade）
    placeManualTrade: placeTrade,
    // 平板信号镜像
    mirrorTabletSignalsToShadow,
    // WebSocket 连接处理
    handleWebSocketConnection
  };
}

module.exports = { createTradingEngine };
