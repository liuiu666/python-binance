import { useEffect, useRef } from "react";

export const PAYOUT = 0.85;

export const DEFAULT_CONFIG = {
  amount: "5",
  duration: "10",
  autoTrade_10m: false,
  realTradingEnabled: false,
  shadowTradingEnabled: false,
  strategyVariants: [
    {
      id: "BTC_10min_NORMAL_STATE_V15_STABLE_D5A5",
      base: "SECOND_NORMAL_STATE_V11",
      label: "正态V15原版对照",
      amount: "5",
      duration: "10",
      enabled: true,
      tradeEnabled: false,
      lookbackSec: 10800,
      horizonSec: 600,
      gapSec: 600,
      confirmDelaySec: 5,
      maxAdverseBps: 5,
      signalHoldSec: 55,
      bandwalkMax: 6,
      minConsensusVotes: 2,
      stateGate: "v15_bw35_or_early_sigma18",
      confirmationVeto: "none"
    },
    {
      id: "BTC_10min_NORMAL_STATE_V19_OB_CONFIRM_D5A5",
      base: "SECOND_NORMAL_STATE_V11",
      label: "正态V19保守实盘",
      amount: "5",
      duration: "10",
      enabled: true,
      tradeEnabled: true,
      lookbackSec: 10800,
      horizonSec: 600,
      gapSec: 600,
      confirmDelaySec: 5,
      maxAdverseBps: 5,
      signalHoldSec: 55,
      bandwalkMax: 6,
      minConsensusVotes: 2,
      stateGate: "v15_bw35_or_early_sigma18",
      confirmationVeto: "ob_confirm_weak"
    },
    {
      id: "BTC_10min_NORMAL_STATE_V19_OB_CONFIRM_HF_G60",
      base: "SECOND_NORMAL_STATE_V11",
      label: "正态V19高频影子",
      amount: "5",
      duration: "10",
      enabled: true,
      tradeEnabled: false,
      lookbackSec: 10800,
      horizonSec: 600,
      gapSec: 60,
      confirmDelaySec: 5,
      maxAdverseBps: 5,
      signalHoldSec: 55,
      bandwalkMax: 6,
      minConsensusVotes: 2,
      stateGate: "v15_bw35_or_early_sigma18",
      confirmationVeto: "ob_confirm_weak"
    }
  ]
};

const STRATEGY_NAMES = {
  BTC_10min_NORMAL_STATE_V15_STABLE_D5A5: "正态V15原版对照",
  BTC_10min_NORMAL_STATE_V19_OB_CONFIRM_D5A5: "正态V19保守实盘",
  BTC_10min_NORMAL_STATE_V19_OB_CONFIRM_HF_G60: "正态V19高频影子",
  BTC_10min_SECOND_VW_STABLE_2700_20_ETA2: "正态成交量确认 稳健",
  BTC_10min_SECOND_VW_FAST_2700_27_ETA3: "正态成交量确认 高频",
  BTC_10min_MULTISCALE_PHASE_GATE_V1: "多周期迁移阶段 V1",
  BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V13_FREQ: "V13 秒级正态反转",
  BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V13_SHADOW: "V13 秒级正态反转（影子）",
  BTC_30min_SHADOW_CANDIDATE: "BTC 30分钟方向影子"
};

export function payoutForDuration(duration) {
  const minutes = Number(duration);
  if (Number.isFinite(minutes)) {
    if (minutes >= 30) return 0.85;
    if (minutes >= 10) return 0.8;
  }
  return PAYOUT;
}

export function clamp(num, min, max) {
  const value = Number(num);
  if (!Number.isFinite(value)) return min;
  return Math.max(min, Math.min(max, value));
}

export function fmt(num, digits = 2) {
  const value = Number(num);
  if (!Number.isFinite(value)) return "--";
  return value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  });
}

export function fmtPrice(num) {
  const value = Number(num);
  return Number.isFinite(value) ? value.toFixed(2) : "--";
}

export function fmtPct(num, digits = 1) {
  const value = Number(num);
  return Number.isFinite(value) ? `${value.toFixed(digits)}%` : "--";
}

export function directionText(direction) {
  if (direction === "UP") return "看涨";
  if (direction === "DOWN") return "看跌";
  return "等待";
}

export function directionClass(direction) {
  if (direction === "UP") return "up";
  if (direction === "DOWN") return "down";
  return "neutral";
}

