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

function cleanAmount(...values) {
  for (const value of values) {
    const text = String(value ?? "").trim();
    if (!text || text === "undefined" || text === "null" || text === "NaN") continue;
    const amount = Number(text.replace(/[^\d.-]/g, ""));
    if (Number.isFinite(amount) && amount > 0) return amount;
  }
  return null;
}

function amountText(amount) {
  return amount == null ? "未记录" : `${amount}U`;
}

function rowAmount(row) {
  if (!row || typeof row !== "object") return null;
  return cleanAmount(
    row.amount,
    row.orderAmount,
    row.tradeAmount,
    row.stake,
    row.signal?.amount,
    row.signal?.orderAmount,
    row.signal?.stake,
    row.raw?.amount
  );
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

function tickAtOrAfterWithin(ticks, targetTime, maxLagMs = Infinity) {
  let lo = 0;
  let hi = ticks.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (Number(ticks[mid].time) < targetTime) lo = mid + 1;
    else hi = mid;
  }
  if (lo >= ticks.length) return null;
  const tick = ticks[lo];
  return Number(tick.time) - Number(targetTime) <= maxLagMs ? tick : null;
}

function fallbackSettlePrice(ticks, targetTime, now, currentPrice, maxLagMs = Infinity) {
  const tick = tickAtOrAfterWithin(ticks, targetTime, maxLagMs);
  if (tick) return Number(tick.price);
  const ageMs = Number(now || 0) - Number(targetTime || 0);
  if (ageMs >= 0 && ageMs <= maxLagMs && Number.isFinite(Number(currentPrice))) {
    return Number(currentPrice);
  }
  return null;
}

function correctedClientTime(row, value) {
  const clientTime = Number(row && row.clientTime);
  const serverTime = Number(row && row.serverTime);
  const candidate = Number(value);
  if (!Number.isFinite(candidate) || candidate <= 0) return null;
  if (!Number.isFinite(clientTime) || !Number.isFinite(serverTime)) return candidate;
  const offsetMs = serverTime - clientTime;
  if (Math.abs(offsetMs) > 5 * 60 * 1000) return candidate;
  return candidate + offsetMs;
}

