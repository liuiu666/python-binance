import {
  Activity,
  ArrowDown,
  ArrowUp,
  BadgeCheck,
  Clock,
  Database,
  Plus,
  RefreshCcw,
  Save,
  Server,
  Trash2,
  Wifi
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

const PAYOUT = 0.85;

const DEFAULT_CONFIG = {
  amount: "5",
  duration: "30",
  autoTrade: false,
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

function clamp(num, min, max) {
  return Math.max(min, Math.min(max, Number(num) || 0));
}

function fmt(num, digits = 2) {
  if (num === null || num === undefined || Number.isNaN(Number(num))) return "--";
  return Number(num).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  });
}

function fmtPrice(num) {
  return num === null || num === undefined || Number.isNaN(Number(num)) ? "--" : Number(num).toFixed(2);
}

function fmtPct(num, digits = 1) {
  return num === null || num === undefined || Number.isNaN(Number(num)) ? "--" : `${Number(num).toFixed(digits)}%`;
}

function directionText(direction) {
  if (direction === "UP") return "看涨";
  if (direction === "DOWN") return "看跌";
  return "--";
}

function directionClass(direction) {
  if (direction === "UP") return "up";
  if (direction === "DOWN") return "down";
  return "neutral";
}

function strategyName(strategyId) {
  if (strategyId === "BTC_10min") return "10 分钟";
  if (strategyId === "BTC_30min") return "30 分钟";
  if (!strategyId || strategyId === "manual") return "手动";
  return String(strategyId).replace(/^auto:/, "");
}

function statusText(status) {
  if (status === "won") return "赢";
  if (status === "lost") return "亏";
  if (status === "tie") return "平";
  if (status === "pending") return "持仓";
  if (status === "aborted") return "失败";
  return status || "--";
}

function statusClass(status) {
  if (status === "won") return "won";
  if (status === "lost") return "lost";
  if (status === "tie") return "tie";
  if (status === "aborted") return "aborted";
  return "pending";
}

