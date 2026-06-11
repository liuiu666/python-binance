import React, { useEffect, useRef } from "react";
import { Activity } from "lucide-react";
import { clamp, directionClass, directionText, fmtPct, signalLabel } from "../utils";

function drawGauge(canvas, signal) {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  const cx = width / 2;
  const cy = height - 12;
  const radius = Math.max(1, height - 34);
  ctx.clearRect(0, 0, width, height);
  ctx.lineWidth = 14;
  ctx.lineCap = "round";
  ctx.strokeStyle = "rgba(255,255,255,0.11)";
  ctx.beginPath();
  ctx.arc(cx, cy, radius, Math.PI, Math.PI * 2);
  ctx.stroke();

  const rsi = signal?.rsi_value !== undefined ? Number(signal.rsi_value) : null;
  const confidence = signal?.confidence !== undefined ? Number(signal.confidence) : null;
  const metric = confidence !== null && Number.isFinite(confidence) ? confidence : rsi;
  if (metric !== null && Number.isFinite(metric)) {
    const pct = clamp(metric, 0, 100) / 100;
    ctx.strokeStyle = signal?.signal === "DOWN" ? "#e45858" : signal?.signal === "UP" ? "#27c3a5" : "#f0c94a";
    ctx.beginPath();
    ctx.arc(cx, cy, radius, Math.PI, Math.PI + Math.PI * pct);
    ctx.stroke();
  }

  ctx.textAlign = "center";
  ctx.fillStyle = signal?.signal === "DOWN" ? "#e45858" : signal?.signal === "UP" ? "#27c3a5" : "#f0c94a";
  ctx.font = "700 24px system-ui, sans-serif";
  if (confidence !== null && Number.isFinite(confidence)) {
    ctx.fillText(`${confidence.toFixed(0)}%`, cx, cy - 34);
  } else if (rsi !== null && Number.isFinite(rsi)) {
    ctx.fillText(`RSI ${rsi.toFixed(0)}`, cx, cy - 34);
  } else {
    ctx.fillStyle = "#7d8792";
    ctx.fillText("--", cx, cy - 34);
  }
  ctx.font = "700 12px system-ui, sans-serif";
  ctx.fillText(signal?.signal ? directionText(signal.signal) : "监控", cx, cy - 12);
}

export default function GaugePanel({ signal }) {
  const gaugeRef = useRef(null);
  useEffect(() => {
    drawGauge(gaugeRef.current, signal);
  }, [signal]);
  
  const probs = Array.isArray(signal?.probs) ? signal.probs : [0.5, 0.5, 0.5];
  const verdict = signal?.signal ? `${directionText(signal.signal)} ${fmtPct(signal.confidence, 0)}` : signalLabel(signal);
  
  const thresholdText = signal?.engine === "two_minute_regime_model"
    ? `${signal?.regime_group || "--"} | p=${Number(signal?.avg_prob || 0).toFixed(3)} / th=${Number(signal?.policy_threshold || signal?.threshold || 0).toFixed(2)}`
    : signal?.rsi_value !== undefined
      ? `RSI=${Number(signal.rsi_value).toFixed(0)} | 强度 ${fmtPct(signal?.confidence, 0)}`
      : "等待信号";

  return (
    <section className="panel signal-panel">
      <header className="panel-header">
        <span><Activity size={15} /> 信号强度</span>
      </header>
      <canvas className="gauge" ref={gaugeRef} width="280" height="140" />
      <div className="model-bars">
        {probs.slice(0, 3).map((prob, index) => {
          const pct = clamp(prob * 100, 0, 100);
          const tone = prob > 0.6 ? "up" : prob < 0.4 ? "down" : "neutral";
          return (
            <div className="model-row" key={`model-${index + 1}`}>
              <span>M{index + 1}</span>
              <div className="bar-track">
                <i className={tone} style={{ width: `${pct}%` }} />
              </div>
              <strong>{pct.toFixed(1)}%</strong>
            </div>
          );
        })}
      </div>
      <div className="verdict">
        <span>判断</span>
        <strong className={directionClass(signal?.signal)}>{verdict}</strong>
        <small>{thresholdText}</small>
      </div>
    </section>
  );
}