function realExecutionTime(row) {
  const candidates = [
    [row?.confirmProbe?.dispatchedAt, "confirm_probe"],
    [row?.uiTiming?.phases?.confirm_action_dispatched?.at, "confirm_phase"],
    [row?.executionTime, "direction_click"],
    [row?.clientTime, "client_report"],
  ];
  for (const [value, source] of candidates) {
    const corrected = correctedClientTime(row, value);
    if (corrected !== null) return { time: corrected, source };
  }
  const serverTime = Number(row && row.serverTime);
  return Number.isFinite(serverTime) && serverTime > 0
    ? { time: serverTime, source: "server_report" }
    : { time: null, source: "missing" };
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

function settleAmountForStatus(status, amount, payoutRate = DEFAULT_PAYOUT_RATE, payout = null) {
  const explicitPayout = cleanAmount(payout);
  if (explicitPayout !== null && ["won", "lost", "tie"].includes(status)) {
    return Number(explicitPayout.toFixed(2));
  }
  const stake = Number(amount);
  if (!Number.isFinite(stake) || stake <= 0) return null;
  if (status === "won") return Number((stake + stake * payoutRate).toFixed(2));
  if (status === "lost") return 0;
  if (status === "tie") return Number(stake.toFixed(2));
  return null;
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
  const execution = realExecutionTime(row);
  const openTime = Number(execution.time || 0);
  if (!duration || !openTime) return null;
  const executionTick = tickAtOrAfterWithin(ticks, openTime, Number(options.maxPriceLagMs ?? 5000));
  const reportedPrice = row.executionPrice ?? row.price;
  const openPrice = executionTick ? Number(executionTick.price) : (reportedPrice != null ? Number(reportedPrice) : null);
  const settleTime = openTime + duration * 60 * 1000;
  const closePrice = fallbackSettlePrice(
    ticks,
    settleTime,
    options.now,
    options.currentPrice,
    Number(options.maxPriceLagMs ?? 5000)
  );
  const status = settleStatus(row.direction, openPrice, closePrice);
  const amount = rowAmount(row);
  const modelReasons = Array.isArray(row.signal?.votes)
    ? row.signal.votes
        .filter(vote => vote && vote.reason)
        .map(vote => ({
          provider: vote.provider,
          model: vote.model,
          direction: vote.direction,
          confidence: vote.confidence,
          reason: vote.reason
        }))
    : [];
  const primaryModelReason = modelReasons.length ? modelReasons[0].reason : null;
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
    openAmount: amount,
    settleAmount: settleAmountForStatus(status, amount, effectivePayoutRate, row.payout),
    duration: String(duration),
    openTime,
    settleTime,
    openPrice,
    closePrice,
    openTimeSource: execution.source,
    reportedServerTime: Number(row.serverTime || 0) || null,
    clientClockOffsetMs: (
      Number.isFinite(Number(row.serverTime)) && Number.isFinite(Number(row.clientTime))
        ? Number(row.serverTime) - Number(row.clientTime)
        : null
    ),
    signalToOpenMs: Number.isFinite(Date.parse(row.actionableTime || row.signalTime || ""))
      ? openTime - Date.parse(row.actionableTime || row.signalTime)
      : null,
    openPriceTime: executionTick ? Number(executionTick.time) : null,
    status,
    payoutRate: effectivePayoutRate,
    pnl: statusPnl(status, amount, effectivePayoutRate),
    confidence: row.confidence,
    rsi_value: row.rsi_value,
    avg_prob: row.avg_prob,
    threshold: row.threshold,
    reason: primaryModelReason || row.signal?.reason || row.reason || null,
    decisionReason: row.signal?.reason || row.reason || null,
    modelReasons,
    amountReason: row.signal?.fixed_amount
      ? `策略固定金额 ${amountText(amount)}`
      : row.strategyId === "manual"
        ? "手动下单金额"
        : `策略配置金额 ${amountText(amount)}`,
    signalSource: row.signal?.signal_source || null,
    votes: Array.isArray(row.signal?.votes) ? row.signal.votes : [],
    failed: Array.isArray(row.signal?.failed) ? row.signal.failed : [],
    dataAgeMs: row.signal?.data_age_ms ?? null,
    modelLatencyMs: row.signal?.model_latency_ms ?? null,
    entryPrice: row.signal?.entry_price ?? null,
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
    amount: rowAmount(row),
    openAmount: rowAmount(row),
    settleAmount: null,
    duration: cleanDuration(row.duration),
    openTime,
    settleTime: openTime,
    openPrice: row.price != null ? Number(row.price) : null,
    closePrice: null,
    status: isUnverified ? "unverified" : "aborted",
    pnl: 0,
    reason: row.reason,
    amountReason: row.strategyId === "manual" ? "手动下单金额" : `策略配置金额 ${amountText(rowAmount(row))}`,
    signalSource: row.signal?.signal_source || null,
    votes: Array.isArray(row.signal?.votes) ? row.signal.votes : [],
    failed: Array.isArray(row.signal?.failed) ? row.signal.failed : [],
    dataAgeMs: row.signal?.data_age_ms ?? null,
    modelLatencyMs: row.signal?.model_latency_ms ?? null,
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
  const amount = cleanAmount(t.amount);
  return {
    id: "server|" + t.id,
    source,
    event: "server_trade",
    strategyId: String(source).replace(/^auto:/, "").replace(/^shadow:/, "") || "manual",
    direction: t.direction,
    amount,
    openAmount: amount,
    settleAmount: settleAmountForStatus(t.status === "active" ? "pending" : t.status, amount, effectivePayoutRate, t.payout),
    duration: cleanDuration(t.duration),
    openTime: Number(t.openTime),
    settleTime: Number(t.settleTime),
    openPrice: t.strikePrice,
    closePrice: t.settlePrice,
    status: t.status === "active" ? "pending" : t.status,
    payoutRate: effectivePayoutRate,
    pnl: statusPnl(t.status, amount, effectivePayoutRate),
    amountReason: source === "manual" ? "手动下单金额" : `策略配置金额 ${amountText(amount)}`,
    confidence: null,
    rsi_value: null
  };
}

function rowGroup(row) {
  const source = String(row.source || "");
  if (source.startsWith("shadow:") || source === "shadow" || row.event === "shadow_trade") return "shadow";
  return "real";
}

function paginateRows(rows, page = 1, pageSize = 100) {
  const safePageSize = Math.min(300, Math.max(10, Number(pageSize) || 100));
  const total = rows.length;
  const totalPages = Math.max(1, Math.ceil(total / safePageSize));
  const safePage = Math.min(totalPages, Math.max(1, Number(page) || 1));
  const offset = (safePage - 1) * safePageSize;
  return {
    rows: rows.slice(offset, offset + safePageSize),
    pagination: {
      page: safePage,
      pageSize: safePageSize,
      total,
      totalPages,
      offset,
      hasPrev: safePage > 1,
      hasNext: safePage < totalPages
    }
  };
}

function summarizeRows(rows) {
  const settled = rows.filter(r => ["won", "lost", "tie"].includes(r.status));
  const wins = settled.filter(r => r.status === "won").length;
  const losses = settled.filter(r => r.status === "lost").length;
  const ties = settled.filter(r => r.status === "tie").length;
  const decided = wins + losses;
  const pnl = settled.reduce((sum, r) => sum + (Number(r.pnl) || 0), 0);
  const pending = rows.filter(r => r.status === "pending").length;
  const aborted = rows.filter(r => r.status === "aborted").length;
  const unverified = rows.filter(r => r.status === "unverified").length;
  return {
    total: rows.length,
    settled: settled.length,
    wins,
    losses,
    ties,
    pending,
    aborted,
    unverified,
    executionFailed: aborted + unverified,
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

function todayDayKey(now = Date.now()) {
  return dayKeyForTime(now);
}

function addDays(day, delta) {
  const m = String(day || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return todayDayKey();
  const utc = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]) + Number(delta || 0));
  return new Date(utc).toISOString().slice(0, 10);
}

function shanghaiDayRange(day) {
  const safeDay = String(day || todayDayKey()).match(/^\d{4}-\d{2}-\d{2}$/) ? String(day) : todayDayKey();
  const startMs = Date.parse(`${safeDay}T00:00:00+08:00`);
  const endDay = addDays(safeDay, 1);
  const endMs = Date.parse(`${endDay}T00:00:00+08:00`);
  return { day: safeDay, startMs, endMs, nextDay: endDay, prevDay: addDays(safeDay, -1) };
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
    const actualOpenTime = Number(open.executionOpenTime || open.openTime || open.serverTime || 0);
    const actualOpenPrice = open.executionStrikePrice ?? open.strikePrice ?? null;
    const settleTime = Number(settle.settleTime || (actualOpenTime + (Number(duration) || 0) * 60000));
    const closePrice = settle.settlePrice ?? fallbackSettlePrice(
      options.ticks || [],
      settleTime,
      options.now,
      options.currentPrice,
      Number(options.maxPriceLagMs ?? 5000)
    );
    const status = settle.status || (closePrice != null ? settleStatus(open.direction, actualOpenPrice, closePrice) : "pending");
    const amount = cleanAmount(open.amount, row.amount, open.signal?.amount, row.signal?.amount);
    const actionableMs = Date.parse(open.actionableTime || open.signalTime || "");
    out.push({
      id: "shadow_audit|" + id,
      source: open.source || row.source || "shadow",
      event: "shadow_trade",
      strategyId: open.strategyId || String(open.source || "").replace(/^shadow:/, "") || "shadow",
      direction: open.direction,
      amount,
      openAmount: amount,
      settleAmount: settleAmountForStatus(status, amount, effectivePayoutRate, settle.payout),
      duration,
      openTime: actualOpenTime,
      settleTime,
      openPrice: actualOpenPrice,
      closePrice,
      status,
      payoutRate: effectivePayoutRate,
      pnl: statusPnl(status, amount, effectivePayoutRate),
      signalEntryPrice: open.signalEntryPrice ?? open.strikePrice ?? null,
      executionStrikePrice: open.executionStrikePrice ?? actualOpenPrice,
      executionOpenTime: open.executionOpenTime ?? actualOpenTime,
      executionDelayMs: open.executionDelayMs ?? (
        Number.isFinite(actionableMs)
          ? actualOpenTime - actionableMs
          : null
      ),
      openTimeSource: open.executionOpenTime ? "shadow_execution" : "shadow_legacy_open",
      amountReason: `影子下单金额 ${amountText(amount)}`,
      confidence: open.confidence,
      rsi_value: open.rsi_value,
      avg_prob: open.avg_prob,
      signalTime: open.signalTime,
      actionableTime: open.actionableTime
    });
  }
  return out;
}

