const crypto = require("crypto");

const BACKTEST_PRESETS = {
  SECOND_VW_CONFIRM: {
    STABLE_2700_20_ETA2_45: { wr: 68.66, tradesPerDay: 9.66, trades: 67, maxLoss: 3, sampleHours: 166.44 },
    FAST_2700_27_ETA3_45: { wr: 67.39, tradesPerDay: 13.27, trades: 92, maxLoss: 3, sampleHours: 166.44 }
  },
  SECOND_NORMAL_STATE_V11: {
    BANDWALK_2OF5_D5A5: { wr: 78.79, tradesPerDay: 1.65, trades: 33, maxLoss: 2, sampleHours: 480.0 },
    V15_STABLE_D5A5: { wr: 80.65, tradesPerDay: 1.35, trades: 31, maxLoss: 2, sampleHours: 506.7 },
    V19_OB_CONFIRM_D5A5: { wr: 83.33, tradesPerDay: 1.36, trades: 30, maxLoss: 1, sampleHours: 506.7 },
    V19_OB_CONFIRM_HF_G60: { wr: 80.56, tradesPerDay: 1.64, trades: 36, maxLoss: 3, sampleHours: 506.7 },
    V21_LOSS_DENSITY_3OF6_8H: { wr: 71.13, tradesPerDay: 4.21, trades: 97, maxLoss: 2, sampleHours: 552.62 }
  },
  SECOND_NORMAL_LOWVOL_V22: {
    LOWVOL_CONFIRM_SHADOW: { wr: 63.64, tradesPerDay: 0.92, trades: 11, maxLoss: 2, maxDrawdownU: 7, sampleHours: 288.0 }
  },
  SECOND_NORMAL_LIQUIDITY_ORDERBOOK_V1: {
    AUGMENTED_V9_SHARED: {
      wr: 60.87,
      tradesPerDay: 6.59,
      trades: 69,
      maxLoss: 3,
      maxDrawdownU: 18,
      sampleHours: 251.16,
      latestWr: 60,
      latestTradesPerDay: 10.24,
      latestTrades: 15,
      latestMaxDrawdownU: 16,
      shadowObservationRequired: true
    },
    AUGMENTED_V13_FREQ_SHADOW: {
      wr: 72.73,
      tradesPerDay: 6.0,
      trades: 66,
      maxLoss: 2,
      maxDrawdownU: 11,
      sampleHours: 264.0,
      latestWr: 72.73,
      latestTradesPerDay: 6.0,
      latestTrades: 66,
      latestMaxDrawdownU: 11,
      shadowObservationRequired: true
    },
    SHADOW_RECLAIM_W600_Z120_OB8_IN55: { wr: 65.0, tradesPerDay: 14.79, trades: 20, maxLoss: 2, maxDrawdownU: 13, sampleHours: 32.46 },
    BIDWALL_TRAP_FLIP_W600_Z120_OB8_IN55: { wr: 64.06, tradesPerDay: 22.17, trades: 64, maxLoss: 2, maxDrawdownU: 12, sampleHours: 69.29 },
    QUALITY_V2_CONSERVATIVE_W600_Z120_OB8_IN55: { wr: 77.78, tradesPerDay: 11.52, trades: 36, maxLoss: 2, maxDrawdownU: 11, sampleHours: 75.0 },
    TREND_SPACE_V3_PULLBACK_W600: { wr: 93.33, tradesPerDay: 3.38, trades: 15, maxLoss: 1, maxDrawdownU: 5, sampleHours: 106.54 }
  },
  SECOND_BRANCH_VOTE_STARTUP_V1: {
    STARTUP_SKIP_4: { wr: 67.86, tradesPerDay: 6.52, trades: 28, maxLoss: 3, maxDrawdownU: 17, sampleHours: 103.08 }
  },
  SECOND_MULTI_NORMAL_HF_STABLE_V1: {
    ADAPTIVE_SIGMA_8_Z05: { wr: 70.51, tradesPerDay: 15.77, trades: 78, maxLoss: 3, maxDrawdownU: 15, sampleHours: 118.68 }
  },
  SECOND_MULTISCALE_PHASE_GATE_V1: {
    CAUSAL_Q75: {
      wr: 73.33,
      tradesPerDay: 15.62,
      trades: 90,
      maxLoss: 2,
      maxDrawdownU: 10,
      sampleHours: 138.25,
      invalidated: true,
      forwardTrades: 8,
      forwardWins: 1,
      forwardWr: 12.5,
      forwardPnlU: -62,
      invalidReason: "规则来自事后分组选择，且上线后的未见数据显著失效"
    }
  }
};

// V13 的实盘身份由固定 ID 和关键参数指纹共同确定；任一参数漂移都只能继续影子观察。
const V13_STRATEGY_ID = "BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V13_FREQ";
const LEGACY_30M_STRATEGY_ID = "BTC_30min_SHADOW_CANDIDATE";
const V13_LIVE_FINGERPRINT = "946f06dce640c35677d16f1edc51417c4518658b2291aee6929a8bc0500bc913";
const V13_FINGERPRINT_FIELDS = Object.freeze({
  id: V13_STRATEGY_ID,
  base: "SECOND_NORMAL_LIQUIDITY_ORDERBOOK_V1",
  horizonSec: 600,
  gapSec: 600,
  v9AugmentedEnabled: true,
  v9OriginalRegimeVetoEnabled: true,
  v9OriginalVetoMatureDowntrend: true,
  v9OriginalVetoShortMigrationUpDown: true,
  v9OriginalAllowMatureDowntrendDownFlowMin: -0.2,
  v9SupplementLooseShortMigrationReversionEnabled: true,
  v9SupplementLooseMatureUptrendDownEnabled: true,
  v9SupplementMatureUptrendDownFlowMin: -0.3
});

const DEFAULT_STRATEGY_VARIANTS = [
  {
    ...V13_FINGERPRINT_FIELDS,
    label: "V13 高频增强（10分钟）",
    role: "v13_candidate",
    observationMode: "shadow",
    amount: "5",
    enabled: true,
    tradeEnabled: false,
    duration: "10"
  },
  {
    id: LEGACY_30M_STRATEGY_ID,
    base: "POC_NORMAL",
    label: "BTC 30分钟候选",
    role: "launch_candidate",
    observationMode: "shadow",
    amount: "5",
    enabled: true,
    tradeEnabled: false,
    horizonSec: 1800,
    gapSec: 1800,
    threshold: 0.55,
    rsiLo: 30,
    rsiHi: 70,
    agreeMode: "majority",
    modelLabel: "BTC_30min",
    duration: "30"
  }
];

const DEFAULT_TRADE_CONFIG = {
  amount: "5",
  strategyVariants: DEFAULT_STRATEGY_VARIANTS,
  duration: "30",
  autoTrade_10m: false,
  realTradingEnabled: false,
  shadowTradingEnabled: true
};

function sanitizePositiveInt(value, fallback, min, max) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  const out = Math.round(n);
  if (out < min || out > max) return fallback;
  return out;
}

function sanitizePositiveFloat(value, fallback, min, max) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  if (n < min || n > max) return fallback;
  return Number(n.toFixed(8));
}

function sanitizeTailPct(value, fallback = 0.2) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  const pct = n > 1 ? n / 100 : n;
  if (pct < 0.05 || pct > 0.45) return fallback;
  return Number(pct.toFixed(4));
}

