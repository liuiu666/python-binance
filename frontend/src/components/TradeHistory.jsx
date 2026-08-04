import { useEffect, useMemo, useState } from "react";
import {
  directionClass,
  fmt,
  fmtPct,
  fmtPrice,
  statusClass,
  strategyName,
  timeParts,
  tradeKind
} from "../utils";

const STATUS_TEXT = {
  won: "赢",
  lost: "亏",
  tie: "平",
  pending: "持仓",
  aborted: "取消",
  unverified: "未成交"
};

const DIRECTION_TEXT = {
  UP: "看涨",
  DOWN: "看跌"
};

function moneyText(value, fallback = "未记录") {
  const amount = Number(value);
  if (!Number.isFinite(amount) || amount < 0) return fallback;
  return `${fmt(amount, Number.isInteger(amount) ? 0 : 2)}U`;
}

function openAmount(row) {
  return row?.openAmount ?? row?.amount;
}

function settleAmount(row) {
  if (row?.settleAmount !== undefined && row?.settleAmount !== null) return row.settleAmount;
  if (!["won", "lost", "tie"].includes(row?.status)) return null;
  const amount = Number(openAmount(row));
  const payoutRate = Number(row?.payoutRate);
  if (!Number.isFinite(amount) || amount <= 0) return null;
  if (row.status === "won") return amount + amount * (Number.isFinite(payoutRate) ? payoutRate : 0.8);
  if (row.status === "lost") return 0;
  return amount;
}

function pnlDisplay(row) {
  if (!row) return "--";
  if (row.status === "unverified") return "未扣款";
  if (row.status === "pending") return "待结算";
  if (row.status === "aborted") return "已取消";
  const pnl = Number(row.pnl || 0);
  return `${pnl > 0 ? "+" : ""}${fmt(pnl, 2)}U`;
}

function StatBlock({ title, data }) {
  const pnl = Number(data?.pnl || 0);
  return (
    <div className="stat-block">
      <span>{title}</span>
      <strong>{fmtPct(data?.winRate, 1)}</strong>
      <small>
        {data?.wins || 0}赢 / {data?.losses || 0}亏 / {pnl > 0 ? "+" : ""}{fmt(pnl, 2)}U
      </small>
    </div>
  );
}

function TradeDetail({ row, onClose }) {
  if (!row) return null;
  const time = timeParts(row.openTime);
  const settle = timeParts(row.settleTime);
  const hasLlmLog = Boolean(row.llm_prompt || row.llm_response);
  return (
    <div className="trade-detail-backdrop" role="presentation" onMouseDown={onClose}>
      <section className="trade-detail" role="dialog" aria-modal="true" aria-label="订单详情" onMouseDown={event => event.stopPropagation()}>
        <header>
          <div>
            <span className="eyebrow">订单详情</span>
            <h3>{strategyName(row.strategyId)} · {DIRECTION_TEXT[row.direction] || "等待"}</h3>
          </div>
          <button type="button" onClick={onClose}>关闭</button>
        </header>
        <div className="trade-detail-grid">
          <div><span>状态</span><strong>{STATUS_TEXT[row.status] || row.status || "等待"}</strong></div>
          <div><span>模型</span><strong>{row.llm_model || "非 LLM 订单"}</strong></div>
          <div><span>下单时间</span><strong>{time.date} {time.time}</strong></div>
          <div><span>到期时间</span><strong>{settle.date} {settle.time}</strong></div>
          <div><span>金额</span><strong>{moneyText(openAmount(row))}</strong></div>
          <div><span>价格</span><strong>{fmtPrice(row.openPrice)} {"->"} {fmtPrice(row.closePrice)}</strong></div>
        </div>
        {row.llm_decision_id ? <p className="trade-detail-id">决策 ID：{row.llm_decision_id}</p> : null}
        {hasLlmLog ? (
          <div className="trade-llm-log">
            <details open>
              <summary>LLM 请求数据（实际提示词）</summary>
              <pre>{row.llm_prompt || "未记录请求原文"}</pre>
            </details>
            <details open>
              <summary>LLM 原始回复</summary>
              <pre>{row.llm_response || "未记录回复原文"}</pre>
            </details>
          </div>
        ) : (
          <div className="empty-state compact">该订单生成时尚未启用 LLM 输入/回复日志。</div>
        )}
      </section>
    </div>
  );
}

function TradeRow({ row, onSelect }) {
  const cls = statusClass(row.status);
  const time = timeParts(row.openTime);
  const settle = timeParts(row.settleTime);
  const kind = tradeKind(row);
  const reasons = [
    row.reason,
    row.decisionReason && row.decisionReason !== row.reason ? row.decisionReason : null,
    row.amountReason
  ].filter(Boolean);

  const openDetail = () => onSelect?.(row);
  return (
    <article
      className={`trade-row ${cls}`}
      role="button"
      tabIndex={0}
      onClick={openDetail}
      onKeyDown={event => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openDetail();
        }
      }}
    >
      <div className="trade-result">
        <span className={`status-pill ${cls}`}>{STATUS_TEXT[row.status] || "等待"}</span>
        <strong className={directionClass(row.direction)}>{DIRECTION_TEXT[row.direction] || "等待"}</strong>
      </div>

      <div className="trade-main">
        <strong>{strategyName(row.strategyId)}</strong>
        <small>{row.strategyId || "manual"}</small>
        {reasons.length ? <p>{reasons.join("；")}</p> : null}
      </div>

      <div className="trade-cell trade-amount">
        <span>{kind === "shadow" ? "影子下单金额" : "真实下单金额"}</span>
        <strong>{moneyText(openAmount(row))}</strong>
      </div>

      <div className="trade-cell trade-return">
        <span>到期金额</span>
        <strong>{moneyText(settleAmount(row), row.status === "pending" ? "待结算" : "--")}</strong>
      </div>

      <div className="trade-cell trade-open">
        <span>下单</span>
        <strong>{time.time}</strong>
        <small>{time.date}</small>
      </div>

      <div className="trade-cell trade-settle">
        <span>到期</span>
        <strong>{settle.time}</strong>
        <small>{row.duration || "--"}分钟</small>
      </div>

      <div className="trade-cell trade-price price-cell">
        <span>价格</span>
        <strong>{fmtPrice(row.openPrice)} {"->"} {fmtPrice(row.closePrice)}</strong>
      </div>

      <div className={`trade-pnl ${cls}`}>{pnlDisplay(row)}</div>
    </article>
  );
}

