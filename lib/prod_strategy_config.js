const fs = require("fs");
const { strategyVariants } = require("./trade_config");

/**
 * 创建生产策略配置服务，集中负责配置读取、参数映射、持久化及重启指纹计算。
 * 配置文件路径由服务端注入，避免模块绑定具体的数据目录。
 */
function createProdStrategyConfig({ prodConfigFile }) {
  const PROD_CONFIG_FILE = prodConfigFile;
function normalizeLlmInteger(value, fallback, min, max) {
  const number = Math.round(Number(value));
  return Number.isFinite(number) && number >= min && number <= max ? number : fallback;
}

function readProdConfig() {
  try {
    if (fs.existsSync(PROD_CONFIG_FILE)) {
      return JSON.parse(fs.readFileSync(PROD_CONFIG_FILE, "utf8"));
    }
  } catch (e) {}
  return {};
}

function applyProdStrategyParams(baseConfig, config) {
  const out = baseConfig && typeof baseConfig === "object" ? { ...baseConfig } : {};
  const variants = strategyVariants(config);
  const safeTemplate = out.BTC_10min_SAFE || {};
  const takerTemplate = out.BTC_10min_TAKER || {};
  for (const key of Object.keys(out)) {
    if ((key.startsWith("BTC_10min_TAKER") || key.startsWith("BTC_10min_SAFE") || key.startsWith("BTC_10min_SECOND") || key.startsWith("BTC_10min_SMART") || key.startsWith("BTC_10min_NORMAL_STATE") || key.startsWith("BTC_10min_NORMAL_LIQ") || key.startsWith("BTC_10min_BRANCH") || key.startsWith("BTC_10min_MULTI") || key.startsWith("BTC_10min_V22")) && !variants.some(v => v.id === key)) delete out[key];
  }
  for (const variant of variants) {
    const current = out[variant.id] && typeof out[variant.id] === "object" ? out[variant.id] : {};
    const template = variant.base === "SAFE" ? safeTemplate : takerTemplate;
    if (variant.base === "llm_direction") {
      // LLM 密钥和连接参数只保存在 prod_config；常规配置接口不会返回这些字段。
      out[variant.id] = {
        ...current,
        enabled: variant.enabled === true,
        trade_enabled: variant.tradeEnabled === true,
        model_type: "llm_direction",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.duration || 10))),
        horizon: Math.max(1, Math.round(Number(variant.duration || 10))),
        llm_api_url: String(current.llm_api_url || ""),
        llm_api_key: String(current.llm_api_key || ""),
        llm_model: String(current.llm_model || ""),
        llm_interval_sec: normalizeLlmInteger(current.llm_interval_sec, 600, 5, 86400),
        llm_max_tokens: normalizeLlmInteger(current.llm_max_tokens, 8000, 1, 32768),
        model_label: variant.label || "GLM-5.2 方向预测"
      };
      continue;
    }
    if (variant.base === "SECOND_VW_CONFIRM") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        model_type: "second_normal_vw_confirm",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_lookback_sec: variant.lookbackSec || 2700,
        second_horizon_sec: variant.horizonSec || 600,
        second_min_gap_sec: variant.gapSec || variant.horizonSec || 600,
        second_tail_pct: variant.tailPct,
        second_zone_filter: variant.zoneFilter || "none",
        eta_target_bps: variant.etaTargetBps || 2,
        eta_max_wait_sec: variant.etaMaxWaitSec || 45,
        up_reversal_confirm_bps: variant.upReversalConfirmBps ?? 0.0,
        up_reversal_confirm_max_sec: variant.upReversalConfirmMaxSec ?? 20,
        incident_filter_enabled: variant.incidentFilterEnabled !== false,
        incident_filter_mode: variant.incidentFilterMode || "directional_only",
        incident_window_sec: variant.incidentWindowSec || 10,
        incident_min_move_bps: variant.incidentMinMoveBps ?? 10,
        incident_min_volume_quantile: variant.incidentMinVolumeQuantile ?? 0.99,
        incident_min_flow_imbalance: variant.incidentMinFlowImbalance ?? 0.8,
        incident_cooldown_sec: variant.incidentCooldownSec ?? 10,
        model_label: variant.label || `SECOND_VW_CONFIRM_${variant.lookbackSec || 2700}_${Math.round(Number(variant.tailPct || 0.2) * 100)}_ETA${variant.etaTargetBps || 2}`
      };
      continue;
    }
    if (variant.base === "SECOND") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        model_type: "second_normal",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_lookback_sec: variant.lookbackSec || 1800,
        second_horizon_sec: variant.horizonSec || 600,
        second_min_gap_sec: variant.gapSec || variant.horizonSec || 600,
        second_tail_pct: variant.tailPct,
        second_filter: variant.secondFilter || "none",
        second_zone_filter: variant.zoneFilter || "none",
        second_sigma_min_bps: variant.sigmaMinBps ?? 0,
        second_sigma_max_bps: variant.sigmaMaxBps ?? 9999,
        model_label: `SECOND_${variant.lookbackSec || 1800}_${Math.round(variant.tailPct * 100)}_${100 - Math.round(variant.tailPct * 100)}`
      };
      continue;
    }
    if (variant.base === "SECOND_CHIP") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        model_type: "second_chip",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_chip_lookback_sec: variant.lookbackSec || 3600,
        second_chip_horizon_sec: variant.horizonSec || 600,
        second_chip_min_gap_sec: variant.gapSec || variant.horizonSec || 600,
        second_chip_target_share: variant.chipTargetShare,
        second_chip_bin_mode: variant.chipBinMode || "fixed",
        second_chip_bin_size: variant.chipBinSize || 20,
        second_chip_bin_pct: variant.chipBinPct,
        second_chip_break_pct: variant.chipBreakPct,
        second_chip_direction_filter: variant.chipDirectionFilter || "breakout_up_only",
        second_chip_filter: variant.chipFilter || "none",
        model_label: `SECOND_CHIP_${variant.lookbackSec || 3600}_${Math.round(Number(variant.chipTargetShare || 0.2) * 100)}_${Math.round(Number(variant.chipBreakPct || 0.0023) * 10000)}`
      };
      continue;
    }
    if (variant.base === "SECOND_VALUE_AREA_SMART") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        model_type: "second_value_area_smart",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_va_lookback_sec: variant.lookbackSec || 4200,
        second_va_horizon_sec: variant.horizonSec || 600,
        second_va_signal_gap_sec: variant.gapSec || variant.horizonSec || 600,
        second_va_tail_pct: variant.tailPct ?? 0.20,
        second_va_sigma_min_bps: variant.sigmaMinBps ?? 8,
        second_va_sigma_max_bps: variant.sigmaMaxBps ?? 80,
        second_va_value_area_sec: variant.valueAreaSec || 3600,
        second_va_bin_size: variant.binSize ?? 10,
        second_va_value_pct: variant.valuePct ?? 0.70,
        second_va_normal_window_sec: variant.normalWindowSec || 600,
        second_va_normal_coverage: variant.normalCoverage ?? 0.70,
        second_va_mode: variant.mode || "failed_break_fade",
        second_va_min_edge_bps: variant.minEdgeBps ?? 1,
        second_va_min_flow: variant.minFlow ?? 0.05,
        second_va_min_trend_bps: variant.minTrendBps ?? 1.0,
        second_va_min_volume_ratio: variant.minVolumeRatio ?? 1.15,
        second_va_min_ob_imbalance: variant.minObImbalance ?? 0.05,
        second_va_min_micro_bps: variant.minMicroBps ?? 0.001,
        second_va_max_against_ob_imbalance: variant.maxAgainstObImbalance ?? 0.25,
        second_va_max_against_flow: variant.maxAgainstFlow ?? 0.35,
        second_va_retest_sec: variant.retestSec || 180,
        second_va_retest_bps: variant.retestBps ?? 4.0,
        second_va_break_hold_sec: variant.breakHoldSec || 30,
        second_va_reclaim_bps: variant.reclaimBps ?? 0.8,
        second_va_absorption_max_progress_bps: variant.absorptionMaxProgressBps ?? 1.5,
        second_va_loss_pause_after: variant.lossPauseAfter ?? 2,
        second_va_loss_pause_sec: variant.lossPauseSec ?? 1800,
        model_label: variant.label || "SMART_OBSAFE_LOSS2_VA3600_E1_R180_CD600"
      };
      continue;
    }
    if (variant.base === "SECOND_NORMAL_STATE_V11") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        model_type: "normal_state_v11",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        normal_state_lookback_sec: variant.lookbackSec || 10800,
        normal_state_horizon_sec: variant.horizonSec || 600,
        normal_state_min_gap_sec: variant.gapSec || variant.horizonSec || 600,
        normal_state_confirm_delay_sec: variant.confirmDelaySec ?? 5,
        normal_state_max_adverse_bps: variant.maxAdverseBps ?? 5,
        normal_state_signal_hold_sec: variant.signalHoldSec ?? 55,
        normal_state_bandwalk_max: variant.bandwalkMax ?? 6,
        normal_state_min_consensus_votes: variant.minConsensusVotes ?? 2,
        normal_state_state_gate: variant.stateGate || "edge_persistence_lt6",
        normal_state_confirmation_veto: variant.confirmationVeto || "none",
        normal_state_loss_density_enabled: variant.lossDensityEnabled === true,
        normal_state_loss_density_window: variant.lossDensityWindow || 6,
        normal_state_loss_density_losses: variant.lossDensityLosses || 3,
        normal_state_loss_density_min_trades: variant.lossDensityMinTrades || 4,
        normal_state_loss_density_cooldown_sec: variant.lossDensityCooldownSec || 28800,
        normal_state_loss_density_lookback_hours: variant.lossDensityLookbackHours || 72,
        model_label: variant.label || "BTC_10min_NORMAL_STATE_V11_BANDWALK_2OF5_D5A5"
      };
      continue;
    }
    if (variant.base === "SECOND_NORMAL_ROUTER_V21") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        model_type: "second_normal_router_v21",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_lookback_sec: variant.lookbackSec || 4200,
        second_horizon_sec: variant.horizonSec || 600,
        second_min_gap_sec: variant.gapSec || variant.horizonSec || 600,
        second_router_route_lookback_sec: variant.routeLookbackSec || 4200,
        second_router_r10_window_sec: variant.r10WindowSec || 600,
        second_router_r10_cap_bps: variant.r10CapBps ?? 42,
        second_router_down_r10_cap_bps: variant.downR10CapBps ?? 35,
        second_router_mid_route_sigma_cap_bps: variant.midRouteSigmaCapBps ?? 20,
        second_router_min_observed_pct: variant.minObservedPct ?? 88,
        second_router_veto_low_up: variant.vetoLowUp !== false,
        normal_state_loss_density_enabled: variant.lossDensityEnabled !== false,
        normal_state_loss_density_window: variant.lossDensityWindow || 6,
        normal_state_loss_density_losses: variant.lossDensityLosses || 3,
        normal_state_loss_density_min_trades: variant.lossDensityMinTrades || 4,
        normal_state_loss_density_cooldown_sec: variant.lossDensityCooldownSec || 28800,
        normal_state_loss_density_lookback_hours: variant.lossDensityLookbackHours || 72,
        normal_state_loss_streak_enabled: variant.lossStreakEnabled !== false,
        normal_state_loss_streak_count: variant.lossStreakCount || 2,
        normal_state_loss_streak_cooldown_sec: variant.lossStreakCooldownSec || 3600,
        model_label: variant.label || "BTC_10min_NORMAL_STATE_V21_LOSS_DENSITY_3OF6_8H"
      };
      continue;
    }
    if (variant.base === "SECOND_NORMAL_LOWVOL_V22") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        trade_enabled: variant.tradeEnabled === true,
        model_type: "second_normal_lowvol_v22",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_lookback_sec: variant.lookbackSec || 4200,
        second_horizon_sec: variant.horizonSec || 600,
        second_min_gap_sec: variant.gapSec || variant.horizonSec || 600,
        second_router_route_lookback_sec: variant.routeLookbackSec || 4200,
        second_router_r10_window_sec: variant.r10WindowSec || 600,
        second_router_r10_cap_bps: variant.r10CapBps ?? 42,
        second_router_down_r10_cap_bps: variant.downR10CapBps ?? 35,
        second_router_mid_route_sigma_cap_bps: variant.midRouteSigmaCapBps ?? 20,
        second_router_min_observed_pct: variant.minObservedPct ?? 88,
        second_router_veto_low_up: variant.vetoLowUp === true,
        second_lowvol_route_sigma_max_bps: variant.lowVolRouteSigmaMaxBps ?? 10,
        second_lowvol_confirm_sec: variant.lowVolConfirmSec ?? 15,
        second_lowvol_reversion_bps: variant.lowVolReversionBps ?? 0.5,
        second_lowvol_breakout_bps: variant.lowVolBreakoutBps ?? 1.5,
        normal_state_loss_density_enabled: false,
        normal_state_loss_streak_enabled: false,
        model_label: variant.label || "正态V22 低波动确认影子"
      };
      continue;
    }
    if (variant.base === "SECOND_NORMAL_LIQUIDITY_ORDERBOOK_V1") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        trade_enabled: variant.tradeEnabled === true,
        model_type: variant.v9AugmentedEnabled === true
          ? "second_normal_liquidity_orderbook_v1"
          : "second_normal_trend_orderbook_latch_v2",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_liq_normal_window_sec: variant.normalWindowSec || 600,
        second_liq_horizon_sec: variant.horizonSec || 600,
        second_liq_signal_gap_sec: variant.gapSec || variant.horizonSec || 600,
        second_liq_z_entry: variant.zEntry ?? 1.2,
        second_liq_z_reclaim: variant.zReclaim ?? 0.85,
        second_liq_retest_sec: variant.retestSec || 120,
        second_liq_inside_min: variant.insideMin ?? 0.55,
        second_liq_observed_min_pct: variant.observedMinPct ?? 88,
        second_liq_center_slope_sec: variant.centerSlopeSec || 300,
        second_liq_center_slope_max_bps: variant.centerSlopeMaxBps ?? 8,
        second_liq_sigma_min_bps: variant.sigmaMinBps ?? 5.8,
        second_liq_sigma_max_bps: variant.sigmaMaxBps ?? 55,
        second_liq_sigma_expand_max: variant.sigmaExpandMax ?? 1.9,
        second_liq_orderbook_max_age_sec: variant.orderbookMaxAgeSec || 3,
        second_liq_ob_imbalance_min: variant.obImbalanceMin ?? 0.08,
        second_liq_micro_min_bps: variant.microMinBps ?? 0.001,
        second_liq_wall_ratio_min: variant.wallRatioMin ?? 1.0,
        second_liq_flow_guard: variant.flowGuard ?? 0.12,
        second_liq_true_break_flow: variant.trueBreakFlow ?? 0.28,
        second_liq_true_break_imbalance: variant.trueBreakImbalance ?? 0.28,
        second_liq_bidwall_trap_enabled: variant.bidwallTrapEnabled !== false,
        second_liq_bidwall_trap_ret300_max_bps: variant.bidwallTrapRet300MaxBps ?? -5,
        second_liq_bidwall_trap_bid20_chg60_min: variant.bidwallTrapBid20Chg60Min ?? 2,
        second_liq_bidwall_trap_ret600_min_bps: variant.bidwallTrapRet600MinBps ?? -20,
        second_liq_quality_v2_enabled: variant.qualityV2Enabled !== false,
        second_liq_quality_v2_down_bid20_chg60_min: variant.qualityV2DownBid20Chg60Min ?? -0.7,
        second_liq_quality_v2_up_flow60_min: variant.qualityV2UpFlow60Min ?? -0.063,
        second_liq_trend_space_enabled: variant.trendSpaceEnabled === true,
        second_liq_trend_space_sigma_expand_max: variant.trendSpaceSigmaExpandMax ?? 1.6,
        second_liq_trend_space_center_slope_abs_max_bps: variant.trendSpaceCenterSlopeAbsMaxBps ?? 6,
        second_liq_trend_space_inside_max: variant.trendSpaceInsideMax ?? 0.75,
        second_liq_trend_space_trend_ret_1800_bps: variant.trendSpaceTrendRet1800Bps ?? 15,
        second_liq_trend_space_up_pos_1800_min: variant.trendSpaceUpPos1800Min ?? 0.72,
        second_liq_trend_space_down_pos_1800_max: variant.trendSpaceDownPos1800Max ?? 0.28,
        second_liq_trend_space_block_countertrend: variant.trendSpaceBlockCountertrend !== false,
        second_liq_trend_space_block_upper_fade_pullback: variant.trendSpaceBlockUpperFadePullback !== false,
        second_liq_trend_space_short_ret_600_up_bps: variant.trendSpaceShortRet600UpBps ?? 12,
        second_liq_trend_space_short_pos_600_min: variant.trendSpaceShortPos600Min ?? 0.65,
        second_liq_mode: variant.liquidityMode || "reclaim",
        v9_augmented_enabled: variant.v9AugmentedEnabled === true,
        v9_efficiency_min: variant.v9EfficiencyMin ?? 0.60,
        v9_trend_strength_min: variant.v9TrendStrengthMin ?? 1.25,
        v9_opposing_min_bps: variant.v9OpposingMinBps ?? 2.0,
        v9_z30_min: variant.v9Z30Min ?? 1.0,
        v9_volume_ratio_min: variant.v9VolumeRatioMin ?? 0.80,
        v9_book_coverage_min: variant.v9BookCoverageMin ?? 0.90,
        v9_book_votes_min: variant.v9BookVotesMin ?? 2,
        v9_max_emit_age_sec: variant.v9MaxEmitAgeSec ?? 8,
        v9_supplement_min_abs_normal_z: variant.v9SupplementMinAbsNormalZ ?? 0,
        v9_original_regime_veto_enabled: variant.v9OriginalRegimeVetoEnabled === true,
        v9_original_veto_mature_downtrend: variant.v9OriginalVetoMatureDowntrend !== false,
        v9_original_veto_short_migration_up_down: variant.v9OriginalVetoShortMigrationUpDown !== false,
        v9_original_allow_mature_downtrend_down_flow_min: variant.v9OriginalAllowMatureDowntrendDownFlowMin ?? null,
        v9_supplement_loose_short_migration_reversion_enabled: variant.v9SupplementLooseShortMigrationReversionEnabled === true,
        v9_supplement_loose_mature_uptrend_down_enabled: variant.v9SupplementLooseMatureUptrendDownEnabled === true,
        v9_supplement_mature_uptrend_down_flow_min: variant.v9SupplementMatureUptrendDownFlowMin ?? -0.3,
        router_latch_sec: 6,
        router_execution_interval_sec: 5,
        router_execution_phase: 0,
        router_max_emit_age_sec: 3,
        router_data_observed_min_pct: 90,
        router_orderbook_coverage_min: 0.9,
        router_trend_confirm_sec: 20,
        router_startup_skip_enabled: variant.startupSkipEnabled === true,
        router_startup_skip_threshold: variant.startupSkipThreshold ?? 4,
        router_band_ultra_low_z_entry: 0.8,
        router_band_ultra_low_z_reclaim: 0.8,
        router_band_ultra_low_confirm_hits: 2,
        router_band_ultra_low_confirm_span_sec: 5,
        router_band_ultra_low_ret600_min_bps: -15,
        router_band_ultra_low_flow120_min: -0.12,
        router_band_low_z_entry: 0.9,
        router_band_low_z_reclaim: 0.85,
        router_band_low_confirm_hits: 2,
        router_band_low_confirm_span_sec: 5,
        router_band_low_ret600_min_bps: -15,
        router_band_low_flow120_min: -0.12,
        router_band_mid_z_entry: 1.0,
        router_band_mid_z_reclaim: 0.9,
        router_band_mid_confirm_hits: 2,
        router_band_mid_confirm_span_sec: 5,
        router_band_mid_ret600_min_bps: -12,
        router_band_mid_flow120_min: -0.08,
        router_band_elevated_z_entry: 1.2,
        router_band_elevated_z_reclaim: 0.85,
        router_band_elevated_confirm_hits: 3,
        router_band_elevated_confirm_span_sec: 8,
        router_band_elevated_ret600_min_bps: -10,
        router_band_elevated_flow120_min: -0.08,
        router_band_high_enabled: false,
        normal_state_loss_density_enabled: false,
        normal_state_loss_streak_enabled: false,
        model_label: variant.label || "正态流动性订单薄V1 影子"
      };
      continue;
    }
    if (variant.base === "SECOND_BRANCH_VOTE_STARTUP_V1") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        trade_enabled: variant.tradeEnabled === true,
        model_type: "second_branch_vote_startup_v1",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_lookback_sec: variant.lookbackSec || 7200,
        second_horizon_sec: variant.horizonSec || 600,
        second_min_gap_sec: variant.gapSec || variant.horizonSec || 600,
        branch_vote_normal_window_sec: variant.normalWindowSec || 600,
        branch_vote_horizon_sec: variant.horizonSec || 600,
        branch_vote_signal_gap_sec: variant.gapSec || variant.horizonSec || 600,
        branch_vote_orderbook_max_age_sec: variant.orderbookMaxAgeSec || 3,
        branch_vote_min_votes: variant.minVotes || 2,
        branch_vote_startup_skip_threshold: variant.startupSkipThreshold || 4,
        branch_vote_rule_path: variant.rulePath || "data/branch_vote_startup_rules.json",
        normal_state_loss_density_enabled: false,
        normal_state_loss_streak_enabled: false,
        model_label: variant.label || "分支投票趋势启动V1"
      };
      continue;
    }
    if (variant.base === "SECOND_MULTI_NORMAL_HF_STABLE_V1") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        trade_enabled: variant.tradeEnabled === true,
        model_type: "second_multi_normal_hf_stable_v1",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_lookback_sec: variant.lookbackSec || 7200,
        second_horizon_sec: variant.horizonSec || 600,
        second_min_gap_sec: variant.gapSec || variant.horizonSec || 600,
        multi_normal_window_sec: variant.normalWindowSec || 600,
        multi_normal_horizon_sec: variant.horizonSec || 600,
        multi_normal_signal_gap_sec: variant.gapSec || variant.horizonSec || 600,
        multi_normal_orderbook_max_age_sec: variant.orderbookMaxAgeSec || 3,
        multi_normal_lowvol_sigma_max_bps: variant.lowVolSigmaMaxBps ?? 3,
        multi_normal_lowvol_range_max_bps: variant.lowVolRangeMaxBps ?? 20,
        multi_normal_lowvol_abs_ret10_max_bps: variant.lowVolAbsRet10MaxBps ?? 5,
        multi_normal_lowvol_z_min: variant.lowVolZMin ?? 1.2,
        multi_normal_lowvol_z_max: variant.lowVolZMax ?? 1.8,
        multi_normal_lowvol_min_signed_flow: variant.lowVolMinSignedFlow ?? 0,
        multi_normal_lowvol_max_adverse_ret30_sigma: variant.lowVolMaxAdverseRet30Sigma ?? 0.5,
        multi_normal_trend_base_z_min: variant.trendBaseZMin ?? 1.2,
        multi_normal_trend_high_vol_sigma_min_bps: variant.trendHighVolSigmaMinBps ?? 8,
        multi_normal_trend_high_vol_z_min: variant.trendHighVolZMin ?? 0.5,
        multi_normal_trend_min_signed_flow: variant.trendMinSignedFlow ?? 0.12,
        multi_normal_trend_max_signed_book: variant.trendMaxSignedBook ?? 0.08,
        incident_filter_enabled: false,
        normal_state_loss_density_enabled: false,
        normal_state_loss_streak_enabled: false,
        model_label: variant.label || "多周期动态正态高频稳定V1"
      };
      continue;
    }
    if (variant.base === "SECOND_MULTISCALE_PHASE_GATE_V1") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        trade_enabled: variant.tradeEnabled === true,
        model_type: "second_multiscale_phase_gate_v1",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_lookback_sec: variant.lookbackSec || 7800,
        second_horizon_sec: variant.horizonSec || 600,
        second_min_gap_sec: variant.gapSec || variant.horizonSec || 600,
        phase_gate_horizon_sec: variant.horizonSec || 600,
        phase_gate_signal_gap_sec: variant.gapSec || variant.horizonSec || 600,
        phase_gate_orderbook_max_age_sec: variant.orderbookMaxAgeSec || 3,
        phase_gate_max_emit_age_sec: variant.maxEmitAgeSec || 8,
        phase_gate_lookback_sec: variant.phaseLookbackSec || 3600,
        phase_gate_maturity_history_sec: variant.maturityHistorySec || 3600,
        phase_gate_maturity_min_periods: variant.maturityMinPeriods || 1800,
        phase_gate_maturity_quantile: variant.maturityQuantile ?? 0.75,
        phase_gate_min_flow60: variant.minFlow60 ?? 0.08,
        phase_gate_min_imbalance20: variant.minImbalance20 ?? 0.05,
        phase_gate_min_microprice_bps: variant.minMicropriceBps ?? 0,
        phase_gate_min_volume_ratio: variant.minVolumeRatio ?? 0.8,
        incident_filter_enabled: false,
        normal_state_loss_density_enabled: false,
        normal_state_loss_streak_enabled: false,
        model_label: variant.label || "多周期迁移阶段 V1"
      };
      continue;
    }
    if (variant.base === "SECOND_RANGE_BREAKOUT_CONFIRM") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        trade_enabled: variant.tradeEnabled === true,
        model_type: "second_range_breakout_confirm",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_range_lookback_sec: variant.lookbackSec || 1800,
        second_range_horizon_sec: variant.horizonSec || 600,
        second_range_signal_gap_sec: variant.gapSec || variant.horizonSec || 600,
        second_range_z_entry: variant.rangeZEntry ?? 2.2,
        second_range_confirm_sec: variant.rangeConfirmSec ?? 60,
        second_range_hold_z: variant.rangeHoldZ ?? 1.0,
        second_range_min_hold_ratio: variant.rangeMinHoldRatio ?? 0.75,
        second_range_pre_slope_sec: variant.rangePreSlopeSec ?? 300,
        second_range_confirm_slope_sec: variant.rangeConfirmSlopeSec ?? 60,
        second_range_min_pre_slope_bps: variant.rangeMinPreSlopeBps ?? 8,
        second_range_min_confirm_slope_bps: variant.rangeMinConfirmSlopeBps ?? 4,
        second_range_min_flow_imbalance: variant.rangeMinFlowImbalance ?? 0.12,
        second_range_min_confirm_flow_imbalance: variant.rangeMinConfirmFlowImbalance ?? 0.08,
        second_range_min_volume_ratio: variant.rangeMinVolumeRatio ?? 0.45,
        second_range_min_volatility_ratio: variant.rangeMinVolatilityRatio ?? 0.55,
        second_range_max_age_beyond_sec: variant.rangeMaxAgeBeyondSec ?? 180,
        model_label: variant.label || "range breakout confirm shadow"
      };
      continue;
    }
    if (variant.base === "SECOND_TREND_DOWN") {
      out[variant.id] = {
        ...current,
        enabled: variant.enabled,
        model_type: "second_trend_pullback_down",
        symbol: "btcusdt",
        interval_min: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        horizon: Math.max(1, Math.round(Number(variant.horizonSec || 600) / 60)),
        second_trend_regime_lookback_sec: variant.regimeLookbackSec || 7200,
        second_trend_regime_drop_pct: variant.regimeDropPct || 0.004,
        second_trend_pullback_sec: variant.pullbackSec || 300,
        second_trend_pullback_pct: variant.pullbackPct || 0.001,
        second_trend_horizon_sec: variant.horizonSec || 600,
        second_trend_min_gap_sec: variant.gapSec || variant.horizonSec || 600,
        second_trend_suppress_reversal: variant.suppressReversal !== false,
        model_label: "SECOND_TREND_DOWN_7200_04_300_10"
      };
      continue;
    }
    out[variant.id] = {
      ...template,
      ...current,
      enabled: variant.enabled,
      model_type: "poc_normal",
      norm_tail_pct: variant.tailPct,
      norm_taker_filter: variant.base === "TAKER" ? "align" : "none",
      model_label: `${variant.base}_${Math.round(variant.tailPct * 100)}_${100 - Math.round(variant.tailPct * 100)}`
    };
  }
  return out;
}