function variantId(index) {
  return index === 0
    ? "BTC_10min_SECOND_VW_STABLE_2700_20_ETA2"
    : index === 1
      ? "BTC_10min_SECOND_VW_FAST_2700_27_ETA3"
      : `BTC_10min_SECOND_VW_CONFIRM_${index + 1}`;
}

function variantLabel(variant, index) {
  if (variant.label) return String(variant.label);
  if (variant.base === "POC_NORMAL") return index === 0 ? "BTC 30分钟稳定锚点" : "BTC 30分钟首发候选";
  if (variant.base === "SECOND_VALUE_AREA_SMART") return "SMART failed-break fade";
  if (variant.base === "SECOND_MULTI_NORMAL_HF_STABLE_V1") return "多周期动态正态高频稳定V1";
  if (variant.base === "SECOND_MULTISCALE_PHASE_GATE_V1") return "多周期迁移阶段 V1";
  if (variant.base === "SECOND_BRANCH_VOTE_STARTUP_V1") return "分支投票趋势启动V1";
  if (variant.base === "SECOND_NORMAL_ROUTER_V21") return "正态V21 亏损密度路由";
  if (variant.base === "SECOND_NORMAL_LOWVOL_V22") return "正态V22 低波动确认影子";
  if (variant.base === "SECOND_NORMAL_LIQUIDITY_ORDERBOOK_V1") return "正态流动性订单薄V1 影子";
  if (variant.base === "SECOND_NORMAL_STATE_V11") return "正态状态V11 bandwalk<6";
  return index === 0 ? "正态成交量确认 稳健" : index === 1 ? "正态成交量确认 高频" : "正态成交量确认";
}

function backtestForNormalizedVariant(variant) {
  const lookback = Number(variant.lookbackSec);
  const tailPct = Number(variant.tailPct);
  const targetBps = Number(variant.etaTargetBps);
  const waitSec = Number(variant.etaMaxWaitSec);
  if (lookback === 2700 && Math.abs(tailPct - 0.20) < 1e-9 && Math.abs(targetBps - 2.0) < 1e-9 && waitSec === 45) {
    return BACKTEST_PRESETS.SECOND_VW_CONFIRM.STABLE_2700_20_ETA2_45;
  }
  if (lookback === 2700 && Math.abs(tailPct - 0.27) < 1e-9 && Math.abs(targetBps - 3.0) < 1e-9 && waitSec === 45) {
    return BACKTEST_PRESETS.SECOND_VW_CONFIRM.FAST_2700_27_ETA3_45;
  }
  if (variant.base === "SECOND_NORMAL_STATE_V11") {
    if (variant.lossDensityEnabled === true) return BACKTEST_PRESETS.SECOND_NORMAL_STATE_V11.V21_LOSS_DENSITY_3OF6_8H;
    if (variant.stateGate === "v15_bw35_or_early_sigma18" && variant.confirmationVeto === "ob_confirm_weak" && Number(variant.gapSec) <= 60) {
      return BACKTEST_PRESETS.SECOND_NORMAL_STATE_V11.V19_OB_CONFIRM_HF_G60;
    }
    if (variant.stateGate === "v15_bw35_or_early_sigma18" && variant.confirmationVeto === "ob_confirm_weak") {
      return BACKTEST_PRESETS.SECOND_NORMAL_STATE_V11.V19_OB_CONFIRM_D5A5;
    }
    if (variant.stateGate === "v15_bw35_or_early_sigma18") return BACKTEST_PRESETS.SECOND_NORMAL_STATE_V11.V15_STABLE_D5A5;
    return BACKTEST_PRESETS.SECOND_NORMAL_STATE_V11.BANDWALK_2OF5_D5A5;
  }
  if (variant.base === "SECOND_NORMAL_ROUTER_V21") {
    if (variant.lossDensityEnabled === true) return BACKTEST_PRESETS.SECOND_NORMAL_STATE_V11.V21_LOSS_DENSITY_3OF6_8H;
  }
  if (variant.base === "SECOND_NORMAL_LOWVOL_V22") {
    return BACKTEST_PRESETS.SECOND_NORMAL_LOWVOL_V22.LOWVOL_CONFIRM_SHADOW;
  }
  if (variant.base === "SECOND_NORMAL_LIQUIDITY_ORDERBOOK_V1") {
    if (variant.v9AugmentedEnabled === true) {
      if (
        variant.v9SupplementLooseShortMigrationReversionEnabled === true
        && variant.v9SupplementLooseMatureUptrendDownEnabled === true
        && Number(variant.v9OriginalAllowMatureDowntrendDownFlowMin) < 0
      ) {
        return BACKTEST_PRESETS.SECOND_NORMAL_LIQUIDITY_ORDERBOOK_V1.AUGMENTED_V13_FREQ_SHADOW;
      }
      return BACKTEST_PRESETS.SECOND_NORMAL_LIQUIDITY_ORDERBOOK_V1.AUGMENTED_V9_SHARED;
    }
    if (variant.trendSpaceEnabled === true) {
      return BACKTEST_PRESETS.SECOND_NORMAL_LIQUIDITY_ORDERBOOK_V1.TREND_SPACE_V3_PULLBACK_W600;
    }
    if (variant.qualityV2Enabled !== false) {
      return BACKTEST_PRESETS.SECOND_NORMAL_LIQUIDITY_ORDERBOOK_V1.QUALITY_V2_CONSERVATIVE_W600_Z120_OB8_IN55;
    }
    if (variant.bidwallTrapEnabled !== false) {
      return BACKTEST_PRESETS.SECOND_NORMAL_LIQUIDITY_ORDERBOOK_V1.BIDWALL_TRAP_FLIP_W600_Z120_OB8_IN55;
    }
    return BACKTEST_PRESETS.SECOND_NORMAL_LIQUIDITY_ORDERBOOK_V1.SHADOW_RECLAIM_W600_Z120_OB8_IN55;
  }
  if (variant.base === "SECOND_BRANCH_VOTE_STARTUP_V1") {
    return BACKTEST_PRESETS.SECOND_BRANCH_VOTE_STARTUP_V1.STARTUP_SKIP_4;
  }
  if (variant.base === "SECOND_MULTI_NORMAL_HF_STABLE_V1") {
    return BACKTEST_PRESETS.SECOND_MULTI_NORMAL_HF_STABLE_V1.ADAPTIVE_SIGMA_8_Z05;
  }
  if (variant.base === "SECOND_MULTISCALE_PHASE_GATE_V1") {
    return BACKTEST_PRESETS.SECOND_MULTISCALE_PHASE_GATE_V1.CAUSAL_Q75;
  }
  return null;
}

