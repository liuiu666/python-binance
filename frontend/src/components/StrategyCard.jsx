import {
  Activity,
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  CircleDashed,
  Clock,
  Minus,
  RefreshCw,
  ShieldCheck,
  Target,
  XCircle
} from "lucide-react";
import {
  dateTimeText,
  directionClass,
  directionText,
  displaySignalTime,
  fmt,
  fmtPrice,
  fmtPct,
  signalHumanSummary,
  signalLabel,
  signalReadinessItems,
  signalReasonText,
  statLine
} from "../utils";

function DirectionBadge({ signal }) {
  const dir = signal?.signal;
  const Icon = dir === "UP" ? ArrowUp : dir === "DOWN" ? ArrowDown : Minus;
  return (
    <span className={`direction-badge ${directionClass(dir)}`}>
      <Icon size={15} />
      {directionText(dir)}
    </span>
  );
}

function Flag({ children, tone = "neutral" }) {
  return <span className={`flag ${tone}`}>{children}</span>;
}

function BacktestLine({ backtest }) {
  if (!backtest) return <span>回测基准：当前参数暂无固定基准</span>;
  return (
    <span>
      回测基准：{fmtPct(backtest.wr, 2)}，{backtest.tradesPerDay}单/天，{backtest.trades}单，最大连亏 {backtest.maxLoss}
      {backtest.sampleHours ? `，样本 ${fmt(backtest.sampleHours, 1)}h` : ""}
    </span>
  );
}

function DetailRows({ signal }) {
  const condition = signal?.condition_summary || {};
  const rows = [
    ["当前状态", signalHumanSummary(signal)],
    ["预计信号", signal?.next_signal_estimate],
    ["下次扫描", signal?.next_check_time_shanghai],
    ["入场规则", condition.entry],
    ["风控规则", condition.risk],
    ["亏损冷却", condition.loss_density],
    ["状态过滤", condition.state],
    ["V19过滤", condition.veto],
    ["间隔限制", condition.gap]
  ].filter(([, value]) => value !== undefined && value !== null && value !== "");

  if (!rows.length) return null;
  return (
    <div className="detail-list">
      {rows.map(([label, value]) => (
        <div className="detail-row" key={label}>
          <span>{label}</span>
          <strong>{value}</strong>
        </div>
      ))}
    </div>
  );
}

function ReadinessChecklist({ signal, variant }) {
  const items = signalReadinessItems(signal, variant);
  if (!items.length || signal?.signal) return null;
  return (
    <div className="readiness-panel">
      <div className="readiness-head">
        <strong>为什么现在没下单</strong>
        <span>{signalReasonText(signal)}</span>
      </div>
      <div className="readiness-grid">
        {items.map(item => (
          <div className={`readiness-item ${item.tone}`} key={item.key}>
            <div>
              <span>{item.label}</span>
              <strong>{item.value}</strong>
            </div>
            <small>{item.target}</small>
            <em>{item.ok === true ? "通过" : item.ok === false ? item.help : "等待数据"}</em>
          </div>
        ))}
      </div>
    </div>
  );
}

function FeatureGrid({ signal }) {
  const rows = [
    ["路由波动", signal?.route_sigma_bps != null ? `${fmt(signal.route_sigma_bps, 2)}bp` : null],
    ["10分钟范围", signal?.r10_bps != null ? `${fmt(signal.r10_bps, 2)}bp` : null],
    ["秒级覆盖", signal?.observed600_pct != null ? `${fmt(signal.observed600_pct, 1)}% / ${fmt(signal.min_observed_pct || 88, 0)}%` : null],
    ["扫描间隔", signal?.scan_interval_sec != null ? `${signal.scan_interval_sec}s` : null],
    ["Z值", signal?.z_score],
    ["峰值Z", signal?.peak_abs_z],
    ["离开区间", signal?.outside_sec != null ? `${signal.outside_sec}s` : null],
    ["10分钟波动", signal?.sigma10_bps != null ? `${fmt(signal.sigma10_bps, 2)}bp` : null],
    ["60秒资金流", signal?.flow60],
    ["订单薄20档", signal?.ob_imb20],
    ["微价格", signal?.ob_micro_bps != null ? `${fmt(signal.ob_micro_bps, 4)}bp` : null],
    ["共识票", signal?.consensus_votes != null ? `${signal.consensus_votes}/${signal?.min_consensus_votes || 2}` : null],
    ["信号年龄", signal?.signal_age_sec != null ? `${fmt(signal.signal_age_sec, 1)}s` : null],
    ["候选数", signal?.candidate_count]
  ].filter(([, value]) => value !== undefined && value !== null && value !== "");

  if (!rows.length) return null;
  return (
    <div className="feature-grid">
      {rows.map(([label, value]) => (
        <div key={label}>
          <span>{label}</span>
          <strong>{value}</strong>
        </div>
      ))}
    </div>
  );
}

