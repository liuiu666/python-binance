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