function normalizeVariant(raw, index) {
  const input = raw && typeof raw === "object" ? raw : {};
  const amount = Number(input.amount);
  const horizonSec = sanitizePositiveInt(input.horizonSec, 600, 60, 7200);
  const allowedBases = new Set([
    "POC_NORMAL",
    "SECOND",
    "SECOND_VW_CONFIRM",
    "SECOND_VALUE_AREA_SMART",
    "SECOND_NORMAL_STATE_V11",
    "SECOND_NORMAL_ROUTER_V21",
    "SECOND_NORMAL_LOWVOL_V22",
    "SECOND_NORMAL_LIQUIDITY_ORDERBOOK_V1",
    "SECOND_BRANCH_VOTE_STARTUP_V1",
    "SECOND_MULTI_NORMAL_HF_STABLE_V1",
    "SECOND_MULTISCALE_PHASE_GATE_V1"
  ]);
  const base = allowedBases.has(input.base) ? input.base : "SECOND_VW_CONFIRM";
  const out = {
    id: String(input.id || variantId(index)),
    base,
    label: variantLabel(input, index),
    role: String(input.role || "legacy"),
    observationMode: String(input.observationMode || "watch"),
    amount: Number.isFinite(amount) && amount > 0 ? String(input.amount) : "5",
    tailPct: sanitizeTailPct(input.tailPct, index === 1 ? 0.27 : 0.20),
    duration: String(Math.max(1, Math.round(horizonSec / 60))),
    enabled: input.enabled !== false,
    tradeEnabled: input.tradeEnabled !== false,
    lookbackSec: sanitizePositiveInt(input.lookbackSec, 2700, 60, 21600),
    horizonSec,
    gapSec: sanitizePositiveInt(input.gapSec, horizonSec, 0, 21600),
    secondFilter: String(input.secondFilter || "none"),
    zoneFilter: String(input.zoneFilter || "none"),
    sigmaMinBps: sanitizePositiveFloat(input.sigmaMinBps, 0, 0, 10000),
    sigmaMaxBps: sanitizePositiveFloat(input.sigmaMaxBps, 9999, 0, 10000),
    upReversalConfirmBps: sanitizePositiveFloat(input.upReversalConfirmBps, 0.0, 0, 20),
    upReversalConfirmMaxSec: sanitizePositiveInt(input.upReversalConfirmMaxSec, 20, 1, 300),
    incidentFilterEnabled: input.incidentFilterEnabled !== false,
    incidentFilterMode: String(input.incidentFilterMode || "directional_only"),
    incidentWindowSec: sanitizePositiveInt(input.incidentWindowSec, 10, 2, 120),
    incidentMinMoveBps: sanitizePositiveFloat(input.incidentMinMoveBps, 10, 0, 100),
    incidentMinVolumeQuantile: sanitizePositiveFloat(input.incidentMinVolumeQuantile, 0.99, 0.5, 0.9999),
    incidentMinFlowImbalance: sanitizePositiveFloat(input.incidentMinFlowImbalance, 0.8, 0, 1),
    incidentCooldownSec: sanitizePositiveInt(input.incidentCooldownSec, 10, 0, 600),
    backtest: null
  };
  if (base === "POC_NORMAL") {
    // 生产实盘固定白名单：只有 30m 首发候选可开启，任何其他 30m/10m 标识即使误传 true 也会被强制关闭。
    out.tradeEnabled = out.id === "BTC_30min_SHADOW_CANDIDATE" && input.tradeEnabled === true;
    out.horizonSec = 1800;
    out.duration = "30";
    out.gapSec = sanitizePositiveInt(input.gapSec, 1800, 0, 21600);
    out.threshold = sanitizePositiveFloat(input.threshold, index === 0 ? 0.58 : 0.55, 0.5, 0.99);
    out.rsiLo = sanitizePositiveFloat(input.rsiLo, 30, 0, 100);
    out.rsiHi = sanitizePositiveFloat(input.rsiHi, 70, 0, 100);
    out.agreeMode = input.agreeMode === "majority" ? "majority" : "all3";
    out.modelLabel = "BTC_30min";
    // 30分钟候选如果明确开启实盘，应保留 live 标记；否则默认保持 shadow/watch 观察状态。
    // 这样 liveStrategyIds() 才能用 observationMode=live + tradeEnabled=true 共同决定真实信号白名单。
    out.observationMode = input.observationMode === "live" && out.tradeEnabled === true
      ? "live"
      : (out.role === "launch_candidate" ? "shadow" : "watch");
  }
  if (base === "SECOND_VW_CONFIRM") {
    out.etaTargetBps = sanitizePositiveFloat(input.etaTargetBps, index === 1 ? 3.0 : 2.0, 0.1, 20);
    out.etaMaxWaitSec = sanitizePositiveInt(input.etaMaxWaitSec, 45, 1, 600);
  }
  if (base === "SECOND_VALUE_AREA_SMART") {
    out.valueAreaSec = sanitizePositiveInt(input.valueAreaSec, 3600, 300, 21600);
    out.binSize = sanitizePositiveFloat(input.binSize, 10, 1, 1000);
    out.valuePct = sanitizePositiveFloat(input.valuePct, 0.70, 0.5, 0.95);
    out.normalWindowSec = sanitizePositiveInt(input.normalWindowSec, 600, 120, 7200);
    out.normalCoverage = sanitizePositiveFloat(input.normalCoverage, 0.70, 0.5, 0.95);
    out.mode = String(input.mode || "failed_break_fade");
    out.minEdgeBps = sanitizePositiveFloat(input.minEdgeBps, 1, 0, 100);
    out.minFlow = sanitizePositiveFloat(input.minFlow, 0.05, 0, 1);
    out.minTrendBps = sanitizePositiveFloat(input.minTrendBps, 1.0, 0, 100);
    out.minVolumeRatio = sanitizePositiveFloat(input.minVolumeRatio, 1.15, 0, 100);
    out.minObImbalance = sanitizePositiveFloat(input.minObImbalance, 0.05, 0, 1);
    out.minMicroBps = sanitizePositiveFloat(input.minMicroBps, 0.001, 0, 10);
    out.maxAgainstObImbalance = sanitizePositiveFloat(input.maxAgainstObImbalance, 0.25, 0, 1);
    out.maxAgainstFlow = sanitizePositiveFloat(input.maxAgainstFlow, 0.35, 0, 1);
    out.retestSec = sanitizePositiveInt(input.retestSec, 180, 10, 3600);
    out.retestBps = sanitizePositiveFloat(input.retestBps, 4.0, 0, 100);
    out.breakHoldSec = sanitizePositiveInt(input.breakHoldSec, 30, 5, 600);
    out.reclaimBps = sanitizePositiveFloat(input.reclaimBps, 0.8, 0, 100);
    out.absorptionMaxProgressBps = sanitizePositiveFloat(input.absorptionMaxProgressBps, 1.5, 0, 100);
    out.lossPauseAfter = sanitizePositiveInt(input.lossPauseAfter, 2, 0, 20);
    out.lossPauseSec = sanitizePositiveInt(input.lossPauseSec, 1800, 0, 21600);
    out.sigmaMinBps = sanitizePositiveFloat(input.sigmaMinBps, 8, 0, 10000);
    out.sigmaMaxBps = sanitizePositiveFloat(input.sigmaMaxBps, 80, 0, 10000);
  }
  if (base === "SECOND_NORMAL_STATE_V11") {
    out.lookbackSec = sanitizePositiveInt(input.lookbackSec, 10800, 3600, 21600);
    out.horizonSec = sanitizePositiveInt(input.horizonSec, 600, 60, 7200);
    out.duration = String(Math.max(1, Math.round(out.horizonSec / 60)));
    out.gapSec = sanitizePositiveInt(input.gapSec, 600, 0, 21600);
    out.confirmDelaySec = sanitizePositiveInt(input.confirmDelaySec, 5, 1, 60);
    out.maxAdverseBps = sanitizePositiveFloat(input.maxAdverseBps, 5, 0, 50);
    out.signalHoldSec = sanitizePositiveInt(input.signalHoldSec, 55, 5, 300);
    out.bandwalkMax = sanitizePositiveFloat(input.bandwalkMax, 6, 1, 20);
    out.minConsensusVotes = sanitizePositiveInt(input.minConsensusVotes, 2, 1, 5);
    out.stateGate = String(input.stateGate || "edge_persistence_lt6");
    out.confirmationVeto = String(input.confirmationVeto || input.confirmation_veto || "none");
    out.lossDensityEnabled = input.lossDensityEnabled === true;
    out.lossDensityWindow = sanitizePositiveInt(input.lossDensityWindow, 6, 2, 50);
    out.lossDensityLosses = sanitizePositiveInt(input.lossDensityLosses, 3, 1, 50);
    if (out.lossDensityLosses > out.lossDensityWindow) out.lossDensityLosses = out.lossDensityWindow;
    out.lossDensityMinTrades = sanitizePositiveInt(
      input.lossDensityMinTrades,
      Math.min(out.lossDensityWindow, out.lossDensityLosses + 1),
      out.lossDensityLosses,
      out.lossDensityWindow
    );
    out.lossDensityCooldownSec = sanitizePositiveInt(input.lossDensityCooldownSec, 28800, 60, 86400);
    out.lossDensityLookbackHours = sanitizePositiveInt(input.lossDensityLookbackHours, 72, 1, 720);
  }
  if (base === "SECOND_NORMAL_ROUTER_V21" || base === "SECOND_NORMAL_LOWVOL_V22") {
    out.lookbackSec = sanitizePositiveInt(input.lookbackSec, 4200, 600, 21600);
    out.horizonSec = sanitizePositiveInt(input.horizonSec, 600, 60, 7200);
    out.duration = String(Math.max(1, Math.round(out.horizonSec / 60)));
    out.gapSec = sanitizePositiveInt(input.gapSec, 600, 0, 21600);
    out.routeLookbackSec = sanitizePositiveInt(input.routeLookbackSec, 4200, 600, 21600);
    out.r10WindowSec = sanitizePositiveInt(input.r10WindowSec, 600, 60, 3600);
    out.r10CapBps = sanitizePositiveFloat(input.r10CapBps, 42, 1, 500);
    out.downR10CapBps = sanitizePositiveFloat(input.downR10CapBps, 35, 1, 500);
    out.midRouteSigmaCapBps = sanitizePositiveFloat(input.midRouteSigmaCapBps, 20, 1, 500);
    out.minObservedPct = sanitizePositiveFloat(input.minObservedPct, 88, 0, 100);
    out.lossDensityEnabled = input.lossDensityEnabled !== false;
    out.lossDensityWindow = sanitizePositiveInt(input.lossDensityWindow, 6, 2, 50);
    out.lossDensityLosses = sanitizePositiveInt(input.lossDensityLosses, 3, 1, 50);
    if (out.lossDensityLosses > out.lossDensityWindow) out.lossDensityLosses = out.lossDensityWindow;
    out.lossDensityMinTrades = sanitizePositiveInt(
      input.lossDensityMinTrades,
      Math.min(out.lossDensityWindow, out.lossDensityLosses + 1),
      out.lossDensityLosses,
      out.lossDensityWindow
    );
    out.lossDensityCooldownSec = sanitizePositiveInt(input.lossDensityCooldownSec, 28800, 60, 86400);
    out.lossDensityLookbackHours = sanitizePositiveInt(input.lossDensityLookbackHours, 72, 1, 720);
    out.lossStreakEnabled = input.lossStreakEnabled !== false;
    out.lossStreakCount = sanitizePositiveInt(input.lossStreakCount, 2, 1, 20);
    out.lossStreakCooldownSec = sanitizePositiveInt(input.lossStreakCooldownSec, 3600, 60, 86400);
    out.vetoLowUp = base === "SECOND_NORMAL_ROUTER_V21" ? input.vetoLowUp !== false : input.vetoLowUp === true;
    if (base === "SECOND_NORMAL_LOWVOL_V22") {
      out.tradeEnabled = input.tradeEnabled === true;
      out.lossDensityEnabled = false;
      out.vetoLowUp = input.vetoLowUp === true;
      out.lowVolRouteSigmaMaxBps = sanitizePositiveFloat(input.lowVolRouteSigmaMaxBps, 10, 1, 100);
      out.lowVolConfirmSec = sanitizePositiveInt(input.lowVolConfirmSec, 15, 1, 120);
      out.lowVolReversionBps = sanitizePositiveFloat(input.lowVolReversionBps, 0.5, 0, 20);
      out.lowVolBreakoutBps = sanitizePositiveFloat(input.lowVolBreakoutBps, 1.5, 0, 50);
    }
  }
  if (base === "SECOND_NORMAL_LIQUIDITY_ORDERBOOK_V1") {
    out.v9AugmentedEnabled = input.v9AugmentedEnabled === true;
    out.label = "动态正态/趋势订单薄锁存 V2";
    out.label = input.label ? String(input.label) : (out.v9AugmentedEnabled ? "当前V2增强 V9（影子观察）" : out.label);
    out.tradeEnabled = input.tradeEnabled === true;
    out.lookbackSec = sanitizePositiveInt(input.lookbackSec, 600, 120, 7200);
    out.horizonSec = sanitizePositiveInt(input.horizonSec, 600, 60, 7200);
    out.duration = String(Math.max(1, Math.round(out.horizonSec / 60)));
    out.gapSec = sanitizePositiveInt(input.gapSec, out.horizonSec, 0, 21600);
    out.normalWindowSec = sanitizePositiveInt(input.normalWindowSec, 600, 120, 7200);
    out.zEntry = sanitizePositiveFloat(input.zEntry, 1.2, 0.5, 4);
    out.zReclaim = sanitizePositiveFloat(input.zReclaim, 0.85, 0.1, 3);
    out.retestSec = sanitizePositiveInt(input.retestSec, 120, 10, 1800);
    out.insideMin = sanitizePositiveFloat(input.insideMin, 0.55, 0.1, 1);
    out.observedMinPct = sanitizePositiveFloat(input.observedMinPct, 88, 0, 100);
    out.centerSlopeSec = sanitizePositiveInt(input.centerSlopeSec, 300, 30, 3600);
    out.centerSlopeMaxBps = sanitizePositiveFloat(input.centerSlopeMaxBps, 8, 0, 200);
    out.sigmaMinBps = sanitizePositiveFloat(input.sigmaMinBps, 5.8, 0, 10000);
    out.sigmaMaxBps = sanitizePositiveFloat(input.sigmaMaxBps, 55, 0, 10000);
    out.sigmaExpandMax = sanitizePositiveFloat(input.sigmaExpandMax, 1.9, 0.1, 20);
    out.orderbookMaxAgeSec = sanitizePositiveInt(input.orderbookMaxAgeSec, 3, 1, 30);
    out.obImbalanceMin = sanitizePositiveFloat(input.obImbalanceMin, 0.08, 0, 1);
    out.microMinBps = sanitizePositiveFloat(input.microMinBps, 0.001, 0, 10);
    out.wallRatioMin = sanitizePositiveFloat(input.wallRatioMin, 1.0, 0, 10);
    out.flowGuard = sanitizePositiveFloat(input.flowGuard, 0.12, 0, 1);
    out.trueBreakFlow = sanitizePositiveFloat(input.trueBreakFlow, 0.28, 0, 1);
    out.trueBreakImbalance = sanitizePositiveFloat(input.trueBreakImbalance, 0.28, 0, 1);
    out.bidwallTrapEnabled = input.bidwallTrapEnabled !== false;
    out.bidwallTrapRet300MaxBps = sanitizePositiveFloat(input.bidwallTrapRet300MaxBps, -5, -200, 0);
    out.bidwallTrapBid20Chg60Min = sanitizePositiveFloat(input.bidwallTrapBid20Chg60Min, 2, 0, 100);
    out.bidwallTrapRet600MinBps = sanitizePositiveFloat(input.bidwallTrapRet600MinBps, -20, -500, 0);
    out.qualityV2Enabled = input.qualityV2Enabled !== false;
    out.qualityV2DownBid20Chg60Min = sanitizePositiveFloat(input.qualityV2DownBid20Chg60Min, -0.7, -10, 10);
    out.qualityV2UpFlow60Min = sanitizePositiveFloat(input.qualityV2UpFlow60Min, -0.063, -1, 1);
    out.trendSpaceEnabled = input.trendSpaceEnabled === true;
    out.trendSpaceSigmaExpandMax = sanitizePositiveFloat(input.trendSpaceSigmaExpandMax, 1.6, 0.1, 20);
    out.trendSpaceCenterSlopeAbsMaxBps = sanitizePositiveFloat(input.trendSpaceCenterSlopeAbsMaxBps, 6, 0, 200);
    out.trendSpaceInsideMax = sanitizePositiveFloat(input.trendSpaceInsideMax, 0.75, 0.1, 1);
    out.trendSpaceTrendRet1800Bps = sanitizePositiveFloat(input.trendSpaceTrendRet1800Bps, 15, 0, 500);
    out.trendSpaceUpPos1800Min = sanitizePositiveFloat(input.trendSpaceUpPos1800Min, 0.72, 0, 1);
    out.trendSpaceDownPos1800Max = sanitizePositiveFloat(input.trendSpaceDownPos1800Max, 0.28, 0, 1);
    out.trendSpaceBlockCountertrend = input.trendSpaceBlockCountertrend !== false;
    out.trendSpaceBlockUpperFadePullback = input.trendSpaceBlockUpperFadePullback !== false;
    out.trendSpaceShortRet600UpBps = sanitizePositiveFloat(input.trendSpaceShortRet600UpBps, 12, 0, 500);
    out.trendSpaceShortPos600Min = sanitizePositiveFloat(input.trendSpaceShortPos600Min, 0.65, 0, 1);
    out.startupSkipEnabled = input.startupSkipEnabled === true;
    out.startupSkipThreshold = sanitizePositiveInt(input.startupSkipThreshold, 4, 1, 6);
    out.liquidityMode = String(input.liquidityMode || "reclaim");
    out.v9EfficiencyMin = sanitizePositiveFloat(input.v9EfficiencyMin, 0.60, 0.1, 1);
    out.v9TrendStrengthMin = sanitizePositiveFloat(input.v9TrendStrengthMin, 1.25, 0.1, 10);
    out.v9OpposingMinBps = sanitizePositiveFloat(input.v9OpposingMinBps, 2.0, 0, 100);
    out.v9Z30Min = sanitizePositiveFloat(input.v9Z30Min, 1.0, 0.1, 5);
    out.v9VolumeRatioMin = sanitizePositiveFloat(input.v9VolumeRatioMin, 0.80, 0.1, 10);
    out.v9BookCoverageMin = sanitizePositiveFloat(input.v9BookCoverageMin, 0.90, 0.5, 1);
    out.v9BookVotesMin = sanitizePositiveInt(input.v9BookVotesMin, 2, 1, 3);
    out.v9MaxEmitAgeSec = sanitizePositiveInt(input.v9MaxEmitAgeSec, 8, 1, 30);
    out.v9SupplementMinAbsNormalZ = sanitizePositiveFloat(input.v9SupplementMinAbsNormalZ, 0, 0, 5);
    out.v9OriginalRegimeVetoEnabled = input.v9OriginalRegimeVetoEnabled === true;
    out.v9OriginalVetoMatureDowntrend = input.v9OriginalVetoMatureDowntrend !== false;
    out.v9OriginalVetoShortMigrationUpDown = input.v9OriginalVetoShortMigrationUpDown !== false;
    out.v9OriginalAllowMatureDowntrendDownFlowMin = input.v9OriginalAllowMatureDowntrendDownFlowMin == null
      ? null
      : sanitizePositiveFloat(input.v9OriginalAllowMatureDowntrendDownFlowMin, -0.2, -1, 0);
    out.v9SupplementLooseShortMigrationReversionEnabled = input.v9SupplementLooseShortMigrationReversionEnabled === true;
    out.v9SupplementLooseMatureUptrendDownEnabled = input.v9SupplementLooseMatureUptrendDownEnabled === true;
    out.v9SupplementMatureUptrendDownFlowMin = sanitizePositiveFloat(input.v9SupplementMatureUptrendDownFlowMin, -0.3, -1, 1);
    out.lossDensityEnabled = false;
    out.lossStreakEnabled = false;
  }
  if (base === "SECOND_BRANCH_VOTE_STARTUP_V1") {
    out.label = input.label ? String(input.label) : "分支投票趋势启动V1";
    out.tradeEnabled = input.tradeEnabled === true;
    out.lookbackSec = sanitizePositiveInt(input.lookbackSec, 7200, 3600, 21600);
    out.horizonSec = sanitizePositiveInt(input.horizonSec, 600, 60, 7200);
    out.duration = String(Math.max(1, Math.round(out.horizonSec / 60)));
    out.gapSec = sanitizePositiveInt(input.gapSec, out.horizonSec, 0, 21600);
    out.normalWindowSec = sanitizePositiveInt(input.normalWindowSec, 600, 120, 7200);
    out.orderbookMaxAgeSec = sanitizePositiveInt(input.orderbookMaxAgeSec, 3, 1, 30);
    out.minVotes = sanitizePositiveInt(input.minVotes, 2, 1, 4);
    out.startupSkipThreshold = sanitizePositiveInt(input.startupSkipThreshold, 4, 1, 6);
    out.rulePath = String(input.rulePath || "data/branch_vote_startup_rules.json");
    out.lossDensityEnabled = false;
    out.lossStreakEnabled = false;
  }
  if (base === "SECOND_MULTI_NORMAL_HF_STABLE_V1") {
    out.label = input.label ? String(input.label) : "多周期动态正态高频稳定V1";
    out.tradeEnabled = input.tradeEnabled === true;
    out.lookbackSec = sanitizePositiveInt(input.lookbackSec, 7200, 3600, 21600);
    out.horizonSec = sanitizePositiveInt(input.horizonSec, 600, 60, 7200);
    out.duration = String(Math.max(1, Math.round(out.horizonSec / 60)));
    out.gapSec = sanitizePositiveInt(input.gapSec, out.horizonSec, 0, 21600);
    out.normalWindowSec = sanitizePositiveInt(input.normalWindowSec, 600, 120, 7200);
    out.orderbookMaxAgeSec = sanitizePositiveInt(input.orderbookMaxAgeSec, 3, 1, 30);
    out.lowVolSigmaMaxBps = sanitizePositiveFloat(input.lowVolSigmaMaxBps, 3, 0.1, 100);
    out.lowVolRangeMaxBps = sanitizePositiveFloat(input.lowVolRangeMaxBps, 20, 1, 500);
    out.lowVolAbsRet10MaxBps = sanitizePositiveFloat(input.lowVolAbsRet10MaxBps, 5, 0, 100);
    out.lowVolZMin = sanitizePositiveFloat(input.lowVolZMin, 1.2, 0.1, 4);
    out.lowVolZMax = sanitizePositiveFloat(input.lowVolZMax, 1.8, 0.1, 6);
    if (out.lowVolZMax < out.lowVolZMin) out.lowVolZMax = out.lowVolZMin;
    out.lowVolMinSignedFlow = sanitizePositiveFloat(input.lowVolMinSignedFlow, 0, -1, 1);
    out.lowVolMaxAdverseRet30Sigma = sanitizePositiveFloat(input.lowVolMaxAdverseRet30Sigma, 0.5, 0, 3);
    out.trendBaseZMin = sanitizePositiveFloat(input.trendBaseZMin, 1.2, 0.1, 6);
    out.trendHighVolSigmaMinBps = sanitizePositiveFloat(input.trendHighVolSigmaMinBps, 8, 0.1, 100);
    out.trendHighVolZMin = sanitizePositiveFloat(input.trendHighVolZMin, 0.5, 0.1, 6);
    out.trendMinSignedFlow = sanitizePositiveFloat(input.trendMinSignedFlow, 0.12, -1, 1);
    out.trendMaxSignedBook = sanitizePositiveFloat(input.trendMaxSignedBook, 0.08, -1, 1);
    out.incidentFilterEnabled = false;
    out.lossDensityEnabled = false;
    out.lossStreakEnabled = false;
  }
  if (base === "SECOND_MULTISCALE_PHASE_GATE_V1") {
    out.label = input.label ? String(input.label) : "多周期迁移阶段 V1";
    out.tradeEnabled = input.tradeEnabled === true;
    out.lookbackSec = sanitizePositiveInt(input.lookbackSec, 7800, 5400, 21600);
    out.horizonSec = sanitizePositiveInt(input.horizonSec, 600, 60, 7200);
    out.duration = String(Math.max(1, Math.round(out.horizonSec / 60)));
    out.gapSec = sanitizePositiveInt(input.gapSec, out.horizonSec, 0, 21600);
    out.orderbookMaxAgeSec = sanitizePositiveInt(input.orderbookMaxAgeSec, 3, 1, 30);
    out.maxEmitAgeSec = sanitizePositiveInt(input.maxEmitAgeSec, 8, 1, 30);
    out.phaseLookbackSec = sanitizePositiveInt(input.phaseLookbackSec, 3600, 1800, 7200);
    out.maturityHistorySec = sanitizePositiveInt(input.maturityHistorySec, 3600, 1800, 7200);
    out.maturityMinPeriods = sanitizePositiveInt(input.maturityMinPeriods, 1800, 600, 7200);
    out.maturityQuantile = sanitizePositiveFloat(input.maturityQuantile, 0.75, 0.5, 0.95);
    out.minFlow60 = sanitizePositiveFloat(input.minFlow60, 0.08, -1, 1);
    out.minImbalance20 = sanitizePositiveFloat(input.minImbalance20, 0.05, -1, 1);
    out.minMicropriceBps = sanitizePositiveFloat(input.minMicropriceBps, 0, -10, 10);
    out.minVolumeRatio = sanitizePositiveFloat(input.minVolumeRatio, 0.8, 0, 100);
    out.incidentFilterEnabled = false;
    out.lossDensityEnabled = false;
    out.lossStreakEnabled = false;
  }
  out.backtest = backtestForNormalizedVariant(out);
  if (base === "SECOND_VALUE_AREA_SMART" && !out.backtest) {
    out.backtest = { wr: 69.05, tradesPerDay: 42, trades: 42, maxLoss: 2, sampleHours: 26 };
  }
  return out;
}