function pathTone(status) {
  if (status === "ready") return "ok";
  if (status === "watching") return "warn";
  if (status === "waiting_data") return "warn";
  return "neutral";
}

function CheckIcon({ ok }) {
  if (ok === true) return <CheckCircle2 size={16} />;
  if (ok === false) return <XCircle size={16} />;
  return <CircleDashed size={16} />;
}

function observationState(reached, gap) {
  if (reached === true) return "价格已到观察区";
  if (Number.isFinite(Number(gap))) return `还差 ${fmt(gap, 2)}bp`;
  return "等待区间价格";
}

function remainingText(seconds) {
  const value = Math.max(0, Math.ceil(Number(seconds) || 0));
  const minutes = Math.floor(value / 60);
  const rest = value % 60;
  return minutes > 0 ? `${minutes}分${rest}秒` : `${rest}秒`;
}

function MultiNormalStatus({ signal }) {
  if (signal?.model_type !== "second_multi_normal_hf_stable_v1") return null;
  const state = signal?.market_state_detail || {};
  const band = signal?.normal_band || {};
  const paths = Array.isArray(signal?.signal_paths) ? signal.signal_paths : [];
  const lowPath = paths.find(path => path.key === "lowvol_normal_reversion");
  const lowObservation = lowPath?.observation || {};
  const snapshotPrice = band.price ?? signal?.price;
  const snapshotTime = signal?.detected_time_shanghai || dateTimeText(signal?.detected_time);
  const reviewTime = signal?.next_review_time_shanghai || dateTimeText(signal?.next_review_time);
  const gapRemaining = Number(signal?.window_remaining_sec);
  const eligibleTime = signal?.last_window_owner_time && Number.isFinite(Number(signal?.min_gap_sec))
    ? dateTimeText(Date.parse(signal.last_window_owner_time) + Number(signal.min_gap_sec) * 1000)
    : null;
  const metricRows = [
    ["快照价格", fmtPrice(snapshotPrice)],
    ["正态中轴", fmtPrice(band.center)],
    ["当前位置", band.z == null ? "--" : `${fmt(band.z, 3)}σ`],
    ["10分钟波动", signal?.sigma10_bps == null ? "--" : `${fmt(signal.sigma10_bps, 2)}bp`],
    ["10分钟振幅", signal?.range10_bps == null ? "--" : `${fmt(signal.range10_bps, 2)}bp`],
    ["成交流 / 订单薄", `${signal?.flow5 == null ? "--" : fmt(signal.flow5, 3)} / ${signal?.imb20 == null ? "--" : fmt(signal.imb20, 3)}`]
  ];

  return (
    <section className="multi-normal-status">
      <div className="multi-state-head">
        <div>
          <span className="eyebrow">当前行情</span>
          <strong>{state.label || "等待完整分钟判断"}</strong>
          <p>{state.detail || signal?.signal_detail || "共享策略核心正在生成实时状态。"}</p>
        </div>
        <span className={`multi-state-badge ${signal?.signal ? "ok" : "warn"}`}>
          {signal?.signal ? `${directionText(signal.signal)}信号已确认` : signalReasonText(signal)}
        </span>
      </div>

      <div className="multi-metrics">
        {metricRows.map(([label, value]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
          </div>
        ))}
      </div>

      <div className="normal-watch-band">
        <div className={`normal-watch-level up ${lowObservation.lower_watch_reached ? "reached" : ""}`}>
          <span>做多观察价 · -1.2σ</span>
          <strong>{fmtPrice(lowObservation.lower_watch_price)}</strong>
          <small>{observationState(lowObservation.lower_watch_reached, lowObservation.lower_gap_bps)}</small>
        </div>
        <div className="normal-watch-level center">
          <span>滚动正态中轴</span>
          <strong>{fmtPrice(band.center)}</strong>
          <small>每个完整分钟重新计算</small>
        </div>
        <div className={`normal-watch-level down ${lowObservation.upper_watch_reached ? "reached" : ""}`}>
          <span>做空观察价 · +1.2σ</span>
          <strong>{fmtPrice(lowObservation.upper_watch_price)}</strong>
          <small>{observationState(lowObservation.upper_watch_reached, lowObservation.upper_gap_bps)}</small>
        </div>
      </div>
      <div className="normal-shift-line">
        <Target size={15} />
        <span>
          换区警戒：低于 {fmtPrice(lowObservation.lower_shift_price)} 或高于 {fmtPrice(lowObservation.upper_shift_price)}（±1.8σ）时，不直接做正态回归。
        </span>
      </div>

      {Number.isFinite(gapRemaining) && gapRemaining > 0 ? (
        <div className="multi-gap-line">
          <Clock size={15} />
          <span>
            同策略10分钟间隔还剩 {remainingText(gapRemaining)}{eligibleTime ? `，最早 ${eligibleTime} 恢复发信号资格` : ""}；到时仍需当前路径全部条件通过，不代表一定下单。
          </span>
        </div>
      ) : null}

      <div className="multi-paths">
        {paths.map(path => {
          const checks = Array.isArray(path.checks) ? path.checks : [];
          const observation = path.observation || {};
          const pathPrice = path.key === "mature_trend_exhaustion" ? observation.watch_price : null;
          return (
            <section className={`multi-path ${pathTone(path.status)}`} key={path.key}>
              <header>
                <div>
                  <strong>{path.label}</strong>
                  <span>{path.summary}</span>
                </div>
                <em className={`multi-path-status ${pathTone(path.status)}`}>{path.status_zh || "等待"}</em>
              </header>
              <div className="multi-path-target">
                <span>候选方向</span>
                <strong>{path.candidate_signal ? directionText(path.candidate_signal) : "尚未确定"}</strong>
                {pathPrice != null ? <small>偏离观察价 {fmtPrice(pathPrice)}</small> : null}
                <small>{path.passed ?? 0}/{path.total ?? checks.length} 项通过</small>
              </div>
              <div className="multi-checks">
                {checks.map(item => (
                  <div className={`multi-check ${item.ok === true ? "ok" : item.ok === false ? "bad" : "warn"}`} key={item.key}>
                    <CheckIcon ok={item.ok} />
                    <div>
                      <strong>{item.label}</strong>
                      <small>目标：{item.target}</small>
                      {item.ok === false ? <em>{item.help}</em> : null}
                    </div>
                    <span>{item.value}</span>
                  </div>
                ))}
              </div>
            </section>
          );
        })}
      </div>

      <div className="multi-review-line">
        <RefreshCw size={16} />
        <div>
          <strong>下次完整分钟复核：{reviewTime || "--"}</strong>
          <span>当前状态快照：{snapshotTime || "--"}。进程每 {signal?.scan_interval_sec || 2} 秒读取数据，但策略只使用已经结束的分钟；观察价会随滚动窗口变化，没有固定的信号倒计时。</span>
        </div>
      </div>
    </section>
  );
}

