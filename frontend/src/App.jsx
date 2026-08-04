import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Clock3,
  Database,
  ListChecks,
  RefreshCcw,
  Server,
  Settings,
  ShieldCheck,
  Wifi,
  Zap
} from "lucide-react";

import {
  DEFAULT_CONFIG,
  activeSignalFromPayload,
  ageText,
  amountForSignal,
  directionClass,
  directionText,
  fmt,
  fmtPct,
  fmtPrice,
  healthText,
  healthTone,
  isShadowTrade,
  pnlText,
  signalHumanSummary,
  signalLabel,
  signalReasonText,
  signalTriggerPlan,
  statLine,
  statusClass,
  statusText,
  strategyName,
  timeParts,
  useInterval
} from "./utils";

import ConfigPanel from "./components/ConfigPanel";
import LoginGate from "./components/LoginGate";
import ManualPanel from "./components/ManualPanel";
import StrategyCard from "./components/StrategyCard";
import TradeHistory from "./components/TradeHistory";

const MarketChart = lazy(() => import("./components/MarketChart"));
const NormalVisual = lazy(() => import("./components/NormalVisual"));

const TABS = [
  { id: "current", label: "当前交易", icon: Activity },
  { id: "strategies", label: "策略", icon: ShieldCheck },
  { id: "orders", label: "订单", icon: ListChecks },
  { id: "data", label: "数据", icon: Database },
  { id: "settings", label: "设置", icon: Settings }
];

function Toasts({ items }) {
  return (
    <div className="toast-stack" aria-live="polite">
      {items.map(item => <div className={`toast ${item.type || "info"}`} key={item.id}>{item.message}</div>)}
    </div>
  );
}

function MetricCard({ label, value, sub, tone = "neutral", icon: Icon }) {
  return (
    <article className={`metric-card ${tone}`}>
      <div>
        <span>{label}</span>
        <strong>{value}</strong>
        {sub ? <small>{sub}</small> : null}
      </div>
      {Icon ? <Icon size={20} /> : null}
    </article>
  );
}

function DashboardTabs({ active, counts, onChange }) {
  return (
    <nav className="dashboard-tabs" aria-label="控制台分页">
      {TABS.map(tab => {
        const Icon = tab.icon;
        return (
          <button type="button" className={active === tab.id ? "active" : ""} key={tab.id} onClick={() => onChange(tab.id)}>
            <Icon size={16} />
            {tab.label}
            {counts?.[tab.id] ? <span aria-hidden="true">{counts[tab.id]}</span> : null}
          </button>
        );
      })}
    </nav>
  );
}

function strategyStats(history, strategyId) {
  const pick = kind => (history?.breakdown?.[kind]?.byStrategy || []).find(item => item.key === strategyId);
  return { real: pick("real"), shadow: pick("shadow") };
}

function compactSignalRows(signalPayload, variants) {
  return (variants || []).map(variant => ({
    variant,
    signal: signalPayload?.[variant.id] || null
  }));
}

function TriggerPlanPanel({ signal }) {
  const plan = signalTriggerPlan(signal);
  if (!plan) return null;
  const upperText = plan.upperTrigger == null ? "--" : fmtPrice(plan.upperTrigger);
  const lowerText = plan.lowerTrigger == null ? "--" : fmtPrice(plan.lowerTrigger);
  const upGap = plan.upGapBps == null ? "--" : `${fmt(plan.upGapBps, 1)}bp`;
  const downGap = plan.downGapBps == null ? "--" : `${fmt(plan.downGapBps, 1)}bp`;
  const upperHint = plan.upGapBps != null && plan.upGapBps <= 0
    ? "已上破观察线，等待回到区间并确认卖压"
    : `先上破约 +${upGap}，再回到区间并有卖压`;
  const lowerHint = plan.downGapBps != null && plan.downGapBps <= 0
    ? "已下破观察线，等待回到区间并确认买盘"
    : `先下破约 -${downGap}，再回到区间并有买盘`;
  const zText = plan.z == null ? "--" : `${fmt(plan.z, 2)}σ`;
  return (
    <div className="trigger-plan">
      <div className="trigger-plan-head">
        <span>下一次可能信号</span>
        <strong>{signal?.signal ? `已出${directionText(signal.signal)}` : plan.nextSide}</strong>
      </div>
      <div className="trigger-price-grid">
        <div className="trigger-price down">
          <span>做空观察</span>
          <strong>{upperText}</strong>
          <small>{upperHint}</small>
        </div>
        <div className="trigger-price up">
          <span>做多观察</span>
          <strong>{lowerText}</strong>
          <small>{lowerHint}</small>
        </div>
      </div>
      <div className="trigger-state-row">
        <span>当前价 {fmtPrice(plan.price)}</span>
        <span>位置 {zText} / 阈值 ±{fmt(plan.zEntry, 1)}σ</span>
        <span>{plan.obBias}</span>
      </div>
      <div className="trigger-state-row subtle">
        <span>订单薄 bid {fmt(plan.bid, 3)} / ask {fmt(plan.ask, 3)}</span>
        <span>imb {fmt(plan.imbalance, 3)}</span>
        <span>flow60 {fmt(plan.flow60, 3)}</span>
      </div>
    </div>
  );
}

