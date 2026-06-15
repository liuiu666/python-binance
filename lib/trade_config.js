const BACKTEST_PRESETS = {
  SAFE: {
    20: { wr: 63.39, tradesPerDay: 3.46, trades: 336, maxLoss: 5 },
    22: { wr: 58.75, tradesPerDay: 5.12, trades: 497, maxLoss: 5 },
    23: { wr: 57.77, tradesPerDay: 6.3, trades: 611, maxLoss: 6 },
    25: { wr: 56.13, tradesPerDay: 8.74, trades: 857, maxLoss: 6 },
    27: { wr: 56.16, tradesPerDay: 11.59, trades: 1136, maxLoss: 7 }
  },
  TAKER: {
    20: { wr: 87.5, tradesPerDay: 0.94, trades: 32, maxLoss: 1 },
    22: { wr: 73.33, tradesPerDay: 1.22, trades: 45, maxLoss: 3 },
    23: { wr: 71.15, tradesPerDay: 1.41, trades: 52, maxLoss: 3 },
    25: { wr: 59.46, tradesPerDay: 2.0, trades: 74, maxLoss: 4 },
    27: { wr: 66.07, tradesPerDay: 2.95, trades: 112, maxLoss: 4 }
  },
  SECOND: {
    20: { wr: 70.59, tradesPerDay: 8.57, trades: 17, maxLoss: 2, sampleHours: 47.58 }
  },
  SECOND_CHIP: {
    OPT_1800_WIDTH3: { wr: 73.91, tradesPerDay: 11.6, trades: 23, maxLoss: 1, sampleHours: 47.58 },
    BALANCED_3600_FLOW: { wr: 84.21, tradesPerDay: 9.41, trades: 19, maxLoss: 1, sampleHours: 48.48 },
    BALANCED_7200_FLOW: { wr: 80.95, tradesPerDay: 10.59, trades: 21, maxLoss: 2, sampleHours: 47.58 }
  }
};

const DEFAULT_STRATEGY_VARIANTS = [
  { id: "BTC_10min_SAFE", base: "SAFE", label: "推荐稳健 20/80", amount: "5", tailPct: 0.2, enabled: true, tradeEnabled: true },
  { id: "BTC_10min_TAKER", base: "TAKER", label: "资金流过滤 20/80", amount: "10", tailPct: 0.2, enabled: true, tradeEnabled: true },
  { id: "BTC_10min_SECOND_3600_20", base: "SECOND", label: "秒级正态 3600s 20/80", amount: "5", tailPct: 0.2, enabled: true, tradeEnabled: false, lookbackSec: 3600, horizonSec: 600, gapSec: 1800, secondFilter: "none" },
  { id: "BTC_10min_SECOND_CHIP_1800_OPT", base: "SECOND_CHIP", label: "秒级筹码区 30m 优化", amount: "5", enabled: true, tradeEnabled: false, lookbackSec: 1800, horizonSec: 600, gapSec: 300, chipTargetShare: 0.2, chipBinMode: "fixed", chipBinSize: 20, chipBinPct: 0.0003, chipBreakPct: 0.004, chipDirectionFilter: "all", chipFilter: "width_lte_3" },
  { id: "BTC_10min_SECOND_CHIP_3600_FLOW", base: "SECOND_CHIP", label: "秒级筹码区 60m 资金流", amount: "10", enabled: true, tradeEnabled: true, lookbackSec: 3600, horizonSec: 600, gapSec: 1800, chipTargetShare: 0.5, chipBinMode: "fixed", chipBinSize: 50, chipBinPct: 0.0003, chipBreakPct: 0.003, chipDirectionFilter: "all", chipFilter: "flow_reversal" }
];

const DEFAULT_TRADE_CONFIG = {
  amount: "5",
  strategyVariants: DEFAULT_STRATEGY_VARIANTS,
  duration: "10",
  autoTrade_10m: false,
  realTradingEnabled: false,
  shadowTradingEnabled: false
};

