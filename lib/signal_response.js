"use strict";

// =============================================================================
// 信号响应管线 + 交易安全门禁模块
// -----------------------------------------------------------------------------
// 本模块从 server.js 整体迁出，负责：
//   1. 信号公共工具（时间解析、参考价、执行上下文）
//   2. 策略适配层（金额计算、变体/ID 列表）
//   3. Entry timing 完整状态机（入场择时）
//   4. 交易安全门禁链（freshness / data health / loss density / execution
//      failure / order lifecycle / auto-trade safety）
//   5. 响应组装 buildSignalResponse
//   6. 缓存失效 invalidateTradeDerivedCaches
//
// 设计要点：
//   - 采用工厂模式 createSignalResponseService(deps)，所有外部依赖通过注入获得。
//   - entryTimingState / entryTimingAllowedSignals / lossDensityCache /
//     executionFailureCache 由本模块独占持有，server.js 不再直接访问。
//   - 动态状态（交易配置、当前价格、服务端成交）通过 getter 注入，保证每次读取最新值。
// =============================================================================

const fs = require("fs");

/**
 * 创建信号响应服务。
 *
 * @param {Object} deps 依赖注入对象
 * @param {Function} deps.getTradeConfig         读取最新交易配置（动态 getter）
 * @param {Function} deps.getCurrentPrice        读取最新现价（动态 getter）
 * @param {Function} deps.getServerTrades        读取服务端成交列表（动态 getter）
 * @param {Function} deps.getPayoutRate          读取赔付率常量（动态 getter）
 * @param {Function} deps.getSignalExpiryMs      读取信号有效期常量（动态 getter）
 * @param {Function} deps.dataHealthGate         数据健康门禁判定函数（来自 data_health）
 * @param {Function} deps.deriveOrderLifecycleGate 订单生命周期门禁派生（来自 trade_history）
 * @param {Function} deps.buildLiveOrderHistory   构建实时订单历史（来自 trade_history）
 * @param {Function} deps.appendTradeAudit        追加交易审计日志
 * @param {Function} deps.tailTradeAudit          读取交易审计尾部
 * @param {Function} deps.readTradeAudit          读取全部交易审计
 * @param {Function} deps.readTradeAuditRange     按时间范围读取交易审计
 * @param {Function} deps.readPriceTicks          读取全部价格 tick
 * @param {Function} deps.readPriceTicksRange     按时间范围读取价格 tick
 * @param {Function} deps.writeOrderLifecycleGateSnapshot 写入订单生命周期快照
 * @param {Function} deps.parseCsvTimeMs          CSV/ISO 时间 -> 毫秒（来自 data_health）
 * @param {Function} deps.shanghaiTime            上海时区格式化（来自 data_health）
 * @param {Function} deps.publicTradeConfig       公开交易配置（来自 trade_config）
 * @param {Function} deps.amountForStrategyConfig 按策略配置计算金额（来自 trade_config）
 * @param {Function} deps.strategyVariants        解析策略变体（来自 trade_config）
 * @param {Function} deps.observedStrategyIds     解析观察策略 ID（来自 trade_config）
 * @param {Function} deps.liveStrategyIds         解析实盘策略 ID（来自 trade_config）
 * @param {string}   deps.signalFile              信号文件路径
 */
