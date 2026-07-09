import { useEffect, useRef } from "react";

export const PAYOUT = 0.85;

export const DEFAULT_CONFIG = {
  amount: "5",
  duration: "10",
  autoTrade_10m: false,
  realTradingEnabled: false,
  shadowTradingEnabled: false,
  strategyVariants: [
    {
      id: "BTC_10min_NORMAL_STATE_V15_STABLE_D5A5",
      base: "SECOND_NORMAL_STATE_V11",
      label: "正态V15原版对照",
      amount: "5",
      duration: "10",
      enabled: true,
      tradeEnabled: false,
      lookbackSec: 10800,
      horizonSec: 600,
      gapSec: 600,
      confirmDelaySec: 5,
      maxAdverseBps: 5,
      signalHoldSec: 55,
      bandwalkMax: 6,
      minConsensusVotes: 2,
      stateGate: "v15_bw35_or_early_sigma18",
      confirmationVeto: "none"
    },
    {
      id: "BTC_10min_NORMAL_STATE_V19_OB_CONFIRM_D5A5",
      base: "SECOND_NORMAL_STATE_V11",
      label: "正态V19保守实盘",
      amount: "5",
      duration: "10",
      enabled: true,
      tradeEnabled: true,
      lookbackSec: 10800,
      horizonSec: 600,
      gapSec: 600,
      confirmDelaySec: 5,
      maxAdverseBps: 5,
      signalHoldSec: 55,
      bandwalkMax: 6,
      minConsensusVotes: 2,
      stateGate: "v15_bw35_or_early_sigma18",
      confirmationVeto: "ob_confirm_weak"
    },
    {
      id: "BTC_10min_NORMAL_STATE_V19_OB_CONFIRM_HF_G60",
      base: "SECOND_NORMAL_STATE_V11",
      label: "正态V19高频影子",
      amount: "5",
      duration: "10",
      enabled: true,
      tradeEnabled: false,
      lookbackSec: 10800,
      horizonSec: 600,
      gapSec: 60,
      confirmDelaySec: 5,
      maxAdverseBps: 5,
      signalHoldSec: 55,
      bandwalkMax: 6,
      minConsensusVotes: 2,
      stateGate: "v15_bw35_or_early_sigma18",
      confirmationVeto: "ob_confirm_weak"
    }
  ]
};

const STRATEGY_NAMES = {
  BTC_10min_NORMAL_STATE_V15_STABLE_D5A5: "正态V15原版对照",
  BTC_10min_NORMAL_STATE_V19_OB_CONFIRM_D5A5: "正态V19保守实盘",
  BTC_10min_NORMAL_STATE_V19_OB_CONFIRM_HF_G60: "正态V19高频影子",
  BTC_10min_SECOND_VW_STABLE_2700_20_ETA2: "正态成交量确认 稳健",
  BTC_10min_SECOND_VW_FAST_2700_27_ETA3: "正态成交量确认 高频"
};

export function payoutForDuration(duration) {
  const minutes = Number(duration);
  if (Number.isFinite(minutes)) {
    if (minutes >= 30) return 0.85;
    if (minutes >= 10) return 0.8;
  }
  return PAYOUT;
}

export function clamp(num, min, max) {
  const value = Number(num);
  if (!Number.isFinite(value)) return min;
  return Math.max(min, Math.min(max, value));
}

export function fmt(num, digits = 2) {
  const value = Number(num);
  if (!Number.isFinite(value)) return "--";
  return value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  });
}

export function fmtPrice(num) {
  const value = Number(num);
  return Number.isFinite(value) ? value.toFixed(2) : "--";
}

export function fmtPct(num, digits = 1) {
  const value = Number(num);
  return Number.isFinite(value) ? `${value.toFixed(digits)}%` : "--";
}

export function directionText(direction) {
  if (direction === "UP") return "看涨";
  if (direction === "DOWN") return "看跌";
  return "等待";
}

export function directionClass(direction) {
  if (direction === "UP") return "up";
  if (direction === "DOWN") return "down";
  return "neutral";
}

export function strategyName(strategyId) {
  const id = String(strategyId || "");
  if (!id || id === "manual") return "手动";
  if (STRATEGY_NAMES[id]) return STRATEGY_NAMES[id];
  if (id.startsWith("BTC_10min_NORMAL_STATE")) return "正态状态策略";
  if (id.startsWith("BTC_10min_SECOND_VW")) return "正态成交量确认";
  if (id.startsWith("BTC_10min_SMART")) return "SMART策略";
  return id;
}

