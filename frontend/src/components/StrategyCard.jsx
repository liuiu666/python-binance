import React from "react";
import { directionClass, signalLabel } from "../utils";

export default function StrategyCard({ title, signal, amount }) {
  const signalClass = directionClass(signal?.signal);
  const duration = signal?.duration || signal?.interval_min || "--";
  const hasSignal = !!signal?.signal;
  
  let cardBorder = "1px solid var(--line)";
  let cardShadow = "none";
  let pulseDot = null;

  if (hasSignal) {
    if (signal.signal === "UP") {
      cardBorder = "1px solid rgba(39, 195, 165, 0.4)";
      cardShadow = "0 0 15px rgba(39, 195, 165, 0.15)";
      pulseDot = <span className="pulse-dot green">●</span>;
    } else if (signal.signal === "DOWN") {
      cardBorder = "1px solid rgba(228, 88, 88, 0.4)";
      cardShadow = "0 0 15px rgba(228, 88, 88, 0.15)";
      pulseDot = <span className="pulse-dot red">●</span>;
    }
  }

  return (
    <div className="strategy-card" style={{
      border: cardBorder,
      boxShadow: cardShadow,
      padding: "16px",
      borderRadius: "6px",
      background: "var(--surface)",
      position: "relative",
      overflow: "hidden",
      flex: 1
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "12px" }}>
        <span style={{ fontSize: "14px", fontWeight: "bold", color: "var(--text-2)" }}>{title} 策略</span>
        <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
          {pulseDot}
          <span className={`status-pill ${signalClass}`} style={{ fontSize: "11px", fontWeight: "800" }}>
            {signalLabel(signal)}
          </span>
        </div>
      </div>
      <div className="strategy-meta" style={{ display: "flex", gap: "10px" }}>
        <span className="badge" style={{ background: "rgba(255,255,255,0.04)", padding: "4px 8px", borderRadius: "4px", fontSize: "11px", color: "var(--text-2)" }}>
          💰 投数: {amount || "--"}U
        </span>
        <span className="badge" style={{ background: "rgba(255,255,255,0.04)", padding: "4px 8px", borderRadius: "4px", fontSize: "11px", color: "var(--text-2)" }}>
          ⏱️ 周期: {duration}m
        </span>
        <span className="badge" style={{ background: "rgba(255,255,255,0.04)", padding: "4px 8px", borderRadius: "4px", fontSize: "11px", color: "var(--text-2)" }}>
          📊 RSI: {signal?.rsi_value !== undefined ? Number(signal.rsi_value).toFixed(0) : "--"}
        </span>
      </div>
    </div>
  );
}