const PHASE_SHAPE_LABELS = {
  balanced_normal: "平衡区间",
  shift_up: "中心上移",
  shift_down: "中心下移",
  upper_escape: "向上脱离",
  lower_escape: "向下脱离",
  contracting: "波动收缩",
  expanding: "波动扩张",
  right_skew: "右偏",
  left_skew: "左偏",
  heavy_tail: "厚尾",
  distorted: "结构切换"
};

function phaseShapeText(value) {
  return PHASE_SHAPE_LABELS[value] || value || "等待完整分钟";
}

function phaseState(signal) {
  if (Number(signal?.window_remaining_sec) > 0 || signal?.reason === "phase_gate_gap") {
    return { tone: "cooldown", label: "10分钟冷却中", detail: "上一单到期后，才会重新接受下一次迁移信号。" };
  }
  if (signal?.signal) {
    return { tone: directionClass(signal.signal), label: `${directionText(signal.signal)}信号已确认`, detail: signal?.signal_detail };
  }
  if (signal?.phase === "countertrend_pullback") {
    return { tone: "ready", label: "逆大周期回调", detail: "短周期迁移与过去60分钟相反，候选方向是反向回归。" };
  }
  if (signal?.phase === "mature") {
    return { tone: "ready", label: "迁移已经成熟", detail: "60分钟位移达到滚动成熟阈值，候选方向是拥挤衰竭回归。" };
  }
  if (signal?.phase === "startup_or_middle") {
    return { tone: "watch", label: "迁移启动或中段", detail: "这个阶段继续追和提前反转都不稳定，策略主动跳过。" };
  }
  return { tone: "neutral", label: "等待迁移候选", detail: "2、3、5分钟尚未形成同向迁移，或10分钟已经不属于旧价格区域。" };
}

