const BACKTEST_PRESETS = {
  SECOND_VW_CONFIRM: {
    STABLE_2700_20_ETA2_45: { wr: 68.66, tradesPerDay: 9.66, trades: 67, maxLoss: 3, sampleHours: 166.44 },
    FAST_2700_27_ETA3_45: { wr: 67.39, tradesPerDay: 13.27, trades: 92, maxLoss: 3, sampleHours: 166.44 }
  }
};

const DEFAULT_STRATEGY_VARIANTS = [
  {
    id: "BTC_10min_SECOND_VW_STABLE_2700_20_ETA2",
    base: "SECOND_VW_CONFIRM",
    label: "正态成交量确认 稳健",
    amount: "5",
    tailPct: 0.20,
    enabled: true,
    tradeEnabled: true,
    lookbackSec: 2700,
    horizonSec: 600,
    gapSec: 600,
    etaTargetBps: 2.0,
    etaMaxWaitSec: 45,
    upReversalConfirmBps: 0.0,
    upReversalConfirmMaxSec: 20,
    incidentFilterEnabled: true,
    incidentFilterMode: "directional_only",
    incidentWindowSec: 10,
    incidentMinMoveBps: 10,
    incidentMinVolumeQuantile: 0.99,
    incidentMinFlowImbalance: 0.8,
    incidentCooldownSec: 10,
    duration: "10"
  },
  {
    id: "BTC_10min_SECOND_VW_FAST_2700_27_ETA3",
    base: "SECOND_VW_CONFIRM",
    label: "正态成交量确认 高频",
    amount: "5",
    tailPct: 0.27,
    enabled: true,
    tradeEnabled: true,
    lookbackSec: 2700,
    horizonSec: 600,
    gapSec: 600,
    etaTargetBps: 3.0,
    etaMaxWaitSec: 45,
    upReversalConfirmBps: 0.0,
    upReversalConfirmMaxSec: 20,
    incidentFilterEnabled: true,
    incidentFilterMode: "directional_only",
    incidentWindowSec: 10,
    incidentMinMoveBps: 10,
    incidentMinVolumeQuantile: 0.99,
    incidentMinFlowImbalance: 0.8,
    incidentCooldownSec: 10,
    duration: "10"
  }
];

const DEFAULT_TRADE_CONFIG = {
  amount: "5",
  strategyVariants: DEFAULT_STRATEGY_VARIANTS,
  duration: "10",
  autoTrade_10m: false,
  realTradingEnabled: false,
  shadowTradingEnabled: false
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
  return null;
}

function normalizeVariant(raw, index) {
  const input = raw && typeof raw === "object" ? raw : {};
  const amount = Number(input.amount);
  const horizonSec = sanitizePositiveInt(input.horizonSec, 600, 60, 7200);
  const out = {
    id: String(input.id || variantId(index)),
    base: "SECOND_VW_CONFIRM",
    label: variantLabel(input, index),
    amount: Number.isFinite(amount) && amount > 0 ? String(input.amount) : "5",
    tailPct: sanitizeTailPct(input.tailPct, index === 1 ? 0.27 : 0.20),
    duration: String(Math.max(1, Math.round(horizonSec / 60))),
    enabled: input.enabled !== false,
    tradeEnabled: input.tradeEnabled !== false,
    lookbackSec: sanitizePositiveInt(input.lookbackSec, 2700, 60, 21600),
    horizonSec,
    gapSec: sanitizePositiveInt(input.gapSec, horizonSec, 0, 21600),
    etaTargetBps: sanitizePositiveFloat(input.etaTargetBps, index === 1 ? 3.0 : 2.0, 0.1, 20),
    etaMaxWaitSec: sanitizePositiveInt(input.etaMaxWaitSec, 45, 1, 600),
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
  out.backtest = backtestForNormalizedVariant(out);
  return out;
}

function normalizeVariants(input) {
  const raw = Array.isArray(input?.strategyVariants) ? input.strategyVariants : [];
  const onlyCurrent = raw.filter(item => item && item.base === "SECOND_VW_CONFIRM");
  const source = onlyCurrent.length ? onlyCurrent : DEFAULT_STRATEGY_VARIANTS;
  const seen = new Set();
  return source.slice(0, 8).map((item, index) => {
    const next = normalizeVariant(item, index);
    if (seen.has(next.id)) next.id = `BTC_10min_SECOND_VW_CONFIRM_${index + 1}`;
    seen.add(next.id);
    return next;
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
    shadowTradingEnabled: false
  };
  if (input.autoTrade !== undefined && input.autoTrade_10m === undefined) cfg.autoTrade_10m = !!input.autoTrade;
  if (input.realTradingOverride !== undefined && input.realTradingEnabled === undefined) cfg.realTradingEnabled = !!input.realTradingOverride;
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
      etaTargetBps: v.etaTargetBps,
      etaMaxWaitSec: v.etaMaxWaitSec,
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
  if (Array.isArray(reqBody.strategyVariants)) tradeConfig.strategyVariants = normalizeVariants({ strategyVariants: reqBody.strategyVariants });
  if (reqBody.duration !== undefined) tradeConfig.duration = String(reqBody.duration);

  const requestedAuto = reqBody.autoTrade_10m !== undefined ? reqBody.autoTrade_10m : reqBody.autoTrade;
  if (requestedAuto !== undefined) {
    const requested = !!requestedAuto;
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
