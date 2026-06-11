import React from "react";
import { Plus, Save, Server, Trash2 } from "lucide-react";
import { DEFAULT_CONFIG, toTierList, toTierLabel } from "../utils";

function CompactToggle({ label, checked, onChange }) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "4px 2px" }}>
      <span style={{ fontSize: "11px", color: "var(--text-2)", fontWeight: "bold" }}>{label}</span>
      <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
        <span style={{ 
          fontSize: "11px", 
          fontWeight: "bold", 
          color: checked ? "var(--green)" : "var(--muted)", 
          transition: "all 0.2s" 
        }}>
          {checked ? "已开启" : "已关闭"}
        </span>
        <button 
          className={`slide-switch ${checked ? "on" : "off"}`} 
          type="button" 
          onClick={onChange} 
        />
      </div>
    </div>
  );
}

function CompactFormRow({ label, children }) {
  return (
    <div style={{ 
      display: "flex", 
      alignItems: "center", 
      justifyContent: "space-between", 
      padding: "8px 4px", 
      borderBottom: "1px solid rgba(255,255,255,0.02)" 
    }}>
      <span style={{ fontSize: "11px", color: "var(--text-2)", fontWeight: "bold" }}>{label}</span>
      {children}
    </div>
  );
}

export default function ConfigPanel({
  draft,
  dirty,
  apiToken,
  onTokenChange,
  onDraftChange,
  onToggle,
  onTierChange,
  onAddTier,
  onRemoveTier,
  onSave
}) {
  const tierRules = draft.tiersEnabled
    ? toTierLabel(draft.tiers, draft.amount)
    : `固定 ${draft.amount || DEFAULT_CONFIG.amount}U`;

  return (
    <section className="panel config-panel" style={{ display: "grid", gap: "14px", padding: "14px" }}>
      <header className="panel-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "center", paddingBottom: "8px", borderBottom: "1px solid var(--line)", margin: 0, minHeight: "auto" }}>
        <span style={{ display: "flex", alignItems: "center", gap: "6px" }}><Server size={15} style={{ color: "var(--green)" }} /> 交易策略控制中枢</span>
        <em className={dirty ? "dirty" : "synced"} style={{ fontSize: "11px" }}>{dirty ? "未保存" : "已同步"}</em>
      </header>

      {/* 状态看板 */}
      <div style={{
        padding: "10px",
        borderRadius: "6px",
        fontSize: "12px",
        lineHeight: "1.4",
        border: draft.realTradingEnabled ? "1px solid rgba(228, 88, 88, 0.3)" : "1px solid rgba(39, 195, 165, 0.2)",
        background: draft.realTradingEnabled ? "var(--red-soft)" : "var(--green-soft)",
        color: draft.realTradingEnabled ? "var(--red)" : "var(--green)"
      }}>
        {draft.realTradingEnabled ? (
          <strong>⚠️ 生产实盘模式：信号产生后，将立刻触发平板消耗【真实资金】执行交易下单！</strong>
        ) : (
          <strong>ℹ️ 影子调试模式：平板下单已被安全截断，后台自动进行【模拟资金】交易及收益记录。</strong>
        )}
      </div>

      {/* 板块 1：核心开关矩阵 */}
      <div style={{ border: "1px solid var(--line)", borderRadius: "6px", padding: "10px", background: "rgba(255,255,255,0.01)" }}>
        <div style={{ fontSize: "11px", fontWeight: "bold", color: "var(--muted)", textTransform: "uppercase", letterSpacing: "1px", marginBottom: "8px" }}>
          ⚙️ 核心交易开关
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}>
          <div style={{ background: "rgba(255,255,255,0.015)", padding: "6px", borderRadius: "4px", border: "1px solid rgba(255,255,255,0.02)" }}>
            <CompactToggle label="10m 自动交易" checked={draft.autoTrade_10m} onChange={() => onToggle("autoTrade_10m")} />
          </div>
          <div style={{ background: "rgba(255,255,255,0.015)", padding: "6px", borderRadius: "4px", border: "1px solid rgba(255,255,255,0.02)" }}>
            <CompactToggle label="30m 自动交易" checked={draft.autoTrade_30m} onChange={() => onToggle("autoTrade_30m")} />
          </div>
          <div style={{ background: "rgba(255,255,255,0.015)", padding: "6px", borderRadius: "4px", border: "1px solid rgba(255,255,255,0.02)" }}>
            <CompactToggle label="实盘资金下单" checked={draft.realTradingEnabled} onChange={() => onToggle("realTradingEnabled")} />
          </div>
          <div style={{ background: "rgba(255,255,255,0.015)", padding: "6px", borderRadius: "4px", border: "1px solid rgba(255,255,255,0.02)" }}>
            <CompactToggle label="影子模拟运行" checked={draft.shadowTradingEnabled} onChange={() => onToggle("shadowTradingEnabled")} />
          </div>
        </div>
      </div>

      {/* 板块 2：风控阀值设定 */}
      <div style={{ border: "1px solid var(--line)", borderRadius: "6px", padding: "10px", background: "rgba(255,255,255,0.01)" }}>
        <div style={{ fontSize: "11px", fontWeight: "bold", color: "var(--muted)", textTransform: "uppercase", letterSpacing: "1px", marginBottom: "8px" }}>
          📏 风控阀值设定
        </div>
        <div style={{ display: "grid", gap: "2px" }}>
          <CompactFormRow label="默认下单金额 (USDT)">
            <input
              min="1"
              step="1"
              type="number"
              value={draft.amount}
              onChange={event => onDraftChange({ amount: event.target.value })}
              style={{ width: "80px", height: "26px", background: "#0d1117", border: "1px solid var(--line)", color: "var(--text)", borderRadius: "4px", padding: "2px 6px", textAlign: "right", fontSize: "12px", outline: "none" }}
            />
          </CompactFormRow>
          <CompactFormRow label="最低信号强度 (%)">
            <input
              min="0"
              max="100"
              step="1"
              type="number"
              value={draft.minConfidence}
              onChange={event => onDraftChange({ minConfidence: Number(event.target.value) })}
              style={{ width: "80px", height: "26px", background: "#0d1117", border: "1px solid var(--line)", color: "var(--text)", borderRadius: "4px", padding: "2px 6px", textAlign: "right", fontSize: "12px", outline: "none" }}
            />
          </CompactFormRow>
          <CompactFormRow label="最长排队延迟 (ms)">
            <input
              min="5000"
              max="600000"
              step="5000"
              type="number"
              value={draft.maxActionableLagMs || 60000}
              onChange={event => onDraftChange({ maxActionableLagMs: Number(event.target.value) })}
              style={{ width: "80px", height: "26px", background: "#0d1117", border: "1px solid var(--line)", color: "var(--text)", borderRadius: "4px", padding: "2px 6px", textAlign: "right", fontSize: "12px", outline: "none" }}
            />
          </CompactFormRow>
        </div>
      </div>

      {/* 板块 3：队列与冲突管理 */}
      <div style={{ border: "1px solid var(--line)", borderRadius: "6px", padding: "10px", background: "rgba(255,255,255,0.01)" }}>
        <div style={{ fontSize: "11px", fontWeight: "bold", color: "var(--muted)", textTransform: "uppercase", letterSpacing: "1px", marginBottom: "8px" }}>
          🔀 高频队列与过滤规则
        </div>
        <div style={{ display: "grid", gap: "2px" }}>
          <div style={{ background: "rgba(255,255,255,0.015)", padding: "4px", borderRadius: "4px", border: "1px solid rgba(255,255,255,0.01)", marginBottom: "4px" }}>
            <CompactToggle label="双周期信号冲突过滤" checked={draft.skipConflictSignals} onChange={() => onToggle("skipConflictSignals")} />
          </div>
          <div style={{ background: "rgba(255,255,255,0.015)", padding: "4px", borderRadius: "4px", border: "1px solid rgba(255,255,255,0.01)", marginBottom: "4px" }}>
            <CompactToggle label="同方向未结算不重录" checked={draft.preventOverlapOrders !== false} onChange={() => onToggle("preventOverlapOrders")} />
          </div>
          <CompactFormRow label="排队执单优先顺序">
            <select 
              value={draft.queueOrderPolicy || "confidence_desc"} 
              onChange={event => onDraftChange({ queueOrderPolicy: event.target.value })}
              style={{ width: "110px", height: "26px", background: "#0d1117", border: "1px solid var(--line)", color: "var(--text)", fontSize: "11px", borderRadius: "4px", outline: "none", padding: "0 4px" }}
            >
              <option value="confidence_desc">高强度优先</option>
              <option value="30_then_10">30分 &gt; 10分</option>
              <option value="10_then_30">10分 &gt; 30分</option>
            </select>
          </CompactFormRow>
        </div>
      </div>

      {/* 板块 4：多级分档资金分配 */}
      <div style={{ border: "1px solid var(--line)", borderRadius: "6px", padding: "10px", background: "rgba(255,255,255,0.01)" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "6px" }}>
          <span style={{ fontSize: "11px", fontWeight: "bold", color: "var(--muted)", textTransform: "uppercase", letterSpacing: "1px" }}>💎 多级置信度资金</span>
          <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <span style={{ 
              fontSize: "11px", 
              fontWeight: "bold", 
              color: draft.tiersEnabled ? "var(--green)" : "var(--muted)", 
              transition: "all 0.2s" 
            }}>
              {draft.tiersEnabled ? "已开启" : "已关闭"}
            </span>
            <button 
              className={`slide-switch ${draft.tiersEnabled ? "on" : "off"}`} 
              type="button" 
              onClick={() => onToggle("tiersEnabled")}
            />
          </div>
        </div>
        
        {draft.tiersEnabled ? (
          <div className="tiers-panel" style={{ marginTop: "10px" }}>
            <div className="tiers-head" style={{ display: "grid", gridTemplateColumns: "1fr 1fr 30px", gap: "6px", color: "var(--muted)", fontSize: "10px", fontWeight: "bold", marginBottom: "4px" }}>
              <span>最低强度阀值 (%)</span>
              <span>下单金额 (U)</span>
              <span />
            </div>
            <div style={{ display: "grid", gap: "6px" }}>
              {toTierList(draft.tiers).map((tier, index) => (
                <div className="tier-row" key={`${tier.min}-${index}`} style={{ display: "grid", gridTemplateColumns: "1fr 1fr 30px", gap: "6px" }}>
                  <input
                    min="0"
                    max="100"
                    step="1"
                    type="number"
                    value={tier.min}
                    onChange={event => onTierChange(index, { min: Number(event.target.value) })}
                    style={{ width: "100%", height: "24px", textAlign: "center", background: "#0d1117", border: "1px solid var(--line)", color: "var(--text)", fontSize: "11px", borderRadius: "4px" }}
                  />
                  <input
                    min="1"
                    step="1"
                    type="number"
                    value={tier.amount}
                    onChange={event => onTierChange(index, { amount: Number(event.target.value) })}
                    style={{ width: "100%", height: "24px", textAlign: "center", background: "#0d1117", border: "1px solid var(--line)", color: "var(--text)", fontSize: "11px", borderRadius: "4px" }}
                  />
                  <button 
                    className="icon-button danger" 
                    type="button" 
                    onClick={() => onRemoveTier(index)} 
                    title="删除档位"
                    style={{ padding: "4px", display: "flex", alignItems: "center", justifyContent: "center", height: "24px" }}
                  >
                    <Trash2 size={12} />
                  </button>
                </div>
              ))}
            </div>
            <button 
              className="secondary-button" 
              type="button" 
              onClick={onAddTier}
              style={{ width: "100%", padding: "6px 0", border: "1px dashed var(--line)", background: "transparent", color: "var(--text-2)", fontSize: "11px", marginTop: "8px", borderRadius: "4px", display: "flex", alignItems: "center", justifyContent: "center", gap: "4px" }}
            >
              <Plus size={12} /> 增加资金分配档位
            </button>
          </div>
        ) : (
          <div style={{ fontSize: "11px", color: "var(--muted)", lineHeight: "1.4", padding: "6px", background: "rgba(255,255,255,0.01)", borderRadius: "4px" }}>
            💡 目前统一执行固定 <strong>{draft.amount} USDT</strong> 投数。
          </div>
        )}
      </div>

      {/* 安全保护密钥 */}
      <CompactFormRow label="安全网密 Key (API Token)">
        <input
          className="token-input"
          type="password"
          value={apiToken}
          onChange={event => onTokenChange(event.target.value)}
          placeholder="无限制"
          style={{ width: "120px", height: "26px", textAlign: "right", background: "#0d1117", border: "1px solid var(--line)", color: "var(--text)", borderRadius: "4px", padding: "0 6px", fontSize: "12px", outline: "none" }}
        />
      </CompactFormRow>

      <button className="primary-button" type="button" onClick={onSave} style={{
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
      }}>
        <Save size={14} /> 保存并下发策略配置
      </button>
    </section>
  );
}