function PhaseGateStatus({ signal, variant }) {
  if (signal?.model_type !== "second_multiscale_phase_gate_v1") return null;
  const state = phaseState(signal);
  const shape3 = signal?.shape_3m;
  const shortDirection = shape3 === "shift_up" ? "UP" : shape3 === "shift_down" ? "DOWN" : null;
  const expectedShift = shortDirection === "UP" ? "shift_up" : shortDirection === "DOWN" ? "shift_down" : null;
  const expectedEscape = shortDirection === "UP" ? "upper_escape" : shortDirection === "DOWN" ? "lower_escape" : null;
  const opposingEscape = shortDirection === "UP" ? "lower_escape" : shortDirection === "DOWN" ? "upper_escape" : null;
  const shortShapeReady = !!shortDirection
    && signal?.shape_5m === expectedShift
    && [expectedShift, expectedEscape].includes(signal?.shape_2m)
    && signal?.shape_1m !== opposingEscape;
  const migration = signal?.migration_direction || (shortShapeReady ? shortDirection : null) || signal?.crowd_direction;
  const confirmed = signal?.crowd_direction;
  const crowdSign = migration === "UP" ? 1 : migration === "DOWN" ? -1 : null;
  const aligned = value => crowdSign == null || value == null ? null : Number(value) * crowdSign;
  const flowAligned = aligned(signal?.flow60);
  const bookAligned = aligned(signal?.imbalance20);
  const microAligned = aligned(signal?.microprice_bps);
  const move = signal?.aligned_ret3600_bps == null ? Number.NaN : Number(signal.aligned_ret3600_bps);
  const threshold = signal?.maturity_threshold_bps == null ? Number.NaN : Number(signal.maturity_threshold_bps);
  const cooldown = Number(signal?.window_remaining_sec);
  const nextReview = signal?.next_review_time_shanghai || dateTimeText(signal?.next_review_time);
  const dependencyRows = [
    {
      label: "多周期形态",
      value: `1分 ${phaseShapeText(signal?.shape_1m)} · 2分 ${phaseShapeText(signal?.shape_2m)} · 3分 ${phaseShapeText(signal?.shape_3m)} · 5分 ${phaseShapeText(signal?.shape_5m)}`,
      target: "2/3/5分钟同向，1分钟没有反向脱离",
      ok: shortShapeReady
    },
    {
      label: "10分钟背景",
      value: phaseShapeText(signal?.shape_10m),
      target: "仍在旧区间、收缩或同向边缘",
      ok: ["balanced_normal", "contracting", "upper_escape", "lower_escape"].includes(signal?.shape_10m)
    },
    {
      label: "主动成交",
      value: flowAligned == null ? `当前 ${fmt(signal?.flow60, 3)}` : `同向 ${fmt(flowAligned, 3)}`,
      target: `同向 ≥ ${fmt(variant?.minFlow60 ?? 0.08, 2)}`,
      ok: flowAligned == null ? null : flowAligned >= Number(variant?.minFlow60 ?? 0.08)
    },
    {
      label: "订单薄与微价格",
      value: crowdSign == null ? `${fmt(signal?.imbalance20, 3)} / ${fmt(signal?.microprice_bps, 4)}bp` : `${fmt(bookAligned, 3)} / ${fmt(microAligned, 4)}bp`,
      target: `同向深度 ≥ ${fmt(variant?.minImbalance20 ?? 0.05, 2)}，微价格同向`,
      ok: crowdSign == null ? null : bookAligned >= Number(variant?.minImbalance20 ?? 0.05) && microAligned >= Number(variant?.minMicropriceBps ?? 0)
    },
    {
      label: "成交量",
      value: `${fmt(signal?.volume_ratio, 2)}倍`,
      target: `近1分钟 ≥ ${fmt(variant?.minVolumeRatio ?? 0.8, 1)}倍基准`,
      ok: signal?.volume_ratio == null ? null : Number(signal.volume_ratio) >= Number(variant?.minVolumeRatio ?? 0.8)
    }
  ];
  const phaseText = !Number.isFinite(move) || !Number.isFinite(threshold)
    ? "等待形成60分钟阶段数据"
    : move <= 0
      ? `逆向 ${fmt(move, 2)}bp：按回调衰竭处理`
      : move >= threshold
        ? `${fmt(move, 2)}bp ≥ 成熟线 ${fmt(threshold, 2)}bp`
        : `${fmt(move, 2)}bp，尚未达到成熟线 ${fmt(threshold, 2)}bp`;
  return (
    <section className="phase-gate-status">
      <div className={`phase-current ${state.tone}`}>
        <div>
          <span>当前阶段</span>
          <strong>{state.label}</strong>
          <p>{state.detail}</p>
        </div>
        <div className="phase-next-review">
          <Clock size={16} />
          <span>下次完整分钟</span>
          <strong>{nextReview || "--"}</strong>
        </div>
      </div>

      <div className="phase-section-head">
        <div>
          <strong>信号依赖</strong>
          <span>从上到下依次确认，缺一项都不会下单</span>
        </div>
        <em>{signal?.migration_direction ? `迁移背景成立 ${directionText(signal.migration_direction)}${confirmed ? "，订单薄已确认" : "，等待成交确认"}` : shortShapeReady ? `短周期${directionText(shortDirection)}，等待10分钟背景` : "短周期迁移未形成"}</em>
      </div>
      <div className="phase-dependencies">
        {dependencyRows.map(item => (
          <div className={`phase-dependency ${item.ok === true ? "ok" : item.ok === false ? "bad" : "waiting"}`} key={item.label}>
            <CheckIcon ok={item.ok} />
            <div>
              <strong>{item.label}</strong>
              <span>{item.value}</span>
              <small>{item.target}</small>
            </div>
          </div>
        ))}
      </div>

      <div className="phase-decision">
        <div>
          <span>60分钟阶段判断</span>
          <strong>{phaseText}</strong>
        </div>
        <div>
          <span>最终动作</span>
          <strong>{signal?.signal ? `${directionText(signal.signal)}，持有10分钟` : signal?.phase === "startup_or_middle" ? "跳过，不追中段" : confirmed ? "等待阶段可交易" : migration ? "等待成交与订单薄确认" : "继续等待形态"}</strong>
        </div>
        <div>
          <span>执行保护</span>
          <strong>{Number.isFinite(cooldown) && cooldown > 0 ? `冷却剩余 ${remainingText(cooldown)}` : `信号年龄必须 ≤ ${variant?.maxEmitAgeSec ?? 8}秒`}</strong>
        </div>
      </div>
    </section>
  );
}

