import { Save, SlidersHorizontal } from "lucide-react";
import { DEFAULT_CONFIG, fmtPct } from "../utils";

const VETO_OPTIONS = [
  ["none", "关闭旧版确认过滤"],
  ["ob_confirm_weak", "订单薄不支持方向就跳过"],
  ["price_confirm_weak", "价格短线不支持方向就跳过"],
  ["ob_or_price_weak", "订单薄或价格任一不支持就跳过"]
];

function toInt(value, fallback, min, max) {
  const n = Math.round(Number(value));
  if (!Number.isFinite(n)) return fallback;
  return Math.max(min, Math.min(max, n));
}

function Toggle({ label, checked, onChange, danger }) {
  return (
    <button type="button" className={`switch-row ${checked ? "on" : ""} ${danger ? "danger" : ""}`} onClick={onChange}>
      <span>{label}</span>
      <strong>{checked ? "开启" : "关闭"}</strong>
    </button>
  );
}

function Field({ label, value, suffix, min, max, step = 1, onChange }) {
  return (
    <label className="field">
      <span>{label}</span>
      <div>
        <input
          type="number"
          value={value ?? ""}
          min={min}
          max={max}
          step={step}
          onChange={event => onChange(event.target.value)}
        />
        {suffix ? <em>{suffix}</em> : null}
      </div>
    </label>
  );
}

function isV21Router(variant) {
  return variant?.base === "SECOND_NORMAL_ROUTER_V21" || String(variant?.id || "").includes("_V21_");
}

function isV22LowVol(variant) {
  return variant?.base === "SECOND_NORMAL_LOWVOL_V22" || String(variant?.id || "").includes("_V22_");
}

function isLegacyNormalState(variant) {
  return variant?.base === "SECOND_NORMAL_STATE_V11";
}

function hoursFromSeconds(value, fallbackHours) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return fallbackHours;
  return Math.max(1, Math.round(seconds / 3600));
}

