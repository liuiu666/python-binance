const DEFAULT_TRADE_CONFIG = {
  amount: "5",
  duration: "30",
  autoTrade: false,
  minConfidence: 35,
  tiersEnabled: false,
  tiers: [{ min: 80, amount: 20 }, { min: 60, amount: 10 }, { min: 40, amount: 5 }],
  skipConflictSignals: false,
  queueOrderPolicy: "confidence_desc",
  preventOverlapOrders: true,
  realTradingOverride: false,
  maxActionableLagMs: 60000
};

const QUEUE_ORDER_POLICIES = new Set(["confidence_desc", "30_then_10", "10_then_30"]);

function normalizeTiers(tiers, fallback = DEFAULT_TRADE_CONFIG.tiers) {
  const input = Array.isArray(tiers) ? tiers : fallback;
  return input
    .map(t => ({ min: Number(t.min), amount: Number(t.amount) }))
    .filter(t => Number.isFinite(t.min) && Number.isFinite(t.amount) && t.min >= 0 && t.min <= 100 && t.amount > 0)
    .sort((a, b) => b.min - a.min);
}

function normalizeTradeConfig(value = {}) {
  const cfg = { ...DEFAULT_TRADE_CONFIG, ...(value && typeof value === "object" ? value : {}) };
  cfg.amount = String(cfg.amount);
  cfg.duration = String(cfg.duration);
  cfg.autoTrade = !!cfg.autoTrade;
  cfg.minConfidence = Number(cfg.minConfidence);
  cfg.tiersEnabled = !!cfg.tiersEnabled;
  cfg.tiers = normalizeTiers(cfg.tiers);
  cfg.skipConflictSignals = !!cfg.skipConflictSignals;
  cfg.preventOverlapOrders = cfg.preventOverlapOrders !== false;
  cfg.realTradingOverride = !!cfg.realTradingOverride;
  cfg.maxActionableLagMs = Number.isFinite(Number(cfg.maxActionableLagMs))
    ? Number(cfg.maxActionableLagMs)
    : DEFAULT_TRADE_CONFIG.maxActionableLagMs;
  if (!QUEUE_ORDER_POLICIES.has(String(cfg.queueOrderPolicy))) cfg.queueOrderPolicy = DEFAULT_TRADE_CONFIG.queueOrderPolicy;
  return cfg;
}

function parseBool(value) {
  return value === true || value === "true";
}

function amountForConfidence(conf, cfg) {
  const config = normalizeTradeConfig(cfg);
  if (config.tiersEnabled && Array.isArray(config.tiers) && config.tiers.length) {
    const sorted = normalizeTiers(config.tiers);
    for (const t of sorted) {
      if (Number(conf) >= Number(t.min)) return String(t.amount);
    }
  }
  return String(config.amount);
}

function applyTradeConfigPatch(currentConfig, body = {}, options = {}) {
  const tradeConfig = normalizeTradeConfig(currentConfig);
  const reqBody = body && typeof body === "object" ? body : {};
  const auditEvents = [];
  let safetyBlocked = null;
  let forceAutoTrade = false;

  if (reqBody.amount !== undefined) tradeConfig.amount = String(reqBody.amount);
  if (reqBody.duration !== undefined) tradeConfig.duration = String(reqBody.duration);

  if (reqBody.autoTrade !== undefined) {
    const requestedAutoTrade = !!reqBody.autoTrade;
    if (requestedAutoTrade) {
      const gate = options.autoTradeSafetyGate ? options.autoTradeSafetyGate() : { blocked: false };
      forceAutoTrade = parseBool(reqBody.forceAutoTrade);
      if (gate.blocked && !forceAutoTrade) {
        tradeConfig.autoTrade = false;
        safetyBlocked = gate;
        auditEvents.push({
          event: "auto_trade_safety_block",
          gate
        });
      } else {
        tradeConfig.autoTrade = true;
        if (forceAutoTrade) tradeConfig.realTradingOverride = true;
        if (forceAutoTrade && gate.blocked) {
          auditEvents.push({
            event: "auto_trade_force_enabled",
            gate,
            config: {
              amount: tradeConfig.amount,
              minConfidence: tradeConfig.minConfidence,
              tiersEnabled: tradeConfig.tiersEnabled,
              tiers: tradeConfig.tiers,
              preventOverlapOrders: tradeConfig.preventOverlapOrders,
              queueOrderPolicy: tradeConfig.queueOrderPolicy
            }
          });
        }
      }
    } else {
      tradeConfig.autoTrade = false;
      tradeConfig.realTradingOverride = false;
    }
  }

  if (reqBody.realTradingOverride !== undefined) tradeConfig.realTradingOverride = parseBool(reqBody.realTradingOverride);
  if (reqBody.minConfidence !== undefined) tradeConfig.minConfidence = Number(reqBody.minConfidence);
  if (reqBody.maxActionableLagMs !== undefined) {
    const lag = Number(reqBody.maxActionableLagMs);
    if (Number.isFinite(lag) && lag >= 5000 && lag <= 10 * 60 * 1000) {
      tradeConfig.maxActionableLagMs = Math.round(lag);
    }
  }
  if (reqBody.tiersEnabled !== undefined) tradeConfig.tiersEnabled = !!reqBody.tiersEnabled;
  if (reqBody.skipConflictSignals !== undefined) tradeConfig.skipConflictSignals = !!reqBody.skipConflictSignals;
  if (reqBody.preventOverlapOrders !== undefined) tradeConfig.preventOverlapOrders = !!reqBody.preventOverlapOrders;
  if (reqBody.queueOrderPolicy !== undefined && QUEUE_ORDER_POLICIES.has(String(reqBody.queueOrderPolicy))) {
    tradeConfig.queueOrderPolicy = String(reqBody.queueOrderPolicy);
  }
  if (Array.isArray(reqBody.tiers)) tradeConfig.tiers = normalizeTiers(reqBody.tiers, []);

  return {
    tradeConfig: normalizeTradeConfig(tradeConfig),
    safetyBlocked,
    forceAutoTrade,
    auditEvents
  };
}

module.exports = {
  DEFAULT_TRADE_CONFIG,
  QUEUE_ORDER_POLICIES,
  normalizeTiers,
  normalizeTradeConfig,
  amountForConfidence,
  applyTradeConfigPatch
};
