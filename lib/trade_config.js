const LIVE_STRATEGY_IDS = ["BTC_10min_SAFE", "BTC_10min_TAKER"];

const DEFAULT_TRADE_CONFIG = {
  amount: "5",
  strategyAmounts: {
    BTC_10min_SAFE: "5",
    BTC_10min_TAKER: "5"
  },
  duration: "10",
  autoTrade_10m: false,
  realTradingEnabled: false,
  shadowTradingEnabled: true,
  minConfidence: 35
};

function normalizeTradeConfig(value = {}) {
  const input = value && typeof value === "object" ? value : {};
  const cfg = {
    ...DEFAULT_TRADE_CONFIG,
    amount: input.amount !== undefined ? input.amount : DEFAULT_TRADE_CONFIG.amount,
    strategyAmounts: input.strategyAmounts,
    duration: input.duration !== undefined ? input.duration : DEFAULT_TRADE_CONFIG.duration,
    autoTrade_10m: input.autoTrade_10m !== undefined ? input.autoTrade_10m : DEFAULT_TRADE_CONFIG.autoTrade_10m,
    realTradingEnabled: input.realTradingEnabled !== undefined ? input.realTradingEnabled : DEFAULT_TRADE_CONFIG.realTradingEnabled,
    shadowTradingEnabled: input.shadowTradingEnabled !== undefined ? input.shadowTradingEnabled : DEFAULT_TRADE_CONFIG.shadowTradingEnabled,
    minConfidence: input.minConfidence !== undefined ? input.minConfidence : DEFAULT_TRADE_CONFIG.minConfidence
  };
  cfg.amount = String(cfg.amount);
  cfg.strategyAmounts = {
    ...DEFAULT_TRADE_CONFIG.strategyAmounts,
    ...(input.strategyAmounts && typeof input.strategyAmounts === "object" ? input.strategyAmounts : {})
  };
  cfg.strategyAmounts = Object.fromEntries(
    LIVE_STRATEGY_IDS.map(strategyId => [strategyId, cfg.strategyAmounts[strategyId]])
  );
  for (const strategyId of LIVE_STRATEGY_IDS) {
    const amount = Number(cfg.strategyAmounts[strategyId]);
    cfg.strategyAmounts[strategyId] = Number.isFinite(amount) && amount > 0 ? String(cfg.strategyAmounts[strategyId]) : String(cfg.amount);
  }
  cfg.duration = String(cfg.duration);
  
  // Adapt legacy autoTrade to the current 10-minute strategy pair.
  if (input.autoTrade_10m !== undefined) {
    cfg.autoTrade_10m = !!input.autoTrade_10m;
  } else if (input.autoTrade !== undefined) {
    cfg.autoTrade_10m = !!input.autoTrade;
  } else {
    cfg.autoTrade_10m = !!cfg.autoTrade_10m;
  }

  // Adapt legacy realTradingOverride to realTradingEnabled
  if (input.realTradingEnabled !== undefined) {
    cfg.realTradingEnabled = !!input.realTradingEnabled;
  } else if (input.realTradingOverride !== undefined) {
    cfg.realTradingEnabled = !!input.realTradingOverride;
  } else {
    cfg.realTradingEnabled = !!cfg.realTradingEnabled;
  }

  cfg.shadowTradingEnabled = input.shadowTradingEnabled !== undefined ? !!input.shadowTradingEnabled : !!cfg.shadowTradingEnabled;

  cfg.minConfidence = Number(cfg.minConfidence);
  if (!Number.isFinite(cfg.minConfidence)) cfg.minConfidence = DEFAULT_TRADE_CONFIG.minConfidence;
  return cfg;
}

function parseBool(value) {
  return value === true || value === "true";
}

function amountForStrategyConfig(strategyId, cfg) {
  const config = normalizeTradeConfig(cfg);
  return String((config.strategyAmounts && config.strategyAmounts[strategyId]) || config.amount);
}

function publicTradeConfig(config) {
  const cfg = normalizeTradeConfig(config);
  return {
    amount: cfg.amount,
    strategyAmounts: cfg.strategyAmounts,
    duration: cfg.duration,
    autoTrade_10m: cfg.autoTrade_10m,
    realTradingEnabled: cfg.realTradingEnabled,
    shadowTradingEnabled: cfg.shadowTradingEnabled,
    minConfidence: cfg.minConfidence
  };
}

function applyTradeConfigPatch(currentConfig, body = {}, options = {}) {
  const tradeConfig = normalizeTradeConfig(currentConfig);
  const reqBody = body && typeof body === "object" ? body : {};
  const auditEvents = [];
  let safetyBlocked = null;
  let forceAutoTrade = false;

  if (reqBody.amount !== undefined) tradeConfig.amount = String(reqBody.amount);
  if (reqBody.strategyAmounts && typeof reqBody.strategyAmounts === "object") {
    tradeConfig.strategyAmounts = {
      ...tradeConfig.strategyAmounts,
      ...reqBody.strategyAmounts
    };
  }
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

  // Support legacy 'autoTrade' key patch requests ONLY if modern keys are not supplied
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
        if (forceAutoTrade && gate.blocked) {
          auditEvents.push({
            event: "auto_trade_force_enabled",
            gate,
            config: {
              amount: tradeConfig.amount,
              minConfidence: tradeConfig.minConfidence,
              strategyAmounts: tradeConfig.strategyAmounts
            }
          });
        }
      }
    } else {
      tradeConfig.autoTrade_10m = false;
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

  return {
    tradeConfig: normalizeTradeConfig(tradeConfig),
    safetyBlocked,
    forceAutoTrade,
    auditEvents
  };
}

module.exports = {
  DEFAULT_TRADE_CONFIG,
  LIVE_STRATEGY_IDS,
  normalizeTradeConfig,
  amountForStrategyConfig,
  publicTradeConfig,
  applyTradeConfigPatch
};
