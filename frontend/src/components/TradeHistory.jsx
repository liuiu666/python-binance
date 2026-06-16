import React, { useMemo, useState } from "react";
import { directionClass, directionText, fmt, fmtPct, fmtPrice, statusClass, statusText, strategyName, timeParts, pnlText } from "../utils";

function getExecutionLabel(row) {
  const source = String(row.source || "");
  const isTablet = source === "autojs";
  const isShadow = source.startsWith("shadow:") || source === "shadow" || row.event === "shadow_trade";
  const isManual = !row.strategyId || row.strategyId === "manual";

  if (row.status === "unverified") return { text: "未成交", color: "var(--yellow)", bg: "var(--yellow-soft)" };
  if (isShadow) return { text: "影子模拟", color: "var(--muted)", bg: "rgba(255,255,255,0.04)" };
  if (isManual) {
    if (isTablet) return { text: "平板手动", color: "var(--violet)", bg: "var(--violet-soft)" };
    return { text: "网页手动", color: "var(--yellow)", bg: "var(--yellow-soft)" };
  }
  if (isTablet) return { text: "信号实盘", color: "var(--green)", bg: "var(--green-soft)" };
  return { text: "服务器模拟", color: "var(--muted)", bg: "rgba(255,255,255,0.04)" };
}

function rowKind(row) {
  const source = String(row.source || "");
  return source.startsWith("shadow:") || source === "shadow" || row.event === "shadow_trade" ? "shadow" : "real";
}

function StatBlock({ title, data }) {
  const pnl = Number(data?.pnl || 0);
  return (
    <div className="history-summary">
      <span>{title}</span>
      <span>胜率 <strong>{fmtPct(data?.winRate, 1)}</strong></span>
      <span>盈亏 <strong className={pnl > 0 ? "up" : pnl < 0 ? "down" : ""}>{pnl > 0 ? "+" : ""}{fmt(pnl, 2)}U</strong></span>
      <span>赢/亏 <strong>{data?.wins || 0}/{data?.losses || 0}</strong></span>
      <span>持仓 <strong>{data?.pending || 0}</strong></span>
    </div>
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
        const exe = getExecutionLabel(row);
        return (
          <div className={`history-row ${cls}`} key={row.id || `${row.openTime}-${row.strategyId}-${row.direction}`}>
            <div className="history-status"><span className={`status-pill ${cls}`}>{statusText(row.status)}</span></div>
            <div className="history-main">
              <div className="history-strategy">
                <span className={`dir-pill ${dir}`}>{directionText(row.direction)}</span>
                <strong>{strategyName(row.strategyId)}</strong>
                <span style={{
                  fontSize: "9px",
                  padding: "1px 6px",
                  borderRadius: "3px",
                  fontWeight: "bold",
                  color: exe.color,
                  background: exe.bg,
                  border: `1px solid ${exe.color}40`,
                  whiteSpace: "nowrap"
                }}>
                  {exe.text}
                </span>
              </div>
              <small>强度 {row.confidence !== undefined && row.confidence !== null ? fmtPct(row.confidence, 0) : "--"} | RSI {row.rsi_value !== undefined && row.rsi_value !== null ? Number(row.rsi_value).toFixed(0) : "--"}</small>
              <small className="strategy-id-mini">{row.strategyId || "manual"}</small>
            </div>
            <div className="history-amount"><strong>{fmt(row.amount, 0)}U</strong><small>{row.duration || "--"} 分钟</small></div>
            <div className="history-time history-detail"><small>{tp.date}</small><strong>{tp.time}</strong></div>
            <div className="history-price history-detail"><span>开 {fmtPrice(row.openPrice)}</span><span>收 {row.closePrice !== null && row.closePrice !== undefined ? fmtPrice(row.closePrice) : "待到期"}</span></div>
            <div className={`history-pnl ${cls}`}>{pnlText(row)}</div>
          </div>
        );
      })}
    </div>
  );
}

export default function TradeHistory({ history }) {
  const [tab, setTab] = useState("real");
  const summary = history?.summary || {};
  const realSummary = summary.real || {};
  const shadowSummary = summary.shadow || {};
  const active = history?.active || [];
  const recent = history?.recent || [];
  const view = tab === "shadow"
    ? { title: "影子单", kind: "shadow", summary: shadowSummary }
    : { title: "真实单", kind: "real", summary: realSummary };
  const filteredActive = useMemo(() => active.filter(row => rowKind(row) === view.kind), [active, view.kind]);
  const filteredRecent = useMemo(() => recent.filter(row => rowKind(row) === view.kind), [recent, view.kind]);

  return (
    <section className="history-section">
      <header className="history-header">
        <div>
          <h2>订单记录 <span>{summary.total ?? recent.length}</span></h2>
          <p>真实单和影子模拟单分开查看，列表按开仓时间倒序</p>
        </div>
        <div style={{ display: "grid", gap: "8px" }}>
          <StatBlock title="真实单" data={realSummary} />
          <StatBlock title="影子单" data={shadowSummary} />
        </div>
      </header>
      <div className="history-tabs">
        <button className={tab === "real" ? "active" : ""} type="button" onClick={() => setTab("real")}>
          真实单 <span>{realSummary.total || 0}</span>
        </button>
        <button className={tab === "shadow" ? "active" : ""} type="button" onClick={() => setTab("shadow")}>
          影子单 <span>{shadowSummary.total || 0}</span>
        </button>
      </div>
      <div className="active-ledger">
        <div className="ledger-title">{view.title}持仓 <span>{view.summary.pending || 0}</span></div>
        <TradeRows rows={filteredActive} emptyText={`暂无${view.title}持仓`} />
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
        <TradeRows rows={filteredRecent} emptyText={`暂无${view.title}历史订单`} />
      </div>
    </section>
  );
}