function createSignalResponseService(deps) {
  // ---- 注入：动态状态 getter ----
  const {
    getTradeConfig,
    getCurrentPrice,
    getServerTrades,
    getPayoutRate,
    getSignalExpiryMs
  } = deps;

  // ---- 注入：gate / history 工具 ----
  const {
    dataHealthGate,
    deriveOrderLifecycleGate,
    buildLiveOrderHistory
  } = deps;

  // ---- 注入：审计与价格 tick 读写 ----
  const {
    appendTradeAudit,
    tailTradeAudit,      // 预留：当前管线未直接使用，保持注入接口完整
    readTradeAudit,
    readTradeAuditRange,
    readPriceTicks,
    readPriceTicksRange,
    writeOrderLifecycleGateSnapshot
  } = deps;

  // ---- 注入：时间工具 ----
  const { parseCsvTimeMs, shanghaiTime } = deps;

  // ---- 注入：策略配置工具 ----
  const {
    publicTradeConfig,
    amountForStrategyConfig,
    strategyVariants,
    observedStrategyIds,
    liveStrategyIds
  } = deps;

  // ---- 注入：常量 ----
  const { signalFile } = deps;

  // ===========================================================================
  // 模块独占状态（server.js 不再直接持有）
  // ===========================================================================

  // Entry timing 状态机：每个 strategyId 对应一个进行中的择时状态。
  const entryTimingState = {};
  // Entry timing 已放行信号锁存：strategyId -> { signal, expiresAt, signalPayload }
  const entryTimingAllowedSignals = {};

  // 亏损密度审计行缓存（带 TTL，避免每次请求全量重算）。
  let lossDensityCache = { checkedAt: 0, rows: [] };
  // 执行失败审计行缓存（带 TTL）。
  let executionFailureCache = { checkedAt: 0, rows: [] };

  // ===========================================================================
  // 1. 缓存失效
  // ===========================================================================

  // 根据交易事件失效派生缓存：成交/中止事件触发相应缓存重算。
  function invalidateTradeDerivedCaches(event) {
    const name = String(event || "");
    if (name === "order_abort" || name === "order_unverified" || name === "order_done") {
      executionFailureCache = { checkedAt: 0, rows: [] };
    }
    if (name === "order_done" || name === "shadow_trade_open" || name === "shadow_trade_settle") {
      lossDensityCache = { checkedAt: 0, rows: [] };
    }
  }

  // ===========================================================================
  // 2. 信号公共工具
  // ===========================================================================

  // 提取信号的可执行时间：优先 actionable_time，回退 candle_close_time / time。
  function signalActionableTime(sig) {
    if (!sig || typeof sig !== "object") return null;
    if (sig.model_type === "llm_direction") {
      return sig.actionable_time || sig.generated_at || sig.candle_close_time || sig.time || null;
    }
    return sig.actionable_time || sig.candle_close_time || sig.time || null;
  }

  // 信号可执行时间的毫秒时间戳。
  function signalTimeMs(sig) {
    return parseCsvTimeMs(signalActionableTime(sig));
  }

  // 可执行信号最大允许延迟（3 分钟），超过则视为过期。
  function configuredMaxActionableLagMs() {
    return 3 * 60 * 1000;
  }

  // 安全四舍五入；非有限值返回 null。
  function roundNullable(value, digits = 4) {
    const n = Number(value);
    if (!Number.isFinite(n)) return null;
    const mul = 10 ** digits;
    return Math.round(n * mul) / mul;
  }

  // 提取信号参考价：择时参考价 > 入场价 > 信号价 > 择时现价。
  function signalReferencePrice(sig) {
    const timing = sig && sig.entry_timing;
    const candidates = [
      timing && timing.reference_price,
      sig && sig.entry,
      sig && sig.price,
      timing && timing.current_price
    ];
    for (const value of candidates) {
      const n = Number(value);
      if (Number.isFinite(n) && n > 0) return n;
    }
    return null;
  }

  // 构建执行上下文：现价、信号价、价差 bps、可执行时长等实时指标。
  function signalExecutionContext(sig) {
    if (!sig || typeof sig !== "object" || sig.shadow) return null;
    const currentPrice = getCurrentPrice();
    const actionableMs = signalTimeMs(sig);
    const maxLagMs = configuredMaxActionableLagMs();
    const now = Date.now();
    const referencePrice = signalReferencePrice(sig);
    const livePrice = Number(currentPrice);
    const priceChangeBps = (
      Number.isFinite(livePrice) && livePrice > 0 && Number.isFinite(referencePrice) && referencePrice > 0
    ) ? ((livePrice - referencePrice) / referencePrice) * 10000 : null;
    let directionMoveBps = null;
    if (priceChangeBps !== null && (sig.signal === "UP" || sig.signal === "DOWN")) {
      directionMoveBps = sig.signal === "UP" ? priceChangeBps : -priceChangeBps;
    }
    return {
      server_time: isoTime(now),
      current_price: Number.isFinite(livePrice) ? livePrice : null,
      signal_price: referencePrice,
      actionable_time_ms: actionableMs,
      actionable_age_ms: actionableMs === null ? null : now - actionableMs,
      max_actionable_lag_ms: maxLagMs,
      price_change_bps: roundNullable(priceChangeBps, 4),
      direction_move_bps: roundNullable(directionMoveBps, 4)
    };
  }

  // 将信号标记为执行受阻（清空方向/置信度，保留原始值与上下文）。
  function blockSignalForExecution(sig, context, reason) {
    return {
      ...sig,
      signal: null,
      confidence: null,
      execution_blocked: true,
      execution_block_reason: reason,
      blocked_signal: sig.blocked_signal || sig.signal || null,
      blocked_confidence: sig.blocked_confidence || (sig.confidence == null ? null : sig.confidence),
      execution_context: context
    };
  }

  // 执行新鲜度门禁：解析失败或超期的信号被阻断。
  function applyExecutionFreshnessGate(signals) {
    const out = { ...signals };
    for (const [strategyId, sig] of Object.entries(signals || {})) {
      if (strategyId.startsWith("_") || !sig || typeof sig !== "object" || sig.shadow) continue;
      const context = signalExecutionContext(sig);
      if (!context) continue;
      const next = { ...sig, execution_context: context };
      if (!sig.signal) {
        out[strategyId] = next;
        continue;
      }
      if (context.actionable_time_ms === null) {
        out[strategyId] = blockSignalForExecution(next, context, "signal_time_parse_failed");
        continue;
      }
      if (context.actionable_age_ms > context.max_actionable_lag_ms) {
        out[strategyId] = blockSignalForExecution(next, context, "stale_actionable_signal");
        continue;
      }
      out[strategyId] = next;
    }
    return out;
  }

  // 自动交易安全门禁：根据实盘开关判定是否允许自动交易。
  function autoTradeSafetyGate(config) {
    const cfg = config || getTradeConfig();
    const allow = !!(cfg && cfg.realTradingEnabled && cfg.autoTrade_10m);
    return {
      allow,
      blocked: !allow,
      verdict: allow ? "manual_real_trading_enabled" : "manual_real_trading_disabled",
      requiredVerdict: "trade_config.realTradingEnabled",
      manualOverride: allow,
      overrideSource: allow ? "trade_config.realTradingEnabled" : null
    };
  }

  // ===========================================================================
  // 3. 策略适配层
  // ===========================================================================

  // 按策略计算下单金额：固定金额 > 信号覆盖金额 > 配置基础金额。
  function amountForStrategy(strategyId, sig) {
    const tradeConfig = getTradeConfig();
    if (sig && sig.amount && sig.fixed_amount === true) return String(sig.amount);
    const baseAmount = amountForStrategyConfig(strategyId, tradeConfig);
    if (sig && sig.amount) return String(sig.amount);
    return String(baseAmount);
  }

  // 当前启用的策略变体列表。
  function currentStrategyVariants() {
    return strategyVariants(getTradeConfig()).filter(v => v.enabled);
  }

  // 当前观察策略 ID 列表（包含仅观察不实盘的策略）。
  function currentObservedStrategyIds() {
    return observedStrategyIds(getTradeConfig());
  }

  // 当前实盘策略 ID 列表。
  function currentLiveStrategyIds() {
    return liveStrategyIds(getTradeConfig());
  }

  // ===========================================================================
  // 4. Entry timing 完整状态机
  // ===========================================================================

  // 全局开关：是否启用入场择时。
  const ENTRY_TIMING_ENABLED = true;

  // 各策略族对应的入场择时策略。
  const ENTRY_TIMING_POLICIES = {
    BTC_10min_SAFE: {
      name: "direct_after_signal",
      type: "none"
    },
    BTC_10min_TAKER: {
      name: "pullback_0bp_then_confirm_5m",
      type: "pullback_then_confirm",
      pullbackBps: 0,
      maxWaitMin: 5,
      minPullbackDelayMs: 60000,
      minConfirmDelayMs: 60000
    },
    SECOND_VW_CONFIRM: {
      name: "eta_target_price",
      type: "eta_target_price",
      pullbackBps: null,
      maxWaitSec: null
    }
  };

  // 根据策略 ID 匹配择时策略（按前缀匹配策略族）。
  function entryTimingPolicyForStrategy(strategyId) {
    if (String(strategyId || "").startsWith("BTC_10min_SAFE")) return ENTRY_TIMING_POLICIES.BTC_10min_SAFE;
    if (String(strategyId || "").startsWith("BTC_10min_TAKER")) return ENTRY_TIMING_POLICIES.BTC_10min_TAKER;
    if (String(strategyId || "").startsWith("BTC_10min_SECOND_VW_")) return ENTRY_TIMING_POLICIES.SECOND_VW_CONFIRM;
    return null;
  }

  // 毫秒时间戳 -> ISO 字符串。
  function isoTime(ms) {
    return new Date(ms).toISOString();
  }

  // 为信号附加展示用时间字段（时间戳、上海时间等），影子信号不处理。
  function attachDisplayTimes(sig) {
    if (!sig || typeof sig !== "object" || sig.shadow) return sig;
    const signalMs = parseCsvTimeMs(sig.time);
    const actionableTime = signalActionableTime(sig);
    const actionableMs = signalTimeMs(sig);
    return {
      ...sig,
      ...(sig.model_type === "llm_direction" ? { actionable_time: actionableTime } : {}),
      time_ms: signalMs,
      time_shanghai: signalMs === null ? null : shanghaiTime(signalMs),
      actionable_time_ms_display: actionableMs,
      actionable_time_shanghai: actionableMs === null ? null : shanghaiTime(actionableMs),
      display_time_zone: "Asia/Shanghai"
    };
  }

  // 信号可执行毫秒时间戳（无效时返回 0）。
  function signalActionableMs(sig) {
    const ms = signalTimeMs(sig);
    return Number.isFinite(ms) ? ms : 0;
  }

  // 信号当前是否仍可执行（未到则 false，超期则 false）。
  function signalIsActionableNow(sig, now = Date.now()) {
    const actionableMs = signalActionableMs(sig);
    if (!actionableMs) return false;
    const ageMs = now - actionableMs;
    return ageMs >= 0 && ageMs <= configuredMaxActionableLagMs();
  }

  // 生成择时状态唯一键：策略 + 方向 + 可执行时间。
  function entryTimingKey(strategyId, sig) {
    return [
      strategyId,
      sig && sig.signal,
      sig && (sig.actionable_time || sig.candle_close_time || sig.time || "")
    ].join("|");
  }

  // 方向是否一致（UP 需 later>reference，DOWN 需 later<reference）。
  function directionOk(direction, later, reference) {
    if (!Number.isFinite(later) || !Number.isFinite(reference)) return false;
    return direction === "UP" ? later > reference : later < reference;
  }

  // 是否出现回踩（UP 需 price<=ref-move，DOWN 需 price>=ref+move）。
  function pullbackOk(direction, price, reference, bps) {
    if (!Number.isFinite(price) || !Number.isFinite(reference)) return false;
    const move = reference * Number(bps || 0) / 10000;
    return direction === "UP" ? price <= reference - move : price >= reference + move;
  }

  // 计算价格变动 bps。
  function priceMoveBps(later, reference) {
    if (!Number.isFinite(later) || !Number.isFinite(reference) || reference <= 0) return null;
    return ((later - reference) / reference) * 10000;
  }

  // 判断是否还有足够的可执行时间余量（距过期至少 marginMs）。
  function hasActionableTimeMargin(sig, marginMs = 15000) {
    const actionableMs = signalTimeMs(sig);
    if (!actionableMs) return true;
    return Date.now() - actionableMs <= configuredMaxActionableLagMs() - marginMs;
  }

  // 将信号标记为择时受阻（记录择时状态快照）。
  function blockSignalForEntryTiming(sig, state, reason) {
    const currentPrice = getCurrentPrice();
    const out = { ...sig };
    out.signal = null;
    out.confidence = null;
    out.entry_timing = {
      enabled: true,
      ok: false,
      reason,
      policy: state.policy.name,
      reference_price: state.referencePrice,
      current_price: currentPrice,
      started_at: isoTime(state.startedAt),
      expires_at: isoTime(state.expiresAt),
      pullback_seen: !!state.pullbackSeen,
      pullback_price: state.pullbackPrice || null,
      pullback_time: state.pullbackTime ? isoTime(state.pullbackTime) : null
    };
    return out;
  }

  // 读取已锁存的放行信号（未过期且方向一致时复用，避免重复择时判定）。
  function latchedEntrySignal(strategyId, sig) {
    const latched = entryTimingAllowedSignals[strategyId];
    if (!latched) return null;
    const now = Date.now();
    if (now > Number(latched.expiresAt || 0)) {
      delete entryTimingAllowedSignals[strategyId];
      return null;
    }
    if (sig && sig.signal && sig.signal !== latched.signal) return null;
    return {
      ...(sig || latched.signalPayload),
      ...latched.signalPayload,
      entry_timing: {
        ...(latched.signalPayload.entry_timing || {}),
        latched: true,
        latch_expires_at: isoTime(latched.expiresAt)
      }
    };
  }

  // 标记信号择时放行，并锁存供后续请求复用。
  function allowSignalForEntryTiming(sig, state, reason) {
    const signalExpiryMs = getSignalExpiryMs();
    const currentPrice = getCurrentPrice();
    const now = Date.now();
    if (!state.allowedAt) {
      state.allowedAt = now;
      state.allowedActionableTime = isoTime(now);
    } else if (now - state.allowedAt > signalExpiryMs) {
      delete entryTimingState[state.strategyId];
      return blockSignalForEntryTiming(sig, state, "entry_timing_entry_window_elapsed");
    }
    const allowed = {
      ...sig,
      actionable_time: state.allowedActionableTime,
      entry_timing: {
        enabled: true,
        ok: true,
        reason,
        policy: state.policy.name,
        original_actionable_time: state.originalActionableTime,
        reference_price: state.referencePrice,
        current_price: currentPrice,
        started_at: isoTime(state.startedAt),
        entry_time: state.allowedActionableTime,
        pullback_seen: !!state.pullbackSeen,
        pullback_price: state.pullbackPrice || null,
        pullback_time: state.pullbackTime ? isoTime(state.pullbackTime) : null
      }
    };
    entryTimingAllowedSignals[state.strategyId] = {
      signal: sig.signal,
      expiresAt: Math.min(state.allowedAt + signalExpiryMs, state.expiresAt + signalExpiryMs),
      signalPayload: allowed
    };
    return allowed;
  }

  // 单个策略信号的入场择时核心状态机。
  function applyEntryTimingForSignal(strategyId, sig) {
    const currentPrice = getCurrentPrice();
    const policy = entryTimingPolicyForStrategy(strategyId);
    if (sig && sig.bypass_entry_timing) return sig;
    const latched = latchedEntrySignal(strategyId, sig);
    if (latched) return latched;
    if (!ENTRY_TIMING_ENABLED || !policy || !sig || !sig.signal) return sig;
    if (policy.type === "none") return sig;

    const now = Date.now();
    const actionableMs = signalActionableMs(sig);
    if (!actionableMs || actionableMs > now) {
      return {
        ...sig,
        signal: null,
        confidence: null,
        entry_timing: { enabled: true, ok: false, policy: policy.name, reason: "wait_actionable_time" }
      };
    }

    const key = entryTimingKey(strategyId, sig);
    let state = entryTimingState[strategyId];
    if (!state || state.key !== key) {
      const referencePrice = Number.isFinite(Number(currentPrice)) ? Number(currentPrice) : Number(sig.price);
      state = {
        key,
        policy,
        strategyId,
        signal: sig.signal,
        referencePrice,
        startedAt: now,
        originalActionableMs: actionableMs,
        originalActionableTime: signalActionableTime(sig),
        earliestPullbackAt: actionableMs + Number(policy.minPullbackDelayMs || 0),
        expiresAt: actionableMs + Number(policy.maxWaitMin || 0) * 60000,
        pullbackSeen: false,
        pullbackPrice: null,
        pullbackTime: null
      };
      entryTimingState[strategyId] = state;
      if (policy.type === "eta_target_price") {
        const waitSec = Number(sig.eta_max_wait_sec || policy.maxWaitSec || 45);
        const targetBps = Number(sig.eta_target_bps || policy.pullbackBps || 2);
        state.expiresAt = actionableMs + waitSec * 1000;
        state.etaTargetBps = targetBps;
        state.etaMaxWaitSec = waitSec;
        state.etaTargetPrice = Number(sig.eta_entry_target_price);
      }
      appendTradeAudit({
        serverTime: now,
        event: "entry_timing_start",
        strategyId,
        signal: sig.signal,
        policy: policy.name,
        referencePrice,
        originalActionableTime: state.originalActionableTime,
        expiresAt: state.expiresAt
      });
    }

    if (!Number.isFinite(Number(currentPrice))) {
      return blockSignalForEntryTiming(sig, state, "missing_live_price");
    }
    if (now > state.expiresAt) {
      appendTradeAudit({
        serverTime: now,
        event: "entry_timing_expired",
        strategyId,
        signal: sig.signal,
        policy: policy.name,
        referencePrice: state.referencePrice,
        currentPrice
      });
      delete entryTimingState[strategyId];
      return blockSignalForEntryTiming(sig, state, "expired_without_confirmation");
    }
    if (now < state.earliestPullbackAt) {
      return blockSignalForEntryTiming(sig, state, "waiting_first_1m_check");
    }

    const price = Number(currentPrice);
    if (policy.type === "pullback_within") {
      if (pullbackOk(sig.signal, price, state.referencePrice, policy.pullbackBps)) {
        appendTradeAudit({
          serverTime: now,
          event: "entry_timing_allow",
          strategyId,
          signal: sig.signal,
          policy: policy.name,
          referencePrice: state.referencePrice,
          entryPrice: price
        });
        return allowSignalForEntryTiming(sig, state, "pullback_seen");
      }
      return blockSignalForEntryTiming(sig, state, "waiting_pullback");
    }

    if (policy.type === "eta_target_price") {
      const targetBps = Number(state.etaTargetBps || sig.eta_target_bps || 2);
      const targetPrice = Number(state.etaTargetPrice);
      const upConfirmBps = Number(sig.up_reversal_confirm_bps ?? 0.0);
      const upConfirmMaxSec = Number(sig.up_reversal_confirm_max_sec ?? 20);
      const hitTarget = Number.isFinite(targetPrice)
        ? (sig.signal === "UP" ? price <= targetPrice : price >= targetPrice)
        : pullbackOk(sig.signal, price, state.referencePrice, targetBps);
      if (sig.signal === "UP") {
        if (hitTarget && !state.upTargetHitAt) {
          state.upTargetHitAt = now;
          state.upTargetPrice = price;
          state.upTargetLow = price;
          appendTradeAudit({
            serverTime: now,
            event: "entry_timing_pullback_seen",
            strategyId,
            signal: sig.signal,
            policy: policy.name,
            referencePrice: state.referencePrice,
            targetPrice: Number.isFinite(targetPrice) ? targetPrice : null,
            targetBps,
            entryPrice: price,
            confirmBps: upConfirmBps,
            confirmMaxSec: upConfirmMaxSec
          });
        }
        if (!state.upTargetHitAt) {
          return blockSignalForEntryTiming(sig, state, "waiting_eta_target_price");
        }
        state.upTargetLow = Math.min(Number(state.upTargetLow || price), price);
        const reboundBps = priceMoveBps(price, Number(state.upTargetLow));
        const confirmOk = Number(upConfirmBps) <= 0 || (reboundBps !== null && reboundBps >= upConfirmBps);
        if (!confirmOk) {
          const reason = now > Number(state.upTargetHitAt) + upConfirmMaxSec * 1000
            ? "up_reversal_confirm_failed"
            : "waiting_up_reversal_confirm";
          return blockSignalForEntryTiming(sig, state, reason);
        }
        if (!hasActionableTimeMargin(sig)) {
          delete entryTimingState[strategyId];
          return blockSignalForEntryTiming(sig, state, "entry_timing_insufficient_actionable_margin");
        }
        appendTradeAudit({
          serverTime: now,
          event: "entry_timing_allow",
          strategyId,
          signal: sig.signal,
          policy: policy.name,
          referencePrice: state.referencePrice,
          targetPrice: Number.isFinite(targetPrice) ? targetPrice : null,
          targetBps,
          entryPrice: price,
          upTargetLow: state.upTargetLow,
          reboundBps
        });
        return allowSignalForEntryTiming(sig, state, "up_reversal_confirmed");
      }
      if (hitTarget) {
        if (!hasActionableTimeMargin(sig)) {
          delete entryTimingState[strategyId];
          return blockSignalForEntryTiming(sig, state, "entry_timing_insufficient_actionable_margin");
        }
        appendTradeAudit({
          serverTime: now,
          event: "entry_timing_allow",
          strategyId,
          signal: sig.signal,
          policy: policy.name,
          referencePrice: state.referencePrice,
          targetPrice: Number.isFinite(targetPrice) ? targetPrice : null,
          targetBps,
          entryPrice: price
        });
        return allowSignalForEntryTiming(sig, state, "eta_target_hit");
      }
      return blockSignalForEntryTiming(sig, state, "waiting_eta_target_price");
    }

    if (policy.type === "pullback_then_confirm") {
      if (!state.pullbackSeen) {
        if (pullbackOk(sig.signal, price, state.referencePrice, policy.pullbackBps)) {
          state.pullbackSeen = true;
          state.pullbackPrice = price;
          state.pullbackTime = now;
          appendTradeAudit({
            serverTime: now,
            event: "entry_timing_pullback_seen",
            strategyId,
            signal: sig.signal,
            policy: policy.name,
            referencePrice: state.referencePrice,
            pullbackPrice: price
          });
        }
        return blockSignalForEntryTiming(sig, state, "waiting_pullback");
      }
      if (now < state.pullbackTime + Number(policy.minConfirmDelayMs || 0)) {
        return blockSignalForEntryTiming(sig, state, "waiting_confirm_1m");
      }
      if (directionOk(sig.signal, price, Number(state.pullbackPrice))) {
        appendTradeAudit({
          serverTime: now,
          event: "entry_timing_allow",
          strategyId,
          signal: sig.signal,
          policy: policy.name,
          referencePrice: state.referencePrice,
          pullbackPrice: state.pullbackPrice,
          entryPrice: price
        });
        return allowSignalForEntryTiming(sig, state, "pullback_confirmed");
      }
      return blockSignalForEntryTiming(sig, state, "waiting_direction_confirm");
    }

    return sig;
  }

  // 对全部实盘策略信号应用入场择时。
  function applyEntryTiming(signals) {
    const out = { ...signals };
    for (const strategyId of currentLiveStrategyIds()) {
      const next = applyEntryTimingForSignal(strategyId, signals[strategyId]);
      if (next) out[strategyId] = next;
      else delete out[strategyId];
      if (!signals[strategyId] || !signals[strategyId].signal) delete entryTimingState[strategyId];
    }
    return out;
  }

  // ===========================================================================
  // 5. Gate 适配层
  // ===========================================================================

  // 自动交易安全门禁适配：被阻断时清空全部非影子信号方向。
  function applyAutoTradeSafetyGate(signals) {
    const gate = autoTradeSafetyGate();
    if (!gate.blocked) return { signals, gate };
    const out = { ...signals };
    for (const [strategyId, sig] of Object.entries(signals)) {
      if (!sig || typeof sig !== "object" || sig.shadow) continue;
      out[strategyId] = {
        ...sig,
        signal: null,
        confidence: null,
        safety_blocked: true,
        safety_block_reason: gate.verdict,
        blocked_signal: sig.signal || null,
        blocked_confidence: sig.confidence == null ? null : sig.confidence
      };
    }
    return { signals: out, gate };
  }

  // 数据健康门禁适配：全局数据异常时清空全部非影子信号方向。
  function applyDataHealthGate(signals, gate) {
    if (!gate.blocked) return { signals, gate };
    const blanketReasons = (gate.reasons || []).filter(reason => reason !== "signal_process_data_health_blocked");
    if (!blanketReasons.length) return { signals, gate };
    const out = { ...signals };
    for (const [strategyId, sig] of Object.entries(signals)) {
      if (!sig || typeof sig !== "object" || sig.shadow) continue;
      out[strategyId] = {
        ...sig,
        signal: null,
        confidence: null,
        data_health_blocked: true,
        data_health_block_reasons: blanketReasons,
        blocked_signal: sig.blocked_signal || sig.signal || null,
        blocked_confidence: sig.blocked_confidence || (sig.confidence == null ? null : sig.confidence)
      };
    }
    return { signals: out, gate };
  }

  // ---- 亏损密度门禁 ----

  // 从策略变体解析亏损密度策略（窗口、阈值、冷却时间等）。
  function lossDensityPolicyForVariant(variant) {
    if (
      !variant
      || !["SECOND_NORMAL_STATE_V11", "SECOND_NORMAL_ROUTER_V21"].includes(variant.base)
      || variant.lossDensityEnabled !== true
    ) return null;
    const window = Math.max(2, Math.min(50, Number(variant.lossDensityWindow) || 6));
    const losses = Math.max(1, Math.min(window, Number(variant.lossDensityLosses) || 3));
    const defaultMinTrades = Math.min(window, Math.max(losses, losses + 1));
    const minTrades = Math.max(losses, Math.min(window, Number(variant.lossDensityMinTrades) || defaultMinTrades));
    const cooldownSec = Math.max(60, Math.min(86400, Number(variant.lossDensityCooldownSec) || 28800));
    const lookbackHours = Math.max(1, Math.min(720, Number(variant.lossDensityLookbackHours) || 72));
    const streakEnabled = variant.lossStreakEnabled === true || variant.base === "SECOND_NORMAL_ROUTER_V21";
    const streakCount = Math.max(1, Math.min(20, Number(variant.lossStreakCount) || 2));
    const streakCooldownSec = Math.max(60, Math.min(86400, Number(variant.lossStreakCooldownSec) || 3600));
    return { window, losses, minTrades, cooldownSec, lookbackHours, streakEnabled, streakCount, streakCooldownSec };
  }

  // 读取近 N 小时的已结算成交行（带 TTL 缓存）。
  function recentLossDensityRows(now, lookbackHours) {
    const currentPrice = getCurrentPrice();
    const payoutRate = getPayoutRate();
    const ttlMs = Number(process.env.LOSS_DENSITY_CACHE_MS || 15000);
    if (lossDensityCache.rows.length && now - lossDensityCache.checkedAt < ttlMs) {
      return lossDensityCache.rows;
    }
    const startMs = now - Math.max(1, Number(lookbackHours) || 72) * 60 * 60 * 1000;
    const endMs = now + 2 * 60 * 1000;
    try {
      const auditRows = readTradeAuditRange(startMs, endMs);
      const priceTicks = readPriceTicksRange(startMs, endMs);
      const history = buildLiveOrderHistory({
        auditRows,
        priceTicks,
        serverTrades: getServerTrades(),
        currentPrice,
        payoutRate,
        now,
        mode: "page",
        kind: "all",
        limit: 300,
        pageSize: 300
      });
      lossDensityCache = {
        checkedAt: now,
        rows: Array.isArray(history.recent) ? history.recent : []
      };
    } catch (e) {
      lossDensityCache = { checkedAt: now, rows: [] };
    }
    return lossDensityCache.rows;
  }

  // 判断历史行是否为影子成交。
  function isShadowHistoryRow(row) {
    return String(row && row.source || "").startsWith("shadow:") || row && row.event === "shadow_trade";
  }

  // 筛选指定策略的已结算（赢/亏）行；实盘模式优先真实成交，否则回退影子成交。
  function settledRowsForLossDensity(strategyId, variant, policy, now) {
    const tradeConfig = getTradeConfig();
    const rows = recentLossDensityRows(now, policy.lookbackHours)
      .filter(row => row && row.strategyId === strategyId && (row.status === "won" || row.status === "lost"))
      .map(row => ({
        status: row.status,
        source: row.source || "",
        openTime: Number(row.openTime || 0),
        settleTime: Number(row.settleTime || row.openTime || 0)
      }))
      .filter(row => Number.isFinite(row.settleTime) && row.settleTime > 0)
      .sort((a, b) => a.settleTime - b.settleTime);

    const realRows = rows.filter(row => !isShadowHistoryRow(row));
    const shadowRows = rows.filter(row => isShadowHistoryRow(row));
    if (tradeConfig.realTradingEnabled && variant.tradeEnabled !== false) return realRows;
    if (shadowRows.length) return shadowRows;
    return rows;
  }

  // 计算策略的亏损密度状态（滑动窗口 + 连亏 streak，输出冷却截止时间）。
  function lossDensityStateForStrategy(strategyId, variant, now = Date.now()) {
    const policy = lossDensityPolicyForVariant(variant);
    if (!policy) return null;
    const rows = settledRowsForLossDensity(strategyId, variant, policy, now);
    const rolling = [];
    let streak = 0;
    let lastTrigger = null;
    let lastStreakTrigger = null;
    for (const row of rows) {
      streak = row.status === "lost" ? streak + 1 : 0;
      if (policy.streakEnabled && streak >= policy.streakCount) {
        lastStreakTrigger = {
          triggerTime: row.settleTime,
          lossCount: streak
        };
        streak = 0;
      }
      rolling.push(row.status);
      while (rolling.length > policy.window) rolling.shift();
      const lossCount = rolling.filter(status => status === "lost").length;
      if (rolling.length >= policy.minTrades && lossCount >= policy.losses) {
        lastTrigger = {
          triggerTime: row.settleTime,
          lossCount,
          windowStatuses: rolling.slice()
        };
        rolling.length = 0;
      }
    }
    const densityUntil = lastTrigger ? lastTrigger.triggerTime + policy.cooldownSec * 1000 : 0;
    const streakUntil = lastStreakTrigger ? lastStreakTrigger.triggerTime + policy.streakCooldownSec * 1000 : 0;
    const cooldownUntil = Math.max(densityUntil, streakUntil);
    const blocked = Boolean(cooldownUntil && now < cooldownUntil);
    return {
      enabled: true,
      blocked,
      policy,
      historyCount: rows.length,
      recentStatuses: rows.slice(-policy.window).map(row => row.status),
      lastTrigger,
      lastStreakTrigger,
      cooldownUntil: blocked ? cooldownUntil : null,
      cooldownUntilIso: blocked ? new Date(cooldownUntil).toISOString() : null,
      cooldownUntilShanghai: blocked ? shanghaiTime(cooldownUntil) : null
    };
  }

  // 将信号标记为亏损密度受阻。
  function blockSignalForLossDensity(sig, state) {
    const cooldownDetail = `正态回归失效冷却：最近${state.policy.window}单窗口内，已观察至少${state.policy.minTrades}单且亏损达到${state.policy.losses}单，暂停到 ${state.cooldownUntilShanghai || state.cooldownUntilIso}`;
    return {
      ...({
      ...sig,
      signal: null,
      confidence: null,
      high_conf: false,
      loss_density_blocked: true,
      loss_density: state,
      blocked_signal: sig && (sig.blocked_signal || sig.signal) || null,
      blocked_confidence: sig && (sig.blocked_confidence || sig.confidence) || null,
      reason: "loss_density_cooldown",
      signal_detail: `正态回归失效冷却：最近${state.policy.window}单内亏损达到${state.policy.losses}单，暂停到 ${state.cooldownUntilShanghai || state.cooldownUntilIso}`
    }),
      signal_detail: cooldownDetail
    };
  }

  // 对全部观察策略应用亏损密度门禁。
  function applyLossDensityGate(signals) {
    const variants = currentStrategyVariants();
    const byId = new Map(variants.map(variant => [variant.id, variant]));
    const out = { ...signals };
    const states = {};
    const now = Date.now();
    for (const strategyId of currentObservedStrategyIds()) {
      const variant = byId.get(strategyId);
      const state = lossDensityStateForStrategy(strategyId, variant, now);
      if (!state) continue;
      states[strategyId] = state;
      const sig = out[strategyId];
      if (!sig || typeof sig !== "object") continue;
      out[strategyId] = { ...sig, loss_density: state };
      if (state.blocked && sig.signal) {
        out[strategyId] = blockSignalForLossDensity(sig, state);
      }
    }
    return { signals: out, gate: { strategies: states } };
  }

  // ---- 执行失败门禁 ----

  // 读取近期的执行失败审计行（带 TTL 缓存）。
  function recentExecutionFailureRows(now) {
    const ttlMs = Number(process.env.EXECUTION_FAILURE_CACHE_MS || 5000);
    if (executionFailureCache.rows.length && now - executionFailureCache.checkedAt < ttlMs) {
      return executionFailureCache.rows;
    }
    const lookbackMs = Math.max(
      10 * 60 * 1000,
      Number(process.env.EXECUTION_FAILURE_LOOKBACK_MS || 3 * 60 * 60 * 1000)
    );
    try {
      executionFailureCache = {
        checkedAt: now,
        rows: readTradeAuditRange(now - lookbackMs, now + 60 * 1000)
      };
    } catch (e) {
      executionFailureCache = { checkedAt: now, rows: [] };
    }
    return executionFailureCache.rows;
  }

  // 计算策略的执行失败状态（单次失败 / 金额失败 / 重复失败，输出冷却截止时间）。
  function executionFailureStateForStrategy(strategyId, now = Date.now()) {
    const baseCooldownMs = Math.max(
      60 * 1000,
      Number(process.env.EXECUTION_FAILURE_COOLDOWN_MS || 10 * 60 * 1000)
    );
    const repeatedWindowMs = Math.max(
      10 * 60 * 1000,
      Number(process.env.EXECUTION_FAILURE_REPEAT_WINDOW_MS || 3 * 60 * 60 * 1000)
    );
    const repeatedThreshold = Math.max(
      2,
      Number(process.env.EXECUTION_FAILURE_REPEAT_THRESHOLD || 3)
    );
    const repeatedCooldownMs = Math.max(
      baseCooldownMs,
      Number(process.env.EXECUTION_FAILURE_REPEAT_COOLDOWN_MS || 60 * 60 * 1000)
    );
    const amountFailedCooldownMs = Math.max(
      baseCooldownMs,
      Number(process.env.EXECUTION_AMOUNT_FAILED_COOLDOWN_MS || 30 * 60 * 1000)
    );
    const strategyRows = recentExecutionFailureRows(now)
      .filter(row => row && row.strategyId === strategyId)
      .map(row => ({
        event: row.event,
        reason: row.reason || "unknown",
        serverTime: Number(row.serverTime || row.clientTime || 0),
        amount: row.amount,
        duration: row.duration,
        device: row.device || null
      }))
      .filter(row => Number.isFinite(row.serverTime) && row.serverTime > 0)
      .sort((a, b) => a.serverTime - b.serverTime);
    const lastSuccess = [...strategyRows].reverse().find(row => row.event === "order_done") || null;
    const rows = strategyRows
      .filter(row => row.event === "order_abort" || row.event === "order_unverified")
      .filter(row => row.reason !== "stale_actionable_signal_before_click")
      .filter(row => !lastSuccess || row.serverTime > lastSuccess.serverTime);
    const last = rows[rows.length - 1] || null;
    const recentSinceWindowStart = last
      ? rows.filter(row => row.serverTime >= last.serverTime - repeatedWindowMs)
      : [];
    let cooldownMs = baseCooldownMs;
    let mode = "single_failure";
    if (last && last.reason === "amount_failed") {
      cooldownMs = Math.max(cooldownMs, amountFailedCooldownMs);
      mode = "amount_failed";
    }
    if (recentSinceWindowStart.length >= repeatedThreshold) {
      cooldownMs = Math.max(cooldownMs, repeatedCooldownMs);
      mode = "repeated_failure";
    }
    const cooldownUntil = last ? last.serverTime + cooldownMs : 0;
    const blocked = Boolean(last && now < cooldownUntil);
    return {
      enabled: true,
      blocked,
      recentCount: rows.length,
      recentCountInWindow: recentSinceWindowStart.length,
      last,
      lastReasonLabel: last ? executionFailureReasonLabel(last.reason) : null,
      lastSuccessTime: lastSuccess ? lastSuccess.serverTime : null,
      lastSuccessIso: lastSuccess ? new Date(lastSuccess.serverTime).toISOString() : null,
      cooldownMs,
      mode,
      policy: {
        baseCooldownMs,
        amountFailedCooldownMs,
        repeatedWindowMs,
        repeatedThreshold,
        repeatedCooldownMs
      },
      cooldownUntil: blocked ? cooldownUntil : null,
      cooldownUntilIso: blocked ? new Date(cooldownUntil).toISOString() : null,
      cooldownUntilShanghai: blocked ? shanghaiTime(cooldownUntil) : null
    };
  }

  // 执行失败原因 -> 中文标签。
  function executionFailureReasonLabel(reason) {
    const key = String(reason || "");
    const map = {
      amount_failed: "金额输入失败",
      duration_failed: "周期选择失败",
      cannot_wake_screen: "平板屏幕唤醒失败",
      balance_before_unavailable: "下单前余额读取失败",
      balance_not_decreased: "余额未变化，无法确认成交",
      confirm_not_found: "确认按钮没找到",
      signal_time_parse_failed_before_click: "信号时间解析失败",
      stale_actionable_signal_before_click: "点击前信号已过期",
      order_failed: "下单执行失败",
      unknown: "未知执行失败"
    };
    return map[key] || key || "未知执行失败";
  }

  // 将信号标记为执行失败受阻。
  function blockSignalForExecutionFailure(sig, state) {
    const reason = state && state.last ? state.last.reason : "order_failed";
    const label = executionFailureReasonLabel(reason);
    return {
      ...sig,
      signal: null,
      confidence: null,
      execution_failure_blocked: true,
      execution_failure: state,
      execution_failure_label: label,
      blocked_signal: sig && (sig.blocked_signal || sig.signal) || null,
      blocked_confidence: sig && (sig.blocked_confidence || sig.confidence) || null,
      reason: "recent_order_failure_cooldown",
      signal_detail: `最近实盘下单失败：${label}，暂停到 ${state.cooldownUntilShanghai || state.cooldownUntilIso}`
    };
  }

  // 对全部实盘策略应用执行失败门禁。
  function applyExecutionFailureGate(signals) {
    const out = { ...signals };
    const states = {};
    const now = Date.now();
    for (const strategyId of currentLiveStrategyIds()) {
      const state = executionFailureStateForStrategy(strategyId, now);
      states[strategyId] = state;
      const sig = out[strategyId];
      if (!sig || typeof sig !== "object") continue;
      out[strategyId] = { ...sig, execution_failure: state };
      if (state.blocked && sig.signal) {
        out[strategyId] = blockSignalForExecutionFailure(sig, state);
      }
    }
    return { signals: out, gate: { strategies: states } };
  }

  // ===========================================================================
  // 6. 订单生命周期适配层
  // ===========================================================================

  // 策略 -> 持仓周期（分钟）映射。
  function configuredDurationMap() {
    const tradeConfig = getTradeConfig();
    return Object.fromEntries(
      currentStrategyVariants().map(variant => [variant.id, variant.duration || tradeConfig.duration || 10])
    );
  }

  // 当前订单生命周期门禁（每次从持久审计/价格事件重新派生）。
  function currentOrderLifecycleGate(now = Date.now()) {
    const currentPrice = getCurrentPrice();
    const payoutRate = getPayoutRate();
    const tradeConfig = getTradeConfig();
    // 每次从持久审计和价格事件重新派生，服务重启后无需依赖旧进程内存。
    const gate = deriveOrderLifecycleGate({
      auditRows: readTradeAudit(),
      priceTicks: readPriceTicks(),
      currentPrice,
      payoutRate,
      now,
      durationForStrategy: configuredDurationMap(),
      defaultDuration: tradeConfig.duration || 10
    });
    try { writeOrderLifecycleGateSnapshot(gate); }
    catch (e) { console.warn("[OrderLifecycle] gate snapshot write failed:", e.message); }
    return gate;
  }

  // 订单生命周期门禁适配：同策略订单未结束时清空该策略信号方向。
  function applyOrderLifecycleGate(signals) {
    const gate = currentOrderLifecycleGate();
    const out = { ...signals };
    for (const [strategyId, state] of Object.entries(gate.strategies || {})) {
      const sig = out[strategyId];
      if (!sig || typeof sig !== "object") continue;
      // 仅清空同策略信号，其他策略保持独立可交易。
      out[strategyId] = {
        ...sig,
        signal: null,
        confidence: null,
        order_lifecycle_blocked: true,
        order_lifecycle: state,
        blocked_signal: sig.blocked_signal || sig.signal || null,
        blocked_confidence: sig.blocked_confidence ?? sig.confidence ?? null,
        reason: state.reason,
        signal_detail: state.reason === "settlement_price_pending"
          ? "订单已到期，等待有效结算价格"
          : `同策略订单生命周期未结束，预计到期 ${new Date(state.settleTime).toISOString()}`
      };
    }
    return { signals: out, gate };
  }

  // ===========================================================================
  // 7. LLM 日志
  // ===========================================================================

  // 根据决策 ID 从信号文件读取 LLM 输入/输出快照。
  function llmLogSnapshotForDecision(decisionId) {
    if (!decisionId || !fs.existsSync(signalFile)) return null;
    try {
      const signals = JSON.parse(fs.readFileSync(signalFile, "utf8"));
      for (const sig of Object.values(signals)) {
        if (!sig || typeof sig !== "object" || sig.llm_decision_id !== decisionId) continue;
        return {
          llm_decision_id: sig.llm_decision_id,
          llm_model: sig.llm_model || null,
          llm_prompt: typeof sig.llm_prompt === "string" ? sig.llm_prompt : null,
          llm_response: typeof sig.llm_response === "string" ? sig.llm_response : null
        };
      }
    } catch (error) {
      console.warn("[LLM Log] Failed to read signal snapshot:", error.message);
    }
    return null;
  }

  // ===========================================================================
  // 8. 响应组装
  // ===========================================================================

  // 从原始信号中选取观察策略 + 全部 _ 前缀元数据。
  function selectObservedSignals(rawSignals, observedIds) {
    return {
      ...Object.fromEntries(Object.entries(rawSignals).filter(([key]) => key.startsWith("_"))),
      ...Object.fromEntries(
        observedIds
          .filter(strategyId => rawSignals[strategyId])
          .map(strategyId => [strategyId, rawSignals[strategyId]])
      )
    };
  }

  // 按来源策略过滤/裁剪信号（dashboard 标记可交易；平板/脚本去除 LLM 明文）。
  function applySignalSourcePolicy(signals, source, observedIds, liveIds) {
    const tradeConfig = getTradeConfig();
    const tradeable = new Set(liveIds);
    for (const strategyId of observedIds) {
      const signal = signals[strategyId];
      if (!signal) continue;
      if (source === "dashboard") {
        signal.trade_enabled = tradeable.has(strategyId);
        continue;
      }
      if (!tradeConfig.realTradingEnabled || !tradeConfig.autoTrade_10m || !tradeable.has(strategyId)) {
        signal.signal = null;
      }
      if (source === "autojs" || source === "tablet") {
        // 平板只需要下单字段和 decision_id，完整模型输入输出留在服务端。
        delete signal.llm_input;
        delete signal.llm_prompt;
        delete signal.llm_response;
      }
    }
  }

  // 组装完整的信号响应：读取原始信号 -> 依次执行门禁链 -> 附加元数据。
  // 门禁执行顺序固定：entry timing -> execution freshness -> data health ->
  // loss density -> execution failure -> order lifecycle -> auto-trade safety ->
  // display times -> source policy。
  function buildSignalResponse(source = "") {
    const currentPrice = getCurrentPrice();
    const tradeConfig = getTradeConfig();
    const rawSignals = fs.existsSync(signalFile) ? JSON.parse(fs.readFileSync(signalFile, "utf8")) : {};
    const observedIds = currentObservedStrategyIds();
    const liveIds = currentLiveStrategyIds();
    const liveRawSignals = selectObservedSignals(rawSignals, observedIds);
    // 交易安全门禁按依赖顺序执行，后一级基于前一级已经过滤的结果。
    const timedSignals = applyEntryTiming(liveRawSignals);
    const freshSignals = applyExecutionFreshnessGate(timedSignals);
    const health = applyDataHealthGate(freshSignals, dataHealthGate(freshSignals));
    const lossDensity = applyLossDensityGate(health.signals);
    const executionFailure = applyExecutionFailureGate(lossDensity.signals);
    const orderLifecycle = applyOrderLifecycleGate(executionFailure.signals);
    const safety = source === "dashboard"
      ? { signals: orderLifecycle.signals, gate: autoTradeSafetyGate() }
      : applyAutoTradeSafetyGate(orderLifecycle.signals);

    // Clone signals to prevent modifying in-memory cache
    const signals = JSON.parse(JSON.stringify(safety.signals));
    for (const [strategyId, sig] of Object.entries(signals)) {
      if (!strategyId.startsWith("_")) signals[strategyId] = attachDisplayTimes(sig);
    }

    applySignalSourcePolicy(signals, source, observedIds, liveIds);

    const strategyAmounts = {};
    for (const strategyId of observedIds) {
      if (signals[strategyId]) strategyAmounts[strategyId] = amountForStrategy(strategyId, signals[strategyId]);
    }
    const legacySig = observedIds.map(id => signals[id]).find(Boolean);
    const legacyAmount = legacySig ? amountForStrategy(legacySig.strategy_id, legacySig) : String(tradeConfig.amount);

    // Supply backward compatible config keys for old tablet/scripts
    const configCopy = {
      ...publicTradeConfig(tradeConfig),
      autoTrade: tradeConfig.realTradingEnabled && tradeConfig.autoTrade_10m
    };

    return {
      ...signals,
      _config: configCopy,
      _strategyVariants: currentStrategyVariants(),
      _strategyAmounts: strategyAmounts,
      _signalAmount: legacyAmount,
      _entryTimingEnabled: ENTRY_TIMING_ENABLED,
      _entryTimingPolicies: Object.fromEntries(observedIds.map(id => [id, entryTimingPolicyForStrategy(id)])),
      _execution: {
        serverTime: isoTime(Date.now()),
        serverTimeMs: Date.now(),
        serverTimeShanghai: shanghaiTime(Date.now()),
        displayTimeZone: "Asia/Shanghai",
        currentPrice: Number.isFinite(Number(currentPrice)) ? Number(currentPrice) : null,
        maxActionableLagMs: configuredMaxActionableLagMs()
      },
      _dataHealthGate: health.gate,
      _lossDensityGate: lossDensity.gate,
      _executionFailureGate: executionFailure.gate,
      _orderLifecycleGate: orderLifecycle.gate,
      _autoTradeSafetyGate: safety.gate
    };
  }

  // ===========================================================================
  // 导出
  // ===========================================================================

  return {
    // 缓存失效
    invalidateTradeDerivedCaches,
    // 信号公共工具
    signalActionableTime,
    signalTimeMs,
    configuredMaxActionableLagMs,
    roundNullable,
    signalReferencePrice,
    signalExecutionContext,
    signalActionableMs,
    signalIsActionableNow,
    autoTradeSafetyGate,
    // 策略适配
    amountForStrategy,
    currentStrategyVariants,
    currentObservedStrategyIds,
    currentLiveStrategyIds,
    // Entry timing
    ENTRY_TIMING_ENABLED,
    entryTimingPolicyForStrategy,
    attachDisplayTimes,
    // Gate 适配
    applyEntryTiming,
    // 响应组装（对外暴露为 build）
    build: buildSignalResponse,
    // LLM 日志
    llmLogSnapshotForDecision
  };
}

module.exports = { createSignalResponseService };
