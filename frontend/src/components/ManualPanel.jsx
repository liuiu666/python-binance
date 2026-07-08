import { Activity, ArrowDown, ArrowUp } from "lucide-react";
import { DEFAULT_CONFIG, fmt, payoutForDuration } from "../utils";

export default function ManualPanel({ draft, onManualTrade, onAmountPreset }) {
  const amount = Math.max(5, Number(draft.amount) || Number(DEFAULT_CONFIG.amount));
  const duration = draft.duration || DEFAULT_CONFIG.duration;
  const payout = payoutForDuration(duration);
  const presets = [5, 10, 20, 50, 100];

  return (
    <section className="panel manual-panel">
      <header className="section-head compact">
        <div>
          <span className="eyebrow">手动</span>
          <h2><Activity size={18} /> 快捷下单</h2>
        </div>
      </header>

      <div className="manual-note">手动下单是你本人确认的指令，不受策略实盘开关影响。</div>

      <div className="preset-row">
        {presets.map(value => (
          <button
            type="button"
            key={value}
            className={Number(amount) === value ? "active" : ""}
            onClick={() => onAmountPreset(value)}
          >
            {value}U
          </button>
        ))}
      </div>

      <div className="manual-grid">
        <button className="trade-button up" type="button" onClick={() => onManualTrade("UP")}>
          <ArrowUp size={22} />
          <span>
            <strong>看涨</strong>
            <small>{duration}分钟，赢约 +{fmt(amount * payout, 2)}U</small>
          </span>
        </button>
        <button className="trade-button down" type="button" onClick={() => onManualTrade("DOWN")}>
          <ArrowDown size={22} />
          <span>
            <strong>看跌</strong>
            <small>{duration}分钟，赢约 +{fmt(amount * payout, 2)}U</small>
          </span>
        </button>
      </div>
    </section>
  );
}
