export const PAYOUT = 0.85;

export const DEFAULT_CONFIG = {
  amount: "5",
  duration: "30",
  autoTrade_10m: false,
  autoTrade_30m: false,
  realTradingEnabled: false,
  shadowTradingEnabled: true,
  minConfidence: 35,
  tiersEnabled: false,
  tiers: [
    { min: 80, amount: 20 },
    { min: 60, amount: 10 },
    { min: 40, amount: 5 }
  ],
  skipConflictSignals: false,
  queueOrderPolicy: "confidence_desc",
  preventOverlapOrders: true,
  maxActionableLagMs: 60000
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
  if (strategyId === "BTC_10min") return "10分钟";
  if (strategyId === "BTC_30min") return "30分钟";
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
  if (status === "lost") return "亏";
  if (status === "tie") return "平";
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
    var reasons = (signal.data_health_block_reasons || []).join(" ") || "延迟";
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

export function toTierList(tiers) {
  return (Array.isArray(tiers) ? tiers : [])
    .map(function(item) {
      return {
        min: clamp(item.min, 0, 100),
        amount: clamp(item.amount, 1, 1000)
      };
    });
}

export function toTierLabel(tiers, baseAmount) {
  if (toTierList(tiers).length === 0) return "固定 " + baseAmount + "U";
  return toTierList(tiers).map(function(t) { return ">=" + t.min + "% " + t.amount + "U"; }).join(" / ") + " / 其他 " + baseAmount + "U";
}

export function amountForConfidence(confidence, config) {
  const base = String((config && config.amount) || DEFAULT_CONFIG.amount);
  if (!(config && config.tiersEnabled) || confidence === null || confidence === undefined) return base;
  var list = toTierList(config.tiers);
  for (var i = 0; i < list.length; i++) {
    if (Number(confidence) >= Number(list[i].min)) return String(list[i].amount);
  }
  return base;
}

export function amountForSignal(strategyId, signal, payload, config) {
  if (signal && signal.confidence !== null && signal.confidence !== undefined) {
    return amountForConfidence(signal.confidence, (payload && payload._config) || config);
  }
  var strategyAmounts = (payload && payload._strategyAmounts) || {};
  return String(strategyAmounts[strategyId] || (config && config.amount) || DEFAULT_CONFIG.amount);
}

export function activeSignalFromPayload(payload) {
  if (!payload) return null;
  var signal30 = payload.BTC_30min || null;
  var signal10 = payload.BTC_10min || null;
  return (signal30 && signal30.signal ? signal30 : null) || (signal10 && signal10.signal ? signal10 : null) || signal30 || signal10;
}

import { useEffect, useRef } from "react";

export function useInterval(callback, delay) {
  var callbackRef = useRef(callback);
  useEffect(function() {
    callbackRef.current = callback;
  }, [callback]);
  useEffect(function() {
    if (!delay) return undefined;
    var id = setInterval(function() { callbackRef.current(); }, delay);
    return function() { clearInterval(id); };
  }, [delay]);
}
