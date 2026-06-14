import React from "react";
import { Plus, Save, Server, Trash2 } from "lucide-react";
import { DEFAULT_CONFIG, fmtPct } from "../utils";

const inputStyle = {
  width: "86px",
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

const selectStyle = { ...inputStyle, width: "136px", textAlign: "left" };

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

function pctInput(value, digits = 2) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "";
  return Number((n * 100).toFixed(digits));
}

function fromPctInput(value, fallback, min, max) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(min, Math.min(max, n / 100));
}

function variantId(base, tailPct, index, lookbackSec = 1800) {
  const lower = Math.round(Number(tailPct || 0.2) * 100);
  if (base === "SAFE") return index === 0 && lower === 20 ? "BTC_10min_SAFE" : `BTC_10min_SAFE_${lower}`;
  if (base === "TAKER") return index === 0 && lower === 20 ? "BTC_10min_TAKER" : `BTC_10min_TAKER_${lower}`;
  if (base === "SECOND_CHIP") return `BTC_10min_SECOND_CHIP_${lookbackSec}${index > 0 ? "_" + index : ""}`;
  return `BTC_10min_SECOND_${lookbackSec}_${lower}${index > 0 ? "_" + index : ""}`;
}

function variantLabel(base, tailPct, lookbackSec = 1800) {
  if (base === "SAFE") return `推荐稳健 ${tailDisplay(tailPct)}`;
  if (base === "TAKER") return `资金流过滤 ${tailDisplay(tailPct)}`;
  if (base === "SECOND_CHIP") return `秒级筹码区 ${Math.round((lookbackSec || 3600) / 60)}m`;
  return `秒级正态 ${lookbackSec}s ${tailDisplay(tailPct)}`;
}

function updateVariant(variants, index, patch) {
  return variants.map((item, i) => {
    if (i !== index) return item;
    const next = { ...item, ...patch };
    if (patch.tailPct !== undefined || patch.lookbackSec !== undefined || !next.id) {
      const sameBaseBefore = variants.slice(0, index).filter(v => v.base === next.base).length;
      next.id = variantId(next.base, next.tailPct, sameBaseBefore, next.lookbackSec);
    }
    next.label = variantLabel(next.base, next.tailPct, next.lookbackSec);
    if (next.base === "SECOND" || next.base === "SECOND_CHIP") {
      next.duration = String(Math.max(1, Math.round(Number(next.horizonSec || 600) / 60)));
    }
    return next;
  });
}

function BacktestBadge({ variant }) {
  const bt = variant.backtest;
  if (!bt) return <span style={{ color: "var(--muted)", fontSize: "10px" }}>无回测参考</span>;
  return (
    <span style={{ color: "var(--muted)", fontSize: "10px" }}>
      回测 {fmtPct(bt.wr, 2)} / {bt.tradesPerDay}笔天 / {bt.trades}笔 / 连亏{bt.maxLoss}
      {bt.sampleHours ? ` / 样本${bt.sampleHours}h` : ""}
    </span>
  );
}

function CommonControls({ variant, onChange }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}>
      <label style={{ display: "grid", gap: "4px", fontSize: "11px", color: "var(--text-2)", fontWeight: "bold" }}>
        投数 USDT
        <input min="1" step="1" type="number" value={variant.amount} onChange={event => onChange({ amount: event.target.value })} style={inputStyle} />
      </label>
      {variant.base === "SECOND_CHIP" ? (
        <label style={{ display: "grid", gap: "4px", fontSize: "11px", color: "var(--text-2)", fontWeight: "bold" }}>
          筹码占比 %
          <input min="1" max="90" step="1" type="number" value={pctInput(variant.chipTargetShare ?? 0.2, 0)} onChange={event => onChange({ chipTargetShare: fromPctInput(event.target.value, 0.2, 0.01, 0.9) })} style={inputStyle} />
        </label>
      ) : (
        <label style={{ display: "grid", gap: "4px", fontSize: "11px", color: "var(--text-2)", fontWeight: "bold" }}>
          阈值 {tailDisplay(variant.tailPct)}
          <input min="5" max="45" step="1" type="number" value={Math.round(Number(variant.tailPct || 0.2) * 100)} onChange={event => onChange({ tailPct: Math.max(5, Math.min(45, Number(event.target.value) || 20)) / 100 })} style={inputStyle} />
        </label>
      )}
    </div>
  );
}