function Breakdown({ title, rows }) {
  const list = (rows || []).slice(0, 8);
  if (!list.length) return <div className="empty-state compact">暂无{title}</div>;
  return (
    <div className="breakdown-list">
      <h3>{title}</h3>
      {list.map(item => {
        const pnl = Number(item.pnl || 0);
        return (
          <div key={item.key}>
            <span>{title === "策略胜率" ? strategyName(item.key) : item.label || item.key}</span>
            <strong>{fmtPct(item.winRate, 1)}</strong>
            <small className={pnl > 0 ? "up" : pnl < 0 ? "down" : ""}>
              {pnl > 0 ? "+" : ""}{fmt(pnl, 2)}U
            </small>
          </div>
        );
      })}
    </div>
  );
}

function todayKey() {
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).formatToParts(new Date());
  const map = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return `${map.year}-${map.month}-${map.day}`;
}

export default function TradeHistory({ history, pageState, onPageChange }) {
  const [kind, setKind] = useState(pageState?.kind || "real");
  const [selectedOrder, setSelectedOrder] = useState(null);
  const summary = history?.summary || {};
  const breakdown = history?.breakdown || {};
  const recent = history?.recent || [];
  const active = history?.active || [];
  const pagination = history?.pagination || {};
  const day = pagination.day || pageState?.day || todayKey();
  const availableDays = pagination.availableDays || [];
  const viewBreakdown = kind === "shadow" ? breakdown.shadow : kind === "all" ? breakdown.combined : breakdown.real;
  const viewSummary = kind === "shadow" ? summary.shadow : kind === "all" ? summary.combined || summary : summary.real;

  useEffect(() => {
    if (pageState?.kind && pageState.kind !== kind) setKind(pageState.kind);
  }, [kind, pageState?.kind]);

  const activeRows = useMemo(() => {
    if (kind === "all") return active;
    return active.filter(row => tradeKind(row) === kind);
  }, [active, kind]);

  const changeKind = nextKind => {
    setKind(nextKind);
    onPageChange?.({ kind: nextKind, mode: "day" });
  };

  const changeDay = nextDay => {
    if (!nextDay) return;
    onPageChange?.({ day: nextDay, mode: "day" });
  };

  return (
    <section className="history-section">
      <header className="section-head">
        <div>
          <span className="eyebrow">记录</span>
          <h2>下单记录</h2>
        </div>
        <div className="history-tabs">
          {["real", "shadow", "all"].map(item => (
            <button className={kind === item ? "active" : ""} type="button" key={item} onClick={() => changeKind(item)}>
              {item === "real" ? "真实单" : item === "shadow" ? "影子单" : "全部"}
            </button>
          ))}
        </div>
      </header>

      <div className="stats-grid">
        <StatBlock title="当前日期" data={viewSummary} />
        <StatBlock title="真实单" data={summary.real} />
        <StatBlock title="影子单" data={summary.shadow} />
      </div>

      <div className="history-layout">
        <div className="history-main-list">
          <div className="sub-head">
            <h3>持仓中</h3>
            <span>{activeRows.length} 单</span>
          </div>
          {activeRows.length ? activeRows.map(row => <TradeRow row={row} onSelect={setSelectedOrder} key={row.id || `${row.openTime}-${row.strategyId}`} />) : (
            <div className="empty-state compact">当前没有持仓</div>
          )}

          <div className="sub-head history-day-head">
            <div>
              <h3>历史记录</h3>
              <span>{day}，共 {pagination.total || recent.length || 0} 单</span>
            </div>
            <div className="day-pager">
              <button type="button" onClick={() => changeDay(pagination.prevDay)}>上一天</button>
              <input
                type="date"
                value={day}
                list="trade-history-days"
                onChange={event => changeDay(event.target.value)}
              />
              <datalist id="trade-history-days">
                {availableDays.map(item => <option value={item} key={item} />)}
              </datalist>
              <button type="button" disabled={!pagination.hasNext} onClick={() => changeDay(pagination.nextDay)}>下一天</button>
              <button type="button" onClick={() => changeDay(todayKey())}>今天</button>
            </div>
          </div>

          {recent.length ? recent.map(row => <TradeRow row={row} onSelect={setSelectedOrder} key={row.id || `${row.openTime}-${row.strategyId}`} />) : (
            <div className="empty-state">这一天暂无历史记录</div>
          )}
        </div>

        <aside className="history-side">
          <Breakdown title="策略胜率" rows={viewBreakdown?.byStrategy} />
          <Breakdown title="日期胜率" rows={viewBreakdown?.byDay} />
        </aside>
      </div>
      <TradeDetail row={selectedOrder} onClose={() => setSelectedOrder(null)} />
    </section>
  );
}