function pctKey(tailPct) {
  return String(Math.round(Number(tailPct) * 100));
}

function sanitizeTailPct(value, fallback = 0.2) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  const pct = n > 1 ? n / 100 : n;
  if (pct < 0.05 || pct > 0.45) return fallback;
  return Number(pct.toFixed(4));
}

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

function variantId(base, tailPct, index = 0, lookbackSec = 1800) {
  const pct = pctKey(tailPct);
  if (base === "SAFE") return index === 0 && pct === "20" ? "BTC_10min_SAFE" : `BTC_10min_SAFE_${pct}`;
  if (base === "TAKER") return index === 0 && pct === "20" ? "BTC_10min_TAKER" : `BTC_10min_TAKER_${pct}`;
  if (base === "SECOND_CHIP") return `BTC_10min_SECOND_CHIP_${lookbackSec}${index > 0 ? "_" + index : ""}`;
  return `BTC_10min_SECOND_${lookbackSec}_${pct}${index > 0 ? "_" + index : ""}`;
}

function variantLabel(base, tailPct, lookbackSec) {
  const pct = pctKey(tailPct);
  const tail = `${pct}/${100 - Number(pct)}`;
  if (base === "SAFE") return `推荐稳健 ${tail}`;
  if (base === "TAKER") return `资金流过滤 ${tail}`;
  if (base === "SECOND_CHIP") return `秒级筹码区 ${Math.round((lookbackSec || 3600) / 60)}m`;
  return `秒级正态 ${lookbackSec || 1800}s ${tail}`;
}

function backtestForVariant(base, tailPct) {
  return (BACKTEST_PRESETS[base] || {})[pctKey(tailPct)] || null;
}

function backtestForNormalizedVariant(variant) {
  if (!variant || variant.base !== "SECOND_CHIP") return backtestForVariant(variant && variant.base, variant && variant.tailPct);
  const lookback = Number(variant.lookbackSec);
  const target = Number(variant.chipTargetShare);
  const binSize = Number(variant.chipBinSize);
  const breakPct = Number(variant.chipBreakPct);
  const gap = Number(variant.gapSec);
  const chipFilter = String(variant.chipFilter || "none");
  if (
    lookback === 1800 &&
    Math.abs(target - 0.2) < 1e-9 &&
    binSize === 20 &&
    Math.abs(breakPct - 0.004) < 1e-9 &&
    gap === 300 &&
    chipFilter === "width_lte_3"
  ) {
    return BACKTEST_PRESETS.SECOND_CHIP.OPT_1800_WIDTH3;
  }
  if (
    lookback === 3600 &&
    Math.abs(target - 0.5) < 1e-9 &&
    binSize === 50 &&
    Math.abs(breakPct - 0.003) < 1e-9 &&
    gap === 1800 &&
    chipFilter === "flow_reversal"
  ) {
    return BACKTEST_PRESETS.SECOND_CHIP.BALANCED_3600_FLOW;
  }
  if (
    lookback === 7200 &&
    Math.abs(target - 0.35) < 1e-9 &&
    binSize === 20 &&
    Math.abs(breakPct - 0.004) < 1e-9 &&
    gap === 300 &&
    chipFilter === "flow_reversal"
  ) {
    return BACKTEST_PRESETS.SECOND_CHIP.BALANCED_7200_FLOW;
  }
  return null;
}