function configuredDurationForStrategy(strategyId, options = {}) {
  const configured = options.durationForStrategy;
  const value = typeof configured === "function"
    ? configured(strategyId)
    : configured && typeof configured === "object"
      ? configured[strategyId]
      : null;
  return Math.max(1, Number(value || options.defaultDuration || 10) || 10);
}

function deriveOrderLifecycleGate(options = {}) {
  const ticks = normalizePriceTicks(options.priceTicks || []);
  const now = Number(options.now || Date.now());
  const pendingByStrategy = {};
  const seen = new Set();
  const candidateRows = new Map();

  // 每个策略只判断最新一笔可能成交的订单，避免价格日志轮转后旧订单被重新判为待结算。
  for (const sourceRow of options.auditRows || []) {
    if (!sourceRow || !["order_done", "order_unverified"].includes(sourceRow.event)) continue;
    const strategyId = String(sourceRow.strategyId || "manual");
    const execution = realExecutionTime(sourceRow);
    const openTime = Number(execution.time || 0);
    const current = candidateRows.get(strategyId);
    if (!current || openTime >= current.openTime) candidateRows.set(strategyId, { sourceRow, openTime });
  }

  for (const candidate of candidateRows.values()) {
    const sourceRow = candidate.sourceRow;
    const strategyId = String(sourceRow.strategyId || "manual");
    const duration = Math.max(
      1,
      Number(sourceRow.duration) || configuredDurationForStrategy(strategyId, options)
    );
    const row = { ...sourceRow, strategyId, duration };
    const order = auditOrderRow(row, ticks, options.payoutRate, {
      now,
      currentPrice: options.currentPrice,
      maxPriceLagMs: options.maxPriceLagMs
    });
    if (!order) continue;

    // 审计导入或客户端重试可能产生重复事件，同一订单只参与一次门禁判定。
    const orderKey = [
      sourceRow.event,
      strategyId,
      sourceRow.signalTime || "",
      sourceRow.queueBatchId || "",
      order.openTime
    ].join("|");
    if (seen.has(orderKey)) continue;
    seen.add(orderKey);
    if (["won", "lost", "tie"].includes(order.status)) continue;

    const current = pendingByStrategy[strategyId];
    if (!current || order.openTime > current.openTime) {
      pendingByStrategy[strategyId] = {
        blocked: true,
        strategyId,
        event: sourceRow.event,
        openTime: order.openTime,
        openTimeSource: order.openTimeSource,
        duration: String(duration),
        settleTime: order.settleTime,
        expired: now >= order.settleTime,
        status: "pending",
        reason: now >= order.settleTime
          ? "settlement_price_pending"
          : "order_duration_pending"
      };
    }
  }

  return {
    updatedAt: now,
    strategies: pendingByStrategy
  };
}