function StrategySettings({ variant, onChange }) {
  const bt = variant.backtest;
  const v21 = isV21Router(variant);
  const v22 = isV22LowVol(variant);
  const router = v21 || v22;
  const legacyNormal = isLegacyNormalState(variant);
  return (
    <article className="strategy-settings">
      <header>
        <div>
          <strong>{variant.label || variant.id}</strong>
          <small>{variant.id}</small>
        </div>
        <span>{bt ? `${fmtPct(bt.wr, 2)} / ${bt.tradesPerDay}单/天` : "暂无固定回测"}</span>
      </header>

      <div className="setting-grid">
        <Field
          label="下单金额"
          value={variant.amount || 5}
          min={5}
          max={999}
          suffix="U"
          onChange={value => onChange({ amount: String(toInt(value, 5, 5, 999)) })}
        />
        <Field
          label={router ? "最短下单间隔" : "同策略间隔"}
          value={variant.gapSec ?? 600}
          min={0}
          max={21600}
          suffix="秒"
          onChange={value => onChange({ gapSec: toInt(value, 600, 0, 21600) })}
        />
        {router ? (
          <>
            <Field
              label="10分钟波动上限"
              value={variant.r10CapBps ?? 42}
              min={5}
              max={120}
              step={0.5}
              suffix="bp"
              onChange={value => onChange({ r10CapBps: Number(value) })}
            />
            <Field
              label="做空波动上限"
              value={variant.downR10CapBps ?? 35}
              min={5}
              max={120}
              step={0.5}
              suffix="bp"
              onChange={value => onChange({ downR10CapBps: Number(value) })}
            />
            <Field
              label="中波动上限"
              value={variant.midRouteSigmaCapBps ?? 20}
              min={5}
              max={80}
              step={0.5}
              suffix="bp"
              onChange={value => onChange({ midRouteSigmaCapBps: Number(value) })}
            />
            <Field
              label="秒级覆盖下限"
              value={variant.minObservedPct ?? 88}
              min={50}
              max={100}
              step={1}
              suffix="%"
              onChange={value => onChange({ minObservedPct: toInt(value, 88, 50, 100) })}
            />
            {v21 ? (
              <Toggle
                label="low+UP否决"
                checked={variant.vetoLowUp !== false}
                onChange={() => onChange({ vetoLowUp: variant.vetoLowUp === false })}
              />
            ) : null}
            {v22 ? (
              <>
                <Field
                  label="低波动上限"
                  value={variant.lowVolRouteSigmaMaxBps ?? 10}
                  min={1}
                  max={100}
                  step={0.5}
                  suffix="bp"
                  onChange={value => onChange({ lowVolRouteSigmaMaxBps: Number(value) })}
                />
                <Field
                  label="确认窗口"
                  value={variant.lowVolConfirmSec ?? 15}
                  min={1}
                  max={120}
                  suffix="秒"
                  onChange={value => onChange({ lowVolConfirmSec: toInt(value, 15, 1, 120) })}
                />
                <Field
                  label="回归确认"
                  value={variant.lowVolReversionBps ?? 0.5}
                  min={0}
                  max={20}
                  step={0.1}
                  suffix="bp"
                  onChange={value => onChange({ lowVolReversionBps: Number(value) })}
                />
                <Field
                  label="突破确认"
                  value={variant.lowVolBreakoutBps ?? 1.5}
                  min={0}
                  max={50}
                  step={0.1}
                  suffix="bp"
                  onChange={value => onChange({ lowVolBreakoutBps: Number(value) })}
                />
              </>
            ) : null}
            <Field
              label="亏损观察笔数"
              value={variant.lossDensityWindow ?? 6}
              min={3}
              max={20}
              onChange={value => onChange({ lossDensityWindow: toInt(value, 6, 3, 20) })}
            />
            <Field
              label="触发亏损笔数"
              value={variant.lossDensityLosses ?? 3}
              min={1}
              max={10}
              onChange={value => onChange({ lossDensityLosses: toInt(value, 3, 1, 10) })}
            />
            <Field
              label="亏损冷却"
              value={hoursFromSeconds(variant.lossDensityCooldownSec, 8)}
              min={1}
              max={24}
              suffix="小时"
              onChange={value => onChange({ lossDensityCooldownSec: toInt(Number(value) * 3600, 28800, 3600, 86400) })}
            />
            <Field
              label="连亏冷却"
              value={hoursFromSeconds(variant.lossStreakCooldownSec, 1)}
              min={1}
              max={12}
              suffix="小时"
              onChange={value => onChange({ lossStreakCooldownSec: toInt(Number(value) * 3600, 3600, 3600, 43200) })}
            />
          </>
        ) : (
          <>
            <Field
              label="确认延迟"
              value={variant.confirmDelaySec ?? 5}
              min={1}
              max={60}
              suffix="秒"
              onChange={value => onChange({ confirmDelaySec: toInt(value, 5, 1, 60) })}
            />
            <Field
              label="最大反向"
              value={variant.maxAdverseBps ?? 5}
              min={0}
              max={50}
              step={0.5}
              suffix="bp"
              onChange={value => onChange({ maxAdverseBps: Number(value) })}
            />
            <Field
              label="Bandwalk上限"
              value={variant.bandwalkMax ?? 6}
              min={1}
              max={20}
              step={0.5}
              onChange={value => onChange({ bandwalkMax: Number(value) })}
            />
            <Field
              label="共识票数"
              value={variant.minConsensusVotes ?? 2}
              min={1}
              max={5}
              onChange={value => onChange({ minConsensusVotes: toInt(value, 2, 1, 5) })}
            />
          </>
        )}
      </div>

      {router ? (
        <div className="settings-note">
          {v22
            ? "V22 是低波动处理影子策略：只在 routeSigma 低于阈值时观察 V21 尾部候选，再用短确认窗口判断回归或突破；不参与实盘下单。"
            : "V21 当前只用秒级正态路由、10分钟波动范围、数据覆盖和亏损密度风控；旧版 V19 过滤、Bandwalk、确认延迟不参与这套策略。"}
        </div>
      ) : legacyNormal ? (
        <label className="field full">
          <span>旧版V19确认过滤</span>
          <select value={variant.confirmationVeto || "none"} onChange={event => onChange({ confirmationVeto: event.target.value })}>
            {VETO_OPTIONS.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
          </select>
        </label>
      ) : null}

      <div className="switch-grid">
        <Toggle label="策略监控" checked={variant.enabled !== false} onChange={() => onChange({ enabled: variant.enabled === false })} />
        <Toggle label={v22 ? "允许V22实盘" : "允许实盘"} checked={variant.tradeEnabled !== false} danger onChange={() => onChange({ tradeEnabled: variant.tradeEnabled === false })} />
      </div>
    </article>
  );
}

export default function ConfigPanel({ draft, dirty, apiToken, onTokenChange, onDraftChange, onToggle, onSave }) {
  const variants = Array.isArray(draft.strategyVariants) && draft.strategyVariants.length
    ? draft.strategyVariants
    : DEFAULT_CONFIG.strategyVariants;
  const liveStrategyCount = variants.filter(variant => variant.enabled !== false && variant.tradeEnabled !== false).length;

  const updateVariant = (index, patch) => {
    onDraftChange({
      strategyVariants: variants.map((item, i) => {
        if (i !== index) return item;
        const next = { ...item, ...patch };
        if (patch.horizonSec !== undefined) {
          next.duration = String(Math.max(1, Math.round(Number(next.horizonSec || 600) / 60)));
        }
        return next;
      })
    });
  };

  return (
    <section className="panel config-panel">
      <header className="section-head">
        <div>
          <span className="eyebrow">配置</span>
          <h2><SlidersHorizontal size={18} /> 策略控制</h2>
        </div>
        <strong className={dirty ? "dirty" : "synced"}>{dirty ? "未保存" : "已同步"}</strong>
      </header>

      <div className={`strategy-live-note ${liveStrategyCount ? "danger" : "safe"}`}>
        {liveStrategyCount
          ? `已选择 ${liveStrategyCount} 个策略允许实盘；保存后策略会自动下单。`
          : "当前没有策略允许实盘；保存后策略只观察。手动下单不受这里影响。"}
      </div>

      <div className={`mode-warning ${draft.realTradingEnabled ? "danger" : "safe"}`}>
        {draft.realTradingEnabled
          ? "实盘资金已开启：只有“允许实盘”的策略会交给下单端。"
          : "当前不是实盘资金模式：策略可以监控或记录影子单，不会消耗真实资金。"}
      </div>

      <div className="setting-grid top-setting-grid">
        <Field
          label="默认金额"
          value={draft.amount || DEFAULT_CONFIG.amount}
          min={5}
          max={999}
          suffix="U"
          onChange={value => onDraftChange({ amount: String(toInt(value, 5, 5, 999)) })}
        />
        <Field
          label="默认周期"
          value={draft.duration || DEFAULT_CONFIG.duration}
          min={1}
          max={60}
          suffix="分钟"
          onChange={value => onDraftChange({ duration: String(toInt(value, 10, 1, 60)) })}
        />
      </div>

      <div className="switch-grid global-switches">
        <Toggle label="允许策略自动下单" checked={!!draft.autoTrade_10m} danger onChange={() => onToggle("autoTrade_10m")} />
        <Toggle label="实盘资金下单" checked={!!draft.realTradingEnabled} danger onChange={() => onToggle("realTradingEnabled")} />
        <Toggle label="记录影子单" checked={!!draft.shadowTradingEnabled} onChange={() => onToggle("shadowTradingEnabled")} />
      </div>

      <div className="strategy-settings-list">
        {variants.map((variant, index) => (
          <StrategySettings key={variant.id || index} variant={variant} onChange={patch => updateVariant(index, patch)} />
        ))}
      </div>

      <label className="field token-field">
        <span>页面密钥</span>
        <input
          type="password"
          value={apiToken}
          placeholder="登录后自动使用"
          onChange={event => onTokenChange(event.target.value)}
        />
      </label>

      <button className="primary-action" type="button" onClick={onSave}>
        <Save size={16} />
        保存并下发配置
      </button>
    </section>
  );
}