function routerStatusText(status) {
  const map = {
    ready: "可触发",
    waiting_tail: "等尾部",
    sigma_out_of_range: "波动不匹配",
    insufficient_data: "数据不足",
    flat_sigma: "波动过低",
    blocked_filter: "资金流拦截",
    blocked_zone: "区间拦截",
    blocked_low_up_veto: "low+UP否决"
  };
  return map[status] || status || "--";
}

function routerStatusTone(status) {
  if (status === "ready") return "ok";
  if (status === "waiting_tail") return "warn";
  if (String(status || "").startsWith("blocked")) return "bad";
  return "neutral";
}

function RouterDiagnostics({ signal }) {
  const rows = Array.isArray(signal?.router_diagnostics) ? signal.router_diagnostics : [];
  if (!rows.length) return null;
  return (
    <div className="router-panel">
      <div className="router-panel-head">
        <strong>V21触发拆解</strong>
        <span>任一分支进入25%尾部且通过风控才下单</span>
      </div>
      <div className="router-rows">
        {rows.map((row) => (
          <div className="router-row" key={row.branch || row.role}>
            <div className="router-main">
              <span>{String(row.role || "--").toUpperCase()}</span>
              <strong>{routerStatusText(row.status)}</strong>
              <em className={`router-tone ${routerStatusTone(row.status)}`}>{row.signal || row.nearest_signal || "--"}</em>
            </div>
            <div className="router-metrics">
              {row.p_up_pct != null ? <span>p涨 {fmt(row.p_up_pct, 1)}%</span> : null}
              {row.edge_gap_pct != null ? <span>差 {fmt(row.edge_gap_pct, 1)}pp</span> : null}
              {row.sigma_10m_bps != null ? <span>sigma {fmt(row.sigma_10m_bps, 1)}bp</span> : null}
              <span>范围 {fmt(row.sigma_min_bps, 0)}-{fmt(row.sigma_max_bps, 0)}bp</span>
            </div>
            {row.detail ? <p>{row.detail}</p> : null}
          </div>
        ))}
      </div>
    </div>
  );
}

