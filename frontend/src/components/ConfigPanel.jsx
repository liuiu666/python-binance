import React from "react";
import { Plus, Save, Server, Trash2 } from "lucide-react";
import { DEFAULT_CONFIG, fmtPct } from "../utils";

const inputStyle = {
  width: "76px",
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

function variantId(base, tailPct, index) {
  const lower = Math.round(Number(tailPct || 0.2) * 100);
  if (base === "SAFE") return index === 0 && lower === 20 ? "BTC_10min_SAFE" : `BTC_10min_SAFE_${lower}`;
  return index === 0 && lower === 20 ? "BTC_10min_TAKER" : `BTC_10min_TAKER_${lower}`;
}

function variantLabel(base, tailPct) {
  return base === "SAFE" ? `推荐稳健 ${tailDisplay(tailPct)}` : `资金流过滤 ${tailDisplay(tailPct)}`;
}

function updateVariant(variants, index, patch) {
  return variants.map((item, i) => {
    if (i !== index) return item;
    const next = { ...item, ...patch };
    if (patch.tailPct !== undefined || !next.id) {
      const sameBaseBefore = variants.slice(0, index).filter(v => v.base === next.base).length;
      next.id = variantId(next.base, next.tailPct, sameBaseBefore);
    }
    next.label = variantLabel(next.base, next.tailPct);
    return next;
  });
}

function BacktestBadge({ variant }) {
  const bt = variant.backtest;
  if (!bt) return <span style={{ color: "var(--muted)", fontSize: "10px" }}>无回测参考</span>;
  return (
    <span style={{ color: "var(--muted)", fontSize: "10px" }}>
      回测 {fmtPct(bt.wr, 2)} | {bt.tradesPerDay}笔/天 | {bt.trades}笔 | 连亏{bt.maxLoss}
    </span>
  );
}

function VariantCard({ variant, canDelete, onChange, onDelete }) {
  const lower = Math.round(Number(variant.tailPct || 0.2) * 100);
  return (
    <div style={{ border: "1px solid var(--line)", borderRadius: "6px", padding: "10px", background: "rgba(255,255,255,0.015)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: "10px", marginBottom: "8px" }}>
        <div>
          <div style={{ fontSize: "12px", fontWeight: "bold", color: "var(--text)" }}>{variant.label}</div>
          <BacktestBadge variant={variant} />
        </div>
        {canDelete ? (
          <button className="icon-button" type="button" onClick={onDelete} title="删除档位" style={{ width: "28px", height: "28px" }}>
            <Trash2 size={14} />
          </button>
        ) : null}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}>
        <label style={{ display: "grid", gap: "4px", fontSize: "11px", color: "var(--text-2)", fontWeight: "bold" }}>
          投数 USDT
          <input min="1" step="1" type="number" value={variant.amount} onChange={event => onChange({ amount: event.target.value })} style={inputStyle} />
        </label>
        <label style={{ display: "grid", gap: "4px", fontSize: "11px", color: "var(--text-2)", fontWeight: "bold" }}>
          阈值 {tailDisplay(variant.tailPct)}
          <input
            min="5"
            max="45"
            step="1"
            type="number"
            value={lower}
            onChange={event => onChange({ tailPct: Math.max(5, Math.min(45, Number(event.target.value) || 20)) / 100 })}
            style={inputStyle}
          />
        </label>
      </div>
    </div>
  );
}

function VariantGroup({ title, base, variants, allVariants, setVariants, defaultAmount }) {
  const rows = allVariants.map((item, index) => ({ item, index })).filter(row => row.item.base === base);

  function addVariant() {
    const existing = rows.map(row => row.item);
    const preferred = base === "SAFE" ? [0.22, 0.23, 0.25, 0.27] : [0.27, 0.23, 0.22, 0.25];
    const tailPct = preferred.find(p => !existing.some(item => Math.round(item.tailPct * 100) === Math.round(p * 100))) || 0.2;
    const index = existing.length;
    setVariants([
      ...allVariants,
      {
        id: variantId(base, tailPct, index),
        base,
        label: variantLabel(base, tailPct),
        amount: defaultAmount,
        tailPct,
        enabled: true
      }
    ]);
  }

  return (
    <div style={{ border: "1px solid var(--line)", borderRadius: "6px", padding: "10px", background: "rgba(255,255,255,0.01)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "8px" }}>
        <div style={{ fontSize: "11px", fontWeight: "bold", color: "var(--muted)", textTransform: "uppercase", letterSpacing: "1px" }}>{title}</div>
        <button className="primary-button" type="button" onClick={addVariant} style={{ height: "28px", padding: "0 8px", fontSize: "11px", borderRadius: "4px" }}>
          <Plus size={13} /> 添加
        </button>
      </div>
      <div style={{ display: "grid", gap: "10px" }}>
        {rows.map(({ item, index }) => (
          <VariantCard
            key={item.id}
            variant={item}
            canDelete={rows.length > 1}
            onChange={patch => setVariants(updateVariant(allVariants, index, patch))}
            onDelete={() => setVariants(allVariants.filter((_, i) => i !== index))}
          />
        ))}
      </div>
    </div>
  );
}

export default function ConfigPanel({ draft, dirty, apiToken, onTokenChange, onDraftChange, onToggle, onSave }) {
  const variants = draft.strategyVariants || DEFAULT_CONFIG.strategyVariants;
  const setVariants = next => onDraftChange({ strategyVariants: next });

  return (
    <section className="panel config-panel" style={{ display: "grid", gap: "14px", padding: "14px" }}>
      <header className="panel-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "center", paddingBottom: "8px", borderBottom: "1px solid var(--line)", margin: 0, minHeight: "auto" }}>
        <span style={{ display: "flex", alignItems: "center", gap: "6px" }}>
          <Server size={15} style={{ color: "var(--green)" }} /> 策略控制
        </span>
        <em className={dirty ? "dirty" : "synced"} style={{ fontSize: "11px" }}>{dirty ? "未保存" : "已同步"}</em>
      </header>

      <div style={{ padding: "10px", borderRadius: "6px", fontSize: "12px", lineHeight: "1.4", border: draft.realTradingEnabled ? "1px solid rgba(228, 88, 88, 0.3)" : "1px solid rgba(39, 195, 165, 0.2)", background: draft.realTradingEnabled ? "var(--red-soft)" : "var(--green-soft)", color: draft.realTradingEnabled ? "var(--red)" : "var(--green)" }}>
        <strong>{draft.realTradingEnabled ? "实盘模式：信号触发后由平板执行真实下单。" : "影子模式：只记录模拟交易，不消耗真实资金。"}</strong>
      </div>

      <div style={{ border: "1px solid var(--line)", borderRadius: "6px", padding: "10px", background: "rgba(255,255,255,0.01)" }}>
        <div style={{ fontSize: "11px", fontWeight: "bold", color: "var(--muted)", textTransform: "uppercase", letterSpacing: "1px", marginBottom: "8px" }}>运行开关</div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}>
          <div style={{ background: "rgba(255,255,255,0.015)", padding: "6px", borderRadius: "4px", border: "1px solid rgba(255,255,255,0.02)" }}>
            <CompactToggle label="全部 10m 策略" checked={draft.autoTrade_10m} onChange={() => onToggle("autoTrade_10m")} />
          </div>
          <div style={{ background: "rgba(255,255,255,0.015)", padding: "6px", borderRadius: "4px", border: "1px solid rgba(255,255,255,0.02)" }}>
            <CompactToggle label="实盘资金下单" checked={draft.realTradingEnabled} onChange={() => onToggle("realTradingEnabled")} />
          </div>
        </div>
      </div>

      <VariantGroup title="推荐稳健档位" base="SAFE" variants={variants} allVariants={variants} setVariants={setVariants} defaultAmount="3" />
      <VariantGroup title="资金流过滤档位" base="TAKER" variants={variants} allVariants={variants} setVariants={setVariants} defaultAmount="8" />

      <label style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "10px", padding: "8px 4px", fontSize: "11px", color: "var(--text-2)", fontWeight: "bold" }}>
        安全网密钥
        <input className="token-input" type="password" value={apiToken} onChange={event => onTokenChange(event.target.value)} placeholder="无限制" style={{ ...inputStyle, width: "120px" }} />
      </label>

      <button className="primary-button" type="button" onClick={onSave} style={{ padding: "10px", borderRadius: "4px", border: "none", background: "var(--green)", color: "#0d1117", fontWeight: "bold", fontSize: "13px", display: "flex", alignItems: "center", justifyContent: "center", gap: "6px", width: "100%", marginTop: "4px" }}>
        <Save size={14} /> 保存并下发
      </button>
    </section>
  );
}
