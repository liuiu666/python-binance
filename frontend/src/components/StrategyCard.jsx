import React from "react";
import { ArrowDown, ArrowUp, Minus } from "lucide-react";
import { directionClass, directionText, fmtPct, signalLabel } from "../utils";

function probabilityText(signal) {
  const rawUp = signal?.p_up ?? signal?.avg_prob;
  const pUp = Number(rawUp);
  if (!Number.isFinite(pUp)) return { up: "--", down: "--" };
  const normalized = pUp > 1 ? pUp / 100 : pUp;
  const up = Math.max(0, Math.min(1, normalized));
  return {
    up: `${(up * 100).toFixed(1)}%`,
    down: `${((1 - up) * 100).toFixed(1)}%`
  };
}

function DirectionBadge({ signal }) {
  const dir = signal?.signal;
  const cls = directionClass(dir);
  const Icon = dir === "UP" ? ArrowUp : dir === "DOWN" ? ArrowDown : Minus;
  const text = dir ? directionText(dir) : "等待";

  return (
    <span
      className={`status-pill ${cls}`}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "5px",
        minWidth: "68px",
        justifyContent: "center",
        fontSize: "12px",
        fontWeight: "900"
      }}
    >
      <Icon size={14} />
      {text}
    </span>
  );
}

export default function StrategyCard({ title, signal, amount }) {
  const hasSignal = !!signal?.signal;
  const direction = signal?.signal;
  const duration = signal?.duration || signal?.interval_min || "10";
  const confidence = signal?.confidence !== undefined && signal?.confidence !== null ? fmtPct(signal.confidence, 0) : "--";
  const probability = probabilityText(signal);

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
    <div
      className="strategy-card"
      style={{
        border: cardBorder,
        boxShadow: cardShadow,
        padding: "16px",
        borderRadius: "6px",
        background: "var(--surface)",
        position: "relative",
        overflow: "hidden",
        flex: 1
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "12px", marginBottom: "12px" }}>
        <span style={{ fontSize: "14px", fontWeight: "bold", color: "var(--text-2)" }}>{title}</span>
        <DirectionBadge signal={signal} />
      </div>

      <div style={{ display: "grid", gap: "8px" }}>
        <strong className={directionClass(direction)} style={{ fontSize: "18px", lineHeight: 1.1 }}>
          {hasSignal ? `${directionText(direction)} ${confidence}` : signalLabel(signal)}
        </strong>
        <div className="strategy-meta" style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
          <span className="badge" style={{ background: "rgba(255,255,255,0.04)", padding: "4px 8px", borderRadius: "4px", fontSize: "11px", color: "var(--text-2)" }}>
            投数 {amount || "--"}U
          </span>
          <span className="badge" style={{ background: "rgba(255,255,255,0.04)", padding: "4px 8px", borderRadius: "4px", fontSize: "11px", color: "var(--text-2)" }}>
            周期 {duration}m
          </span>
          <span className="badge" style={{ background: "rgba(255,255,255,0.04)", padding: "4px 8px", borderRadius: "4px", fontSize: "11px", color: "var(--text-2)" }}>
            强度 {confidence}
          </span>
          <span className="badge" style={{ background: "var(--green-soft)", padding: "4px 8px", borderRadius: "4px", fontSize: "11px", color: "var(--green)" }}>
            上涨 {probability.up}
          </span>
          <span className="badge" style={{ background: "var(--red-soft)", padding: "4px 8px", borderRadius: "4px", fontSize: "11px", color: "var(--red)" }}>
            下跌 {probability.down}
          </span>
        </div>
      </div>
    </div>
  );
}