function CurrentTradePanel({ history, activeSignal, activeVariant, signalAmount, currentPrice }) {
  const activeRows = history?.active || [];
  const realRows = activeRows.filter(row => !isShadowTrade(row));
  const shadowRows = activeRows.filter(isShadowTrade);

  return (
    <section className="panel current-panel">
      <header className="section-head compact">
        <div>
          <span className="eyebrow">当前</span>
          <h2>交易状态</h2>
        </div>
        <strong>{realRows.length} 实 / {shadowRows.length} 影</strong>
      </header>

      <div className="current-signal-box">
        <span className={`big-direction ${directionClass(activeSignal?.signal)}`}>{directionText(activeSignal?.signal)}</span>
        <div>
          <strong>{signalLabel(activeSignal)}</strong>
          <small>金额 {signalAmount || "--"}U / 当前价 {fmtPrice(currentPrice)}</small>
        </div>
      </div>

      <div className="hint-box">
        <span>为什么现在没下单</span>
        <p>{signalHumanSummary(activeSignal, activeVariant)}</p>
      </div>

      <TriggerPlanPanel signal={activeSignal} />

      {activeSignal?.next_check_time_shanghai ? (
        <div className="hint-box">
          <span>下次扫描</span>
          <p>{activeSignal.next_check_time_shanghai}</p>
        </div>
      ) : null}

      <div className="active-trades">
        <div className="sub-head">
          <h3>持仓</h3>
          <span>{activeRows.length} 单</span>
        </div>
        {activeRows.length ? activeRows.slice(0, 6).map(row => {
          const tp = timeParts(row.openTime);
          const cls = statusClass(row.status);
          return (
            <article className={`active-trade ${cls}`} key={row.id || `${row.openTime}-${row.strategyId}`}>
              <div>
                <span className={`status-pill ${cls}`}>{statusText(row.status)}</span>
                <strong className={directionClass(row.direction)}>{directionText(row.direction)}</strong>
              </div>
              <p>{strategyName(row.strategyId)}</p>
              <span>{fmt(row.amount, 0)}U</span>
              <small>{tp.time}</small>
              <b>{pnlText(row)}</b>
            </article>
          );
        }) : <div className="empty-state compact">当前没有持仓，等待下一次策略信号。</div>}
      </div>
    </section>
  );
}

function StrategySnapshot({ signalRows, tradeHistory, configDraft, signalPayload, disabledCount = 0 }) {
  return (
    <section className="strategy-view">
      <header className="strategy-view-head">
        <div>
          <span className="eyebrow">运行状态</span>
          <h2>启用中的策略</h2>
          <p>这里只展示正在计算信号的策略；已停用模型仍可在设置中查看和管理。</p>
        </div>
        <strong>{signalRows.length} 运行 / {disabledCount} 停用</strong>
      </header>
      <div className="strategy-snapshot">
        {signalRows.length ? signalRows.map(({ variant, signal }) => (
          <StrategyCard
            key={variant.id}
            title={strategyName(variant.id)}
            signal={signal}
            amount={amountForSignal(variant.id, signal, signalPayload, configDraft)}
            variant={variant}
            stats={strategyStats(tradeHistory, variant.id)}
          />
        )) : <div className="empty-state">当前没有启用的策略。</div>}
      </div>
    </section>
  );
}

