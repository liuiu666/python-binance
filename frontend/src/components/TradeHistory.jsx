import React, { useMemo, useState } from "react";
import { directionClass, directionText, fmt, fmtPct, fmtPrice, statusClass, statusText, strategyName, timeParts, pnlText } from "../utils";

const PAGE_SIZE = 5;

function getExecutionLabel(row) {
  const source = String(row.source || "");
  const isTablet = source === "autojs";
  const isShadow = source.startsWith("shadow:") || source === "shadow" || row.event === "shadow_trade";
  const isManual = !row.strategyId || row.strategyId === "manual";

  if (row.status === "unverified") return { text: "未成交", color: "var(--yellow)", bg: "var(--yellow-soft)" };
  if (isShadow) return { text: "影子模拟", color: "var(--muted)", bg: "rgba(255,255,255,0.04)" };
  if (isManual) return { text: isTablet ? "平板手动" : "网页手动", color: "var(--yellow)", bg: "var(--yellow-soft)" };
  if (isTablet) return { text: "信号实盘", color: "var(--green)", bg: "var(--green-soft)" };
  return { text: "服务模拟", color: "var(--muted)", bg: "rgba(255,255,255,0.04)" };
}

function rowKind(row) {
  const source = String(row.source || "");
  return source.startsWith("shadow:") || source === "shadow" || row.event === "shadow_trade" ? "shadow" : "real";
}

function dayKey(row) {
  if (!row.openTime) return "unknown";
  const d = new Date(Number(row.openTime));
  if (Number.isNaN(d.getTime())) return "unknown";
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).formatToParts(d);
  const map = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return `${map.year}-${map.month}-${map.day}`;
}

function StatBlock({ title, data }) {
  const pnl = Number(data?.pnl || 0);
  return (
    <div className="history-summary">
      <span>{title}</span>
      <span>总胜率 <strong>{fmtPct(data?.winRate, 1)}</strong></span>
      <span>盈亏 <strong className={pnl > 0 ? "up" : pnl < 0 ? "down" : ""}>{pnl > 0 ? "+" : ""}{fmt(pnl, 2)}U</strong></span>
      <span>胜/负 <strong>{data?.wins || 0}/{data?.losses || 0}</strong></span>
      <span>持仓 <strong>{data?.pending || 0}</strong></span>
    </div>
  );
}

function MiniStats({ title, rows, emptyText }) {
  if (!rows?.length) return <div className="empty-state compact">{emptyText}</div>;
  return (
    <div className="mini-stats">
      <div className="mini-stats-title">{title}</div>
      {rows.map(row => {
        const pnl = Number(row.pnl || 0);
        return (
          <div className="mini-stat-row" key={row.key}>
            <strong>{title === "策略胜率" ? strategyName(row.key) : row.label || row.key}</strong>
            <span>{fmtPct(row.winRate, 1)}</span>
            <span>{row.wins || 0}/{row.losses || 0}</span>
            <span className={pnl > 0 ? "up" : pnl < 0 ? "down" : ""}>{pnl > 0 ? "+" : ""}{fmt(pnl, 2)}U</span>
          </div>
        );
      })}
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
                <span className="exec-pill" style={{ color: exe.color, background: exe.bg, borderColor: `${exe.color}40` }}>
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

function DayPager({ days, page, setPage }) {
  if (days.length <= PAGE_SIZE) return null;
  const totalPages = Math.ceil(days.length / PAGE_SIZE);
  return (
    <div className="day-pager">
      <button type="button" disabled={page <= 0} onClick={() => setPage(page - 1)}>上一页</button>
      <span>{page + 1} / {totalPages}</span>
      <button type="button" disabled={page >= totalPages - 1} onClick={() => setPage(page + 1)}>下一页</button>
    </div>
  );
}

export default function TradeHistory({ history }) {
  const [tab, setTab] = useState("real");
  const [dayPage, setDayPage] = useState(0);
  const summary = history?.summary || {};
  const breakdown = history?.breakdown || {};
  const active = history?.active || [];
  const recent = history?.recent || [];
  const view = tab === "shadow"
    ? { title: "影子单", kind: "shadow", summary: summary.shadow || {}, breakdown: breakdown.shadow || {} }
    : { title: "真实单", kind: "real", summary: summary.real || {}, breakdown: breakdown.real || {} };

  const filteredActive = useMemo(() => active.filter(row => rowKind(row) === view.kind), [active, view.kind]);
  const filteredRecent = useMemo(() => recent.filter(row => rowKind(row) === view.kind), [recent, view.kind]);
  const days = useMemo(() => {
    const grouped = new Map();
    for (const row of filteredRecent) {
      const key = dayKey(row);
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key).push(row);
    }
    return Array.from(grouped.entries())
      .map(([key, rows]) => ({ key, rows, stat: view.breakdown.byDay?.find(item => item.key === key) }))
      .sort((a, b) => b.key.localeCompare(a.key));
  }, [filteredRecent, view.breakdown.byDay]);
  const maxPage = Math.max(0, Math.ceil(days.length / PAGE_SIZE) - 1);
  const page = Math.min(dayPage, maxPage);
  const pageDays = days.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE);

  return (
    <section className="history-section">
      <header className="history-header">
        <div>
          <h2>订单记录 <span>{summary.total ?? recent.length}</span></h2>
          <p>总胜率、策略胜率、按天胜率分开统计；真实单和影子单独立查看</p>
        </div>
        <div className="history-summary-stack">
          <StatBlock title="真实单" data={summary.real || {}} />
          <StatBlock title="影子单" data={summary.shadow || {}} />
        </div>
      </header>

      <div className="history-tabs">
        <button className={tab === "real" ? "active" : ""} type="button" onClick={() => { setTab("real"); setDayPage(0); }}>
          真实单<span>{summary.real?.total || 0}</span>
        </button>
        <button className={tab === "shadow" ? "active" : ""} type="button" onClick={() => { setTab("shadow"); setDayPage(0); }}>
          影子单<span>{summary.shadow?.total || 0}</span>
        </button>
      </div>

      <div className="history-stats-grid">
        <StatBlock title={`${view.title}总计`} data={view.summary} />
        <MiniStats title="策略胜率" rows={view.breakdown.byStrategy || []} emptyText="暂无策略统计" />
        <MiniStats title="天胜率" rows={view.breakdown.byDay || []} emptyText="暂无按天统计" />
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
          <span>下单时间</span>
          <span>价格</span>
          <span>盈亏</span>
        </div>
        {pageDays.length ? pageDays.map(day => {
          const pnl = Number(day.stat?.pnl || 0);
          return (
            <div className="day-group" key={day.key}>
              <div className="day-group-head">
                <strong>{day.key}</strong>
                <span>天胜率 {fmtPct(day.stat?.winRate, 1)}</span>
                <span>胜/负 {day.stat?.wins || 0}/{day.stat?.losses || 0}</span>
                <span className={pnl > 0 ? "up" : pnl < 0 ? "down" : ""}>{pnl > 0 ? "+" : ""}{fmt(pnl, 2)}U</span>
              </div>
              <TradeRows rows={day.rows} emptyText={`暂无${view.title}历史订单`} />
            </div>
          );
        }) : <TradeRows rows={[]} emptyText={`暂无${view.title}历史订单`} />}
        <DayPager days={days} page={page} setPage={setDayPage} />
      </div>
    </section>
  );
}
