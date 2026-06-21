const DEFAULT_PAYOUT_RATE = 0.85;
const TEN_MIN_PAYOUT_RATE = 0.8;
const THIRTY_MIN_PAYOUT_RATE = 0.85;

function payoutRateForDuration(duration, fallback = DEFAULT_PAYOUT_RATE) {
  const minutes = Number(duration);
  if (Number.isFinite(minutes)) {
    if (minutes >= 30) return THIRTY_MIN_PAYOUT_RATE;
    if (minutes >= 10) return TEN_MIN_PAYOUT_RATE;
  }
  return Number.isFinite(Number(fallback)) ? Number(fallback) : DEFAULT_PAYOUT_RATE;
}

function cleanDuration(value) {
  const text = String(value ?? "").trim();
  if (!text || text === "undefined" || text === "null" || text === "NaN") return "";
  return text;
}

function priceAtOrAfter(ticks, targetTime) {
  let lo = 0;
  let hi = ticks.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (Number(ticks[mid].time) < targetTime) lo = mid + 1;
    else hi = mid;
  }
  return lo < ticks.length ? Number(ticks[lo].price) : null;
}

function fallbackSettlePrice(ticks, targetTime, now, currentPrice) {
  const tickPrice = priceAtOrAfter(ticks, targetTime);
  if (tickPrice !== null) return tickPrice;
  if (Number(now || 0) >= Number(targetTime || 0) && Number.isFinite(Number(currentPrice))) {
    return Number(currentPrice);
  }
  return null;
}

function settleStatus(direction, openPrice, closePrice) {
  if (openPrice == null || closePrice == null) return "pending";
  if (Number(closePrice) === Number(openPrice)) return "tie";
  if (direction === "UP") return Number(closePrice) > Number(openPrice) ? "won" : "lost";
  if (direction === "DOWN") return Number(closePrice) < Number(openPrice) ? "won" : "lost";
  return "pending";
}

function statusPnl(status, amount, payoutRate = DEFAULT_PAYOUT_RATE) {
  const stake = Number(amount) || 0;
  if (status === "won") return Number((stake * payoutRate).toFixed(2));
  if (status === "lost") return -stake;
  return 0;
}

function normalizePriceTicks(priceTicks = []) {
  return priceTicks
    .filter(t => Number.isFinite(Number(t.time)) && Number.isFinite(Number(t.price)))
    .map(t => ({ time: Number(t.time), price: Number(t.price) }))
    .sort((a, b) => a.time - b.time);
}

function auditOrderRow(row, ticks, payoutRate, options = {}) {
  const duration = Math.max(1, Number(row.duration) || 0);
  const effectivePayoutRate = payoutRateForDuration(duration, payoutRate);
  const openTime = Number(row.serverTime || row.clientTime || 0);
  if (!duration || !openTime) return null;
  const openPrice = row.price != null ? Number(row.price) : priceAtOrAfter(ticks, openTime);
  const settleTime = openTime + duration * 60 * 1000;
  const closePrice = fallbackSettlePrice(ticks, settleTime, options.now, options.currentPrice);
  const status = settleStatus(row.direction, openPrice, closePrice);
  const amount = Number(row.amount) || 0;
  const id = [
    "autojs",
    row.strategyId || "manual",
    row.signalTime || "",
    row.queueBatchId || "",
    openTime
  ].join("|");
  return {
    id,
    source: "autojs",
    event: row.event,
    strategyId: row.strategyId || "manual",
    direction: row.direction,
    amount,
    duration: String(duration),
    openTime,
    settleTime,
    openPrice,
    closePrice,
    status,
    payoutRate: effectivePayoutRate,
    pnl: statusPnl(status, amount, effectivePayoutRate),
    confidence: row.confidence,
    rsi_value: row.rsi_value,
    avg_prob: row.avg_prob,
    threshold: row.threshold,
    signalTime: row.signalTime,
    actionableTime: row.actionableTime,
    queueBatchId: row.queueBatchId,
    queuePosition: row.queuePosition,
    queueLength: row.queueLength,
    queueOrderPolicy: row.queueOrderPolicy,
    device: row.device,
    balance: row.balance,
    realBalance: row.realBalance
  };
}