function normalizeVariant(raw, indexByBase) {
  const input = raw && typeof raw === "object" ? raw : {};
  const base = ["SAFE", "TAKER", "SECOND", "SECOND_CHIP"].includes(input.base) ? input.base : "TAKER";
  const tailPct = sanitizeTailPct(input.tailPct, 0.2);
  const index = Number(indexByBase[base] || 0);
  indexByBase[base] = index + 1;
  const amount = Number(input.amount);
  const lookbackFallback = base === "SECOND_CHIP" ? 3600 : 1800;
  const lookbackSec = sanitizePositiveInt(input.lookbackSec, lookbackFallback, 60, 21600);
  const horizonSec = sanitizePositiveInt(input.horizonSec, 600, 60, 7200);
  const gapSec = sanitizePositiveInt(input.gapSec, horizonSec, 0, 21600);
  const secondFilter = String(input.secondFilter || "none");
  const chipTargetShare = sanitizePositiveFloat(input.chipTargetShare, 0.2, 0.01, 0.9);
  const shouldMigrateChipBin =
    base === "SECOND_CHIP" &&
    input.chipBinSize === undefined &&
    String(input.chipBinMode || "").toLowerCase() === "percent" &&
    Math.abs(Number(input.chipBinPct ?? 0.0003) - 0.0003) < 1e-12;
  const chipBinMode = shouldMigrateChipBin ? "fixed" : String(input.chipBinMode || "fixed");
  const chipBinSize = sanitizePositiveFloat(input.chipBinSize, 20, 1, 1000);
  const chipBinPct = sanitizePositiveFloat(input.chipBinPct, 0.0003, 0.00001, 0.01);
  const chipBreakPct = sanitizePositiveFloat(input.chipBreakPct, 0.0023, 0.0001, 0.05);
  const chipDirectionFilter = String(input.chipDirectionFilter || "breakout_up_only");
  const chipFilter = String(input.chipFilter || "none");
  const out = {
    id: String(input.id || variantId(base, tailPct, index, lookbackSec)),
    base,
    label: input.label || variantLabel(base, tailPct, lookbackSec),
    amount: Number.isFinite(amount) && amount > 0 ? String(input.amount) : (base === "TAKER" ? "10" : "5"),
    tailPct,
    duration: String(input.duration || "10"),
    enabled: input.enabled !== false,
    tradeEnabled: input.tradeEnabled !== false,
    backtest: null
  };
  if (base === "SECOND") {
    out.lookbackSec = lookbackSec;
    out.horizonSec = horizonSec;
    out.gapSec = gapSec;
    out.secondFilter = [
      "none",
      "vol_high",
      "vol_not_high",
      "flow_align",
      "flow_strong_align",
      "flow_align_vol_not_high"
    ].includes(secondFilter) ? secondFilter : "none";
    out.duration = String(Math.max(1, Math.round(horizonSec / 60)));
  }
  if (base === "SECOND_CHIP") {
    out.lookbackSec = lookbackSec;
    out.horizonSec = horizonSec;
    out.gapSec = gapSec;
    out.chipTargetShare = chipTargetShare;
    out.chipBinMode = ["fixed", "percent"].includes(chipBinMode) ? chipBinMode : "fixed";
    out.chipBinSize = chipBinSize;
    out.chipBinPct = chipBinPct;
    out.chipBreakPct = chipBreakPct;
    out.chipDirectionFilter = ["all", "breakout_up_only", "breakout_down_only"].includes(chipDirectionFilter) ? chipDirectionFilter : "breakout_up_only";
    out.chipFilter = ["none", "width_lte_3", "width_lte_5", "flow_reversal"].includes(chipFilter) ? chipFilter : "none";
    out.duration = String(Math.max(1, Math.round(horizonSec / 60)));
  }
  out.backtest = backtestForNormalizedVariant(out);
  return out;
}

function migrateLegacyVariants(input) {
  if (Array.isArray(input.strategyVariants)) return input.strategyVariants;
  const amounts = input.strategyAmounts && typeof input.strategyAmounts === "object" ? input.strategyAmounts : {};
  const params = input.strategyParams && typeof input.strategyParams === "object" ? input.strategyParams : {};
  return [
    { id: "BTC_10min_SAFE", base: "SAFE", amount: amounts.BTC_10min_SAFE || "5", tailPct: params.BTC_10min_SAFE?.tailPct ?? 0.2, enabled: true, tradeEnabled: true },
    { id: "BTC_10min_TAKER", base: "TAKER", amount: amounts.BTC_10min_TAKER || "10", tailPct: params.BTC_10min_TAKER?.tailPct ?? 0.2, enabled: true, tradeEnabled: true }
  ];
}