function DataHealthPanel({ dataHealth, secondDataHealth, orderbookHealth, orderbookPrediction, runtime, tablet, onRefreshData, onRefreshReports }) {
  const secondStatus = secondDataHealth?.status || {};
  const node = secondStatus.node_selection || {};
  const prediction = orderbookPrediction?.status?.prediction || {};
  const validation = orderbookPrediction?.status?.validation || {};
  const targets = prediction.targets || [];

  return (
    <main className="data-grid">
      <section className="panel">
        <header className="section-head compact">
          <div>
            <span className="eyebrow">采集</span>
            <h2><Database size={18} /> 数据健康</h2>
          </div>
          <div className="inline-actions">
            <button type="button" onClick={onRefreshData}><RefreshCcw size={15} /> 拉取</button>
            <button type="button" onClick={onRefreshReports}><BarChart3 size={15} /> 报告</button>
          </div>
        </header>

        <div className="health-grid">
          <MetricCard
            label="1分钟K线"
            value={healthText(dataHealth?.files?.klines1m?.ok, dataHealth?.files?.klines1m?.ageMs)}
            tone={healthTone(dataHealth?.files?.klines1m?.ok)}
            icon={BarChart3}
          />
          <MetricCard
            label="秒级成交"
            value={healthText(secondDataHealth?.ok, secondDataHealth?.ageMs)}
            sub={`${secondStatus.rows || "--"} 行 / ${secondStatus.last_ts_shanghai || secondStatus.last_ts || "--"}`}
            tone={healthTone(secondDataHealth?.ok)}
            icon={Zap}
          />
          <MetricCard
            label="订单薄"
            value={healthText(orderbookHealth?.ok, orderbookHealth?.ageMs)}
            sub={`中间价 ${fmtPrice(orderbookHealth?.status?.mid)} / 价差 ${fmt(orderbookHealth?.status?.spread_bps, 3)}bp`}
            tone={healthTone(orderbookHealth?.ok)}
            icon={Wifi}
          />
          <MetricCard
            label="采集节点"
            value={node.active_proxy ? "代理" : "直连"}
            sub={node.active_node || "未上报"}
            tone={node.active_proxy ? "ok" : "neutral"}
            icon={Server}
          />
        </div>

        {dataHealth?.reasons?.length ? <div className="reason-line bad">数据拦截原因：{dataHealth.reasons.join("；")}</div> : null}
      </section>

      <section className="panel">
        <header className="section-head compact">
          <div>
            <span className="eyebrow">流动性</span>
            <h2>订单薄预测</h2>
          </div>
          <strong className={orderbookPrediction?.ok ? "synced" : "dirty"}>{orderbookPrediction?.ok ? "正常" : "等待"}</strong>
        </header>
        <div className="orderbook-summary">
          <span className={`big-direction ${prediction.direction === "UP" ? "up" : prediction.direction === "DOWN" ? "down" : "neutral"}`}>
            {prediction.direction === "UP" ? "看涨" : prediction.direction === "DOWN" ? "看跌" : "震荡"}
          </span>
          <div>
            <strong>置信度 {fmtPct(prediction.confidence, 1)}</strong>
            <small>挂单偏向 {fmt(prediction.features?.imbalance5, 3)} / {fmt(prediction.features?.imbalance20, 3)}</small>
          </div>
        </div>
        <div className="target-list">
          {targets.length ? targets.map(target => {
            const v = validation[String(target.horizonSec)] || {};
            return (
              <div key={target.horizonSec}>
                <span>{target.horizonSec}s 后</span>
                <strong>{fmtPrice(target.predictedPrice)}</strong>
                <small>预计 {fmt(target.predictedBps, 3)}bp / 命中 {fmtPct(v.hitRate, 1)}</small>
              </div>
            );
          }) : <div className="empty-state compact">暂无订单薄预测</div>}
        </div>
      </section>

      <section className="panel">
        <header className="section-head compact">
          <div>
            <span className="eyebrow">运行</span>
            <h2>服务状态</h2>
          </div>
        </header>
        <div className="runtime-list">
          <span>服务ID <strong>{runtime?.serverId || "--"}</strong></span>
          <span>数据目录 <strong>{runtime?.dataDir || "--"}</strong></span>
          <span>信号服务 <strong>{runtime?.managedProcessesEnabled === false ? "本地模式" : "托管模式"}</strong></span>
          <span>平板心跳 <strong>{tablet?.checks?.heartbeatOnline ? `在线 ${ageText(tablet.latestHeartbeatAgeMs)}` : "等待"}</strong></span>
          <span>脚本版本 <strong>{runtime?.scriptVersion || "--"}</strong></span>
        </div>
      </section>
    </main>
  );
}