function auditAbortRow(row) {
  const openTime = Number(row.serverTime || row.clientTime || 0);
  if (!openTime) return null;
  const isUnverified = row.event === "order_unverified";
  const id = [isUnverified ? "autojs_unverified" : "autojs_abort", row.strategyId || "manual", row.signalTime || "", openTime].join("|");
  return {
    id,
    source: "autojs",
    event: row.event,
    strategyId: row.strategyId || "manual",
    direction: row.direction,
    amount: Number(row.amount) || 0,
    duration: cleanDuration(row.duration),
    openTime,
    settleTime: openTime,
    openPrice: row.price != null ? Number(row.price) : null,
    closePrice: null,
    status: isUnverified ? "unverified" : "aborted",
    pnl: 0,
    reason: row.reason,
    verifiedBy: row.verifiedBy,
    beforeBalance: row.beforeBalance,
    afterBalance: row.afterBalance,
    balanceDelta: row.balanceDelta,
    confidence: row.confidence,
    rsi_value: row.rsi_value,
    signalTime: row.signalTime,
    queueBatchId: row.queueBatchId,
    queuePosition: row.queuePosition,
    queueLength: row.queueLength,
    device: row.device,
    balance: row.balance,
    realBalance: row.realBalance
  };
}

function serverTradeRow(t, payoutRate) {
  const effectivePayoutRate = payoutRateForDuration(t.duration, payoutRate);
  const source = t.source || "server";
  return {
    id: "server|" + t.id,
    source,
    event: "server_trade",
    strategyId: String(source).replace(/^auto:/, "").replace(/^shadow:/, "") || "manual",
    direction: t.direction,
    amount: Number(t.amount) || 0,
    duration: cleanDuration(t.duration),
    openTime: Number(t.openTime),
    settleTime: Number(t.settleTime),
    openPrice: t.strikePrice,
    closePrice: t.settlePrice,
    status: t.status === "active" ? "pending" : t.status,
    payoutRate: effectivePayoutRate,
    pnl: statusPnl(t.status, t.amount, effectivePayoutRate),
    confidence: null,
    rsi_value: null
  };
}

function rowGroup(row) {
  const source = String(row.source || "");
  if (source.startsWith("shadow:") || source === "shadow" || row.event === "shadow_trade") return "shadow";
  return "real";
}

function summarizeRows(rows) {
  const settled = rows.filter(r => ["won", "lost", "tie"].includes(r.status));
  const wins = settled.filter(r => r.status === "won").length;
  const losses = settled.filter(r => r.status === "lost").length;
  const ties = settled.filter(r => r.status === "tie").length;
  const decided = wins + losses;
  const pnl = settled.reduce((sum, r) => sum + (Number(r.pnl) || 0), 0);
  const pending = rows.filter(r => r.status === "pending").length;
  return {
    total: rows.length,
    settled: settled.length,
    wins,
    losses,
    ties,
    pending,
    winRate: decided ? Number((wins / decided * 100).toFixed(2)) : null,
    pnl: Number(pnl.toFixed(2))
  };
}

function dayKeyForTime(time) {
  const date = new Date(Number(time) || 0);
  if (Number.isNaN(date.getTime())) return "unknown";
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).formatToParts(date);
  const map = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return `${map.year}-${map.month}-${map.day}`;
}