function normalizeVariants(input) {
  const raw = migrateLegacyVariants(input || {});
  const withDefaults = raw.length ? raw : DEFAULT_STRATEGY_VARIANTS;
  const indexByBase = { SAFE: 0, TAKER: 0, SECOND: 0, SECOND_CHIP: 0 };
  const seen = new Set();
  const out = [];
  for (const item of withDefaults.slice(0, 16)) {
    const next = normalizeVariant(item, indexByBase);
    let id = next.id;
    if (seen.has(id)) id = `${variantId(next.base, next.tailPct, indexByBase[next.base], next.lookbackSec)}_${out.length + 1}`;
    seen.add(id);
    out.push({ ...next, id });
  }
  if (!out.some(v => v.base === "SAFE")) out.unshift(normalizeVariant(DEFAULT_STRATEGY_VARIANTS[0], { SAFE: 0, TAKER: 0, SECOND: 0, SECOND_CHIP: 0 }));
  if (!out.some(v => v.base === "TAKER")) out.push(normalizeVariant(DEFAULT_STRATEGY_VARIANTS[1], { SAFE: 1, TAKER: 0, SECOND: 0, SECOND_CHIP: 0 }));
  if (!out.some(v => v.base === "SECOND")) out.push(normalizeVariant(DEFAULT_STRATEGY_VARIANTS[2], { SAFE: 1, TAKER: 1, SECOND: 0, SECOND_CHIP: 0 }));
  if (!out.some(v => v.base === "SECOND_CHIP")) out.push(normalizeVariant(DEFAULT_STRATEGY_VARIANTS[3], { SAFE: 1, TAKER: 1, SECOND: 1, SECOND_CHIP: 0 }));
  if (!out.some(v => v.id === "BTC_10min_SECOND_CHIP_3600_FLOW")) {
    out.push(normalizeVariant(DEFAULT_STRATEGY_VARIANTS[4], { SAFE: 1, TAKER: 1, SECOND: 1, SECOND_CHIP: out.filter(v => v.base === "SECOND_CHIP").length }));
  }
  return out;
}