function TopBar({ currentPrice, priceChange, dataHealth, secondDataHealth, orderbookHealth, realBalance, onRefresh }) {
  const priceTone = priceChange?.diff > 0 ? "up" : priceChange?.diff < 0 ? "down" : "neutral";
  return (
    <header className="topbar">
      <div className="brand">
        <div className="brand-mark">B</div>
        <div>
          <strong>BTC 策略控制台</strong>
          <span>行情、策略、模拟单与数据状态</span>
        </div>
      </div>
      <div className="top-metrics">
        <MetricCard label="BTC" value={fmtPrice(currentPrice)} sub={priceChange ? `${priceChange.diff >= 0 ? "+" : ""}${fmtPrice(priceChange.diff)} (${priceChange.pct >= 0 ? "+" : ""}${priceChange.pct.toFixed(2)}%)` : "--"} tone={priceTone} />
        <MetricCard label="秒级" value={healthText(secondDataHealth?.ok, secondDataHealth?.ageMs)} tone={healthTone(secondDataHealth?.ok)} />
        <MetricCard label="订单薄" value={healthText(orderbookHealth?.ok, orderbookHealth?.ageMs)} tone={healthTone(orderbookHealth?.ok)} />
        <MetricCard label="1分钟" value={healthText(dataHealth?.files?.klines1m?.ok, dataHealth?.files?.klines1m?.ageMs)} tone={healthTone(dataHealth?.files?.klines1m?.ok)} />
        <MetricCard label="余额" value={realBalance?.amount !== undefined ? fmt(realBalance.amount, 2) : "--"} sub="USDT" tone="ok" />
        <button className="icon-button" type="button" onClick={onRefresh} title="刷新">
          <RefreshCcw size={17} />
        </button>
      </div>
    </header>
  );
}

function shanghaiDayKey() {
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).formatToParts(new Date());
  const map = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return `${map.year}-${map.month}-${map.day}`;
}