export function shortId(strategyId) {
  return String(strategyId || "")
    .replace(/^BTC_10min_/, "")
    .replace(/NORMAL_STATE_/g, "NS_");
}

export function statusClass(status) {
  if (status === "won") return "won";
  if (status === "lost") return "lost";
  if (status === "tie") return "tie";
  if (status === "aborted" || status === "unverified") return "warn";
  return "pending";
}

export function statusText(status) {
  if (status === "won") return "胜";
  if (status === "lost") return "负";
  if (status === "tie") return "平";
  if (status === "aborted") return "取消";
  if (status === "unverified") return "未成交";
  return "持仓";
}

export function pnlText(row) {
  if (!row) return "--";
  const pnl = Number(row.pnl || 0);
  if (row.status === "unverified") return "未扣款";
  if (row.status === "pending") return "待结算";
  if (row.status === "aborted") return "已取消";
  return `${pnl > 0 ? "+" : ""}${fmt(pnl, 2)}U`;
}

export function timeParts(time) {
  if (!time) return { date: "--", time: "--", full: "--" };
  const date = new Date(Number(time));
  if (Number.isNaN(date.getTime())) return { date: "--", time: "--", full: "--" };
  const options = { timeZone: "Asia/Shanghai", hour12: false };
  const day = date.toLocaleDateString("zh-CN", { ...options, month: "2-digit", day: "2-digit" });
  const clock = date.toLocaleTimeString("zh-CN", { ...options, hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return { date: day, time: clock, full: `${day} ${clock}` };
}

export function dateTimeText(time) {
  if (!time) return "--";
  const ms = typeof time === "string" && Number.isNaN(Number(time)) ? Date.parse(time) : Number(time);
  return timeParts(ms).full;
}

export function ageText(ms) {
  const value = Number(ms);
  if (!Number.isFinite(value)) return "--";
  const seconds = Math.max(0, Math.floor(value / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

export function healthTone(ok) {
  if (ok === true) return "ok";
  if (ok === false) return "bad";
  return "warn";
}

export function healthText(ok, ageMs) {
  const prefix = ok === true ? "正常" : ok === false ? "异常" : "未知";
  return ageMs === null || ageMs === undefined ? prefix : `${prefix} ${ageText(ageMs)}`;
}

export function signalLabel(signal) {
  if (!signal) return "等待数据";
  if (signal.signal) return `${directionText(signal.signal)} ${fmtPct(signal.confidence, 0)}`;
  if (signal.reason === "confirmed_signal_expired") return "信号过期，等待下一次";
  if (signal.reason === "no_confirmed_false_break") return "等待假突破回归确认";
  if (signal.reason === "no_router_branch" && Array.isArray(signal.router_rejects) && signal.router_rejects.includes("low_up_veto")) return "low+UP否决";
  if (signal.reason === "no_router_branch") return "等待极端尾部";
  if (signal.reason === "observed600_low") return "等待秒级覆盖";
  if (signal.reason === "r10_cap") return "波动过大暂停";
  if (signal.reason === "router_metrics_unavailable") return "等待路由指标";
  if (signal.data_health_blocked) return "数据延迟，策略暂停";
  if (signal.safety_blocked) return "风控拦截";
  return signal.signal_detail || signal.next_signal_estimate || "等待触发";
}

function asNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function statusTone(ok) {
  if (ok === true) return "ok";
  if (ok === false) return "bad";
  return "warn";
}

export function signalReasonText(signal) {
  if (!signal) return "等待策略数据";
  const reason = signal?.reason;
  const map = {
    liq_normal_not_ready: "等待可交易的震荡区",
    liq_wait_reclaim: "震荡区已形成，等待假突破回归",
    liq_strategy_gap: "上一笔信号后冷却中",
    liq_orderbook_missing: "订单薄数据缺失",
    liq_orderbook_missing_or_stale: "订单薄数据延迟",
    liq_feature_error: "策略指标计算异常",
    liq_v2_skip_down_bid_fade: "做空质量不够，跳过",
    liq_v2_skip_up_negative_flow: "做多质量不够，跳过",
    confirmed_signal_expired: "信号过期，等待下一次",
    no_confirmed_false_break: "等待假突破回归确认",
    observed600_low: "秒级数据覆盖不足",
    r10_cap: "10分钟波动过大，暂停",
    router_metrics_unavailable: "等待行情指标"
  };
  return map[reason] || signalLabel(signal);
}

export function signalReadinessItems(signal, variant = {}) {
  if (!signal) return [];
  const observed = asNumber(signal.observed_pct ?? signal.observed600_pct);
  const observedMin = asNumber(variant.observedMinPct ?? signal.min_observed_pct) ?? 88;
  const inside = asNumber(signal.inside1_ratio);
  const insidePct = inside == null ? null : inside * 100;
  const insideMin = (asNumber(variant.insideMin) ?? 0.55) * 100;
  const slope = asNumber(signal.center_slope_bps);
  const slopeMax = asNumber(variant.centerSlopeMaxBps) ?? 8;
  const sigma = asNumber(signal.sigma_bps ?? signal.sigma10_bps);
  const sigmaMin = asNumber(variant.sigmaMinBps) ?? 5.8;
  const sigmaMax = asNumber(variant.sigmaMaxBps) ?? 55;
  const expand = asNumber(signal.sigma_expand);
  const expandMax = asNumber(variant.sigmaExpandMax) ?? 1.9;
  const obAge = asNumber(signal.ob_age_sec);
  const obMax = asNumber(variant.orderbookMaxAgeSec) ?? 3;

  const items = [
    {
      key: "coverage",
      label: "数据完整",
      ok: observed == null ? null : observed >= observedMin,
      value: observed == null ? "--" : `${fmt(observed, 1)}%`,
      target: `要求 >= ${fmt(observedMin, 0)}%`,
      help: "秒级数据要够完整，否则正态区间不可信。"
    },
    {
      key: "inside",
      label: "震荡成型",
      ok: insidePct == null ? null : insidePct >= insideMin,
      value: insidePct == null ? "--" : `${fmt(insidePct, 1)}%`,
      target: `要求 >= ${fmt(insideMin, 0)}%`,
      help: "过去10分钟多数时间要在区间内，才算震荡。"
    },
    {
      key: "trend",
      label: "趋势不过强",
      ok: slope == null ? null : Math.abs(slope) <= slopeMax,
      value: slope == null ? "--" : `${fmt(slope, 2)}bp`,
      target: `要求 -${fmt(slopeMax, 0)} 到 ${fmt(slopeMax, 0)}bp`,
      help: "中线快速上移或下移时，先不做回归。"
    },
    {
      key: "sigma",
      label: "波动合适",
      ok: sigma == null ? null : sigma >= sigmaMin && sigma <= sigmaMax,
      value: sigma == null ? "--" : `${fmt(sigma, 2)}bp`,
      target: `要求 ${fmt(sigmaMin, 1)}-${fmt(sigmaMax, 0)}bp`,
      help: sigma != null && sigma < sigmaMin
        ? "波动太小，空间不够，容易被噪声打掉。"
        : "波动太大时可能是突破行情，暂时不做回归。"
    },
    {
      key: "expand",
      label: "没有暴走",
      ok: expand == null ? null : expand <= expandMax,
      value: expand == null ? "--" : `${fmt(expand, 2)}x`,
      target: `要求 <= ${fmt(expandMax, 1)}x`,
      help: "波动突然放大，可能正在切换行情。"
    }
  ];

  if (obAge != null || signal.model_type === "second_normal_liquidity_orderbook_v1") {
    items.push({
      key: "orderbook",
      label: "订单薄新鲜",
      ok: obAge == null ? null : obAge <= obMax,
      value: obAge == null ? "--" : `${fmt(obAge, 1)}秒`,
      target: `要求 <= ${fmt(obMax, 0)}秒`,
      help: "订单薄太旧，支撑/压力判断不可靠。"
    });
  }

  return items.map(item => ({ ...item, tone: statusTone(item.ok) }));
}

export function signalTriggerPlan(signal) {
  if (!signal) return null;
  const price = asNumber(signal.price ?? signal.entry);
  const z = asNumber(signal.z_score);
  const center = asNumber(signal.normal_center);
  const sigmaBps = asNumber(signal.sigma_bps);
  const zEntry = asNumber(signal.z_entry) ?? 1.2;
  const normalLow = asNumber(signal.normal_low);
  const normalHigh = asNumber(signal.normal_high);
  const sigmaPrice = center != null && sigmaBps != null ? center * sigmaBps / 10000 : null;
  const lowerTrigger = center != null && sigmaPrice != null ? center - zEntry * sigmaPrice : normalLow;
  const upperTrigger = center != null && sigmaPrice != null ? center + zEntry * sigmaPrice : normalHigh;
  const downGapBps = price != null && lowerTrigger != null ? (price / lowerTrigger - 1) * 10000 : null;
  const upGapBps = price != null && upperTrigger != null ? (upperTrigger / price - 1) * 10000 : null;
  const bid = asNumber(signal.bid_qty_20);
  const ask = asNumber(signal.ask_qty_20);
  const imbalance = asNumber(signal.imbalance_20);
  const micro = asNumber(signal.micro_bps);
  const flow60 = asNumber(signal.flow_60);
  const obBias = imbalance == null
    ? "订单薄未确认"
    : imbalance <= -0.08 && micro != null && micro <= -0.001
      ? "当前偏空压"
      : imbalance >= 0.08 && micro != null && micro >= 0.001
        ? "当前偏多撑"
        : "订单薄中性";
  const nextSide = signal.signal
    ? directionText(signal.signal)
    : upGapBps != null && downGapBps != null
      ? (upGapBps < downGapBps ? "更接近做空触发" : "更接近做多触发")
      : "等待触发";
  return {
    price,
    z,
    center,
    lowerTrigger,
    upperTrigger,
    downGapBps,
    upGapBps,
    obBias,
    bid,
    ask,
    imbalance,
    micro,
    flow60,
    nextSide,
    zEntry
  };
}

export function signalHumanSummary(signal, variant = {}) {
  if (!signal) return "正在等待策略数据。";
  if (signal.signal) {
    return `已经出现${directionText(signal.signal)}信号，按${signal.duration || variant.duration || 10}分钟到期判断。`;
  }
  if (signal.data_health_blocked) return "数据有延迟或缺口，策略先不下单。";
  if (signal.safety_blocked) return "风控正在拦截，暂时不下单。";
  if (signal.reason === "liq_wait_reclaim") {
    return "震荡区已经基本形成，现在等价格先冲出区间、再回到区间内，才给10分钟方向。";
  }
  if (signal.reason === "liq_strategy_gap") {
    return "上一笔信号后还在10分钟间隔内，避免同一段行情重复下单。";
  }
  if (signal.reason === "liq_orderbook_missing" || signal.reason === "liq_orderbook_missing_or_stale") {
    return "订单薄数据不够新，暂时不根据流动性下单。";
  }
  if (String(signal.reason || "").startsWith("liq_v2_skip")) {
    return signal.quality_v2_rule || "这个候选信号质量不够，已经跳过。";
  }
  const blocked = signalReadinessItems(signal, variant).find(item => item.ok === false);
  if (blocked) {
    return `当前卡在：${blocked.label}。现在 ${blocked.value}，${blocked.target}。${blocked.help}`;
  }
  return signalReasonText(signal);
}

export function signalTimeText(time) {
  if (!time) return "--:--:--";
  const ms = typeof time === "string" && Number.isNaN(Number(time)) ? Date.parse(time) : Number(time);
  if (!Number.isFinite(ms)) return "--:--:--";
  return new Date(ms).toLocaleTimeString("zh-CN", {
    hour12: false,
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  });
}

export function displaySignalTime(signal) {
  if (!signal) return "--:--:--";
  return signal.time_shanghai || signalTimeText(signal.actionable_time || signal.time);
}

export function amountForSignal(strategyId, signal, payload, config) {
  const cfg = payload?._config || config || DEFAULT_CONFIG;
  const variants = payload?._strategyVariants || cfg.strategyVariants || DEFAULT_CONFIG.strategyVariants;
  const variant = Array.isArray(variants) ? variants.find(item => item.id === strategyId) : null;
  if (signal?.fixed_amount && signal.amount) return String(signal.amount);
  return String(variant?.amount || cfg.strategyAmounts?.[strategyId] || cfg.amount || DEFAULT_CONFIG.amount);
}

export function activeSignalFromPayload(payload) {
  if (!payload) return null;
  const variants = payload._strategyVariants || DEFAULT_CONFIG.strategyVariants;
  const signals = variants.map(item => payload[item.id]).filter(Boolean);
  return signals.find(item => item.signal) || signals[0] || null;
}

export function isShadowTrade(row) {
  const source = String(row?.source || "");
  return source.startsWith("shadow:") || source === "shadow" || row?.event === "shadow_trade";
}

export function tradeKind(row) {
  return isShadowTrade(row) ? "shadow" : "real";
}

export function statLine(stat) {
  if (!stat) return "--";
  const pnl = Number(stat.pnl || 0);
  const pnlPart = `${pnl > 0 ? "+" : ""}${fmt(pnl, 2)}U`;
  return `${fmtPct(stat.winRate, 1)} / ${stat.wins || 0}-${stat.losses || 0} / ${pnlPart}`;
}

export function useInterval(callback, delay) {
  const callbackRef = useRef(callback);
  useEffect(() => {
    callbackRef.current = callback;
  }, [callback]);
  useEffect(() => {
    if (!delay) return undefined;
    const id = window.setInterval(() => callbackRef.current(), delay);
    return () => window.clearInterval(id);
  }, [delay]);
}