function normalizeTradeConfig(value = {}) {
  const input = value && typeof value === "object" ? value : {};
  const cfg = {
    ...DEFAULT_TRADE_CONFIG,
    amount: input.amount !== undefined ? input.amount : DEFAULT_TRADE_CONFIG.amount,
    strategyVariants: normalizeVariants(input),
    duration: input.duration !== undefined ? input.duration : DEFAULT_TRADE_CONFIG.duration,
    autoTrade_10m: input.autoTrade_10m !== undefined ? input.autoTrade_10m : DEFAULT_TRADE_CONFIG.autoTrade_10m,
    realTradingEnabled: input.realTradingEnabled !== undefined ? input.realTradingEnabled : DEFAULT_TRADE_CONFIG.realTradingEnabled,
    shadowTradingEnabled: false
  };
  cfg.amount = String(cfg.amount);
  cfg.duration = String(cfg.duration);
  if (input.autoTrade_10m !== undefined) cfg.autoTrade_10m = !!input.autoTrade_10m;
  else if (input.autoTrade !== undefined) cfg.autoTrade_10m = !!input.autoTrade;
  else cfg.autoTrade_10m = !!cfg.autoTrade_10m;
  if (input.realTradingEnabled !== undefined) cfg.realTradingEnabled = !!input.realTradingEnabled;
  else if (input.realTradingOverride !== undefined) cfg.realTradingEnabled = !!input.realTradingOverride;
  else cfg.realTradingEnabled = !!cfg.realTradingEnabled;
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

function liveStrategyIds(config) {
  return strategyVariants(config).filter(v => v.enabled && v.tradeEnabled !== false).map(v => v.id);
}

function amountForStrategyConfig(strategyId, cfg) {
  const found = strategyVariants(cfg).find(v => v.id === strategyId);
  return String((found && found.amount) || normalizeTradeConfig(cfg).amount);
}

function publicTradeConfig(config) {
  const cfg = normalizeTradeConfig(config);
  return {
    amount: cfg.amount,
    strategyVariants: cfg.strategyVariants,
    duration: cfg.duration,
    autoTrade_10m: cfg.autoTrade_10m,
    realTradingEnabled: cfg.realTradingEnabled,
    shadowTradingEnabled: cfg.shadowTradingEnabled,
    strategyAmounts: Object.fromEntries(cfg.strategyVariants.map(v => [v.id, v.amount])),
    strategyParams: Object.fromEntries(cfg.strategyVariants.map(v => [v.id, {
      tailPct: v.tailPct,
      tradeEnabled: v.tradeEnabled !== false,
      lookbackSec: v.lookbackSec,
      horizonSec: v.horizonSec,
      gapSec: v.gapSec,
      secondFilter: v.secondFilter,
      chipTargetShare: v.chipTargetShare,
      chipBinMode: v.chipBinMode,
      chipBinSize: v.chipBinSize,
      chipBinPct: v.chipBinPct,
      chipBreakPct: v.chipBreakPct,
      chipDirectionFilter: v.chipDirectionFilter,
      chipFilter: v.chipFilter
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
  if (Array.isArray(reqBody.strategyVariants)) tradeConfig.strategyVariants = normalizeVariants({ strategyVariants: reqBody.strategyVariants });
  if (reqBody.duration !== undefined) tradeConfig.duration = String(reqBody.duration);

  if (reqBody.autoTrade_10m !== undefined) {
    const requested = !!reqBody.autoTrade_10m;
    if (requested) {
      const gate = options.autoTradeSafetyGate ? options.autoTradeSafetyGate() : { blocked: false };
      forceAutoTrade = parseBool(reqBody.forceAutoTrade);
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

  if (reqBody.autoTrade !== undefined && reqBody.autoTrade_10m === undefined) {
    const requested = !!reqBody.autoTrade;
    const gate = options.autoTradeSafetyGate ? options.autoTradeSafetyGate() : { blocked: false };
    forceAutoTrade = forceAutoTrade || parseBool(reqBody.forceAutoTrade);
    if (requested) {
      if (gate.blocked && !forceAutoTrade) {
        tradeConfig.autoTrade_10m = false;
        safetyBlocked = gate;
        auditEvents.push({ event: "auto_trade_safety_block", gate });
      } else {
        tradeConfig.autoTrade_10m = true;
        if (forceAutoTrade) tradeConfig.realTradingEnabled = true;
        if (forceAutoTrade && gate.blocked) auditEvents.push({ event: "auto_trade_force_enabled", gate, config: { amount: tradeConfig.amount } });
      }
    } else {
      tradeConfig.autoTrade_10m = false;
      tradeConfig.realTradingEnabled = false;
    }
  }

  if (reqBody.realTradingEnabled !== undefined) tradeConfig.realTradingEnabled = parseBool(reqBody.realTradingEnabled);
  else if (reqBody.realTradingOverride !== undefined) tradeConfig.realTradingEnabled = parseBool(reqBody.realTradingOverride);
  tradeConfig.shadowTradingEnabled = false;

  return { tradeConfig: normalizeTradeConfig(tradeConfig), safetyBlocked, forceAutoTrade, auditEvents };
}

module.exports = {
  BACKTEST_PRESETS,
  DEFAULT_TRADE_CONFIG,
  normalizeTradeConfig,
  strategyVariants,
  observedStrategyIds,
  liveStrategyIds,
  amountForStrategyConfig,
  publicTradeConfig,
  applyTradeConfigPatch
};