function normalizeVariants(input) {
  const raw = Array.isArray(input?.strategyVariants) ? input.strategyVariants : [];
  const incomingById = new Map(raw.map(item => [String(item?.id || ""), item]));
  return DEFAULT_STRATEGY_VARIANTS.map((defaults, index) => {
    const incoming = incomingById.get(defaults.id);
    // 槽位身份和策略族固定；页面只能修改显式开关与常规参数，不能把其他策略伪装成 V13。
    return normalizeVariant({
      ...defaults,
      ...(incoming || {}),
      id: defaults.id,
      base: defaults.base,
      role: defaults.role,
      modelLabel: defaults.modelLabel
    }, index);
  });
}

function normalizeTradeConfig(value = {}) {
  const input = value && typeof value === "object" ? value : {};
  const cfg = {
    ...DEFAULT_TRADE_CONFIG,
    amount: input.amount !== undefined ? String(input.amount) : DEFAULT_TRADE_CONFIG.amount,
    strategyVariants: normalizeVariants(input),
    duration: input.duration !== undefined ? String(input.duration) : DEFAULT_TRADE_CONFIG.duration,
    autoTrade_10m: input.autoTrade_10m !== undefined ? !!input.autoTrade_10m : !!DEFAULT_TRADE_CONFIG.autoTrade_10m,
    realTradingEnabled: input.realTradingEnabled !== undefined ? !!input.realTradingEnabled : !!DEFAULT_TRADE_CONFIG.realTradingEnabled,
    shadowTradingEnabled: input.shadowTradingEnabled !== undefined ? !!input.shadowTradingEnabled : !!DEFAULT_TRADE_CONFIG.shadowTradingEnabled
  };
  if (input.autoTrade !== undefined && input.autoTrade_10m === undefined) cfg.autoTrade_10m = !!input.autoTrade;
  if (input.realTradingOverride !== undefined && input.realTradingEnabled === undefined) cfg.realTradingEnabled = !!input.realTradingOverride;
  // 默认周期允许由页面保存，不再因为保留 30m 候选而强制改回 30 分钟。
  return cfg;
}

