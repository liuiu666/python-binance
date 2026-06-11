import React from "react";
import { Save, Server } from "lucide-react";
import { DEFAULT_CONFIG } from "../utils";

function CompactToggle({ label, checked, onChange }) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "4px 2px" }}>
      <span style={{ fontSize: "11px", color: "var(--text-2)", fontWeight: "bold" }}>{label}</span>
      <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
        <span
          style={{
            fontSize: "11px",
            fontWeight: "bold",
            color: checked ? "var(--green)" : "var(--muted)",
            transition: "all 0.2s"
          }}
        >
          {checked ? "已开启" : "已关闭"}
        </span>
        <button className={`slide-switch ${checked ? "on" : "off"}`} type="button" onClick={onChange} />
      </div>
    </div>
  );
}

function CompactFormRow({ label, children }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: "10px",
        padding: "8px 4px",
        borderBottom: "1px solid rgba(255,255,255,0.02)"
      }}
    >
      <span style={{ fontSize: "11px", color: "var(--text-2)", fontWeight: "bold" }}>{label}</span>
      {children}
    </div>
  );
}

const inputStyle = {
  width: "80px",
  height: "26px",
  background: "#0d1117",
  border: "1px solid var(--line)",
  color: "var(--text)",
  borderRadius: "4px",
  padding: "2px 6px",
  textAlign: "right",
  fontSize: "12px",
  outline: "none"
};

export default function ConfigPanel({
  draft,
  dirty,
  apiToken,
  onTokenChange,
  onDraftChange,
  onToggle,
  onSave
}) {
  const strategyAmounts = draft.strategyAmounts || DEFAULT_CONFIG.strategyAmounts;

  return (
    <section className="panel config-panel" style={{ display: "grid", gap: "14px", padding: "14px" }}>
      <header
        className="panel-header"
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          paddingBottom: "8px",
          borderBottom: "1px solid var(--line)",
          margin: 0,
          minHeight: "auto"
        }}
      >
        <span style={{ display: "flex", alignItems: "center", gap: "6px" }}>
          <Server size={15} style={{ color: "var(--green)" }} /> 策略控制
        </span>
        <em className={dirty ? "dirty" : "synced"} style={{ fontSize: "11px" }}>
          {dirty ? "未保存" : "已同步"}
        </em>
      </header>

      <div
        style={{
          padding: "10px",
          borderRadius: "6px",
          fontSize: "12px",
          lineHeight: "1.4",
          border: draft.realTradingEnabled ? "1px solid rgba(228, 88, 88, 0.3)" : "1px solid rgba(39, 195, 165, 0.2)",
          background: draft.realTradingEnabled ? "var(--red-soft)" : "var(--green-soft)",
          color: draft.realTradingEnabled ? "var(--red)" : "var(--green)"
        }}
      >
        <strong>
          {draft.realTradingEnabled
            ? "实盘模式：信号触发后由平板执行真实下单。"
            : "影子模式：只记录模拟交易，不消耗真实资金。"}
        </strong>
      </div>

      <div style={{ border: "1px solid var(--line)", borderRadius: "6px", padding: "10px", background: "rgba(255,255,255,0.01)" }}>
        <div style={{ fontSize: "11px", fontWeight: "bold", color: "var(--muted)", textTransform: "uppercase", letterSpacing: "1px", marginBottom: "8px" }}>
          运行开关
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}>
          <div style={{ background: "rgba(255,255,255,0.015)", padding: "6px", borderRadius: "4px", border: "1px solid rgba(255,255,255,0.02)" }}>
            <CompactToggle label="两套 10m 策略" checked={draft.autoTrade_10m} onChange={() => onToggle("autoTrade_10m")} />
          </div>
          <div style={{ background: "rgba(255,255,255,0.015)", padding: "6px", borderRadius: "4px", border: "1px solid rgba(255,255,255,0.02)" }}>
            <CompactToggle label="实盘资金下单" checked={draft.realTradingEnabled} onChange={() => onToggle("realTradingEnabled")} />
          </div>
          <div style={{ background: "rgba(255,255,255,0.015)", padding: "6px", borderRadius: "4px", border: "1px solid rgba(255,255,255,0.02)" }}>
            <CompactToggle label="影子模拟记录" checked={draft.shadowTradingEnabled} onChange={() => onToggle("shadowTradingEnabled")} />
          </div>
        </div>
      </div>

      <div style={{ border: "1px solid var(--line)", borderRadius: "6px", padding: "10px", background: "rgba(255,255,255,0.01)" }}>
        <div style={{ fontSize: "11px", fontWeight: "bold", color: "var(--muted)", textTransform: "uppercase", letterSpacing: "1px", marginBottom: "8px" }}>
          两套策略投数
        </div>
        <div style={{ display: "grid", gap: "2px" }}>
          <CompactFormRow label="推荐稳健 (USDT)">
            <input
              min="1"
              step="1"
              type="number"
              value={strategyAmounts.BTC_10min_SAFE || DEFAULT_CONFIG.strategyAmounts.BTC_10min_SAFE}
              onChange={event =>
                onDraftChange({
                  strategyAmounts: {
                    ...strategyAmounts,
                    BTC_10min_SAFE: event.target.value
                  }
                })
              }
              style={inputStyle}
            />
          </CompactFormRow>
          <CompactFormRow label="资金流过滤 (USDT)">
            <input
              min="1"
              step="1"
              type="number"
              value={strategyAmounts.BTC_10min_TAKER || DEFAULT_CONFIG.strategyAmounts.BTC_10min_TAKER}
              onChange={event =>
                onDraftChange({
                  strategyAmounts: {
                    ...strategyAmounts,
                    BTC_10min_TAKER: event.target.value
                  }
                })
              }
              style={inputStyle}
            />
          </CompactFormRow>
          <CompactFormRow label="最低执行强度 (%)">
            <input
              min="0"
              max="100"
              step="1"
              type="number"
              value={draft.minConfidence}
              onChange={event => onDraftChange({ minConfidence: Number(event.target.value) })}
              style={inputStyle}
            />
          </CompactFormRow>
        </div>
      </div>

      <CompactFormRow label="安全网密钥">
        <input
          className="token-input"
          type="password"
          value={apiToken}
          onChange={event => onTokenChange(event.target.value)}
          placeholder="无限制"
          style={{
            width: "120px",
            height: "26px",
            textAlign: "right",
            background: "#0d1117",
            border: "1px solid var(--line)",
            color: "var(--text)",
            borderRadius: "4px",
            padding: "0 6px",
            fontSize: "12px",
            outline: "none"
          }}
        />
      </CompactFormRow>

      <button
        className="primary-button"
        type="button"
        onClick={onSave}
        style={{
          padding: "10px",
          borderRadius: "4px",
          border: "none",
          background: "var(--green)",
          color: "#0d1117",
          fontWeight: "bold",
          fontSize: "13px",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          gap: "6px",
          width: "100%",
          marginTop: "4px"
        }}
      >
        <Save size={14} /> 保存并下发
      </button>
    </section>
  );
}