function saveProdStrategyParams(config) {
  const next = applyProdStrategyParams(readProdConfig(), config);
  fs.writeFileSync(PROD_CONFIG_FILE, JSON.stringify(next, null, 2));
}

function strategyRestartFingerprint(config) {
  return JSON.stringify(strategyVariants(config).map(v => ({
    id: v.id,
    base: v.base,
    amount: v.amount,
    tailPct: v.tailPct,
    enabled: v.enabled,
    tradeEnabled: v.tradeEnabled,
    lookbackSec: v.lookbackSec,
    horizonSec: v.horizonSec,
    gapSec: v.gapSec,
    secondFilter: v.secondFilter,
    zoneFilter: v.zoneFilter,
    sigmaMinBps: v.sigmaMinBps,
    sigmaMaxBps: v.sigmaMaxBps,
    ...(v.base === "SECOND_VW_CONFIRM" ? {
      etaTargetBps: v.etaTargetBps,
      etaMaxWaitSec: v.etaMaxWaitSec
    } : {}),
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
    ...(v.base === "SECOND_NORMAL_STATE_V11" ? {
      confirmDelaySec: v.confirmDelaySec,
      maxAdverseBps: v.maxAdverseBps,
      signalHoldSec: v.signalHoldSec,
      bandwalkMax: v.bandwalkMax,
      minConsensusVotes: v.minConsensusVotes,
      stateGate: v.stateGate,
      confirmationVeto: v.confirmationVeto
    } : {}),
    ...(v.base === "SECOND_NORMAL_ROUTER_V21" || v.base === "SECOND_NORMAL_LOWVOL_V22" ? {
      routeLookbackSec: v.routeLookbackSec,
      r10WindowSec: v.r10WindowSec,
      r10CapBps: v.r10CapBps,
      downR10CapBps: v.downR10CapBps,
      midRouteSigmaCapBps: v.midRouteSigmaCapBps,
      minObservedPct: v.minObservedPct,
      lossDensityEnabled: v.lossDensityEnabled,
      lossDensityWindow: v.lossDensityWindow,
      lossDensityLosses: v.lossDensityLosses,
      lossDensityMinTrades: v.lossDensityMinTrades,
      lossDensityCooldownSec: v.lossDensityCooldownSec,
      lossDensityLookbackHours: v.lossDensityLookbackHours,
      lossStreakEnabled: v.lossStreakEnabled,
      lossStreakCount: v.lossStreakCount,
      lossStreakCooldownSec: v.lossStreakCooldownSec,
      vetoLowUp: v.vetoLowUp
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
      startupSkipEnabled: v.startupSkipEnabled === true,
      startupSkipThreshold: v.startupSkipThreshold,
      liquidityMode: v.liquidityMode
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
    incidentCooldownSec: v.incidentCooldownSec,
    chipTargetShare: v.chipTargetShare,
    chipBinMode: v.chipBinMode,
    chipBinSize: v.chipBinSize,
    chipBinPct: v.chipBinPct,
    chipBreakPct: v.chipBreakPct,
    chipDirectionFilter: v.chipDirectionFilter,
    chipFilter: v.chipFilter,
    regimeLookbackSec: v.regimeLookbackSec,
    regimeDropPct: v.regimeDropPct,
    pullbackSec: v.pullbackSec,
    pullbackPct: v.pullbackPct,
    suppressReversal: v.suppressReversal
  })));
}
  return {
    normalizeLlmInteger,
    readProdConfig,
    applyProdStrategyParams,
    saveProdStrategyParams,
    strategyRestartFingerprint
  };
}

module.exports = { createProdStrategyConfig };