function summarizeBy(rows, keyFn, labelFn = key => key) {
  const groups = new Map();
  for (const row of rows) {
    const key = keyFn(row);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  return Array.from(groups.entries())
    .map(([key, groupRows]) => ({
      key,
      label: labelFn(key, groupRows),
      ...summarizeRows(groupRows)
    }))
    .sort((a, b) => String(b.key).localeCompare(String(a.key)));
}

function buildBreakdown(rows) {
  return {
    total: summarizeRows(rows),
    byStrategy: summarizeBy(
      rows,
      row => row.strategyId || "manual",
      key => key
    ),
    byDay: summarizeBy(
      rows,
      row => dayKeyForTime(row.openTime),
      key => key
    )
  };
}

function shadowAuditKey(row) {
  const source = row.source || "";
  const strategyId = row.strategyId || String(source).replace(/^shadow:/, "") || "";
  const openTime = row.openTime || row.signalTime || row.actionableTime || row.serverTime || "";
  return [source, strategyId, openTime, row.tradeId || ""].join("|");
}

function shadowAuditRows(auditRows, payoutRate, options = {}) {
  const byId = new Map();
  for (const row of auditRows || []) {
    if (row.event !== "shadow_trade_open" && row.event !== "shadow_trade_settle") continue;
    const id = shadowAuditKey(row);
    if (!id) continue;
    const current = byId.get(id) || {};
    byId.set(id, { ...current, ...row, open: row.event === "shadow_trade_open" ? row : current.open, settle: row.event === "shadow_trade_settle" ? row : current.settle });
  }
  const out = [];
  for (const [id, row] of byId.entries()) {
    const open = row.open || row;
    const settle = row.settle || {};
    const duration = cleanDuration(open.duration || row.duration);
    const effectivePayoutRate = payoutRateForDuration(duration, payoutRate);
    const settleTime = Number(settle.settleTime || (Number(open.openTime || open.serverTime || 0) + (Number(duration) || 0) * 60000));
    const closePrice = settle.settlePrice ?? fallbackSettlePrice(options.ticks || [], settleTime, options.now, options.currentPrice);
    const status = settle.status || (closePrice != null ? settleStatus(open.direction, open.strikePrice, closePrice) : "pending");
    out.push({
      id: "shadow_audit|" + id,
      source: open.source || row.source || "shadow",
      event: "shadow_trade",
      strategyId: open.strategyId || String(open.source || "").replace(/^shadow:/, "") || "shadow",
      direction: open.direction,
      amount: Number(open.amount) || 0,
      duration,
      openTime: Number(open.openTime || open.serverTime || 0),
      settleTime,
      openPrice: open.strikePrice,
      closePrice,
      status,
      payoutRate: effectivePayoutRate,
      pnl: statusPnl(status, open.amount, effectivePayoutRate),
      confidence: open.confidence,
      rsi_value: open.rsi_value,
      avg_prob: open.avg_prob,
      signalTime: open.signalTime,
      actionableTime: open.actionableTime
    });
  }
  return out;
}

function buildLiveOrderHistory(options = {}) {
  const limit = Math.min(300, Math.max(1, Number(options.limit) || 100));
  const payoutRate = Number.isFinite(Number(options.payoutRate)) ? Number(options.payoutRate) : DEFAULT_PAYOUT_RATE;
  const ticks = normalizePriceTicks(options.priceTicks || []);
  const now = options.now || Date.now();
  const rows = [];

  for (const row of options.auditRows || []) {
    if (row.event === "order_done") {
      const item = auditOrderRow(row, ticks, payoutRate, { now, currentPrice: options.currentPrice });
      if (item) rows.push(item);
    }
    if (row.event === "order_abort" || row.event === "order_unverified") {
      const item = auditAbortRow(row);
      if (item) rows.push(item);
    }
  }

  rows.push(...shadowAuditRows(options.auditRows || [], payoutRate, { ticks, now, currentPrice: options.currentPrice }));

  for (const trade of options.serverTrades || []) {
    rows.push(serverTradeRow(trade, payoutRate));
  }

  rows.sort((a, b) => Number(b.openTime || 0) - Number(a.openTime || 0));

  const seen = new Set();
  const unique = rows.filter(row => {
    const key = row.id || JSON.stringify([row.source, row.strategyId, row.signalTime, row.openTime]);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });

  const realRows = unique.filter(r => rowGroup(r) === "real");
  const shadowRows = unique.filter(r => rowGroup(r) === "shadow");
  const combined = summarizeRows(unique);
  combined.combined = { ...combined };
  combined.real = summarizeRows(realRows);
  combined.shadow = summarizeRows(shadowRows);
  combined.byStrategy = summarizeBy(unique, row => row.strategyId || "manual", key => key);
  combined.byDay = summarizeBy(unique, row => dayKeyForTime(row.openTime), key => key);
  return {
    updatedAt: now,
    summary: combined,
    breakdown: {
      combined: buildBreakdown(unique),
      real: buildBreakdown(realRows),
      shadow: buildBreakdown(shadowRows)
    },
    active: unique.filter(r => r.status === "pending").slice(0, limit),
    recent: unique.slice(0, limit)
  };
}

module.exports = {
  DEFAULT_PAYOUT_RATE,
  TEN_MIN_PAYOUT_RATE,
  THIRTY_MIN_PAYOUT_RATE,
  payoutRateForDuration,
  priceAtOrAfter,
  fallbackSettlePrice,
  settleStatus,
  statusPnl,
  normalizePriceTicks,
  buildLiveOrderHistory
};
