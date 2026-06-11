import React, { useMemo } from "react";
import { ageText } from "../utils";

export default function ReportStrip({ reports, runtime, tablet }) {
  const cards = useMemo(() => {
    const decision = reports?.decision || {};
    const health = reports?.health || decision.system_health || {};
    const summary = decision.production_summary || decision.validated_walkforward || {};
    const edge10 = summary.BTC_10min?.edge_over_breakeven;
    const edge30 = summary.BTC_30min?.edge_over_breakeven;
    const portfolio = decision.parallel_portfolio || {};
    const shadow = reports?.shadowDecision || {};
    const shadowCounts = shadow.summary_counts || {};
    
    const tabletStatus = tablet?.status === "has_order_done"
      ? "orders ok"
      : tablet?.status === "autojs_online_waiting_for_order_done"
        ? `heartbeat ${ageText(tablet.latestHeartbeatAgeMs)}`
        : tablet?.status ? "seen" : "--";
        
    const liveState = tablet?.checks?.orderDoneSeen
      ? "已下单"
      : tablet?.checks?.heartbeatOnline
        ? "在线"
        : "等待";
        
    return [
      { label: "Health", value: health.overall || "--", tone: health.overall === "ok" ? "ok" : health.overall === "fail" ? "fail" : "warn" },
      { label: "10m Edge", value: edge10 !== undefined ? `+${Number(edge10).toFixed(2)}pp` : "--", tone: edge10 > 0 ? "ok" : "warn" },
      { label: "30m Edge", value: edge30 !== undefined ? `+${Number(edge30).toFixed(2)}pp` : "--", tone: edge30 > 0 ? "ok" : "warn" },
      {
        label: "Filter",
        value: portfolio.win_rate !== undefined
          ? `${Number(portfolio.win_rate).toFixed(1)}% / ${Number(portfolio.frequency?.trades_per_day || 0).toFixed(1)}/d`
          : "--",
        tone: portfolio.win_rate ? "ok" : "warn"
      },
      { label: "Tablet", value: tabletStatus, tone: tablet?.checks?.heartbeatOnline || tablet?.checks?.orderDoneSeen ? "ok" : "warn" },
      { label: "Shadow", value: shadow.summary_counts ? `watch ${shadowCounts.watch || 0} / reject ${(shadowCounts.reject_live_weak || 0) + (shadowCounts.reject_offline_weak || 0)}` : "--", tone: shadow.summary_counts ? "warn" : "" },
      { label: "Server", value: runtime?.serverId || "--", tone: runtime?.serverId ? "ok" : "warn" },
      { label: "Live", value: liveState, tone: tablet?.checks?.heartbeatOnline || tablet?.checks?.orderDoneSeen ? "ok" : "warn" }
    ];
  }, [reports, runtime, tablet]);

  return (
    <section className="report-strip">
      {cards.map(card => (
        <div className="report-card" key={card.label} title={`${card.label}: ${card.value}`}>
          <span>{card.label}</span>
          <strong className={card.tone}>{card.value}</strong>
        </div>
      ))}
    </section>
  );
}