export default function App() {
  const [isAuthenticated, setIsAuthenticated] = useState(() => !!window.localStorage.getItem("btc_auth_token"));
  const [activeTab, setActiveTab] = useState("current");
  const [currentPrice, setCurrentPrice] = useState(null);
  const [firstPrice, setFirstPrice] = useState(null);
  const [priceHistory, setPriceHistory] = useState([]);
  const [candles, setCandles] = useState([]);
  const [signalPayload, setSignalPayload] = useState(null);
  const [runtime, setRuntime] = useState(null);
  const [tablet, setTablet] = useState(null);
  const [tradeHistory, setTradeHistory] = useState(null);
  const [tradeHistoryPage, setTradeHistoryPage] = useState({ mode: "day", day: shanghaiDayKey(), kind: "real" });
  const [dataHealth, setDataHealth] = useState(null);
  const [secondDataHealth, setSecondDataHealth] = useState(null);
  const [orderbookHealth, setOrderbookHealth] = useState(null);
  const [orderbookPrediction, setOrderbookPrediction] = useState(null);
  const [realBalance, setRealBalance] = useState(null);
  const [configDraft, setConfigDraft] = useState(DEFAULT_CONFIG);
  const [configDirty, setConfigDirty] = useState(false);
  const [llmConfig, setLlmConfig] = useState({
    apiUrl: "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions",
    apiKey: "",
    model: "glm-5.2",
    intervalSec: 600,
    maxTokens: 8000
  });
  const [llmStatus, setLlmStatus] = useState(null);
  const [apiToken, setApiToken] = useState(() => window.localStorage.getItem("btcApiToken") || "");
  const [toasts, setToasts] = useState([]);
  const dirtyRef = useRef(false);
  const lastWsPriceRef = useRef(0);

  const apiFetch = useCallback((url, options = {}) => {
    const headers = { ...(options.headers || {}) };
    const authToken = window.localStorage.getItem("btc_auth_token");
    if (authToken) headers["X-API-Token"] = authToken;
    else if (apiToken) headers["X-API-Token"] = apiToken;
    return fetch(url, { ...options, headers });
  }, [apiToken]);

  const loadJson = useCallback((url, setter) => {
    return apiFetch(url)
      .then(res => res.json())
      .then(setter)
      .catch(() => {});
  }, [apiFetch]);

  const notify = useCallback((message, type = "info") => {
    const id = `${Date.now()}-${Math.random()}`;
    setToasts(items => [...items, { id, message, type }].slice(-4));
    window.setTimeout(() => setToasts(items => items.filter(item => item.id !== id)), 2600);
  }, []);

  const allVariants = useMemo(
    () => configDraft.strategyVariants || DEFAULT_CONFIG.strategyVariants,
    [configDraft.strategyVariants]
  );
  const visibleVariants = useMemo(
    () => (signalPayload?._strategyVariants || allVariants).filter(variant => variant.enabled !== false),
    [allVariants, signalPayload]
  );
  const disabledVariantCount = allVariants.filter(variant => variant.enabled === false).length;

  const signalRows = useMemo(() => compactSignalRows(signalPayload, visibleVariants), [signalPayload, visibleVariants]);
  const activeSignal = useMemo(() => activeSignalFromPayload(signalPayload), [signalPayload]);
  const activeVariant = useMemo(() => {
    if (!activeSignal) return visibleVariants[0] || null;
    return visibleVariants.find(item => item.id === activeSignal.strategy_id) || visibleVariants[0] || null;
  }, [activeSignal, visibleVariants]);
  const signalAmount = useMemo(() => {
    if (!activeSignal) return String(configDraft.amount || DEFAULT_CONFIG.amount);
    return amountForSignal(activeSignal.strategy_id, activeSignal, signalPayload, configDraft);
  }, [activeSignal, configDraft, signalPayload]);

  const priceChange = useMemo(() => {
    if (!currentPrice || !firstPrice) return null;
    const diff = Number(currentPrice) - Number(firstPrice);
    return { diff, pct: diff / Number(firstPrice) * 100 };
  }, [currentPrice, firstPrice]);

  const counts = useMemo(() => ({
    current: tradeHistory?.active?.length || "",
    strategies: signalRows.filter(row => row.signal?.signal).length || "",
    orders: tradeHistory?.pagination?.total || tradeHistory?.summary?.total || "",
    data: dataHealth?.blocked || secondDataHealth?.ok === false || orderbookHealth?.ok === false ? "异常" : "",
    settings: configDirty ? "未保存" : ""
  }), [configDirty, dataHealth, orderbookHealth, secondDataHealth, signalRows, tradeHistory]);

  const loadSignals = useCallback(() => loadJson("/api/signal?source=dashboard", setSignalPayload), [loadJson]);
  const loadLlmStatus = useCallback(() => loadJson("/api/llm-status", setLlmStatus), [loadJson]);
  const loadLlmConfig = useCallback(() => loadJson("/api/llm-config", setLlmConfig), [loadJson]);
  const loadRuntime = useCallback(() => loadJson("/api/runtime", setRuntime), [loadJson]);
  const loadTablet = useCallback(() => loadJson("/api/tablet-diagnostics", setTablet), [loadJson]);
  const loadBalance = useCallback(() => loadJson("/api/balance", setRealBalance), [loadJson]);
  const loadHealth = useCallback(() => {
    loadJson("/api/data-health", setDataHealth);
    loadJson("/api/second-data-health", setSecondDataHealth);
    loadJson("/api/orderbook-health", setOrderbookHealth);
    loadJson("/api/orderbook-prediction", setOrderbookPrediction);
  }, [loadJson]);

  const loadTradeHistory = useCallback((pageState = tradeHistoryPage) => {
    const params = new URLSearchParams({
      mode: pageState.mode || "day",
      day: pageState.day || shanghaiDayKey(),
      kind: pageState.kind || "real"
    });
    if (pageState.mode === "page") {
      params.set("page", String(pageState.page || 1));
      params.set("pageSize", String(pageState.pageSize || 40));
    }
    return loadJson(`/api/trade-history?${params}`, setTradeHistory);
  }, [loadJson, tradeHistoryPage]);

  const loadConfig = useCallback((force = false) => {
    if (dirtyRef.current && !force) return Promise.resolve();
    return apiFetch("/api/config")
      .then(res => res.json())
      .then(config => {
        setConfigDraft({
          ...DEFAULT_CONFIG,
          ...config,
          strategyVariants: config.strategyVariants || DEFAULT_CONFIG.strategyVariants
        });
        dirtyRef.current = false;
        setConfigDirty(false);
      })
      .catch(() => {});
  }, [apiFetch]);

  const loadPriceFallback = useCallback(() => {
    if (Date.now() - lastWsPriceRef.current >= 5000) {
      apiFetch("/api/price")
        .then(res => res.json())
        .then(data => {
          const price = Number(data?.price);
          if (!Number.isFinite(price)) return;
          setCurrentPrice(price);
          setFirstPrice(old => old || price);
          setPriceHistory(history => [...history.slice(-599), { time: Date.now(), price }]);
        })
        .catch(() => {});
    }
    apiFetch("/api/candles")
      .then(res => res.json())
      .then(data => {
        if (Array.isArray(data?.candles)) setCandles(data.candles);
      })
      .catch(() => {});
  }, [apiFetch]);

  const refreshAll = useCallback(() => {
    loadSignals();
    loadRuntime();
    loadTablet();
    loadTradeHistory();
    loadHealth();
    loadBalance();
    loadConfig(true);
    loadLlmConfig();
    loadLlmStatus();
    loadPriceFallback();
    notify("已刷新", "success");
  }, [loadBalance, loadConfig, loadHealth, loadLlmConfig, loadLlmStatus, loadPriceFallback, loadRuntime, loadSignals, loadTablet, loadTradeHistory, notify]);

  const markDraft = useCallback(patch => {
    dirtyRef.current = true;
    setConfigDirty(true);
    setConfigDraft(old => ({ ...old, ...patch }));
  }, []);

  const toggleDraft = useCallback(key => {
    dirtyRef.current = true;
    setConfigDirty(true);
    setConfigDraft(old => ({ ...old, [key]: !old[key] }));
  }, []);

  const handleTokenChange = useCallback(value => {
    setApiToken(value);
    window.localStorage.setItem("btcApiToken", value);
  }, []);

  const saveLlmConfig = useCallback(() => {
    apiFetch("/api/llm-config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(llmConfig)
    })
      .then(async res => {
        const body = await res.json();
        if (!res.ok || body.ok !== true) throw new Error(body.error || body.message || "llm_config_save_failed");
        return body;
      })
      .then(body => {
        setLlmConfig(body);
        loadLlmStatus();
        notify("LLM 配置已明文保存并重启策略服务", "success");
      })
      .catch(() => notify("LLM 配置保存失败", "error"));
  }, [apiFetch, llmConfig, loadLlmStatus, notify]);

  const predictLlmNow = useCallback(() => {
    apiFetch("/api/llm-predict-now", { method: "POST" })
      .then(res => res.json())
      .then(body => {
        if (!body.ok) throw new Error(body.reason || "predict_failed");
        notify("LLM 策略服务已重启并请求预测", "success");
        window.setTimeout(loadLlmStatus, 1500);
      })
      .catch(() => notify("LLM 未启用或预测触发失败", "error"));
  }, [apiFetch, loadLlmStatus, notify]);

  const saveConfig = useCallback(() => {
    const variants = Array.isArray(configDraft.strategyVariants)
      ? configDraft.strategyVariants
      : DEFAULT_CONFIG.strategyVariants;
    const payload = {
      ...configDraft,
      strategyVariants: variants,
      amount: String(configDraft.amount || DEFAULT_CONFIG.amount),
      duration: String(configDraft.duration || DEFAULT_CONFIG.duration),
      // 全局资金开关尊重用户选择；策略允许实盘不等于自动打开真实资金。
      realTradingEnabled: !!configDraft.realTradingEnabled,
      autoTrade_10m: !!configDraft.realTradingEnabled
    };
    apiFetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(res => res.json())
      .then(body => {
        if (body.error) {
          notify(body.error, "error");
          return;
        }
        setConfigDraft({
          ...DEFAULT_CONFIG,
          ...body,
          strategyVariants: body.strategyVariants || DEFAULT_CONFIG.strategyVariants
        });
        dirtyRef.current = false;
        setConfigDirty(false);
        notify("配置已保存并下发", "success");
      })
      .catch(() => notify("保存失败", "error"));
  }, [apiFetch, configDraft, notify]);

  const manualTrade = useCallback(direction => {
    apiFetch("/api/manual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        direction,
        amount: String(configDraft.amount || DEFAULT_CONFIG.amount),
        duration: String(configDraft.duration || DEFAULT_CONFIG.duration)
      })
    })
      .then(res => res.json())
      .then(body => {
        if (body.error) notify(`手动下单失败：${body.error}`, "error");
        else notify(`已发布手动${directionText(direction)}指令`, "success");
      })
      .catch(() => notify("手动下单请求失败", "error"));
  }, [apiFetch, configDraft, notify]);

  const triggerAction = useCallback((url, label, after) => {
    apiFetch(url, { method: "POST" })
      .then(res => res.json())
      .then(body => {
        if (body.error) notify(`${label}失败：${body.error}`, "error");
        else {
          notify(`${label}成功`, "success");
          after?.();
        }
      })
      .catch(() => notify(`${label}请求失败`, "error"));
  }, [apiFetch, notify]);

  const handleTradeHistoryPageChange = useCallback(patch => {
    const next = {
      ...tradeHistoryPage,
      ...patch,
      mode: patch.mode || tradeHistoryPage.mode || "day",
      day: patch.day || tradeHistoryPage.day || shanghaiDayKey(),
      page: patch.kind && patch.kind !== tradeHistoryPage.kind ? 1 : (patch.page || tradeHistoryPage.page || 1)
    };
    setTradeHistoryPage(next);
    loadTradeHistory(next);
  }, [loadTradeHistory, tradeHistoryPage]);

  useEffect(() => {
    loadSignals();
    loadRuntime();
    loadTablet();
    loadTradeHistory();
    loadHealth();
    loadBalance();
    loadConfig();
    loadLlmConfig();
    loadLlmStatus();
    loadPriceFallback();
  }, [loadBalance, loadConfig, loadHealth, loadLlmConfig, loadLlmStatus, loadPriceFallback, loadRuntime, loadSignals, loadTablet, loadTradeHistory]);

  useEffect(() => {
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    let closed = false;
    let ws;
    let retryTimer;

    function connect() {
      ws = new WebSocket(`${scheme}//${window.location.host}/ws`);
      ws.onmessage = event => {
        let message;
        try {
          message = JSON.parse(event.data);
        } catch {
          return;
        }
        if (message.type === "init" || message.type === "price") {
          const price = Number(message.price);
          if (Number.isFinite(price)) {
            setCurrentPrice(price);
            setFirstPrice(old => old || price);
            lastWsPriceRef.current = Date.now();
          }
          if (Array.isArray(message.history)) setPriceHistory(message.history);
          if (Array.isArray(message.candles)) setCandles(message.candles);
          if (message.realBalance?.amount !== undefined) setRealBalance(message.realBalance);
        }
        if (message.type === "state" && message.realBalance?.amount !== undefined) setRealBalance(message.realBalance);
        if (message.type === "balance" && message.amount !== undefined) setRealBalance(message);
        if (message.type === "trade_update") {
          notify(`订单更新：${message.trade?.status || ""}`, "info");
          loadTradeHistory();
        }
        if (message.type === "error") notify(message.message || "服务端消息异常", "error");
      };
      ws.onclose = () => {
        if (!closed) retryTimer = window.setTimeout(connect, 2000);
      };
      ws.onerror = () => ws.close();
    }

    connect();
    return () => {
      closed = true;
      window.clearTimeout(retryTimer);
      if (ws) ws.close();
    };
  }, [loadTradeHistory, notify]);

  useInterval(loadSignals, 3000);
  useInterval(loadPriceFallback, 3000);
  useInterval(loadTradeHistory, activeTab === "current" || activeTab === "orders" ? 10000 : null);
  useInterval(loadHealth, 10000);
  useInterval(loadTablet, 15000);
  useInterval(loadRuntime, 30000);
  useInterval(loadBalance, 30000);
  useInterval(loadConfig, 10000);
  useInterval(loadLlmStatus, 10000);

  const routePath = window.location.pathname.replace(/\/+$/, "");
  if (routePath === "/dashboard/normal-visual") {
    return (
      <Suspense fallback={<main className="app-shell loading-shell">正在加载正态分布图...</main>}>
        <NormalVisual />
      </Suspense>
    );
  }

  if (!isAuthenticated) {
    return <LoginGate onLoginSuccess={() => setIsAuthenticated(true)} />;
  }

  return (
    <>
      <div className="app-shell">
        <TopBar
          currentPrice={currentPrice}
          priceChange={priceChange}
          dataHealth={dataHealth}
          secondDataHealth={secondDataHealth}
          orderbookHealth={orderbookHealth}
          realBalance={realBalance}
          onRefresh={refreshAll}
        />

        <section className="signal-strip">
          <div>
            <span className={`signal-dot ${directionClass(activeSignal?.signal)}`} />
            <strong>{signalLabel(activeSignal)}</strong>
            <small>{signalHumanSummary(activeSignal, activeVariant)}</small>
          </div>
          <div className="signal-strip-meta">
            <span><Clock3 size={14} /> {activeSignal?.next_check_time_shanghai || activeSignal?.time_shanghai || "--"}</span>
            <span><AlertTriangle size={14} /> {signalReasonText(activeSignal)}</span>
          </div>
        </section>

        <DashboardTabs active={activeTab} counts={counts} onChange={setActiveTab} />

        {activeTab === "current" ? (
          <main className="current-layout">
            <section className="market-column">
              <div className="hero-metrics">
                <MetricCard label="当前价格" value={fmtPrice(currentPrice)} sub="BTC/USDT" tone={priceChange?.diff > 0 ? "up" : priceChange?.diff < 0 ? "down" : "neutral"} icon={BarChart3} />
                <MetricCard label="当前信号" value={directionText(activeSignal?.signal)} sub={signalLabel(activeSignal)} tone={directionClass(activeSignal?.signal)} icon={Activity} />
                <MetricCard label="实盘开关" value={configDraft.realTradingEnabled ? "实盘" : "影子/观察"} sub={configDraft.realTradingEnabled ? "允许实盘策略自动下单" : "真实资金下单关闭"} tone={configDraft.realTradingEnabled ? "bad" : "warn"} icon={ShieldCheck} />
                <MetricCard
                  label="运行策略"
                  value={`${visibleVariants.length}`}
                  sub={visibleVariants.some(v => v.tradeEnabled !== false) ? `${visibleVariants.filter(v => v.tradeEnabled !== false).length} 条允许实盘` : "全部仅观察/影子"}
                  tone={visibleVariants.some(v => v.tradeEnabled !== false) ? "warn" : "ok"}
                  icon={ListChecks}
                />
              </div>
              <Suspense fallback={<div className="chart-shell"><div className="chart-empty">正在加载图表...</div></div>}>
                <MarketChart candles={candles} trades={tradeHistory?.recent || []} />
              </Suspense>
            </section>
            <CurrentTradePanel
              history={tradeHistory}
              activeSignal={activeSignal}
              activeVariant={activeVariant}
              signalAmount={signalAmount}
              currentPrice={currentPrice}
            />
          </main>
        ) : null}

        {activeTab === "strategies" ? (
          <main className="dashboard-view">
            <StrategySnapshot
              signalRows={signalRows}
              tradeHistory={tradeHistory}
              configDraft={configDraft}
              signalPayload={signalPayload}
              disabledCount={disabledVariantCount}
            />
          </main>
        ) : null}

        {activeTab === "orders" ? (
          <main className="dashboard-view">
            <TradeHistory history={tradeHistory} pageState={tradeHistoryPage} onPageChange={handleTradeHistoryPageChange} />
          </main>
        ) : null}

        {activeTab === "data" ? (
          <DataHealthPanel
            dataHealth={dataHealth}
            secondDataHealth={secondDataHealth}
            orderbookHealth={orderbookHealth}
            orderbookPrediction={orderbookPrediction}
            runtime={runtime}
            tablet={tablet}
            onRefreshData={() => triggerAction("/api/data-update/refresh", "数据拉取", () => {
              loadRuntime();
              loadHealth();
              loadSignals();
            })}
            onRefreshReports={() => triggerAction("/api/reports/refresh", "报告刷新")}
          />
        ) : null}

        {activeTab === "settings" ? (
          <main className="settings-layout">
            <ConfigPanel
              draft={configDraft}
              dirty={configDirty}
              apiToken={apiToken}
              llmConfig={llmConfig}
              llmStatus={llmStatus}
              onLlmChange={patch => setLlmConfig(current => ({ ...current, ...patch }))}
              onLlmSave={saveLlmConfig}
              onLlmPredictNow={predictLlmNow}
              onTokenChange={handleTokenChange}
              onDraftChange={markDraft}
              onToggle={toggleDraft}
              onSave={saveConfig}
            />
            <ManualPanel
              draft={configDraft}
              onManualTrade={manualTrade}
              onAmountPreset={value => markDraft({ amount: String(value) })}
            />
          </main>
        ) : null}
      </div>
      <Toasts items={toasts} />
    </>
  );
}
