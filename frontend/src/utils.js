import { useEffect, useRef } from "react";

export const PAYOUT = 0.85;

export const DEFAULT_CONFIG = {
  amount: "5",
  strategyAmounts: {
    BTC_10min_SAFE: "5",
    BTC_10min_TAKER: "5"
  },
  duration: "10",
  autoTrade_10m: false,
  realTradingEnabled: false,
  shadowTradingEnabled: true,
  minConfidence: 35
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
  if (strategyId === "BTC_10min_SAFE") return "推荐稳健";
  if (strategyId === "BTC_10min_TAKER") return "资金流过滤";
  if (!strategyId || strategyId === "manual") return "手动";
  return strategyId;
}

export function statusClass(status) {
  if (status === "won") return "won";
  if (status === "lost") return "lost";
  if (status === "tie") return "tie";
  return "pending";
}

export function statusText(status) {
  if (status === "won") return "赢";
  if (status === "lost") return "输";
  if (status === "tie") return "平";
  if (status === "aborted") return "取消";
  return "持仓";
}

export function pnlText(row) {
  const pnl = Number(row.pnl || 0);
  if (row.status === "pending") return "待结算";
  if (row.status === "aborted") return "已取消";
  return (pnl > 0 ? "+" : "") + fmt(pnl, 2) + "U";
}

export function timeParts(time) {
  if (!time) return { date: "--", time: "--" };
  const d = new Date(Number(time));
  if (isNaN(d.getTime())) return { date: "--", time: "--" };
  const dateStr = d.toLocaleDateString("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit"
  });
  const timeStr = d.toLocaleTimeString("zh-CN", {
    hour12: false,
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  });
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
  return "监控中";
}

export function signalTimeText(time) {
  if (!time) return "--:--:--";
  return new Date(time).toLocaleTimeString("zh-CN", {
    hour12: false,
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  });
}

export function amountForSignal(strategyId, signal, payload, config) {
  const strategyAmounts = (payload && payload._strategyAmounts) || {};
  const cfg = (payload && payload._config) || config || {};
  const baseAmount = strategyAmounts[strategyId] || (cfg.strategyAmounts && cfg.strategyAmounts[strategyId]) || cfg.amount || DEFAULT_CONFIG.amount;
  if (signal && signal.amount && signal.fixed_amount === true) return String(signal.amount);
  return String(baseAmount);
}

export function activeSignalFromPayload(payload) {
  if (!payload) return null;
  const taker = payload.BTC_10min_TAKER || null;
  const safe = payload.BTC_10min_SAFE || null;
  return (taker && taker.signal ? taker : null) || (safe && safe.signal ? safe : null) || taker || safe;
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
