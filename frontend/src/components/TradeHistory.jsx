import React from "react";
import { ArrowDown, ArrowUp } from "lucide-react";
import { directionClass, directionText, fmt, fmtPct, fmtPrice, statusClass, statusText, timeParts, pnlText } from "../utils";

function getExecutionLabel(row) {
  const isTablet = row.source === "autojs";
  const isManual = !row.strategyId || row.strategyId === "manual";
  
  if (isManual) {
    if (isTablet) return { text: "平板手动", color: "var(--violet)", bg: "var(--violet-soft)" };
    return { text: "网页手动", color: "var(--yellow)", bg: "var(--yellow-soft)" };
  } else {
    if (isTablet) return { text: "信号实盘", color: "var(--green)", bg: "var(--green-soft)" };
    return { text: "影子模拟", color: "var(--muted)", bg: "rgba(255,255,255,0.04)" };
  }
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
            <div><span className={`status-pill ${cls}`}>{statusText(row.status)}</span></div>
            <div className="history-main">
              <div className="history-strategy" style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <span className={`dir-pill ${dir}`}>{directionText(row.direction)}</span>
                <strong>{row.strategyId ? row.strategyId.replace("BTC_", "") : "手动"}</strong>
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

export default function TradeHistory({ history }) {
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
        <TradeRows rows={active} emptyText="暂无持仓订单" />
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