function parseBool(value) {
  return value === true || value === "true";
}

function strategyVariants(config) {
  return normalizeTradeConfig(config).strategyVariants;
}

function observedStrategyIds(config) {
  return strategyVariants(config).filter(v => v.enabled).map(v => v.id);
}

function v13Fingerprint(variant) {
  // 固定字段顺序后再计算 SHA-256，保证前后端多余字段不会影响实盘身份校验。
  const payload = Object.fromEntries(
    Object.keys(V13_FINGERPRINT_FIELDS).map(key => [key, variant?.[key]])
  );
  return crypto.createHash("sha256").update(JSON.stringify(payload)).digest("hex");
}

function liveStrategyIds(config) {
  // 两个策略槽位各自使用 enabled + tradeEnabled + observationMode=live 控制，互不覆盖：
  // V13 还必须精确匹配固定指纹；原有 30m 仅保留固定 ID、策略族和 30 分钟周期。
  // 修复：仅标记为 shadow/观察的策略，即使 tradeEnabled 被误置为 true，也不能进入真实下单白名单。
  return strategyVariants(config)
    .filter(variant => {
      if (variant.enabled !== true || variant.tradeEnabled !== true || variant.observationMode !== "live") return false;
      if (variant.id === V13_STRATEGY_ID) {
        return v13Fingerprint(variant) === V13_LIVE_FINGERPRINT;
      }
      return (
        variant.id === LEGACY_30M_STRATEGY_ID
        && variant.base === "POC_NORMAL"
        && String(variant.duration) === "30"
      );
    })
    .map(variant => variant.id);
}

