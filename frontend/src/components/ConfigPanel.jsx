import React from "react";
import { Save, Server } from "lucide-react";
import { DEFAULT_CONFIG, fmtPct } from "../utils";

const inputStyle = {
  width: "100%",
  height: "28px",
  background: "#0d1117",
  border: "1px solid var(--line)",
  color: "var(--text)",
  borderRadius: "4px",
  padding: "2px 6px",
  textAlign: "right",
  fontSize: "12px",
  outline: "none"
};

function CompactToggle({ label, checked, onChange }) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "4px 2px" }}>
      <span style={{ fontSize: "11px", color: "var(--text-2)", fontWeight: "bold" }}>{label}</span>
      <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
        <span style={{ fontSize: "11px", fontWeight: "bold", color: checked ? "var(--green)" : "var(--muted)" }}>
          {checked ? "已开启" : "已关闭"}
        </span>
        <button className={`slide-switch ${checked ? "on" : "off"}`} type="button" onClick={onChange} />
      </div>
    </div>
  );
}

function tailDisplay(tailPct) {
  const lower = Math.round(Number(tailPct || 0.2) * 100);
  return `${lower}/${100 - lower}`;
}

function NumberField({ label, value, min, max, step, onChange, suffix }) {
  return (
    <label style={{ display: "grid", gap: "4px", fontSize: "11px", color: "var(--text-2)", fontWeight: "bold" }}>
      {label}
      <div style={{ display: "grid", gridTemplateColumns: suffix ? "1fr auto" : "1fr", alignItems: "center", gap: "4px" }}>
        <input min={min} max={max} step={step} type="number" value={value} onChange={event => onChange(Number(event.target.value))} style={inputStyle} />
        {suffix ? <span style={{ color: "var(--muted)", fontSize: "10px" }}>{suffix}</span> : null}
      </div>
    </label>
  );
}

function updateVariant(variants, index, patch) {
  return variants.map((item, i) => {
    if (i !== index) return item;
    const next = { ...item, ...patch, base: "SECOND_VW_CONFIRM" };
    if (patch.horizonSec !== undefined) next.duration = String(Math.max(1, Math.round(Number(next.horizonSec || 600) / 60)));
    return next;
  });
}

function BacktestBadge({ variant }) {
  const bt = variant.backtest;
  if (!bt) return <span style={{ color: "var(--muted)", fontSize: "10px" }}>回测参数已改变</span>;
  return (
    <span style={{ color: "var(--muted)", fontSize: "10px" }}>
      回测 {fmtPct(bt.wr, 2)} / {bt.tradesPerDay}笔天 / {bt.trades}笔 / 连亏{bt.maxLoss}
      {bt.sampleHours ? ` / 样本${bt.sampleHours}h` : ""}
    </span>
  );
}

