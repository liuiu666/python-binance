const DEFAULT_TRADE_CONFIG = {
  amount: "5",
  duration: "30",
  autoTrade_10m: false,
  autoTrade_30m: false,
  realTradingEnabled: false,
  shadowTradingEnabled: true,
  minConfidence: 35,
  tiersEnabled: false,
  tiers: [{ min: 80, amount: 20 }, { min: 60, amount: 10 }, { min: 40, amount: 5 }],
  skipConflictSignals: false,
  queueOrderPolicy: "confidence_desc",
  preventOverlapOrders: true,
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
  
  // Adapt legacy autoTrade to split autoTrade_10m and autoTrade_30m
  if (value.autoTrade_10m !== undefined) {
    cfg.autoTrade_10m = !!value.autoTrade_10m;
  } else if (value.autoTrade !== undefined) {
    cfg.autoTrade_10m = !!value.autoTrade;
  } else {
    cfg.autoTrade_10m = !!cfg.autoTrade_10m;
  }

  if (value.autoTrade_30m !== undefined) {
    cfg.autoTrade_30m = !!value.autoTrade_30m;
  } else if (value.autoTrade !== undefined) {
    cfg.autoTrade_30m = !!value.autoTrade;
  } else {
    cfg.autoTrade_30m = !!cfg.autoTrade_30m;
  }

  // Adapt legacy realTradingOverride to realTradingEnabled
  if (value.realTradingEnabled !== undefined) {
    cfg.realTradingEnabled = !!value.realTradingEnabled;
  } else if (value.realTradingOverride !== undefined) {
    cfg.realTradingEnabled = !!value.realTradingOverride;
  } else {
    cfg.realTradingEnabled = !!cfg.realTradingEnabled;
  }

  cfg.shadowTradingEnabled = value.shadowTradingEnabled !== undefined ? !!value.shadowTradingEnabled : !!cfg.shadowTradingEnabled;

  cfg.minConfidence = Number(cfg.minConfidence);
  cfg.tiersEnabled = !!cfg.tiersEnabled;
  cfg.tiers = normalizeTiers(cfg.tiers);
  cfg.skipConflictSignals = !!cfg.skipConflictSignals;
  cfg.preventOverlapOrders = cfg.preventOverlapOrders !== false;
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

  // Parse safety gate for 10m auto-trading enable trigger
  if (reqBody.autoTrade_10m !== undefined) {
    const requested = !!reqBody.autoTrade_10m;
    if (requested) {
      const gate = options.autoTradeSafetyGate ? options.autoTradeSafetyGate() : { blocked: false };
      forceAutoTrade = parseBool(reqBody.forceAutoTrade);
      if (gate.blocked && !forceAutoTrade) {
        tradeConfig.autoTrade_10m = false;
        safetyBlocked = gate;
        auditEvents.push({
          event: "auto_trade_safety_block_10m",
          gate
        });
      } else {
        tradeConfig.autoTrade_10m = true;
        if (forceAutoTrade) tradeConfig.realTradingEnabled = true;
        if (forceAutoTrade && gate.blocked) {
          auditEvents.push({
            event: "auto_trade_force_enabled_10m",
            gate,
            config: { amount: tradeConfig.amount, minConfidence: tradeConfig.minConfidence }
          });
        }
      }
    } else {
      tradeConfig.autoTrade_10m = false;
    }
  }

  // Parse safety gate for 30m auto-trading enable trigger
  if (reqBody.autoTrade_30m !== undefined) {
    const requested = !!reqBody.autoTrade_30m;
    if (requested) {
      const gate = options.autoTradeSafetyGate ? options.autoTradeSafetyGate() : { blocked: false };
      forceAutoTrade = forceAutoTrade || parseBool(reqBody.forceAutoTrade);
      if (gate.blocked && !forceAutoTrade) {
        tradeConfig.autoTrade_30m = false;
        safetyBlocked = gate;
        auditEvents.push({
          event: "auto_trade_safety_block_30m",
          gate
        });
      } else {
        tradeConfig.autoTrade_30m = true;
        if (forceAutoTrade) tradeConfig.realTradingEnabled = true;
        if (forceAutoTrade && gate.blocked) {
          auditEvents.push({
            event: "auto_trade_force_enabled_30m",
            gate,
            config: { amount: tradeConfig.amount, minConfidence: tradeConfig.minConfidence }
          });
        }
      }
    } else {
      tradeConfig.autoTrade_30m = false;
    }
  }

  // Support legacy 'autoTrade' key patch requests ONLY if modern keys are not supplied
  if (reqBody.autoTrade !== undefined && reqBody.autoTrade_10m === undefined && reqBody.autoTrade_30m === undefined) {
    const requested = !!reqBody.autoTrade;
    const gate = options.autoTradeSafetyGate ? options.autoTradeSafetyGate() : { blocked: false };
    forceAutoTrade = forceAutoTrade || parseBool(reqBody.forceAutoTrade);
    if (requested) {
      if (gate.blocked && !forceAutoTrade) {
        tradeConfig.autoTrade_10m = false;
        tradeConfig.autoTrade_30m = false;
        safetyBlocked = gate;
        auditEvents.push({ event: "auto_trade_safety_block", gate });
      } else {
        tradeConfig.autoTrade_10m = true;
        tradeConfig.autoTrade_30m = true;
        if (forceAutoTrade) tradeConfig.realTradingEnabled = true;
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
      tradeConfig.autoTrade_10m = false;
      tradeConfig.autoTrade_30m = false;
      tradeConfig.realTradingEnabled = false;
    }
  }

  if (reqBody.realTradingEnabled !== undefined) {
    tradeConfig.realTradingEnabled = parseBool(reqBody.realTradingEnabled);
  } else if (reqBody.realTradingOverride !== undefined) {
    tradeConfig.realTradingEnabled = parseBool(reqBody.realTradingOverride);
  }

  if (reqBody.shadowTradingEnabled !== undefined) {
    tradeConfig.shadowTradingEnabled = parseBool(reqBody.shadowTradingEnabled);
  }

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