export function strategyName(strategyId) {
  const id = String(strategyId || "");
  if (!id || id === "manual") return "手动";
  if (STRATEGY_NAMES[id]) return STRATEGY_NAMES[id];
  if (id.startsWith("BTC_10min_NORMAL_STATE")) return "正态状态策略";
  if (id.startsWith("BTC_10min_SECOND_VW")) return "正态成交量确认";
  if (id.startsWith("BTC_10min_SMART")) return "SMART策略";
  return id;
}

export function shortId(strategyId) {
  return String(strategyId || "")
    .replace(/^BTC_10min_/, "")
    .replace(/NORMAL_STATE_/g, "NS_");
}

export function statusClass(status) {
  if (status === "won") return "won";
  if (status === "lost") return "lost";
  if (status === "tie") return "tie";
  if (status === "aborted" || status === "unverified") return "warn";
  return "pending";
}

export function statusText(status) {
  if (status === "won") return "胜";
  if (status === "lost") return "负";
  if (status === "tie") return "平";
  if (status === "aborted") return "取消";
  if (status === "unverified") return "未成交";
  return "持仓";
}

export function pnlText(row) {
  if (!row) return "--";
  const pnl = Number(row.pnl || 0);
  if (row.status === "unverified") return "未扣款";
  if (row.status === "pending") return "待结算";
  if (row.status === "aborted") return "已取消";
  return `${pnl > 0 ? "+" : ""}${fmt(pnl, 2)}U`;
}