export default function StrategyCard({ title, signal, amount, variant, stats }) {
  const active = !!signal?.signal;
  const multiNormal = signal?.model_type === "second_multi_normal_hf_stable_v1";
  const phaseGate = signal?.model_type === "second_multiscale_phase_gate_v1";
  const live = variant?.enabled !== false && variant?.tradeEnabled !== false;
  const observed = variant?.enabled !== false;
  const cardTone = active ? directionClass(signal.signal) : "neutral";

  return (
    <article className={`strategy-card ${cardTone} ${multiNormal ? "multi-normal-card" : ""} ${phaseGate ? "phase-gate-card" : ""}`}>
      <header className="strategy-card-head">
        <div>
          <span className="eyebrow">策略</span>
          <h3>{title || variant?.label || signal?.strategy_id || "未命名策略"}</h3>
          <small>{signal?.strategy_id || variant?.id}</small>
        </div>
        <DirectionBadge signal={signal} />
      </header>

      <div className="strategy-summary">
        <strong className={directionClass(signal?.signal)}>{signalLabel(signal)}</strong>
        <span>
          <Clock size={14} />
          {active ? "信号时间" : multiNormal ? "状态快照" : "状态时间"} {multiNormal && !active ? signal?.detected_time_shanghai || displaySignalTime(signal) : displaySignalTime(signal)}
        </span>
      </div>

      <div className="flag-row">
        <Flag tone={observed ? "ok" : "warn"}>{observed ? "已监控" : "未监控"}</Flag>
        <Flag tone={live ? "danger" : "neutral"}>{live ? "实盘可下单" : "仅观察/影子"}</Flag>
        <Flag>{amount || variant?.amount || "--"}U</Flag>
        <Flag>{variant?.duration || signal?.duration || "10"}分钟</Flag>
        {variant?.confirmationVeto ? <Flag tone={variant.confirmationVeto === "none" ? "neutral" : "ok"}>{variant.confirmationVeto}</Flag> : null}
      </div>

      <div className="strategy-footnote">
        <ShieldCheck size={14} />
        <BacktestLine backtest={variant?.backtest} />
      </div>

      <div className="strategy-stats">
        <span>
          <Activity size={14} />
          实盘 {statLine(stats?.real)}
        </span>
        <span>影子 {statLine(stats?.shadow)}</span>
      </div>

      {phaseGate ? (
        <PhaseGateStatus signal={signal} variant={variant} />
      ) : multiNormal ? (
        <MultiNormalStatus signal={signal} />
      ) : (
        <>
          <FeatureGrid signal={signal} />
          <ReadinessChecklist signal={signal} variant={variant} />
          <DetailRows signal={signal} />
          <RouterDiagnostics signal={signal} />
        </>
      )}

      {!multiNormal && !phaseGate && signal?.reason ? <div className="reason-line">状态：{signalReasonText(signal)}</div> : null}
      {signal?.error ? <div className="reason-line bad">异常：{signal.error}</div> : null}
      {signal?.actionable_time ? <div className="reason-line">可执行时间：{dateTimeText(signal.actionable_time)}</div> : null}
    </article>
  );
}