function SecondNormalControls({ variant, onChange }) {
  if (variant.base !== "SECOND") return null;
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}>
      <NumberField label="回看秒数" value={variant.lookbackSec || 1800} min="60" max="21600" step="60" onChange={value => onChange({ lookbackSec: value || 1800 })} />
      <NumberField label="到期秒数" value={variant.horizonSec || 600} min="60" max="7200" step="60" onChange={value => onChange({ horizonSec: value || 600, duration: String(Math.max(1, Math.round((value || 600) / 60))) })} />
      <NumberField label="去重秒数" value={variant.gapSec || 600} min="0" max="21600" step="60" onChange={value => onChange({ gapSec: value || 0 })} />
      <label style={{ display: "grid", gap: "4px", fontSize: "11px", color: "var(--text-2)", fontWeight: "bold" }}>
        过滤器
        <select value={variant.secondFilter || "none"} onChange={event => onChange({ secondFilter: event.target.value })} style={selectStyle}>
          <option value="none">不过滤</option>
          <option value="vol_high">高成交量</option>
          <option value="vol_not_high">避开高成交量</option>
          <option value="flow_align">资金流同向</option>
          <option value="flow_strong_align">强资金流同向</option>
          <option value="flow_align_vol_not_high">资金流同向+避开高量</option>
        </select>
      </label>
    </div>
  );
}

function SecondChipControls({ variant, onChange }) {
  if (variant.base !== "SECOND_CHIP") return null;
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}>
      <NumberField label="回看秒数" value={variant.lookbackSec || 3600} min="60" max="21600" step="60" onChange={value => onChange({ lookbackSec: value || 3600 })} />
      <NumberField label="到期秒数" value={variant.horizonSec || 600} min="60" max="7200" step="60" onChange={value => onChange({ horizonSec: value || 600, duration: String(Math.max(1, Math.round((value || 600) / 60))) })} />
      <NumberField label="去重秒数" value={variant.gapSec || 600} min="0" max="21600" step="60" onChange={value => onChange({ gapSec: value || 0 })} />
      <label style={{ display: "grid", gap: "4px", fontSize: "11px", color: "var(--text-2)", fontWeight: "bold" }}>
        档位模式
        <select value={variant.chipBinMode || "percent"} onChange={event => onChange({ chipBinMode: event.target.value })} style={selectStyle}>
          <option value="fixed">固定U</option>
          <option value="percent">价格百分比</option>
        </select>
      </label>
      {variant.chipBinMode === "fixed" ? (
        <NumberField label="档位大小 U" value={variant.chipBinSize || 20} min="1" max="1000" step="1" onChange={value => onChange({ chipBinSize: value || 20 })} />
      ) : null}
      <label style={{ display: "grid", gap: "4px", fontSize: "11px", color: "var(--text-2)", fontWeight: "bold" }}>
        档位比例 %
        <input min="0.001" max="1" step="0.001" type="number" value={pctInput(variant.chipBinPct ?? 0.0003, 3)} onChange={event => onChange({ chipBinPct: fromPctInput(event.target.value, 0.0003, 0.00001, 0.01) })} style={inputStyle} />
      </label>
      <label style={{ display: "grid", gap: "4px", fontSize: "11px", color: "var(--text-2)", fontWeight: "bold" }}>
        突破比例 %
        <input min="0.01" max="5" step="0.01" type="number" value={pctInput(variant.chipBreakPct ?? 0.0023, 2)} onChange={event => onChange({ chipBreakPct: fromPctInput(event.target.value, 0.0023, 0.0001, 0.05) })} style={inputStyle} />
      </label>
      <label style={{ display: "grid", gap: "4px", fontSize: "11px", color: "var(--text-2)", fontWeight: "bold" }}>
        方向过滤
        <select value={variant.chipDirectionFilter || "breakout_up_only"} onChange={event => onChange({ chipDirectionFilter: event.target.value })} style={selectStyle}>
          <option value="breakout_up_only">只做上破回落</option>
          <option value="breakout_down_only">只做下破回拉</option>
          <option value="all">上下都做</option>
        </select>
      </label>
    </div>
  );
}

function NumberField({ label, value, min, max, step, onChange }) {
  return (
    <label style={{ display: "grid", gap: "4px", fontSize: "11px", color: "var(--text-2)", fontWeight: "bold" }}>
      {label}
      <input min={min} max={max} step={step} type="number" value={value} onChange={event => onChange(Number(event.target.value))} style={inputStyle} />
    </label>
  );
}