function amountForStrategyConfig(strategyId, cfg) {
  const found = strategyVariants(cfg).find(v => v.id === strategyId);
  return String((found && found.amount) || normalizeTradeConfig(cfg).amount);
}

function publicTradeConfig(config) {
  const cfg = normalizeTradeConfig(config);
  return {
    // 明确声明页面主策略，避免兼容字段 autoTrade_10m 被误解为当前主策略。
    primaryStrategy: {
      family: "V13",
      horizonMin: 10,
      mode: "v13_with_30m_candidate",
      realTradingAllowed: true,
      stableAnchorId: V13_STRATEGY_ID,
      launchCandidateId: "BTC_30min_SHADOW_CANDIDATE"
    },
    amount: cfg.amount,
    strategyVariants: cfg.strategyVariants,
    duration: cfg.duration,
    realTradingEnabled: cfg.realTradingEnabled,
    shadowTradingEnabled: cfg.shadowTradingEnabled,
    strategyAmounts: Object.fromEntries(cfg.strategyVariants.map(v => [v.id, v.amount])),
    strategyParams: Object.fromEntries(cfg.strategyVariants.map(v => [v.id, {
      role: v.role,
      observationMode: v.observationMode,
      tailPct: v.tailPct,
      tradeEnabled: v.tradeEnabled !== false,
      lookbackSec: v.lookbackSec,
      horizonSec: v.horizonSec,
      gapSec: v.gapSec,
      secondFilter: v.secondFilter,
      zoneFilter: v.zoneFilter,
      sigmaMinBps: v.sigmaMinBps,
      sigmaMaxBps: v.sigmaMaxBps,
      ...(v.base === "SECOND_VALUE_AREA_SMART" ? {
        valueAreaSec: v.valueAreaSec,
        binSize: v.binSize,
        valuePct: v.valuePct,
        normalWindowSec: v.normalWindowSec,
        normalCoverage: v.normalCoverage,
        mode: v.mode,
        minEdgeBps: v.minEdgeBps,
        minFlow: v.minFlow,
        minTrendBps: v.minTrendBps,
        minVolumeRatio: v.minVolumeRatio,
        minObImbalance: v.minObImbalance,
        minMicroBps: v.minMicroBps,
        maxAgainstObImbalance: v.maxAgainstObImbalance,
        maxAgainstFlow: v.maxAgainstFlow,
        retestSec: v.retestSec,
        retestBps: v.retestBps,
        breakHoldSec: v.breakHoldSec,
        reclaimBps: v.reclaimBps,
        absorptionMaxProgressBps: v.absorptionMaxProgressBps,
        lossPauseAfter: v.lossPauseAfter,
        lossPauseSec: v.lossPauseSec
      } : {}),
      ...(v.base === "SECOND_VW_CONFIRM" ? {
        etaTargetBps: v.etaTargetBps,
        etaMaxWaitSec: v.etaMaxWaitSec
      } : {}),
      ...(v.base === "SECOND_NORMAL_STATE_V11" ? {
        confirmDelaySec: v.confirmDelaySec,
        maxAdverseBps: v.maxAdverseBps,
        signalHoldSec: v.signalHoldSec,
        bandwalkMax: v.bandwalkMax,
        minConsensusVotes: v.minConsensusVotes,
        stateGate: v.stateGate,
        confirmationVeto: v.confirmationVeto,
        lossDensityEnabled: v.lossDensityEnabled === true,
        lossDensityWindow: v.lossDensityWindow,
        lossDensityLosses: v.lossDensityLosses,
        lossDensityMinTrades: v.lossDensityMinTrades,
        lossDensityCooldownSec: v.lossDensityCooldownSec,
        lossDensityLookbackHours: v.lossDensityLookbackHours
      } : {}),
      ...(v.base === "SECOND_NORMAL_ROUTER_V21" || v.base === "SECOND_NORMAL_LOWVOL_V22" ? {
        routeLookbackSec: v.routeLookbackSec,
        r10WindowSec: v.r10WindowSec,
        r10CapBps: v.r10CapBps,
        downR10CapBps: v.downR10CapBps,
        midRouteSigmaCapBps: v.midRouteSigmaCapBps,
        minObservedPct: v.minObservedPct,
        lossDensityEnabled: v.lossDensityEnabled !== false,
        lossDensityWindow: v.lossDensityWindow,
        lossDensityLosses: v.lossDensityLosses,
        lossDensityMinTrades: v.lossDensityMinTrades,
        lossDensityCooldownSec: v.lossDensityCooldownSec,
        lossDensityLookbackHours: v.lossDensityLookbackHours,
        lossStreakEnabled: v.lossStreakEnabled !== false,
        lossStreakCount: v.lossStreakCount,
        lossStreakCooldownSec: v.lossStreakCooldownSec
      } : {}),
      ...(v.base === "SECOND_NORMAL_LOWVOL_V22" ? {
        lowVolRouteSigmaMaxBps: v.lowVolRouteSigmaMaxBps,
        lowVolConfirmSec: v.lowVolConfirmSec,
        lowVolReversionBps: v.lowVolReversionBps,
        lowVolBreakoutBps: v.lowVolBreakoutBps
      } : {}),
      ...(v.base === "SECOND_NORMAL_LIQUIDITY_ORDERBOOK_V1" ? {
        normalWindowSec: v.normalWindowSec,
        zEntry: v.zEntry,
        zReclaim: v.zReclaim,
        retestSec: v.retestSec,
        insideMin: v.insideMin,
        observedMinPct: v.observedMinPct,
        centerSlopeSec: v.centerSlopeSec,
        centerSlopeMaxBps: v.centerSlopeMaxBps,
        sigmaExpandMax: v.sigmaExpandMax,
        orderbookMaxAgeSec: v.orderbookMaxAgeSec,
        obImbalanceMin: v.obImbalanceMin,
        microMinBps: v.microMinBps,
        wallRatioMin: v.wallRatioMin,
        flowGuard: v.flowGuard,
        trueBreakFlow: v.trueBreakFlow,
        trueBreakImbalance: v.trueBreakImbalance,
        bidwallTrapEnabled: v.bidwallTrapEnabled !== false,
        bidwallTrapRet300MaxBps: v.bidwallTrapRet300MaxBps,
        bidwallTrapBid20Chg60Min: v.bidwallTrapBid20Chg60Min,
        bidwallTrapRet600MinBps: v.bidwallTrapRet600MinBps,
        qualityV2Enabled: v.qualityV2Enabled !== false,
        qualityV2DownBid20Chg60Min: v.qualityV2DownBid20Chg60Min,
        qualityV2UpFlow60Min: v.qualityV2UpFlow60Min,
        trendSpaceEnabled: v.trendSpaceEnabled === true,
        trendSpaceSigmaExpandMax: v.trendSpaceSigmaExpandMax,
        trendSpaceCenterSlopeAbsMaxBps: v.trendSpaceCenterSlopeAbsMaxBps,
        trendSpaceInsideMax: v.trendSpaceInsideMax,
        trendSpaceTrendRet1800Bps: v.trendSpaceTrendRet1800Bps,
        trendSpaceUpPos1800Min: v.trendSpaceUpPos1800Min,
        trendSpaceDownPos1800Max: v.trendSpaceDownPos1800Max,
        trendSpaceBlockCountertrend: v.trendSpaceBlockCountertrend !== false,
        trendSpaceBlockUpperFadePullback: v.trendSpaceBlockUpperFadePullback !== false,
        trendSpaceShortRet600UpBps: v.trendSpaceShortRet600UpBps,
        trendSpaceShortPos600Min: v.trendSpaceShortPos600Min,
        startupSkipEnabled: v.startupSkipEnabled === true,
        startupSkipThreshold: v.startupSkipThreshold,
        liquidityMode: v.liquidityMode,
        v9AugmentedEnabled: v.v9AugmentedEnabled === true,
        v9EfficiencyMin: v.v9EfficiencyMin,
        v9TrendStrengthMin: v.v9TrendStrengthMin,
        v9OpposingMinBps: v.v9OpposingMinBps,
        v9Z30Min: v.v9Z30Min,
        v9VolumeRatioMin: v.v9VolumeRatioMin,
        v9BookCoverageMin: v.v9BookCoverageMin,
        v9BookVotesMin: v.v9BookVotesMin,
        v9MaxEmitAgeSec: v.v9MaxEmitAgeSec,
        v9SupplementMinAbsNormalZ: v.v9SupplementMinAbsNormalZ,
        v9OriginalRegimeVetoEnabled: v.v9OriginalRegimeVetoEnabled === true,
        v9OriginalVetoMatureDowntrend: v.v9OriginalVetoMatureDowntrend !== false,
        v9OriginalVetoShortMigrationUpDown: v.v9OriginalVetoShortMigrationUpDown !== false,
        v9OriginalAllowMatureDowntrendDownFlowMin: v.v9OriginalAllowMatureDowntrendDownFlowMin,
        v9SupplementLooseShortMigrationReversionEnabled: v.v9SupplementLooseShortMigrationReversionEnabled === true,
        v9SupplementLooseMatureUptrendDownEnabled: v.v9SupplementLooseMatureUptrendDownEnabled === true,
        v9SupplementMatureUptrendDownFlowMin: v.v9SupplementMatureUptrendDownFlowMin
      } : {}),
      ...(v.base === "SECOND_BRANCH_VOTE_STARTUP_V1" ? {
        normalWindowSec: v.normalWindowSec,
        orderbookMaxAgeSec: v.orderbookMaxAgeSec,
        minVotes: v.minVotes,
        startupSkipThreshold: v.startupSkipThreshold,
        rulePath: v.rulePath
      } : {}),
      ...(v.base === "SECOND_MULTI_NORMAL_HF_STABLE_V1" ? {
        normalWindowSec: v.normalWindowSec,
        orderbookMaxAgeSec: v.orderbookMaxAgeSec,
        lowVolSigmaMaxBps: v.lowVolSigmaMaxBps,
        lowVolRangeMaxBps: v.lowVolRangeMaxBps,
        lowVolAbsRet10MaxBps: v.lowVolAbsRet10MaxBps,
        lowVolZMin: v.lowVolZMin,
        lowVolZMax: v.lowVolZMax,
        lowVolMinSignedFlow: v.lowVolMinSignedFlow,
        lowVolMaxAdverseRet30Sigma: v.lowVolMaxAdverseRet30Sigma,
        trendBaseZMin: v.trendBaseZMin,
        trendHighVolSigmaMinBps: v.trendHighVolSigmaMinBps,
        trendHighVolZMin: v.trendHighVolZMin,
        trendMinSignedFlow: v.trendMinSignedFlow,
        trendMaxSignedBook: v.trendMaxSignedBook
      } : {}),
      ...(v.base === "SECOND_MULTISCALE_PHASE_GATE_V1" ? {
        orderbookMaxAgeSec: v.orderbookMaxAgeSec,
        maxEmitAgeSec: v.maxEmitAgeSec,
        phaseLookbackSec: v.phaseLookbackSec,
        maturityHistorySec: v.maturityHistorySec,
        maturityMinPeriods: v.maturityMinPeriods,
        maturityQuantile: v.maturityQuantile,
        minFlow60: v.minFlow60,
        minImbalance20: v.minImbalance20,
        minMicropriceBps: v.minMicropriceBps,
        minVolumeRatio: v.minVolumeRatio
      } : {}),
      upReversalConfirmBps: v.upReversalConfirmBps,
      upReversalConfirmMaxSec: v.upReversalConfirmMaxSec,
      incidentFilterEnabled: v.incidentFilterEnabled,
      incidentFilterMode: v.incidentFilterMode,
      incidentWindowSec: v.incidentWindowSec,
      incidentMinMoveBps: v.incidentMinMoveBps,
      incidentMinVolumeQuantile: v.incidentMinVolumeQuantile,
      incidentMinFlowImbalance: v.incidentMinFlowImbalance,
      incidentCooldownSec: v.incidentCooldownSec
    }]))
  };
}