export function timeParts(time) {
  if (!time) return { date: "--", time: "--", full: "--" };
  const date = new Date(Number(time));
  if (Number.isNaN(date.getTime())) return { date: "--", time: "--", full: "--" };
  const options = { timeZone: "Asia/Shanghai", hour12: false };
  const day = date.toLocaleDateString("zh-CN", { ...options, month: "2-digit", day: "2-digit" });
  const clock = date.toLocaleTimeString("zh-CN", { ...options, hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return { date: day, time: clock, full: `${day} ${clock}` };
}

export function dateTimeText(time) {
  if (!time) return "--";
  const ms = typeof time === "string" && Number.isNaN(Number(time)) ? Date.parse(time) : Number(time);
  return timeParts(ms).full;
}

export function ageText(ms) {
  const value = Number(ms);
  if (!Number.isFinite(value)) return "--";
  const seconds = Math.max(0, Math.floor(value / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

export function healthTone(ok) {
  if (ok === true) return "ok";
  if (ok === false) return "bad";
  return "warn";
}

export function healthText(ok, ageMs) {
  const prefix = ok === true ? "正常" : ok === false ? "异常" : "未知";
  return ageMs === null || ageMs === undefined ? prefix : `${prefix} ${ageText(ageMs)}`;
}

export function signalLabel(signal) {
  if (!signal) return "等待数据";
  if (signal.signal) return `${directionText(signal.signal)} ${fmtPct(signal.confidence, 0)}`;
  if (signal.model_type === "second_multi_normal_hf_stable_v1") {
    return signal.market_state_detail?.label || "等待完整分钟判断";
  }
  if (signal.reason === "confirmed_signal_expired") return "信号过期，等待下一次";
  if (signal.reason === "no_confirmed_false_break") return "等待假突破回归确认";
  if (signal.reason === "no_router_branch" && Array.isArray(signal.router_rejects) && signal.router_rejects.includes("low_up_veto")) return "low+UP否决";
  if (signal.reason === "no_router_branch") return "等待极端尾部";
  if (signal.reason === "observed600_low") return "等待秒级覆盖";
  if (signal.reason === "r10_cap") return "波动过大暂停";
  if (signal.reason === "router_metrics_unavailable") return "等待路由指标";
  if (signal.data_health_blocked) return "数据延迟，策略暂停";
  if (signal.safety_blocked) return "风控拦截";
  return signal.signal_detail || signal.next_signal_estimate || "等待触发";
}

function asNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function statusTone(ok) {
  if (ok === true) return "ok";
  if (ok === false) return "bad";
  return "warn";
}

export function signalReasonText(signal) {
  if (!signal) return "等待策略数据";
  const reason = signal?.reason;
  if (signal?.model_type === "llm_direction") {
    const map = {
      order_lifecycle_pending: "上一笔同策略订单等待到期结算",
      order_duration_pending: "上一笔同策略订单尚未到期",
      settlement_price_pending: "订单已到期，等待有效结算价格",
      llm_request_pending: "LLM 正在生成预测",
      data_insufficient_6500_rows_required: "1分钟源数据不足6500行"
    };
    if (String(reason || "").startsWith("taker_buy_vol_incomplete")) return "主动买量数据不完整，停止预测";
    if (String(reason || "").startsWith("data_stale_")) return "1分钟行情已过期，停止预测";
    if (String(reason || "").startsWith("llm_error:")) return `LLM 请求失败：${String(reason).slice(10).trim()}`;
    return map[reason] || signal.signal_detail || (signal.signal ? "LLM 已生成方向" : "等待下一次 LLM 预测");
  }
  if (signal?.model_type === "second_multi_normal_hf_stable_v1") {
    const map = {
      snapshot_incomplete: "分钟特征或订单薄不完整",
      no_completed_snapshot: "等待第一个完整分钟",
      lowvol_flow_not_reversed: "已到正态尾部，等待成交流转向",
      trend_not_mature_exhaustion: "趋势存在，衰竭条件尚未同时满足",
      flat_tail_may_be_regime_shift: "偏离超过1.8σ，警惕形成新区间",
      waiting_supported_regime: "等待当前路径条件补齐",
      multi_normal_gap: "上一单10分钟窗口尚未结束",
      multi_normal_orderbook_missing: "订单薄数据不可用",
      multi_normal_orderbook_insufficient: "秒级价格或订单薄覆盖不足",
      multi_normal_feature_error: "策略指标计算异常"
    };
    return map[reason] || signal.signal_detail || "等待下一完整分钟复核";
  }
  if (signal?.model_type === "second_branch_vote_startup_v1") {
    if (reason === "vote_not_enough") return "等待分支投票达到2票同向";
    if (reason === "branch_vote_gap") return "上一单后10分钟间隔内";
    if (reason === "branch_vote_orderbook_missing") return "订单薄数据不可用";
    if (reason === "branch_vote_orderbook_insufficient") return "订单薄覆盖不足";
    if (reason === "branch_vote_feature_error") return "分支投票指标计算异常";
    if (String(reason || "").startsWith("skip_trend_start")) return "趋势刚启动，跳过反向做空";
    if (String(reason || "").startsWith("skip_")) return "候选信号未通过确认";
  }
  const map = {
    liq_normal_not_ready: "等待可交易的震荡区",
    liq_wait_reclaim: "震荡区已形成，等待假突破回归",
    liq_strategy_gap: "上一笔信号后冷却中",
    v9_original_candidate_gap: "原V2候选冷却中",
    liq_orderbook_missing: "订单薄数据缺失",
    liq_orderbook_missing_or_stale: "订单薄数据延迟",
    liq_feature_error: "策略指标计算异常",
    liq_v2_skip_down_bid_fade: "做空质量不够，跳过",
    liq_v2_skip_up_negative_flow: "做多质量不够，跳过",
    confirmed_signal_expired: "信号过期，等待下一次",
    no_confirmed_false_break: "等待假突破回归确认",
    observed600_low: "秒级数据覆盖不足",
    r10_cap: "10分钟波动过大，暂停",
    router_metrics_unavailable: "等待行情指标"
  };
  return map[reason] || signalLabel(signal);
}

export function signalReadinessItems(signal, variant = {}) {
  if (!signal) return [];
  if (signal.model_type === "second_multi_normal_hf_stable_v1") {
    const paths = Array.isArray(signal.signal_paths) ? signal.signal_paths : [];
    const activeKey = signal.market_state_detail?.active_path;
    const path = paths.find(item => item.key === activeKey) || paths.find(item => item.status !== "inactive") || paths[0];
    const checks = Array.isArray(path?.checks) ? path.checks : [];
    return checks.map(item => ({ ...item, tone: statusTone(item.ok) }));
  }
  if (signal.model_type === "second_branch_vote_startup_v1") {
    const upVotes = asNumber(signal.upVotes) ?? 0;
    const downVotes = asNumber(signal.downVotes) ?? 0;
    const minVotes = asNumber(variant.minVotes ?? signal.minVotes) ?? 2;
    const bestVotes = Math.max(upVotes, downVotes);
    const hasDirection = bestVotes >= minVotes && upVotes !== downVotes;
    const reason = String(signal.reason || "");
    const trendLabels = {
      trend_up: "上涨趋势",
      trend_down: "下跌趋势",
      drift_up: "弱上涨",
      drift_down: "弱下跌",
      flat: "震荡",
      transition: "切换中"
    };
    const posLabels = {
      above_upper: "上沿外",
      upper_edge: "上沿",
      upper_inside: "上半区",
      center: "中轴",
      lower_inside: "下半区",
      lower_edge: "下沿",
      below_lower: "下沿外"
    };
    const sprintLabels = {
      up_sprint: "上涨冲刺",
      down_sprint: "下跌冲刺",
      up_walk: "连续上涨",
      down_walk: "连续下跌",
      none: "无冲刺"
    };
    const startupScore = asNumber(signal.startupScore);
    const startupThreshold = asNumber(variant.startupSkipThreshold) ?? 4;
    const flow5 = asNumber(signal.flow5);
    const imb20 = asNumber(signal.imb20);
    const branchText = [
      trendLabels[signal.trend] || signal.trend,
      signal.volatility,
      posLabels[signal.normal_pos] || signal.normal_pos,
      sprintLabels[signal.sprint] || signal.sprint
    ].filter(Boolean).join(" / ");
    const items = [
      {
        key: "branch_votes",
        label: "分支投票",
        ok: hasDirection,
        value: `${upVotes}涨 / ${downVotes}跌`,
        target: `要求至少 ${minVotes} 票同向`,
        help: "当前历史分支规则还没有形成足够同向共识，所以不下单。"
      },
      {
        key: "branch_state",
        label: "行情分支",
        ok: branchText ? true : null,
        value: branchText || "--",
        target: "趋势 + 波动 + 正态位置 + 短线形态",
        help: "独立策略按分钟归类行情，再用固定历史规则投票。"
      },
      {
        key: "normal_position",
        label: "正态位置",
        ok: signal.normal_pos ? true : null,
        value: posLabels[signal.normal_pos] || signal.normal_pos || "--",
        target: "判断价格在区间内、贴边还是突破",
        help: "这不是旧V2的5.8-55bp过滤，新策略允许低波动分支参与投票。"
      },
      {
        key: "sprint",
        label: "短线形态",
        ok: signal.sprint ? true : null,
        value: sprintLabels[signal.sprint] || signal.sprint || "--",
        target: "识别冲刺、连续行走或无冲刺",
        help: "上涨冲刺做空需要额外成熟确认，刚启动会跳过。"
      },
      {
        key: "startup_guard",
        label: "启动保护",
        ok: reason.startsWith("skip_trend_start") ? false : startupScore == null ? null : startupScore < startupThreshold,
        value: startupScore == null ? "--" : `${startupScore}/6`,
        target: `上涨启动评分 < ${startupThreshold} 才允许反向做空`,
        help: reason.startsWith("skip_trend_start")
          ? "上涨刚启动，历史上反向做空容易被继续拉升打掉，所以跳过。"
          : "只有上涨冲刺做空场景才需要这个保护。"
      },
      {
        key: "flow_book",
        label: "资金流/订单薄",
        ok: flow5 == null && imb20 == null ? null : true,
        value: flow5 == null && imb20 == null ? "--" : `flow ${flow5 == null ? "--" : fmt(flow5, 3)} / imb ${imb20 == null ? "--" : fmt(imb20, 3)}`,
        target: "用于确认反弹或衰竭质量",
        help: "订单薄和主动成交流只作为分支确认，不是旧V2的正态成型卡片。"
      }
    ];
    return items.map(item => ({ ...item, tone: statusTone(item.ok) }));
  }
  if (signal.model_type === "second_normal_trend_orderbook_latch_v2") {
    const observed = asNumber(signal.observed_pct);
    const coverage = asNumber(signal.ob_coverage_60);
    const state = String(signal.router_state || "transition");
    const band = String(signal.volatility_band || "unknown");
    const sigma = asNumber(signal.sigma_bps);
    const z = asNumber(signal.z_score);
    const latchActive = signal.latch_active === true;
    const latchSignal = signal.latch_signal;
    const stateLabels = {
      normal: "正态震荡",
      trend_formation: "趋势形成",
      transition: "行情过渡"
    };
    const bandLabels = {
      ultra_low: "超低波动",
      low: "低波动",
      mid: "中等波动",
      elevated: "较高波动",
      high: "高波动"
    };
    const bandTargets = {
      ultra_low: "0.8σ，5秒内2次确认",
      low: "0.9σ，5秒内2次确认",
      mid: "1.0σ，5秒内2次确认",
      elevated: "1.2σ，8秒内3次确认",
      high: "停用正态回归，只判断趋势"
    };
    const zTarget = {
      ultra_low: 0.8,
      low: 0.9,
      mid: 1.0,
      elevated: 1.2
    }[band];
    const qualityOk = observed != null && coverage != null
      ? observed >= 90 && coverage >= 0.9
      : null;
    const items = [
      {
        key: "router_quality",
        label: "秒级数据质量",
        ok: qualityOk,
        value: observed == null || coverage == null ? "--" : `${fmt(observed, 1)}% / ${fmt(coverage * 100, 1)}%`,
        target: "价格覆盖 >= 90%，订单薄覆盖 >= 90%",
        help: "秒K或订单薄覆盖不足，策略不会建立行情状态。"
      },
      {
        key: "router_state",
        label: "行情路由",
        ok: state === "normal" || state === "trend_formation" ? true : null,
        value: stateLabels[state] || state,
        target: "进入正态震荡，或形成成熟趋势",
        help: "当前处于行情切换阶段，正态回归和趋势跟随都暂不执行。"
      },
      {
        key: "router_band",
        label: "动态波动档位",
        ok: band === "unknown" ? null : true,
        value: sigma == null ? (bandLabels[band] || "--") : `${bandLabels[band] || band} ${fmt(sigma, 2)}bp`,
        target: bandTargets[band] || "等待波动率数据",
        help: "策略会根据当前波动率自动切换入场σ和确认次数。"
      },
      {
        key: "router_reclaim",
        label: "假突破回归",
        ok: null,
        value: z == null ? "--" : `${fmt(z, 3)}σ`,
        target: zTarget == null ? "当前档位不做正态回归" : `先越过 ±${fmt(zTarget, 1)}σ，再回到区间`,
        help: "只看当前Z值不够，必须先发生越界，再确认价格回到正态区间。"
      },
      {
        key: "router_latch",
        label: "信号锁存",
        ok: latchActive ? true : null,
        value: latchActive ? `${latchSignal === "UP" ? "上涨" : "下跌"}，剩余确认中` : "尚未锁存",
        target: "候选信号锁存6秒，等待5秒执行点",
        help: "尚未出现满足确认次数的正态回归或成熟趋势信号。"
      },
      {
        key: "router_execution",
        label: "信号执行",
        ok: latchActive ? true : null,
        value: latchActive ? "等待最近执行点" : "尚无可执行信号",
        target: "锁存有效期6秒，每5秒检查一次",
        help: "候选信号锁存后，到下一个执行点直接下单，不再进行第二次盘口否决。"
      }
    ];
    return items.map(item => ({ ...item, tone: statusTone(item.ok) }));
  }
  const observed = asNumber(signal.observed_pct ?? signal.observed600_pct);
  const observedMin = asNumber(variant.observedMinPct ?? signal.min_observed_pct) ?? 88;
  const inside = asNumber(signal.inside1_ratio);
  const insidePct = inside == null ? null : inside * 100;
  const insideMin = (asNumber(variant.insideMin) ?? 0.55) * 100;
  const slope = asNumber(signal.center_slope_bps);
  const baseSlopeMax = asNumber(variant.centerSlopeMaxBps) ?? 8;
  const trendSlopeMax = variant.trendSpaceEnabled
    ? (asNumber(variant.trendSpaceCenterSlopeAbsMaxBps) ?? 6)
    : null;
  const slopeMax = trendSlopeMax == null ? baseSlopeMax : Math.min(baseSlopeMax, trendSlopeMax);
  const sigma = asNumber(signal.sigma_bps ?? signal.sigma10_bps);
  const sigmaMin = asNumber(variant.sigmaMinBps) ?? 5.8;
  const sigmaMax = asNumber(variant.sigmaMaxBps) ?? 55;
  const expand = asNumber(signal.sigma_expand);
  const expandMax = asNumber(variant.sigmaExpandMax) ?? 1.9;
  const obAge = asNumber(signal.ob_age_sec);
  const obMax = asNumber(variant.orderbookMaxAgeSec) ?? 3;

  const items = [
    {
      key: "coverage",
      label: "数据完整",
      ok: observed == null ? null : observed >= observedMin,
      value: observed == null ? "--" : `${fmt(observed, 1)}%`,
      target: `要求 >= ${fmt(observedMin, 0)}%`,
      help: "秒级数据要够完整，否则正态区间不可信。"
    },
    {
      key: "inside",
      label: "震荡成型",
      ok: insidePct == null ? null : insidePct >= insideMin,
      value: insidePct == null ? "--" : `${fmt(insidePct, 1)}%`,
      target: `要求 >= ${fmt(insideMin, 0)}%`,
      help: "过去10分钟多数时间要在区间内，才算震荡。"
    },
    {
      key: "trend",
      label: "趋势不过强",
      ok: slope == null ? null : Math.abs(slope) <= slopeMax,
      value: slope == null ? "--" : `${fmt(slope, 2)}bp`,
      target: `要求 -${fmt(slopeMax, 0)} 到 ${fmt(slopeMax, 0)}bp`,
      help: "中线快速上移或下移时，先不做回归。"
    },
    {
      key: "sigma",
      label: "波动合适",
      ok: sigma == null ? null : sigma >= sigmaMin && sigma <= sigmaMax,
      value: sigma == null ? "--" : `${fmt(sigma, 2)}bp`,
      target: `要求 ${fmt(sigmaMin, 1)}-${fmt(sigmaMax, 0)}bp`,
      help: sigma != null && sigma < sigmaMin
        ? "波动太小，空间不够，容易被噪声打掉。"
        : "波动太大时可能是突破行情，暂时不做回归。"
    },
    {
      key: "expand",
      label: "没有暴走",
      ok: expand == null ? null : expand <= expandMax,
      value: expand == null ? "--" : `${fmt(expand, 2)}x`,
      target: `要求 <= ${fmt(expandMax, 1)}x`,
      help: "波动突然放大，可能正在切换行情。"
    }
  ];

  if (obAge != null || signal.model_type === "second_normal_liquidity_orderbook_v1") {
    items.push({
      key: "orderbook",
      label: "订单薄新鲜",
      ok: obAge == null ? null : obAge <= obMax,
      value: obAge == null ? "--" : `${fmt(obAge, 1)}秒`,
      target: `要求 <= ${fmt(obMax, 0)}秒`,
      help: "订单薄太旧，支撑/压力判断不可靠。"
    });
  }

  return items.map(item => ({ ...item, tone: statusTone(item.ok) }));
}

export function signalTriggerPlan(signal) {
  if (!signal) return null;
  const price = asNumber(signal.price ?? signal.entry);
  const z = asNumber(signal.z_score);
  const center = asNumber(signal.normal_center);
  const sigmaBps = asNumber(signal.sigma_bps);
  const zEntry = asNumber(signal.z_entry) ?? 1.2;
  const normalLow = asNumber(signal.normal_low);
  const normalHigh = asNumber(signal.normal_high);
  const hasNormalBand = center != null || normalLow != null || normalHigh != null;
  if (!hasNormalBand) return null;
  const sigmaPrice = center != null && sigmaBps != null ? center * sigmaBps / 10000 : null;
  const lowerTrigger = center != null && sigmaPrice != null ? center - zEntry * sigmaPrice : normalLow;
  const upperTrigger = center != null && sigmaPrice != null ? center + zEntry * sigmaPrice : normalHigh;
  const downGapBps = price != null && lowerTrigger != null ? (price / lowerTrigger - 1) * 10000 : null;
  const upGapBps = price != null && upperTrigger != null ? (upperTrigger / price - 1) * 10000 : null;
  const bid = asNumber(signal.bid_qty_20);
  const ask = asNumber(signal.ask_qty_20);
  const imbalance = asNumber(signal.imbalance_20);
  const micro = asNumber(signal.micro_bps);
  const flow60 = asNumber(signal.flow_60);
  const obBias = imbalance == null
    ? "订单薄未确认"
    : imbalance <= -0.08 && micro != null && micro <= -0.001
      ? "当前偏空压"
      : imbalance >= 0.08 && micro != null && micro >= 0.001
        ? "当前偏多撑"
        : "订单薄中性";
  const nextSide = signal.signal
    ? directionText(signal.signal)
    : upGapBps != null && upGapBps <= 0
      ? "已上破，等待做空回收确认"
      : downGapBps != null && downGapBps <= 0
        ? "已下破，等待做多回收确认"
    : upGapBps != null && downGapBps != null
      ? (upGapBps < downGapBps ? "更接近做空触发" : "更接近做多触发")
      : "等待触发";
  return {
    price,
    z,
    center,
    lowerTrigger,
    upperTrigger,
    downGapBps,
    upGapBps,
    obBias,
    bid,
    ask,
    imbalance,
    micro,
    flow60,
    nextSide,
    zEntry
  };
}

export function signalHumanSummary(signal, variant = {}) {
  if (!signal) return "正在等待策略数据。";
  if (signal.signal) {
    return `已经出现${directionText(signal.signal)}信号，按${signal.duration || variant.duration || 10}分钟到期判断。`;
  }
  if (signal.model_type === "second_multi_normal_hf_stable_v1") {
    const state = signal.market_state_detail;
    if (state?.label && state?.detail) return `${state.label}。${state.detail}`;
    return signal.signal_detail || "等待下一完整分钟复核两条信号路径。";
  }
  if (signal.data_health_blocked) return "数据有延迟或缺口，策略先不下单。";
  if (signal.safety_blocked) return "风控正在拦截，暂时不下单。";
  if (signal.reason === "liq_wait_reclaim") {
    return "震荡区已经基本形成，现在等价格先冲出区间、再回到区间内，才给10分钟方向。";
  }
  if (signal.reason === "liq_strategy_gap") {
    return "上一笔信号后还在10分钟间隔内，避免同一段行情重复下单。";
  }
  if (signal.reason === "v9_original_candidate_gap") {
    return "原V2候选没有通过最终确认，正在候选冷却；V9补充分支仍会继续寻找独立信号。";
  }
  if (signal.reason === "liq_orderbook_missing" || signal.reason === "liq_orderbook_missing_or_stale") {
    return "订单薄数据不够新，暂时不根据流动性下单。";
  }
  if (String(signal.reason || "").startsWith("liq_v2_skip")) {
    return signal.quality_v2_rule || "这个候选信号质量不够，已经跳过。";
  }
  const blocked = signalReadinessItems(signal, variant).find(item => item.ok === false);
  if (blocked) {
    return `当前卡在：${blocked.label}。现在 ${blocked.value}，${blocked.target}。${blocked.help}`;
  }
  return signalReasonText(signal);
}

export function signalTimeText(time) {
  if (!time) return "--:--:--";
  const ms = typeof time === "string" && Number.isNaN(Number(time)) ? Date.parse(time) : Number(time);
  if (!Number.isFinite(ms)) return "--:--:--";
  return new Date(ms).toLocaleTimeString("zh-CN", {
    hour12: false,
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  });
}

export function displaySignalTime(signal) {
  if (!signal) return "--:--:--";
  return signal.time_shanghai || signalTimeText(signal.actionable_time || signal.time);
}

export function amountForSignal(strategyId, signal, payload, config) {
  const cfg = payload?._config || config || DEFAULT_CONFIG;
  const variants = payload?._strategyVariants || cfg.strategyVariants || DEFAULT_CONFIG.strategyVariants;
  const variant = Array.isArray(variants) ? variants.find(item => item.id === strategyId) : null;
  if (signal?.fixed_amount && signal.amount) return String(signal.amount);
  return String(variant?.amount || cfg.strategyAmounts?.[strategyId] || cfg.amount || DEFAULT_CONFIG.amount);
}

export function activeSignalFromPayload(payload) {
  if (!payload) return null;
  const variants = payload._strategyVariants || DEFAULT_CONFIG.strategyVariants;
  const signals = variants.map(item => payload[item.id]).filter(Boolean);
  return signals.find(item => item.signal) || signals[0] || null;
}

export function isShadowTrade(row) {
  const source = String(row?.source || "");
  return source.startsWith("shadow:") || source === "shadow" || row?.event === "shadow_trade";
}

export function tradeKind(row) {
  return isShadowTrade(row) ? "shadow" : "real";
}

export function statLine(stat) {
  if (!stat) return "--";
  const pnl = Number(stat.pnl || 0);
  const pnlPart = `${pnl > 0 ? "+" : ""}${fmt(pnl, 2)}U`;
  return `${fmtPct(stat.winRate, 1)} / ${stat.wins || 0}-${stat.losses || 0} / ${pnlPart}`;
}

export function useInterval(callback, delay) {
  const callbackRef = useRef(callback);
  useEffect(() => {
    callbackRef.current = callback;
  }, [callback]);
  useEffect(() => {
    if (!delay) return undefined;
    const id = window.setInterval(() => callbackRef.current(), delay);
    return () => window.clearInterval(id);
  }, [delay]);
}