function VariantCard({ variant, canDelete, onChange, onDelete }) {
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
      <div style={{ display: "grid", gap: "8px" }}>
        <CommonControls variant={variant} onChange={onChange} />
        <SecondNormalControls variant={variant} onChange={onChange} />
        <SecondChipControls variant={variant} onChange={onChange} />
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}>
          <CompactToggle label="观察并记录" checked={variant.enabled !== false} onChange={() => onChange({ enabled: variant.enabled === false })} />
          <CompactToggle label="实盘执行" checked={variant.tradeEnabled !== false} onChange={() => onChange({ tradeEnabled: variant.tradeEnabled === false })} />
        </div>
      </div>
    </div>
  );
}

function defaultsForBase(base, defaultAmount, index) {
  if (base === "SECOND_CHIP") {
    return {
      id: variantId(base, 0.2, index, 3600),
      base,
      label: variantLabel(base, 0.2, 3600),
      amount: defaultAmount,
      enabled: true,
      tradeEnabled: false,
      duration: "10",
      lookbackSec: 3600,
      horizonSec: 600,
      gapSec: 600,
      chipTargetShare: 0.2,
      chipBinMode: "fixed",
      chipBinSize: 20,
      chipBinPct: 0.0003,
      chipBreakPct: 0.0023,
      chipDirectionFilter: "breakout_up_only"
    };
  }
  const preferred = base === "SAFE" ? [0.22, 0.23, 0.25, 0.27] : base === "TAKER" ? [0.27, 0.23, 0.22, 0.25] : [0.27, 0.2, 0.22, 0.25];
  const tailPct = preferred[index % preferred.length] || 0.2;
  const lookbackSec = base === "SECOND" ? 1800 : undefined;
  return {
    id: variantId(base, tailPct, index, lookbackSec),
    base,
    label: variantLabel(base, tailPct, lookbackSec),
    amount: defaultAmount,
    tailPct,
    duration: "10",
    enabled: true,
    tradeEnabled: base !== "SECOND",
    ...(base === "SECOND" ? { lookbackSec: 1800, horizonSec: 600, gapSec: 600, secondFilter: "none" } : {})
  };
}

function VariantGroup({ title, base, allVariants, setVariants, defaultAmount }) {
  const rows = allVariants.map((item, index) => ({ item, index })).filter(row => row.item.base === base);

  function addVariant() {
    setVariants([...allVariants, defaultsForBase(base, defaultAmount, rows.length)]);
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
        <strong>{draft.realTradingEnabled ? "实盘模式：只有开启“实盘执行”的策略会交给平板下单；其他策略仍记录影子单。" : "影子模式：全部观察策略只记录模拟交易，不消耗真实资金。"}</strong>
      </div>

      <div style={{ border: "1px solid var(--line)", borderRadius: "6px", padding: "10px", background: "rgba(255,255,255,0.01)" }}>
        <div style={{ fontSize: "11px", fontWeight: "bold", color: "var(--muted)", textTransform: "uppercase", letterSpacing: "1px", marginBottom: "8px" }}>运行开关</div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}>
          <div style={{ background: "rgba(255,255,255,0.015)", padding: "6px", borderRadius: "4px", border: "1px solid rgba(255,255,255,0.02)" }}>
            <CompactToggle label="允许策略自动下单" checked={draft.autoTrade_10m} onChange={() => onToggle("autoTrade_10m")} />
          </div>
          <div style={{ background: "rgba(255,255,255,0.015)", padding: "6px", borderRadius: "4px", border: "1px solid rgba(255,255,255,0.02)" }}>
            <CompactToggle label="实盘资金下单" checked={draft.realTradingEnabled} onChange={() => onToggle("realTradingEnabled")} />
          </div>
        </div>
      </div>

      <VariantGroup title="推荐稳健档位" base="SAFE" allVariants={variants} setVariants={setVariants} defaultAmount="5" />
      <VariantGroup title="资金流过滤档位" base="TAKER" allVariants={variants} setVariants={setVariants} defaultAmount="8" />
      <VariantGroup title="秒级正态档位" base="SECOND" allVariants={variants} setVariants={setVariants} defaultAmount="5" />
      <VariantGroup title="秒级筹码区档位" base="SECOND_CHIP" allVariants={variants} setVariants={setVariants} defaultAmount="5" />

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