function applyTradeConfigPatch(currentConfig, body = {}, options = {}) {
  const tradeConfig = normalizeTradeConfig(currentConfig);
  const reqBody = body && typeof body === "object" ? body : {};
  const auditEvents = [];
  let safetyBlocked = null;
  let forceAutoTrade = false;

  if (reqBody.amount !== undefined) tradeConfig.amount = String(reqBody.amount);
  if (Array.isArray(reqBody.strategyVariants)) {
    // 只合并固定 V13 与 30m 候选槽位，其他未知策略不会进入运行配置。
    tradeConfig.strategyVariants = normalizeVariants({ strategyVariants: reqBody.strategyVariants });
  }
  if (reqBody.duration !== undefined) tradeConfig.duration = String(reqBody.duration);
  if (reqBody.realTradingEnabled !== undefined) tradeConfig.realTradingEnabled = parseBool(reqBody.realTradingEnabled);
  else if (reqBody.realTradingOverride !== undefined) tradeConfig.realTradingEnabled = parseBool(reqBody.realTradingOverride);
  if (reqBody.shadowTradingEnabled !== undefined) tradeConfig.shadowTradingEnabled = parseBool(reqBody.shadowTradingEnabled);

  const requestedAuto = reqBody.autoTrade_10m !== undefined ? reqBody.autoTrade_10m : reqBody.autoTrade;
  if (requestedAuto !== undefined) {
    const requested = !!requestedAuto;
    if (requested) {
      forceAutoTrade = parseBool(reqBody.forceAutoTrade);
      const candidate = { ...tradeConfig, autoTrade_10m: true, realTradingEnabled: tradeConfig.realTradingEnabled || forceAutoTrade };
      const gate = options.autoTradeSafetyGate ? options.autoTradeSafetyGate(candidate) : { blocked: false };
      if (gate.blocked && !forceAutoTrade) {
        tradeConfig.autoTrade_10m = false;
        safetyBlocked = gate;
        auditEvents.push({ event: "auto_trade_safety_block_10m", gate });
      } else {
        tradeConfig.autoTrade_10m = true;
        if (forceAutoTrade) tradeConfig.realTradingEnabled = true;
        if (forceAutoTrade && gate.blocked) auditEvents.push({ event: "auto_trade_force_enabled_10m", gate, config: { amount: tradeConfig.amount } });
      }
    } else {
      tradeConfig.autoTrade_10m = false;
    }
  }

  return { tradeConfig: normalizeTradeConfig(tradeConfig), safetyBlocked, forceAutoTrade, auditEvents };
}

module.exports = {
  BACKTEST_PRESETS,
  DEFAULT_TRADE_CONFIG,
  V13_STRATEGY_ID,
  LEGACY_30M_STRATEGY_ID,
  V13_LIVE_FINGERPRINT,
  v13Fingerprint,
  normalizeTradeConfig,
  strategyVariants,
  observedStrategyIds,
  liveStrategyIds,
  amountForStrategyConfig,
  publicTradeConfig,
  applyTradeConfigPatch
};
