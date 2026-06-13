import React from "react";
import { ArrowDown, ArrowUp, Minus } from "lucide-react";
import { directionClass, directionText, fmtPct, signalLabel } from "../utils";

const baseBadgeStyle = {
  background: "rgba(255,255,255,0.04)",
  padding: "4px 8px",
  borderRadius: "4px",
  fontSize: "11px",
  color: "var(--text-2)"
};

function tailText(variant, signal) {
  const tail = Number(variant?.tailPct ?? signal?.tail_pct ?? 0.2);
  const lower = Math.round(tail * 100);
  return `${lower}/${100 - lower}`;
}

function probabilityText(signal) {
  const rawUp = signal?.p_up ?? signal?.avg_prob;
  const pUp = Number(rawUp);
  if (!Number.isFinite(pUp)) return { up: "--", down: "--", edge: "--", zone: "等待数据" };
  const up = Math.max(0, Math.min(1, pUp > 1 ? pUp / 100 : pUp));
  const tailPct = Number(signal?.tail_pct ?? 0.2);
  const upperTrigger = 1 - tailPct;
  const lowerGap = Math.max(0, up - tailPct);
  const upperGap = Math.max(0, upperTrigger - up);
  return {
    up: `${(up * 100).toFixed(1)}%`,
    down: `${((1 - up) * 100).toFixed(1)}%`,
    edge: signal?.signal ? "已进极端" : `${(Math.min(lowerGap, upperGap) * 100).toFixed(1)}%`,
    zone: up <= tailPct ? "极端下行" : up >= upperTrigger ? "极端上行" : "等待极端区间"
  };
}

function headlineText(signal, confidence) {
  if (!signal?.signal) return signalLabel(signal);
  if ((signal?.mode || "reversal") === "reversal") {
    if (signal.signal === "UP") return `极端下行，反转看涨 ${confidence}`;
    if (signal.signal === "DOWN") return `极端上行，反转看跌 ${confidence}`;
  }
  return `${directionText(signal.signal)} ${confidence}`;
}

function takerFlowText(signal) {
  const filter = String(signal?.taker_filter || "none").toLowerCase();
  if (!filter || filter === "none" || filter === "off" || filter === "false") return null;
  if (signal?.taker_data_ok === false) return { text: "资金流延迟", tone: "warn" };
  const ratio = Number(signal?.taker_ratio);
  const ratioText = Number.isFinite(ratio) ? ` ${ratio.toFixed(2)}` : "";
  const bias = signal?.taker_flow_bias || "unknown";
  const biasText = { bullish: "偏多", bearish: "偏空", neutral: "中性", unknown: "未知" }[bias] || "未知";
  if (signal?.blocked_signal && signal?.reason === "taker_not_aligned") {
    return { text: `资金流未对齐 ${biasText}${ratioText}`, tone: "warn" };
  }
  if (signal?.signal && signal?.taker_filter_ok) {
    return { text: `资金流已对齐 ${biasText}${ratioText}`, tone: bias === "bearish" ? "down" : "up" };
  }
  return { text: `资金流 ${biasText}${ratioText}`, tone: bias === "bullish" ? "up" : bias === "bearish" ? "down" : "neutral" };
}

function badgeStyle(tone) {
  if (tone === "up") return { ...baseBadgeStyle, background: "var(--green-soft)", color: "var(--green)" };
  if (tone === "down") return { ...baseBadgeStyle, background: "var(--red-soft)", color: "var(--red)" };
  if (tone === "warn") return { ...baseBadgeStyle, background: "rgba(230, 181, 74, 0.14)", color: "#e6b54a" };
  return baseBadgeStyle;
}

function DirectionBadge({ signal }) {
  const dir = signal?.signal;
  const cls = directionClass(dir);
  const Icon = dir === "UP" ? ArrowUp : dir === "DOWN" ? ArrowDown : Minus;
  return (
    <span className={`status-pill ${cls}`} style={{ display: "inline-flex", alignItems: "center", gap: "5px", minWidth: "68px", justifyContent: "center", fontSize: "12px", fontWeight: "900" }}>
      <Icon size={14} />
      {dir ? directionText(dir) : "等待"}
    </span>
  );
}

export default function StrategyCard({ title, signal, amount, variant }) {
  const direction = signal?.signal;
  const duration = signal?.duration || signal?.interval_min || "10";
  const confidence = signal?.confidence !== undefined && signal?.confidence !== null ? fmtPct(signal.confidence, 0) : "--";
  const probability = probabilityText(signal);
  const takerFlow = takerFlowText(signal);
  const headline = headlineText(signal, confidence);
  const backtest = variant?.backtest;

  let cardBorder = "1px solid var(--line)";
  let cardShadow = "none";
  if (direction === "UP") {
    cardBorder = "1px solid rgba(39, 195, 165, 0.45)";
    cardShadow = "0 0 15px rgba(39, 195, 165, 0.15)";
  } else if (direction === "DOWN") {
    cardBorder = "1px solid rgba(228, 88, 88, 0.45)";
    cardShadow = "0 0 15px rgba(228, 88, 88, 0.15)";
  }

  return (
    <div className="strategy-card" style={{ border: cardBorder, boxShadow: cardShadow, padding: "16px", borderRadius: "6px", background: "var(--surface)", position: "relative", overflow: "hidden", flex: "1 1 320px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "12px", marginBottom: "12px" }}>
        <span style={{ fontSize: "14px", fontWeight: "bold", color: "var(--text-2)" }}>{title}</span>
        <DirectionBadge signal={signal} />
      </div>
      <div style={{ display: "grid", gap: "8px" }}>
        <strong className={directionClass(direction)} style={{ fontSize: "18px", lineHeight: 1.1 }}>{headline}</strong>
        <div className="strategy-meta" style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
          <span className="badge" style={baseBadgeStyle}>投数 {amount || "--"}U</span>
          <span className="badge" style={baseBadgeStyle}>周期 {duration}m</span>
          <span className="badge" style={baseBadgeStyle}>阈值 {tailText(variant, signal)}</span>
          {backtest ? <span className="badge" style={baseBadgeStyle}>回测 {fmtPct(backtest.wr, 2)} / {backtest.tradesPerDay}笔天</span> : null}
          <span className="badge" style={baseBadgeStyle}>策略 正态尾部反转</span>
          <span className="badge" style={{ ...baseBadgeStyle, background: "var(--green-soft)", color: "var(--green)" }}>正态自然上行 {probability.up}</span>
          <span className="badge" style={{ ...baseBadgeStyle, background: "var(--red-soft)", color: "var(--red)" }}>正态自然下行 {probability.down}</span>
          <span className="badge" style={baseBadgeStyle}>反转触发差距 {probability.edge}</span>
          <span className="badge" style={baseBadgeStyle}>{probability.zone}</span>
          {takerFlow ? <span className="badge" style={badgeStyle(takerFlow.tone)}>{takerFlow.text}</span> : null}
        </div>
      </div>
    </div>
  );
}
