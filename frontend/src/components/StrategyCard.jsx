import { Activity, ArrowDown, ArrowUp, Clock, Minus, ShieldCheck } from "lucide-react";
import {
  dateTimeText,
  directionClass,
  directionText,
  displaySignalTime,
  fmt,
  fmtPct,
  signalLabel,
  statLine
} from "../utils";

function DirectionBadge({ signal }) {
  const dir = signal?.signal;
  const Icon = dir === "UP" ? ArrowUp : dir === "DOWN" ? ArrowDown : Minus;
  return (
    <span className={`direction-badge ${directionClass(dir)}`}>
      <Icon size={15} />
      {directionText(dir)}
    </span>
  );
}

function Flag({ children, tone = "neutral" }) {
  return <span className={`flag ${tone}`}>{children}</span>;
}

function BacktestLine({ backtest }) {
  if (!backtest) return <span>回测基准：当前参数暂无固定基准</span>;
  return (
    <span>
      回测基准：{fmtPct(backtest.wr, 2)}，{backtest.tradesPerDay}单/天，{backtest.trades}单，最大连亏 {backtest.maxLoss}
      {backtest.sampleHours ? `，样本 ${fmt(backtest.sampleHours, 1)}h` : ""}
    </span>
  );
}

function DetailRows({ signal }) {
  const condition = signal?.condition_summary || {};
  const rows = [
    ["当前判断", signal?.signal_detail || signalLabel(signal)],
    ["预计信号", signal?.next_signal_estimate],
    ["下次扫描", signal?.next_check_time_shanghai],
    ["入场规则", condition.entry],
    ["风控规则", condition.risk],
    ["亏损冷却", condition.loss_density],
    ["状态过滤", condition.state],
    ["V19过滤", condition.veto],
    ["间隔限制", condition.gap]
  ].filter(([, value]) => value !== undefined && value !== null && value !== "");

  if (!rows.length) return null;
  return (
    <div className="detail-list">
      {rows.map(([label, value]) => (
        <div className="detail-row" key={label}>
          <span>{label}</span>
          <strong>{value}</strong>
        </div>
      ))}
    </div>
  );
}

function FeatureGrid({ signal }) {
  const rows = [
    ["路由波动", signal?.route_sigma_bps != null ? `${fmt(signal.route_sigma_bps, 2)}bp` : null],
    ["10分钟范围", signal?.r10_bps != null ? `${fmt(signal.r10_bps, 2)}bp` : null],
    ["秒级覆盖", signal?.observed600_pct != null ? `${fmt(signal.observed600_pct, 1)}% / ${fmt(signal.min_observed_pct || 88, 0)}%` : null],
    ["扫描间隔", signal?.scan_interval_sec != null ? `${signal.scan_interval_sec}s` : null],
    ["Z值", signal?.z_score],
    ["峰值Z", signal?.peak_abs_z],
    ["离开区间", signal?.outside_sec != null ? `${signal.outside_sec}s` : null],
    ["10分钟波动", signal?.sigma10_bps != null ? `${fmt(signal.sigma10_bps, 2)}bp` : null],
    ["60秒资金流", signal?.flow60],
    ["订单薄20档", signal?.ob_imb20],
    ["微价格", signal?.ob_micro_bps != null ? `${fmt(signal.ob_micro_bps, 4)}bp` : null],
    ["共识票", signal?.consensus_votes != null ? `${signal.consensus_votes}/${signal?.min_consensus_votes || 2}` : null],
    ["信号年龄", signal?.signal_age_sec != null ? `${fmt(signal.signal_age_sec, 1)}s` : null],
    ["候选数", signal?.candidate_count]
  ].filter(([, value]) => value !== undefined && value !== null && value !== "");

  if (!rows.length) return null;
  return (
    <div className="feature-grid">
      {rows.map(([label, value]) => (
        <div key={label}>
          <span>{label}</span>
          <strong>{value}</strong>
        </div>
      ))}
    </div>
  );
}

function routerStatusText(status) {
  const map = {
    ready: "可触发",
    waiting_tail: "等尾部",
    sigma_out_of_range: "波动不匹配",
    insufficient_data: "数据不足",
    flat_sigma: "波动过低",
    blocked_filter: "资金流拦截",
    blocked_zone: "区间拦截",
    blocked_low_up_veto: "low+UP否决"
  };
  return map[status] || status || "--";
}

