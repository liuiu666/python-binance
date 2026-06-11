import React from "react";
import { ArrowDown, ArrowUp, Activity } from "lucide-react";
import { DEFAULT_CONFIG, fmt, PAYOUT } from "../utils";

export default function ManualPanel({ draft, onManualTrade, onAmountPreset }) {
  const amount = Number(draft.amount) || Number(DEFAULT_CONFIG.amount);
  const presets = [5, 10, 20, 50, 100];
  
  return (
    <section className="panel manual-panel" style={{ background: "var(--surface)", border: "1px solid var(--line)", padding: "16px", borderRadius: "6px" }}>
      <header className="panel-header" style={{ marginBottom: "12px", display: "flex", alignItems: "center", gap: "6px" }}>
        <Activity size={15} style={{ color: "var(--yellow)" }} />
        <span>手动快捷下单</span>
      </header>
      
      {/* Quick Presets */}
      <div style={{ display: "flex", gap: "6px", marginBottom: "14px" }}>
        {presets.map(val => (
          <button
            key={val}
            type="button"
            className="preset-btn"
            onClick={() => onAmountPreset(val)}
            style={{
              flex: 1,
              padding: "6px 0",
              borderRadius: "4px",
              border: String(amount) === String(val) ? "1px solid var(--yellow)" : "1px solid var(--line)",
              background: String(amount) === String(val) ? "var(--yellow-soft)" : "rgba(255,255,255,0.02)",
              color: String(amount) === String(val) ? "var(--yellow)" : "var(--text-2)",
              fontSize: "11px",
              fontWeight: "bold",
              transition: "all 0.2s ease"
            }}
          >
            {val}U
          </button>
        ))}
      </div>

      <div className="manual-grid">
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
      </div>
    </section>
  );
}
