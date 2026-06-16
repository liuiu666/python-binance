import { useEffect, useRef } from "react";

export const PAYOUT = 0.85;

export function payoutForDuration(duration) {
  const minutes = Number(duration);
  if (Number.isFinite(minutes)) {
    if (minutes >= 30) return 0.85;
    if (minutes >= 10) return 0.8;
  }
  return PAYOUT;
}

export const DEFAULT_CONFIG = {
  amount: "5",
  strategyVariants: [
    { id: "BTC_10min_SAFE", base: "SAFE", label: "推荐稳健 20/80", amount: "5", tailPct: 0.2, enabled: false, tradeEnabled: false },
    { id: "BTC_10min_TAKER", base: "TAKER", label: "资金流过滤 20/80", amount: "5", tailPct: 0.2, enabled: false, tradeEnabled: false },
    { id: "BTC_10min_SECOND_4200_22", base: "SECOND", label: "秒级正态 70m 22/78", amount: "10", tailPct: 0.22, enabled: true, tradeEnabled: true, lookbackSec: 4200, horizonSec: 600, gapSec: 1200, secondFilter: "none", duration: "10" },
    { id: "BTC_10min_SECOND_CHIP_1800_OPT", base: "SECOND_CHIP", label: "秒级筹码区 30m 优化", amount: "5", enabled: true, tradeEnabled: false, lookbackSec: 1800, horizonSec: 600, gapSec: 300, chipTargetShare: 0.2, chipBinMode: "fixed", chipBinSize: 20, chipBinPct: 0.0003, chipBreakPct: 0.004, chipDirectionFilter: "all", chipFilter: "width_lte_3", duration: "10" },
    { id: "BTC_10min_SECOND_CHIP_3600_WIDE_FLOW", base: "SECOND_CHIP", label: "秒级筹码区 WIDE_FLOW 60m", amount: "15", enabled: true, tradeEnabled: true, lookbackSec: 3600, horizonSec: 600, gapSec: 600, chipTargetShare: 0.2, chipBinMode: "fixed", chipBinSize: 100, chipBinPct: 0.0003, chipBreakPct: 0.005, chipDirectionFilter: "all", chipFilter: "flow_reversal", duration: "10" }
  ],
  duration: "10",
  autoTrade_10m: false,
  realTradingEnabled: false,
  shadowTradingEnabled: false
};

export function clamp(num, min, max) {
  return Math.max(min, Math.min(max, Number(num) || 0));
}

export function fmt(num, digits = 2) {
  if (num === null || num === undefined || Number.isNaN(Number(num))) return "--";
  return Number(num).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  });
}

export function fmtPrice(num) {
  return num === null || num === undefined || Number.isNaN(Number(num)) ? "--" : Number(num).toFixed(2);
}

export function fmtPct(num, digits = 1) {
  return num === null || num === undefined || Number.isNaN(Number(num)) ? "--" : Number(num).toFixed(digits) + "%";
}

export function directionText(direction) {
  if (direction === "UP") return "看涨";
  if (direction === "DOWN") return "看跌";
  return "--";
}

export function directionClass(direction) {
  if (direction === "UP") return "up";
  if (direction === "DOWN") return "down";
  return "neutral";
}

export function strategyName(strategyId) {
  const id = String(strategyId || "");
  if (!strategyId || strategyId === "manual") return "手动";
  const exact = {
    BTC_10min_SAFE: "推荐稳健 20/80",
    BTC_10min_TAKER: "资金流过滤 20/80",
    BTC_10min_TAKER_27: "资金流过滤 27/73",
    BTC_10min_SECOND_3600_20: "秒级正态 60m 20/80",
    BTC_10min_SECOND_4200_22: "秒级正态 70m 22/78",
    BTC_10min_SECOND_CHIP_1800_OPT: "秒级筹码区 30m 窄区",
    BTC_10min_SECOND_CHIP_3600_FLOW: "秒级筹码区 60m 资金流",
    BTC_10min_SECOND_CHIP_3600_WIDE_FLOW: "秒级筹码区 60m WIDE_FLOW"
  };
  if (exact[id]) return exact[id];
  const normal = id.match(/^BTC_10min_SECOND_(\d+)_(\d+)/);
  if (normal) {
    const minutes = Math.round(Number(normal[1]) / 60);
    const lower = Number(normal[2]);
    return `秒级正态 ${minutes}m ${lower}/${100 - lower}`;
  }
  const chip = id.match(/^BTC_10min_SECOND_CHIP_(\d+)/);
  if (chip) return `秒级筹码区 ${Math.round(Number(chip[1]) / 60)}m`;
  const taker = id.match(/^BTC_10min_TAKER_(\d+)/);
  if (taker) return `资金流过滤 ${taker[1]}/${100 - Number(taker[1])}`;
  const safe = id.match(/^BTC_10min_SAFE_(\d+)/);
  if (safe) return `推荐稳健 ${safe[1]}/${100 - Number(safe[1])}`;
  return strategyId;
}

