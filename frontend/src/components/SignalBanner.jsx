import React from "react";
import { Activity } from "lucide-react";
import { directionClass, directionText, fmtPct, strategyName, signalTimeText } from "../utils";

export default function SignalBanner({ signalPayload, activeSignal, signalAmount }) {
  const safe = signalPayload?.BTC_10min_SAFE || null;
  const taker = signalPayload?.BTC_10min_TAKER || null;
  const activeItems = [taker, safe].filter(item => item?.signal);
  
  return (
    <section className="signal-banner">
      <div className="signal-label">
        <Activity size={18} />
        <span>AI 分析</span>
      </div>
      <div className="signal-list">
        {activeItems.length ? (
          activeItems.map(item => (
            <span className={`signal-pill ${directionClass(item.signal)}`} key={item.strategy_id || item.interval_min}>
              {strategyName(item.strategy_id)} {directionText(item.signal)} {fmtPct(item.confidence, 0)}
            </span>
          ))
        ) : (
          <span className="signal-pill neutral">推荐稳健 / 资金流过滤 监控中</span>
        )}
      </div>
      <div className="signal-side">
        <span>{signalAmount || "--"}U</span>
        <small>{signalTimeText(activeSignal?.time)}</small>
      </div>
    </section>
  );
}