function buildLiveOrderHistory(options = {}) {
  const limit = Math.min(300, Math.max(1, Number(options.limit) || 100));
  const page = Math.max(1, Number(options.page) || 1);
  const pageSize = Math.min(300, Math.max(10, Number(options.pageSize || options.limit) || limit));
  const kind = options.kind === "shadow" ? "shadow" : options.kind === "real" ? "real" : "all";
  const mode = options.mode === "day" ? "day" : "page";
  const dayRange = mode === "day" ? shanghaiDayRange(options.day) : null;
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
  const scopedUnique = dayRange ? unique.filter(row => dayKeyForTime(row.openTime) === dayRange.day) : unique;
  const scopedRealRows = scopedUnique.filter(r => rowGroup(r) === "real");
  const scopedShadowRows = scopedUnique.filter(r => rowGroup(r) === "shadow");
  const visibleRows = kind === "real" ? scopedRealRows : kind === "shadow" ? scopedShadowRows : scopedUnique;
  const combinedSummary = summarizeRows(scopedUnique);
  const visibleSummary = summarizeRows(visibleRows);
  visibleSummary.combined = { ...combinedSummary };
  visibleSummary.real = summarizeRows(scopedRealRows);
  visibleSummary.shadow = summarizeRows(scopedShadowRows);
  visibleSummary.byStrategy = summarizeBy(visibleRows, row => row.strategyId || "manual", key => key);
  visibleSummary.byDay = summarizeBy(visibleRows, row => dayKeyForTime(row.openTime), key => key);
  const paged = mode === "day"
    ? {
        rows: visibleRows,
        pagination: {
          mode,
          day: dayRange.day,
          prevDay: dayRange.prevDay,
          nextDay: dayRange.nextDay,
          page: 1,
          pageSize: visibleRows.length,
          total: visibleRows.length,
          totalPages: 1,
          offset: 0,
          hasPrev: true,
          hasNext: dayRange.day < todayDayKey(now)
        }
      }
    : paginateRows(visibleRows, page, pageSize);
  return {
    updatedAt: now,
    summary: visibleSummary,
    breakdown: {
      combined: buildBreakdown(scopedUnique),
      real: buildBreakdown(scopedRealRows),
      shadow: buildBreakdown(scopedShadowRows)
    },
    active: unique.filter(r => r.status === "pending").slice(0, limit),
    recent: paged.rows,
    pagination: {
      ...paged.pagination,
      kind,
      mode,
      day: dayRange ? dayRange.day : undefined,
      availableDays: Array.isArray(options.availableDays) ? options.availableDays : undefined
    }
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
  settleAmountForStatus,
  normalizePriceTicks,
  realExecutionTime,
  dayKeyForTime,
  shanghaiDayRange,
  deriveOrderLifecycleGate,
  buildLiveOrderHistory
};