export function statusClass(status) {
  if (status === "won") return "won";
  if (status === "lost") return "lost";
  if (status === "tie") return "tie";
  if (status === "unverified") return "pending";
  return "pending";
}

export function statusText(status) {
  if (status === "unverified") return "未成交";
  if (status === "won") return "赢";
  if (status === "lost") return "输";
  if (status === "tie") return "平";
  if (status === "aborted") return "取消";
  return "持仓";
}

export function pnlText(row) {
  const pnl = Number(row.pnl || 0);
  if (row.status === "unverified") return "未扣款";
  if (row.status === "pending") return "待结算";
  if (row.status === "aborted") return "已取消";
  return (pnl > 0 ? "+" : "") + fmt(pnl, 2) + "U";
}

export function timeParts(time) {
  if (!time) return { date: "--", time: "--" };
  const d = new Date(Number(time));
  if (isNaN(d.getTime())) return { date: "--", time: "--" };
  const dateStr = d.toLocaleDateString("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit" });
  const timeStr = d.toLocaleTimeString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return { date: dateStr, time: timeStr };
}

export function ageText(ms) {
  if (ms === null || ms === undefined) return "--";
  const seconds = Math.max(0, Math.floor(Number(ms) / 1000));
  if (seconds < 60) return seconds + "s";
  const minutes = Math.floor(seconds / 60);
  return minutes + "m " + (seconds % 60) + "s";
}

export function signalLabel(signal) {
  if (!signal) return "等待数据";
  if (signal.signal) return directionText(signal.signal) + " " + fmtPct(signal.confidence, 0);
  if (signal.data_health_blocked) {
    const reasons = (signal.data_health_block_reasons || []).join(" ") || "延迟";
    return "策略拦截: " + reasons;
  }
  if (signal.safety_blocked) return "避险拦截: 极端趋势";
  if (signal.model_type === "second_chip") {
    if (signal.reason === "already_outside_chip_zone") {
      if (signal.chip_state === "below") return "已下破，等待重新进区";
      if (signal.chip_state === "above") return "已上破，等待重新进区";
      return "已在区外，等待重新进区";
    }
    if (signal.reason === "direction_filter") return "方向过滤，未执行";
    if (signal.chip_state === "inside") return "区间内，等待突破";
    return "等待筹码区突破";
  }
  return "等待极端区间";
}

export function signalTimeText(time) {
  if (!time) return "--:--:--";
  return new Date(time).toLocaleTimeString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function displaySignalTime(signal) {
  if (!signal) return "--:--:--";
  return signal.time_shanghai || signalTimeText(signal.time);
}

export function amountForSignal(strategyId, signal, payload, config) {
  const strategyAmounts = (payload && payload._strategyAmounts) || {};
  const cfg = (payload && payload._config) || config || {};
  const variants = (payload && payload._strategyVariants) || cfg.strategyVariants || [];
  const variant = Array.isArray(variants) ? variants.find(item => item.id === strategyId) : null;
  const baseAmount = strategyAmounts[strategyId] || (cfg.strategyAmounts && cfg.strategyAmounts[strategyId]) || cfg.amount || DEFAULT_CONFIG.amount;
  if (signal && signal.amount && signal.fixed_amount === true) return String(signal.amount);
  return String(variant?.amount || baseAmount);
}

export function activeSignalFromPayload(payload) {
  if (!payload) return null;
  const variants = payload._strategyVariants || DEFAULT_CONFIG.strategyVariants;
  const active = variants.map(v => payload[v.id]).find(sig => sig && sig.signal);
  return active || variants.map(v => payload[v.id]).find(Boolean) || null;
}

export function useInterval(callback, delay) {
  const callbackRef = useRef(callback);
  useEffect(() => {
    callbackRef.current = callback;
  }, [callback]);
  useEffect(() => {
    if (!delay) return undefined;
    const id = setInterval(() => callbackRef.current(), delay);
    return () => clearInterval(id);
  }, [delay]);
}
