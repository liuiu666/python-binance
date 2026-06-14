import React from "react";
import { Activity } from "lucide-react";
import { directionClass, directionText, fmtPct, strategyName, displaySignalTime } from "../utils";

export default function SignalBanner({ signalPayload, activeSignal, signalAmount }) {
  const variants = signalPayload?._strategyVariants || [];
  const activeItems = variants.map(item => signalPayload?.[item.id]).filter(item => item?.signal);

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
          <span className="signal-pill neutral">策略监控中，等待触发区间</span>
        )}
      </div>
      <div className="signal-side">
        <span>{signalAmount || "--"}U</span>
        <small>{displaySignalTime(activeSignal)} 北京时间</small>
      </div>
    </section>
  );
}