function StrategyConfigCard({ variant, index, onChange }) {
  return (
    <div style={{ border: "1px solid var(--line)", borderRadius: "6px", padding: "10px", background: "rgba(255,255,255,0.015)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: "10px", marginBottom: "8px" }}>
        <div>
          <div style={{ fontSize: "12px", fontWeight: "bold", color: "var(--text)" }}>{variant.label}</div>
          <BacktestBadge variant={variant} />
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}>
        <NumberField label="投数" value={variant.amount || 5} min="1" max="999" step="1" suffix="U" onChange={value => onChange({ amount: String(value || 5) })} />
        <NumberField label={`正态阈值 ${tailDisplay(variant.tailPct)}`} value={Math.round(Number(variant.tailPct || 0.2) * 100)} min="5" max="45" step="1" suffix="%" onChange={value => onChange({ tailPct: Math.max(5, Math.min(45, value || 20)) / 100 })} />
        <NumberField label="回看秒数" value={variant.lookbackSec || 2700} min="60" max="21600" step="60" onChange={value => onChange({ lookbackSec: value || 2700 })} />
        <NumberField label="到期秒数" value={variant.horizonSec || 600} min="60" max="7200" step="60" onChange={value => onChange({ horizonSec: value || 600 })} />
        <NumberField label="同策略锁" value={variant.gapSec || 600} min="0" max="21600" step="60" suffix="秒" onChange={value => onChange({ gapSec: value || 0 })} />
        <NumberField label="ETA目标" value={variant.etaTargetBps || (index === 0 ? 2 : 3)} min="0.1" max="20" step="0.1" suffix="bp" onChange={value => onChange({ etaTargetBps: value || (index === 0 ? 2 : 3) })} />
        <NumberField label="ETA等待" value={variant.etaMaxWaitSec || 45} min="1" max="600" step="1" suffix="秒" onChange={value => onChange({ etaMaxWaitSec: value || 45 })} />
        <div style={{ display: "grid", gap: "4px" }}>
          <CompactToggle label="观察并记录" checked={variant.enabled !== false} onChange={() => onChange({ enabled: variant.enabled === false })} />
          <CompactToggle label="实盘执行" checked={variant.tradeEnabled !== false} onChange={() => onChange({ tradeEnabled: variant.tradeEnabled === false })} />
        </div>
      </div>
    </div>
  );
}

export default function ConfigPanel({ draft, dirty, apiToken, onTokenChange, onDraftChange, onToggle, onSave }) {
  const variants = (draft.strategyVariants || DEFAULT_CONFIG.strategyVariants).filter(item => item.base === "SECOND_VW_CONFIRM");
  const rows = variants.length ? variants : DEFAULT_CONFIG.strategyVariants;
  const setVariant = (index, patch) => onDraftChange({ strategyVariants: updateVariant(rows, index, patch) });

  return (
    <section className="panel config-panel" style={{ display: "grid", gap: "14px", padding: "14px" }}>
      <header className="panel-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "center", paddingBottom: "8px", borderBottom: "1px solid var(--line)", margin: 0, minHeight: "auto" }}>
        <span style={{ display: "flex", alignItems: "center", gap: "6px" }}>
          <Server size={15} style={{ color: "var(--green)" }} /> 策略控制
        </span>
        <em className={dirty ? "dirty" : "synced"} style={{ fontSize: "11px" }}>{dirty ? "未保存" : "已同步"}</em>
      </header>

      <div style={{ padding: "10px", borderRadius: "6px", fontSize: "12px", lineHeight: "1.4", border: draft.realTradingEnabled ? "1px solid rgba(228, 88, 88, 0.3)" : "1px solid rgba(39, 195, 165, 0.2)", background: draft.realTradingEnabled ? "var(--red-soft)" : "var(--green-soft)", color: draft.realTradingEnabled ? "var(--red)" : "var(--green)" }}>
        <strong>{draft.realTradingEnabled ? "实盘模式：只有开启实盘执行的策略会交给平板下单，其他策略只记录。" : "影子模式：策略只记录模拟交易，不消耗真实资金。"}</strong>
      </div>

      <div style={{ border: "1px solid var(--line)", borderRadius: "6px", padding: "10px", background: "rgba(255,255,255,0.01)" }}>
        <div style={{ fontSize: "11px", fontWeight: "bold", color: "var(--muted)", textTransform: "uppercase", letterSpacing: "1px", marginBottom: "8px" }}>运行开关</div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}>
          <CompactToggle label="允许策略自动下单" checked={draft.autoTrade_10m} onChange={() => onToggle("autoTrade_10m")} />
          <CompactToggle label="实盘资金下单" checked={draft.realTradingEnabled} onChange={() => onToggle("realTradingEnabled")} />
        </div>
      </div>

      <div style={{ border: "1px solid var(--line)", borderRadius: "6px", padding: "10px", background: "rgba(255,255,255,0.01)", display: "grid", gap: "10px" }}>
        <div style={{ fontSize: "11px", fontWeight: "bold", color: "var(--muted)", textTransform: "uppercase", letterSpacing: "1px" }}>正态 + 成交量确认 + ETA</div>
        {rows.map((item, index) => (
          <StrategyConfigCard key={item.id} variant={item} index={index} onChange={patch => setVariant(index, patch)} />
        ))}
      </div>

      <label style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "10px", padding: "8px 4px", fontSize: "11px", color: "var(--text-2)", fontWeight: "bold" }}>
        安全密钥
        <input className="token-input" type="password" value={apiToken} onChange={event => onTokenChange(event.target.value)} placeholder="无限制" style={{ ...inputStyle, width: "120px" }} />
      </label>

      <button className="primary-button" type="button" onClick={onSave} style={{ padding: "10px", borderRadius: "4px", border: "none", background: "var(--green)", color: "#0d1117", fontWeight: "bold", fontSize: "13px", display: "flex", alignItems: "center", justifyContent: "center", gap: "6px", width: "100%", marginTop: "4px" }}>
        <Save size={14} /> 保存并下发
      </button>
    </section>
  );
}