function routerStatusTone(status) {
  if (status === "ready") return "ok";
  if (status === "waiting_tail") return "warn";
  if (String(status || "").startsWith("blocked")) return "bad";
  return "neutral";
}

function RouterDiagnostics({ signal }) {
  const rows = Array.isArray(signal?.router_diagnostics) ? signal.router_diagnostics : [];
  if (!rows.length) return null;
  return (
    <div className="router-panel">
      <div className="router-panel-head">
        <strong>V21触发拆解</strong>
        <span>任一分支进入25%尾部且通过风控才下单</span>
      </div>
      <div className="router-rows">
        {rows.map((row) => (
          <div className="router-row" key={row.branch || row.role}>
            <div className="router-main">
              <span>{String(row.role || "--").toUpperCase()}</span>
              <strong>{routerStatusText(row.status)}</strong>
              <em className={`router-tone ${routerStatusTone(row.status)}`}>{row.signal || row.nearest_signal || "--"}</em>
            </div>
            <div className="router-metrics">
              {row.p_up_pct != null ? <span>p涨 {fmt(row.p_up_pct, 1)}%</span> : null}
              {row.edge_gap_pct != null ? <span>差 {fmt(row.edge_gap_pct, 1)}pp</span> : null}
              {row.sigma_10m_bps != null ? <span>sigma {fmt(row.sigma_10m_bps, 1)}bp</span> : null}
              <span>范围 {fmt(row.sigma_min_bps, 0)}-{fmt(row.sigma_max_bps, 0)}bp</span>
            </div>
            {row.detail ? <p>{row.detail}</p> : null}
          </div>
        ))}
      </div>
    </div>
  );
}

export default function StrategyCard({ title, signal, amount, variant, stats }) {
  const active = !!signal?.signal;
  const live = variant?.enabled !== false && variant?.tradeEnabled !== false;
  const observed = variant?.enabled !== false;
  const cardTone = active ? directionClass(signal.signal) : "neutral";

  return (
    <article className={`strategy-card ${cardTone}`}>
      <header className="strategy-card-head">
        <div>
          <span className="eyebrow">策略</span>
          <h3>{title || variant?.label || signal?.strategy_id || "未命名策略"}</h3>
          <small>{signal?.strategy_id || variant?.id}</small>
        </div>
        <DirectionBadge signal={signal} />
      </header>

      <div className="strategy-summary">
        <strong className={directionClass(signal?.signal)}>{signalLabel(signal)}</strong>
        <span>
          <Clock size={14} />
          信号时间 {displaySignalTime(signal)}
        </span>
      </div>

      <div className="flag-row">
        <Flag tone={observed ? "ok" : "warn"}>{observed ? "已监控" : "未监控"}</Flag>
        <Flag tone={live ? "danger" : "neutral"}>{live ? "实盘可下单" : "仅观察/影子"}</Flag>
        <Flag>{amount || variant?.amount || "--"}U</Flag>
        <Flag>{variant?.duration || signal?.duration || "10"}分钟</Flag>
        {variant?.confirmationVeto ? <Flag tone={variant.confirmationVeto === "none" ? "neutral" : "ok"}>{variant.confirmationVeto}</Flag> : null}
      </div>

      <div className="strategy-footnote">
        <ShieldCheck size={14} />
        <BacktestLine backtest={variant?.backtest} />
      </div>

      <div className="strategy-stats">
        <span>
          <Activity size={14} />
          实盘 {statLine(stats?.real)}
        </span>
        <span>影子 {statLine(stats?.shadow)}</span>
      </div>

      <FeatureGrid signal={signal} />
      <DetailRows signal={signal} />
      <RouterDiagnostics signal={signal} />

      {signal?.reason ? <div className="reason-line">原因：{signal.reason}</div> : null}
      {signal?.error ? <div className="reason-line bad">异常：{signal.error}</div> : null}
      {signal?.actionable_time ? <div className="reason-line">可执行时间：{dateTimeText(signal.actionable_time)}</div> : null}
    </article>
  );
}