function ageText(ms) {
  if (ms === null || ms === undefined) return "--";
  const seconds = Math.max(0, Math.floor(Number(ms) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h`;
}

function timeParts(ms) {
  if (!ms) return { date: "--", time: "--" };
  const date = new Date(Number(ms));
  return {
    date: date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" }),
    time: date.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" })
  };
}

function signalTimeText(value) {
  if (!value) return "";
  const time = Date.parse(value);
  if (!Number.isFinite(time)) return String(value);
  return new Date(time).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function toTierList(tiers) {
  return (Array.isArray(tiers) ? tiers : [])
    .map(item => ({
      min: clamp(item.min, 0, 100),
      amount: Math.max(1, Number(item.amount) || 1)
    }))
    .sort((a, b) => Number(b.min) - Number(a.min));
}

function amountForConfidence(confidence, config) {
  const base = String(config?.amount || DEFAULT_CONFIG.amount);
  if (!config?.tiersEnabled || confidence === null || confidence === undefined) return base;
  for (const tier of toTierList(config.tiers)) {
    if (Number(confidence) >= Number(tier.min)) return String(tier.amount);
  }
  return base;
}

function amountForSignal(strategyId, signal, payload, config) {
  if (signal?.confidence !== null && signal?.confidence !== undefined) {
    return amountForConfidence(signal.confidence, payload?._config || config);
  }
  const strategyAmounts = payload?._strategyAmounts || {};
  return String(strategyAmounts[strategyId] || config.amount || DEFAULT_CONFIG.amount);
}

function signalLabel(signal) {
  if (!signal) return "等待数据";
  if (signal.signal) return `${directionText(signal.signal)} ${fmtPct(signal.confidence, 0)}`;
  const reasons = [];
  if (signal.agree === false) reasons.push("模型分歧");
  if (signal.high_conf === false) reasons.push("强度不足");
  if (signal.rsi_extreme === false) reasons.push(`RSI ${signal.rsi_value !== undefined ? Number(signal.rsi_value).toFixed(0) : "--"}`);
  if (signal.vol_ok === false) reasons.push("波动不足");
  if (signal.session_gate_ok === false || signal.session_ok === false) reasons.push("时段确认不足");
  return reasons.length ? reasons.join(" | ") : "监控中";
}

function activeSignalFromPayload(payload) {
  if (!payload) return null;
  const signal30 = payload.BTC_30min || null;
  const signal10 = payload.BTC_10min || null;
  return (signal30?.signal ? signal30 : null) || (signal10?.signal ? signal10 : null) || signal30 || signal10;
}

function pnlText(row) {
  if (!row || row.status === "pending") return "待结算";
  if (row.status === "aborted") return row.reason ? `失败: ${row.reason}` : "失败";
  const pnl = Number(row.pnl || 0);
  return `${pnl > 0 ? "+" : ""}${fmt(pnl, 2)}U`;
}

function useInterval(callback, delay) {
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

function drawPriceChart(canvas, history) {
  if (!canvas) return;
  const parent = canvas.parentElement;
  const rect = parent.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(1, rect.width);
  const height = Math.max(1, rect.height);
  canvas.width = Math.floor(width * dpr);
  canvas.height = Math.floor(height * dpr);
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const points = (history || [])
    .slice(-240)
    .map(item => ({ time: Number(item.time || Date.now()), price: Number(item.price) }))
    .filter(item => Number.isFinite(item.price));

  ctx.fillStyle = "#7d8792";
  ctx.font = "13px system-ui, sans-serif";
  if (points.length < 2) {
    ctx.fillText("等待价格数据...", 18, 28);
    return;
  }

  const values = points.map(item => item.price);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const padding = (max - min) * 0.12 || 1;
  const low = min - padding;
  const high = max + padding;

  ctx.strokeStyle = "rgba(255,255,255,0.08)";
  ctx.lineWidth = 1;
  for (let i = 0; i < 5; i += 1) {
    const y = 18 + (i * (height - 36)) / 4;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
    ctx.stroke();
  }

  const gradient = ctx.createLinearGradient(0, 0, width, 0);
  gradient.addColorStop(0, "#27c3a5");
  gradient.addColorStop(0.5, "#f0c94a");
  gradient.addColorStop(1, "#e45858");
  ctx.strokeStyle = gradient;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.beginPath();
  points.forEach((point, index) => {
    const x = (index / (points.length - 1)) * width;
    const y = height - 18 - ((point.price - low) / (high - low)) * (height - 36);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  const last = points[points.length - 1];
  const lastY = height - 18 - ((last.price - low) / (high - low)) * (height - 36);
  ctx.fillStyle = "#27c3a5";
  ctx.beginPath();
  ctx.arc(width - 3, lastY, 4, 0, Math.PI * 2);
  ctx.fill();
}

function drawGauge(canvas, signal) {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  const cx = width / 2;
  const cy = height - 12;
  const radius = Math.max(1, height - 34);
  ctx.clearRect(0, 0, width, height);
  ctx.lineWidth = 14;
  ctx.lineCap = "round";
  ctx.strokeStyle = "rgba(255,255,255,0.11)";
  ctx.beginPath();
  ctx.arc(cx, cy, radius, Math.PI, Math.PI * 2);
  ctx.stroke();

  const rsi = signal?.rsi_value !== undefined ? Number(signal.rsi_value) : null;
  const confidence = signal?.confidence !== undefined ? Number(signal.confidence) : null;
  const metric = confidence !== null && Number.isFinite(confidence) ? confidence : rsi;
  if (metric !== null && Number.isFinite(metric)) {
    const pct = clamp(metric, 0, 100) / 100;
    ctx.strokeStyle = signal?.signal === "DOWN" ? "#e45858" : signal?.signal === "UP" ? "#27c3a5" : "#f0c94a";
    ctx.beginPath();
    ctx.arc(cx, cy, radius, Math.PI, Math.PI + Math.PI * pct);
    ctx.stroke();
  }

  ctx.textAlign = "center";
  ctx.fillStyle = signal?.signal === "DOWN" ? "#e45858" : signal?.signal === "UP" ? "#27c3a5" : "#f0c94a";
  ctx.font = "700 24px system-ui, sans-serif";
  if (confidence !== null && Number.isFinite(confidence)) {
    ctx.fillText(`${confidence.toFixed(0)}%`, cx, cy - 34);
  } else if (rsi !== null && Number.isFinite(rsi)) {
    ctx.fillText(`RSI ${rsi.toFixed(0)}`, cx, cy - 34);
  } else {
    ctx.fillStyle = "#7d8792";
    ctx.fillText("--", cx, cy - 34);
  }
  ctx.font = "700 12px system-ui, sans-serif";
  ctx.fillText(signal?.signal ? directionText(signal.signal) : "监控", cx, cy - 12);
}

function Toasts({ items }) {
  return (
    <div className="toast-stack" aria-live="polite">
      {items.map(item => (
        <div className={`toast ${item.type || "info"}`} key={item.id}>
          {item.message}
        </div>
      ))}
    </div>
  );
}

function Metric({ label, value, unit, tone }) {
  return (
    <div className="metric">
      <span className="metric-label">{label}</span>
      <span className={`metric-value ${tone || ""}`}>
        {value}
        {unit ? <small>{unit}</small> : null}
      </span>
    </div>
  );
}

function StrategyCard({ title, signal, amount }) {
  const signalClass = directionClass(signal?.signal);
  const duration = signal?.duration || signal?.interval_min || "--";
  return (
    <section className="strategy-card">
      <div>
        <span className="strategy-title">{title}</span>
        <strong className={signalClass}>{signalLabel(signal)}</strong>
      </div>
      <div className="strategy-meta">
        <span>{amount || "--"}U</span>
        <span>{duration} 分钟</span>
        <span>RSI {signal?.rsi_value !== undefined ? Number(signal.rsi_value).toFixed(0) : "--"}</span>
      </div>
    </section>
  );
}

function ReportStrip({ reports, runtime, tablet }) {
  const cards = useMemo(() => {
    const decision = reports?.decision || {};
    const health = reports?.health || decision.system_health || {};
    const summary = decision.production_summary || decision.validated_walkforward || {};
    const edge10 = summary.BTC_10min?.edge_over_breakeven;
    const edge30 = summary.BTC_30min?.edge_over_breakeven;
    const portfolio = decision.parallel_portfolio || {};
    const shadow = reports?.shadowDecision || {};
    const shadowCounts = shadow.summary_counts || {};
    const tabletStatus = tablet?.status === "has_order_done"
      ? "orders ok"
      : tablet?.status === "autojs_online_waiting_for_order_done"
        ? `heartbeat ${ageText(tablet.latestHeartbeatAgeMs)}`
        : tablet?.status ? "seen" : "--";
    const liveState = tablet?.checks?.orderDoneSeen
      ? "已下单"
      : tablet?.checks?.heartbeatOnline
        ? "在线"
        : "等待";
    return [
      { label: "Health", value: health.overall || "--", tone: health.overall === "ok" ? "ok" : health.overall === "fail" ? "fail" : "warn" },
      { label: "10m Edge", value: edge10 !== undefined ? `+${Number(edge10).toFixed(2)}pp` : "--", tone: edge10 > 0 ? "ok" : "warn" },
      { label: "30m Edge", value: edge30 !== undefined ? `+${Number(edge30).toFixed(2)}pp` : "--", tone: edge30 > 0 ? "ok" : "warn" },
      {
        label: "Filter",
        value: portfolio.win_rate !== undefined
          ? `${Number(portfolio.win_rate).toFixed(1)}% / ${Number(portfolio.frequency?.trades_per_day || 0).toFixed(1)}/d`
          : "--",
        tone: portfolio.win_rate ? "ok" : "warn"
      },
      { label: "Tablet", value: tabletStatus, tone: tablet?.checks?.heartbeatOnline || tablet?.checks?.orderDoneSeen ? "ok" : "warn" },
      { label: "Shadow", value: shadow.summary_counts ? `watch ${shadowCounts.watch || 0} / reject ${(shadowCounts.reject_live_weak || 0) + (shadowCounts.reject_offline_weak || 0)}` : "--", tone: shadow.summary_counts ? "warn" : "" },
      { label: "Server", value: runtime?.serverId || "--", tone: runtime?.serverId ? "ok" : "warn" },
      { label: "Live", value: liveState, tone: tablet?.checks?.heartbeatOnline || tablet?.checks?.orderDoneSeen ? "ok" : "warn" }
    ];
  }, [reports, runtime, tablet]);

  return (
    <section className="report-strip">
      {cards.map(card => (
        <div className="report-card" key={card.label} title={`${card.label}: ${card.value}`}>
          <span>{card.label}</span>
          <strong className={card.tone}>{card.value}</strong>
        </div>
      ))}
    </section>
  );
}

function SignalBanner({ signalPayload, activeSignal, signalAmount }) {
  const signal30 = signalPayload?.BTC_30min || null;
  const signal10 = signalPayload?.BTC_10min || null;
  const activeItems = [signal30, signal10].filter(item => item?.signal);
  return (
    <section className="signal-banner">
      <div className="signal-label">
        <Activity size={18} />
        <span>AI 分析</span>
      </div>
      <div className="signal-list">
        {activeItems.length ? (
          activeItems.map(item => (
            <span className={`signal-pill ${directionClass(item.signal)}`} key={item.strategy_id || item.interval_min}>
              {strategyName(item.strategy_id)} {directionText(item.signal)} {fmtPct(item.confidence, 0)}
            </span>
          ))
        ) : (
          <span className="signal-pill neutral">10 分钟 / 30 分钟监控中</span>
        )}
      </div>
      <div className="signal-side">
        <span>{signalAmount || "--"}U</span>
        <small>{signalTimeText(activeSignal?.time)}</small>
      </div>
    </section>
  );
}

function GaugePanel({ signal }) {
  const gaugeRef = useRef(null);
  useEffect(() => {
    drawGauge(gaugeRef.current, signal);
  }, [signal]);
  const probs = Array.isArray(signal?.probs) ? signal.probs : [0.5, 0.5, 0.5];
  const verdict = signal?.signal ? `${directionText(signal.signal)} ${fmtPct(signal.confidence, 0)}` : signalLabel(signal);
  const thresholdText = signal?.engine === "two_minute_regime_model"
    ? `${signal?.regime_group || "--"} | p=${Number(signal?.avg_prob || 0).toFixed(3)} / th=${Number(signal?.policy_threshold || signal?.threshold || 0).toFixed(2)}`
    : signal?.rsi_value !== undefined
      ? `RSI=${Number(signal.rsi_value).toFixed(0)} | 强度 ${fmtPct(signal?.confidence, 0)}`
      : "等待信号";

  return (
    <section className="panel signal-panel">
      <header className="panel-header">
        <span><Activity size={15} /> 信号强度</span>
      </header>
      <canvas className="gauge" ref={gaugeRef} width="280" height="140" />
      <div className="model-bars">
        {probs.slice(0, 3).map((prob, index) => {
          const pct = clamp(prob * 100, 0, 100);
          const tone = prob > 0.6 ? "up" : prob < 0.4 ? "down" : "neutral";
          return (
            <div className="model-row" key={`model-${index + 1}`}>
              <span>M{index + 1}</span>
              <div className="bar-track">
                <i className={tone} style={{ width: `${pct}%` }} />
              </div>
              <strong>{pct.toFixed(1)}%</strong>
            </div>
          );
        })}
      </div>
      <div className="verdict">
        <span>判断</span>
        <strong className={directionClass(signal?.signal)}>{verdict}</strong>
        <small>{thresholdText}</small>
      </div>
    </section>
  );
}

function ConfigPanel({
  draft,
  dirty,
  apiToken,
  onTokenChange,
  onDraftChange,
  onToggle,
  onTierChange,
  onAddTier,
  onRemoveTier,
  onSave
}) {
  const tierRules = draft.tiersEnabled
    ? `${toTierList(draft.tiers).map(t => `强度>=${t.min}% ${t.amount}U`).join(" / ")} / 其他 ${draft.amount}U`
    : `固定 ${draft.amount || DEFAULT_CONFIG.amount}U`;

  return (
    <section className="panel config-panel">
      <header className="panel-header">
        <span><Server size={15} /> 平板交易配置</span>
        <em className={dirty ? "dirty" : "synced"}>{dirty ? "未保存" : "已同步"}</em>
      </header>

      <div className="stake-preview">
        <span>10 分钟策略</span>
        <strong>{tierRules}</strong>
      </div>
      <div className="stake-preview">
        <span>30 分钟策略</span>
        <strong>{tierRules}</strong>
      </div>

      <label className="form-row">
        <span>默认金额</span>
        <input
          min="1"
          step="1"
          type="number"
          value={draft.amount}
          onChange={event => onDraftChange({ amount: event.target.value })}
        />
      </label>
      <label className="form-row">
        <span>到期时间</span>
        <select value={draft.duration} onChange={event => onDraftChange({ duration: event.target.value })}>
          <option value="10">10 分钟</option>
          <option value="30">30 分钟</option>
          <option value="60">1 小时</option>
        </select>
      </label>
      <label className="form-row">
        <span>最低强度</span>
        <input
          min="0"
          max="100"
          step="1"
          type="number"
          value={draft.minConfidence}
          onChange={event => onDraftChange({ minConfidence: Number(event.target.value) })}
        />
      </label>

      <ToggleRow label="分级金额" checked={draft.tiersEnabled} onChange={() => onToggle("tiersEnabled")} />
      {draft.tiersEnabled ? (
        <div className="tiers-panel">
          <div className="tiers-head">
            <span>强度</span>
            <span>金额</span>
            <span />
          </div>
          {toTierList(draft.tiers).map((tier, index) => (
            <div className="tier-row" key={`${tier.min}-${index}`}>
              <input
                min="0"
                max="100"
                step="1"
                type="number"
                value={tier.min}
                onChange={event => onTierChange(index, { min: Number(event.target.value) })}
              />
              <input
                min="1"
                step="1"
                type="number"
                value={tier.amount}
                onChange={event => onTierChange(index, { amount: Number(event.target.value) })}
              />
              <button className="icon-button danger" type="button" onClick={() => onRemoveTier(index)} title="删除档位">
                <Trash2 size={14} />
              </button>
            </div>
          ))}
          <button className="secondary-button" type="button" onClick={onAddTier}>
            <Plus size={14} /> 增加档位
          </button>
        </div>
      ) : null}

      <ToggleRow label="自动交易" checked={draft.autoTrade} onChange={() => onToggle("autoTrade")} />
      <ToggleRow label="冲突过滤" checked={draft.skipConflictSignals} onChange={() => onToggle("skipConflictSignals")} />
      <ToggleRow label="同策略不重录" checked={draft.preventOverlapOrders !== false} onChange={() => onToggle("preventOverlapOrders")} />

      <label className="form-row">
        <span>队列顺序</span>
        <select value={draft.queueOrderPolicy || "confidence_desc"} onChange={event => onDraftChange({ queueOrderPolicy: event.target.value })}>
          <option value="confidence_desc">高强度优先</option>
          <option value="30_then_10">30 分优先</option>
          <option value="10_then_30">10 分优先</option>
        </select>
      </label>

      <label className="form-row">
        <span>API Token</span>
        <input
          className="token-input"
          type="password"
          value={apiToken}
          onChange={event => onTokenChange(event.target.value)}
          placeholder="可选"
        />
      </label>

      <button className="primary-button" type="button" onClick={onSave}>
        <Save size={16} /> 保存配置
      </button>
    </section>
  );
}

function ToggleRow({ label, checked, onChange }) {
  return (
    <div className="toggle-row">
      <span>{label}</span>
      <button className={`toggle ${checked ? "on" : "off"}`} type="button" onClick={onChange}>
        <i />
        {checked ? "开启" : "关闭"}
      </button>
    </div>
  );
}

function ManualPanel({ draft, onManualTrade }) {
  const amount = Number(draft.amount) || Number(DEFAULT_CONFIG.amount);
  return (
    <section className="manual-grid">
      <button className="trade-button up" type="button" onClick={() => onManualTrade("UP")}>
        <ArrowUp size={22} />
        <span>
          <strong>看涨</strong>
          <small>+{fmt(amount * PAYOUT, 2)} USDT</small>
        </span>
      </button>
      <button className="trade-button down" type="button" onClick={() => onManualTrade("DOWN")}>
        <ArrowDown size={22} />
        <span>
          <strong>看跌</strong>
          <small>+{fmt(amount * PAYOUT, 2)} USDT</small>
        </span>
      </button>
    </section>
  );
}

function OpsPanel({ runtime, tablet, onRefreshData, onRefreshReports }) {
  const links = [
    { label: "平板页", url: runtime?.tabletPageUrl },
    { label: "Loader", url: runtime?.loaderUrl },
    { label: "脚本", url: runtime?.scriptUrl },
    { label: "信号", url: runtime?.signalUrl }
  ].filter(item => item.url);
  return (
    <section className="panel ops-panel">
      <header className="panel-header">
        <span><RefreshCcw size={15} /> 运行操作</span>
        <em className={tablet?.checks?.heartbeatOnline ? "synced" : "dirty"}>
          {tablet?.checks?.heartbeatOnline ? "平板在线" : "等待平板"}
        </em>
      </header>
      <div className="ops-actions">
        <button className="secondary-button" type="button" onClick={onRefreshData}>
          <RefreshCcw size={14} /> 刷新数据
        </button>
        <button className="secondary-button" type="button" onClick={onRefreshReports}>
          <RefreshCcw size={14} /> 刷新报告
        </button>
      </div>
      <div className="ops-links">
        {links.map(item => (
          <a href={item.url} target="_blank" rel="noreferrer" key={item.label}>
            {item.label}
          </a>
        ))}
      </div>
    </section>
  );
}

function TradeHistory({ history }) {
  const summary = history?.summary || {};
  const active = history?.active || [];
  const recent = history?.recent || [];
  const pnl = Number(summary.pnl || 0);
  return (
    <section className="history-section">
      <header className="history-header">
        <div>
          <h2>实盘订单记录 <span>{summary.total ?? recent.length}</span></h2>
          <p>按开仓时间倒序，包含平板上报和服务器模拟记录</p>
        </div>
        <div className="history-summary">
          <span>胜率 <strong>{fmtPct(summary.winRate, 1)}</strong></span>
          <span>盈亏 <strong className={pnl > 0 ? "up" : pnl < 0 ? "down" : ""}>{pnl > 0 ? "+" : ""}{fmt(pnl, 2)}U</strong></span>
          <span>赢/亏 <strong>{summary.wins || 0}/{summary.losses || 0}</strong></span>
        </div>
      </header>
      <div className="active-ledger">
        <div className="ledger-title">持仓中 <span>{summary.pending ?? active.length}</span></div>
        <TradeRows rows={active} compact emptyText="暂无持仓订单" />
      </div>
      <div className="trade-table">
        <div className="trade-table-head">
          <span>结果</span>
          <span>策略 / 方向</span>
          <span>金额</span>
          <span>开仓</span>
          <span>价格</span>
          <span>盈亏</span>
        </div>
        <TradeRows rows={recent} emptyText="暂无历史订单" />
      </div>
    </section>
  );
}

function TradeRows({ rows, emptyText }) {
  if (!rows?.length) return <div className="empty-state">{emptyText}</div>;
  return (
    <div className="history-list">
      {rows.map(row => {
        const cls = statusClass(row.status);
        const dir = directionClass(row.direction);
        const tp = timeParts(row.openTime);
        return (
          <div className={`history-row ${cls}`} key={row.id || `${row.openTime}-${row.strategyId}-${row.direction}`}>
            <div><span className={`status-pill ${cls}`}>{statusText(row.status)}</span></div>
            <div className="history-main">
              <div className="history-strategy">
                <span className={`dir-pill ${dir}`}>{directionText(row.direction)}</span>
                <strong>{strategyName(row.strategyId)}</strong>
              </div>
              <small>强度 {row.confidence !== undefined && row.confidence !== null ? fmtPct(row.confidence, 0) : "--"} | RSI {row.rsi_value !== undefined && row.rsi_value !== null ? Number(row.rsi_value).toFixed(0) : "--"}</small>
            </div>
            <div className="history-amount"><strong>{fmt(row.amount, 0)}U</strong><small>{row.duration || "--"} 分钟</small></div>
            <div className="history-time"><small>{tp.date}</small><strong>{tp.time}</strong></div>
            <div className="history-price"><span>开 {fmtPrice(row.openPrice)}</span><span>收 {row.closePrice !== null && row.closePrice !== undefined ? fmtPrice(row.closePrice) : "待到期"}</span></div>
            <div className={`history-pnl ${cls}`}>{pnlText(row)}</div>
          </div>
        );
      })}
    </div>
  );
}

export default function App() {
  const [currentPrice, setCurrentPrice] = useState(null);
  const [firstPrice, setFirstPrice] = useState(null);
  const [priceHistory, setPriceHistory] = useState([]);
  const [signalPayload, setSignalPayload] = useState(null);
  const [reports, setReports] = useState(null);
  const [runtime, setRuntime] = useState(null);
  const [tablet, setTablet] = useState(null);
  const [tradeHistory, setTradeHistory] = useState(null);
  const [realBalance, setRealBalance] = useState(null);
  const [configDraft, setConfigDraft] = useState(DEFAULT_CONFIG);
  const [configDirty, setConfigDirty] = useState(false);
  const [apiToken, setApiToken] = useState(() => window.localStorage.getItem("btcApiToken") || "");
  const [toasts, setToasts] = useState([]);
  const lastWsPriceRef = useRef(0);
  const dirtyRef = useRef(false);
  const chartRef = useRef(null);

  const activeSignal = useMemo(() => activeSignalFromPayload(signalPayload), [signalPayload]);
  const signalAmount = useMemo(() => {
    if (!activeSignal) return String(configDraft.amount || DEFAULT_CONFIG.amount);
    return amountForSignal(activeSignal.strategy_id, activeSignal, signalPayload, configDraft);
  }, [activeSignal, configDraft, signalPayload]);

  const signal10Amount = useMemo(() => amountForSignal("BTC_10min", signalPayload?.BTC_10min, signalPayload, configDraft), [configDraft, signalPayload]);
  const signal30Amount = useMemo(() => amountForSignal("BTC_30min", signalPayload?.BTC_30min, signalPayload, configDraft), [configDraft, signalPayload]);

  const priceChange = useMemo(() => {
    if (!currentPrice || !firstPrice) return null;
    const diff = Number(currentPrice) - Number(firstPrice);
    const pct = (diff / Number(firstPrice)) * 100;
    return { diff, pct };
  }, [currentPrice, firstPrice]);

  const notify = useCallback((message, type = "info") => {
    const id = `${Date.now()}-${Math.random()}`;
    setToasts(items => [...items, { id, message, type }].slice(-4));
    setTimeout(() => {
      setToasts(items => items.filter(item => item.id !== id));
    }, 2800);
  }, []);

  const apiFetch = useCallback((url, options = {}) => {
    const headers = { ...(options.headers || {}) };
    if (apiToken) headers["X-API-Token"] = apiToken;
    return fetch(url, { ...options, headers });
  }, [apiToken]);

  const loadSignals = useCallback(() => {
    apiFetch("/api/signal")
      .then(res => res.json())
      .then(setSignalPayload)
      .catch(() => {});
  }, [apiFetch]);

  const loadReports = useCallback(() => {
    apiFetch("/api/reports").then(res => res.json()).then(setReports).catch(() => {});
  }, [apiFetch]);

  const loadRuntime = useCallback(() => {
    apiFetch("/api/runtime").then(res => res.json()).then(setRuntime).catch(() => {});
  }, [apiFetch]);

  const loadTablet = useCallback(() => {
    apiFetch("/api/tablet-diagnostics").then(res => res.json()).then(setTablet).catch(() => {});
  }, [apiFetch]);

  const loadTradeHistory = useCallback(() => {
    apiFetch("/api/trade-history?limit=120").then(res => res.json()).then(setTradeHistory).catch(() => {});
  }, [apiFetch]);

  const loadConfig = useCallback((force = false) => {
    if (dirtyRef.current && !force) return;
    apiFetch("/api/config")
      .then(res => res.json())
      .then(config => {
        setConfigDraft({ ...DEFAULT_CONFIG, ...config, tiers: toTierList(config.tiers?.length ? config.tiers : DEFAULT_CONFIG.tiers) });
        dirtyRef.current = false;
        setConfigDirty(false);
      })
      .catch(() => {});
  }, [apiFetch]);

  const loadPriceFallback = useCallback(() => {
    if (Date.now() - lastWsPriceRef.current < 5000) return;
    apiFetch("/api/price")
      .then(res => res.json())
      .then(data => {
        if (!data?.price) return;
        const price = Number(data.price);
        setCurrentPrice(price);
        setFirstPrice(old => old || price);
        setPriceHistory(history => [...history.slice(-599), { time: Date.now(), price }]);
      })
      .catch(() => {});
  }, [apiFetch]);

  const markDraft = useCallback(patch => {
    dirtyRef.current = true;
    setConfigDirty(true);
    setConfigDraft(old => ({ ...old, ...patch }));
  }, []);

  const toggleDraft = useCallback(key => {
    dirtyRef.current = true;
    setConfigDirty(true);
    setConfigDraft(old => ({ ...old, [key]: key === "preventOverlapOrders" ? old.preventOverlapOrders === false : !old[key] }));
  }, []);

  const handleTierChange = useCallback((index, patch) => {
    dirtyRef.current = true;
    setConfigDirty(true);
    setConfigDraft(old => {
      const tiers = toTierList(old.tiers);
      tiers[index] = { ...tiers[index], ...patch };
      return { ...old, tiers: toTierList(tiers) };
    });
  }, []);

  const handleAddTier = useCallback(() => {
    dirtyRef.current = true;
    setConfigDirty(true);
    setConfigDraft(old => ({ ...old, tiers: toTierList([...(old.tiers || []), { min: 50, amount: Number(old.amount) || 5 }]) }));
  }, []);

  const handleRemoveTier = useCallback(index => {
    dirtyRef.current = true;
    setConfigDirty(true);
    setConfigDraft(old => ({ ...old, tiers: toTierList(old.tiers).filter((_, i) => i !== index) }));
  }, []);

  const handleTokenChange = useCallback(value => {
    setApiToken(value);
    window.localStorage.setItem("btcApiToken", value);
  }, []);

  const saveConfig = useCallback(() => {
    const payload = {
      ...configDraft,
      amount: String(configDraft.amount || DEFAULT_CONFIG.amount),
      duration: String(configDraft.duration || DEFAULT_CONFIG.duration),
      minConfidence: Number(configDraft.minConfidence),
      tiers: toTierList(configDraft.tiers)
    };
    apiFetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(async res => {
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
        return body;
      })
      .then(body => {
        if (body.safetyBlocked) {
          notify(`策略确认未通过：${body.safetyBlocked.verdict || "not allowed"}`, "error");
        } else {
          notify("配置已保存", "success");
        }
        setConfigDraft({ ...DEFAULT_CONFIG, ...body, tiers: toTierList(body.tiers?.length ? body.tiers : DEFAULT_CONFIG.tiers) });
        dirtyRef.current = false;
        setConfigDirty(false);
      })
      .catch(error => notify(`保存失败：${error.message}`, "error"));
  }, [apiFetch, configDraft, notify]);

  const manualTrade = useCallback(direction => {
    const payload = {
      direction,
      amount: String(configDraft.amount || DEFAULT_CONFIG.amount),
      duration: String(configDraft.duration || DEFAULT_CONFIG.duration)
    };
    apiFetch("/api/manual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(async res => {
        const body = await res.json().catch(() => ({}));
        if (!res.ok || body.error) throw new Error(body.error || `HTTP ${res.status}`);
        return body;
      })
      .then(() => notify(`${directionText(direction)} ${payload.amount}U x ${payload.duration} 分钟已发送到平板`, "success"))
      .catch(error => notify(`发送失败：${error.message}`, "error"));
  }, [apiFetch, configDraft, notify]);

  const triggerServerAction = useCallback((url, label, after) => {
    apiFetch(url, { method: "POST" })
      .then(async res => {
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
        return body;
      })
      .then(body => {
        notify(`${label}已触发`, "success");
        if (after) after(body);
      })
      .catch(error => notify(`${label}失败：${error.message}`, "error"));
  }, [apiFetch, notify]);

  const refreshDataNow = useCallback(() => {
    triggerServerAction("/api/data-update/refresh", "数据刷新", () => {
      loadRuntime();
      loadSignals();
    });
  }, [loadRuntime, loadSignals, triggerServerAction]);

  const refreshReportsNow = useCallback(() => {
    triggerServerAction("/api/reports/refresh", "报告刷新", () => {
      loadReports();
    });
  }, [loadReports, triggerServerAction]);

  const refreshAll = useCallback(() => {
    loadSignals();
    loadReports();
    loadRuntime();
    loadTablet();
    loadTradeHistory();
    loadConfig(true);
    loadPriceFallback();
    notify("已刷新", "success");
  }, [loadConfig, loadPriceFallback, loadReports, loadRuntime, loadSignals, loadTablet, loadTradeHistory, notify]);

  useEffect(() => {
    loadSignals();
    loadReports();
    loadRuntime();
    loadTablet();
    loadTradeHistory();
    loadConfig();
    loadPriceFallback();
  }, [loadConfig, loadPriceFallback, loadReports, loadRuntime, loadSignals, loadTablet, loadTradeHistory]);

  useEffect(() => {
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    let closed = false;
    let ws;
    let retryTimer;

    function connect() {
      ws = new WebSocket(`${scheme}//${window.location.host}/ws`);
      ws.onmessage = event => {
        const message = JSON.parse(event.data);
        if (message.type === "init") {
          if (message.price) {
            const price = Number(message.price);
            setCurrentPrice(price);
            setFirstPrice(old => old || price);
            lastWsPriceRef.current = Date.now();
          }
          if (Array.isArray(message.history)) setPriceHistory(message.history);
          if (message.realBalance?.amount !== undefined) setRealBalance(message.realBalance);
        }
        if (message.type === "price") {
          const price = Number(message.price);
          if (Number.isFinite(price)) {
            setCurrentPrice(price);
            setFirstPrice(old => old || price);
            lastWsPriceRef.current = Date.now();
          }
          if (Array.isArray(message.history)) setPriceHistory(message.history);
        }
        if (message.type === "state" && message.realBalance?.amount !== undefined) setRealBalance(message.realBalance);
        if (message.type === "balance" && message.amount !== undefined) setRealBalance(message);
        if (message.type === "trade_update") {
          notify(`订单 #${message.trade?.id || ""} ${message.trade?.status || ""}`, "info");
          loadTradeHistory();
        }
        if (message.type === "error") notify(message.message || "服务端消息错误", "error");
      };
      ws.onclose = () => {
        if (!closed) retryTimer = window.setTimeout(connect, 2000);
      };
      ws.onerror = () => ws.close();
    }

    connect();
    return () => {
      closed = true;
      window.clearTimeout(retryTimer);
      if (ws) ws.close();
    };
  }, [loadTradeHistory, notify]);

  useEffect(() => {
    drawPriceChart(chartRef.current, priceHistory);
    const observer = new ResizeObserver(() => drawPriceChart(chartRef.current, priceHistory));
    if (chartRef.current?.parentElement) observer.observe(chartRef.current.parentElement);
    return () => observer.disconnect();
  }, [priceHistory]);

  useInterval(loadSignals, 3000);
  useInterval(loadPriceFallback, 3000);
  useInterval(loadTradeHistory, 5000);
  useInterval(loadReports, 15000);
  useInterval(loadTablet, 15000);
  useInterval(loadRuntime, 30000);
  useInterval(loadConfig, 10000);

  const confidenceTone = Number(activeSignal?.confidence || 0) >= 60 ? "ok" : "";
  const topAmount = signalAmount || configDraft.amount || DEFAULT_CONFIG.amount;
  const priceTone = priceChange?.diff > 0 ? "ok" : priceChange?.diff < 0 ? "fail" : "";

  return (
    <>
      <div className="app-shell">
        <header className="topbar">
          <div className="brand">
            <BadgeCheck size={20} />
            <div>
              <strong>BTC 实盘仪表盘</strong>
              <span>{runtime?.managedProcessesEnabled === false ? "本地测试 API" : "服务器实时"}</span>
            </div>
          </div>
          <div className="top-actions">
            <Metric label="BTC 价格" value={fmtPrice(currentPrice)} unit="USDT" tone={priceTone} />
            <Metric label="RSI" value={activeSignal?.rsi_value !== undefined ? Number(activeSignal.rsi_value).toFixed(0) : "--"} />
            <Metric label="信号强度" value={activeSignal?.confidence !== undefined ? fmtPct(activeSignal.confidence, 0) : "--"} tone={confidenceTone} />
            <Metric label="账户余额" value={realBalance?.amount !== undefined ? fmt(realBalance.amount, 2) : "--"} unit="USDT" tone={realBalance?.amount !== undefined ? "ok" : ""} />
            <Metric label="下单金额" value={topAmount} unit="USDT" />
            <button className="icon-button" type="button" onClick={refreshAll} title="刷新">
              <RefreshCcw size={16} />
            </button>
          </div>
        </header>

        <SignalBanner signalPayload={signalPayload} activeSignal={activeSignal} signalAmount={signalAmount} />

        <section className="strategy-strip">
          <StrategyCard title="10 分钟" signal={signalPayload?.BTC_10min} amount={signal10Amount} />
          <StrategyCard title="30 分钟" signal={signalPayload?.BTC_30min} amount={signal30Amount} />
        </section>

        <ReportStrip reports={reports} runtime={runtime} tablet={tablet} />

        <main className="main-grid">
          <section className="workspace">
            <section className="market-panel">
              <header className="market-header">
                <div>
                  <span>BTC / USDT</span>
                  <h1>{fmtPrice(currentPrice)}</h1>
                </div>
                <div className={`price-change ${priceTone}`}>
                  {priceChange ? `${priceChange.diff >= 0 ? "+" : ""}${fmtPrice(priceChange.diff)} (${priceChange.pct >= 0 ? "+" : ""}${priceChange.pct.toFixed(2)}%)` : "--"}
                </div>
              </header>
              <div className="chart-frame">
                <canvas ref={chartRef} />
              </div>
            </section>
            <TradeHistory history={tradeHistory} />
          </section>

          <aside className="side-rail">
            <GaugePanel signal={activeSignal} />
            <ConfigPanel
              draft={configDraft}
              dirty={configDirty}
              apiToken={apiToken}
              onTokenChange={handleTokenChange}
              onDraftChange={markDraft}
              onToggle={toggleDraft}
              onTierChange={handleTierChange}
              onAddTier={handleAddTier}
              onRemoveTier={handleRemoveTier}
              onSave={saveConfig}
            />
            <ManualPanel draft={configDraft} onManualTrade={manualTrade} />
            <OpsPanel
              runtime={runtime}
              tablet={tablet}
              onRefreshData={refreshDataNow}
              onRefreshReports={refreshReportsNow}
            />
            <section className="panel runtime-panel">
              <header className="panel-header">
                <span><Wifi size={15} /> 运行状态</span>
              </header>
              <div className="runtime-list">
                <span><Server size={14} /> {runtime?.serverId || "--"}</span>
                <span><Database size={14} /> {runtime?.dataDir || "--"}</span>
                <span><Clock size={14} /> 平板 {tablet?.checks?.heartbeatOnline ? `在线 ${ageText(tablet.latestHeartbeatAgeMs)}` : "等待"}</span>
              </div>
            </section>
          </aside>
        </main>
      </div>
      <Toasts items={toasts} />
    </>
  );
}
