import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createChart, CandlestickSeries, createSeriesMarkers } from "lightweight-charts";
import {
  BadgeCheck,
  Server,
  Database,
  Clock,
  Wifi,
  RefreshCcw
} from "lucide-react";

import {
  DEFAULT_CONFIG,
  fmt,
  fmtPrice,
  fmtPct,
  ageText,
  amountForSignal,
  activeSignalFromPayload,
  strategyName,
  useInterval
} from "./utils";

import StrategyCard from "./components/StrategyCard";
import SignalBanner from "./components/SignalBanner";
import ConfigPanel from "./components/ConfigPanel";
import ManualPanel from "./components/ManualPanel";
import OpsPanel from "./components/OpsPanel";
import LoginGate from "./components/LoginGate";
import TradeHistory from "./components/TradeHistory";

const chartTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  hour12: false,
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit"
});

function formatChartTime(time) {
  const seconds = typeof time === "number" ? time : Number(time?.timestamp || time);
  if (!Number.isFinite(seconds)) return "";
  return chartTimeFormatter.format(new Date(seconds * 1000));
}

function Metric({ label, value, unit, tone }) {
  return (
    <div className="metric">
      <span className="metric-label">{label}</span>
      <span className={`metric-value ${tone || ""}`}>
        {value}
        {unit ? <small>{unit}</small> : null}
      </span>
    </div>
  );
}

function Toasts({ items }) {
  return (
    <div className="toast-stack" aria-live="polite">
      {items.map(item => (
        <div className={`toast ${item.type || "info"}`} key={item.id}>
          {item.message}
        </div>
      ))}
    </div>
  );
}

export default function App() {
  const [isAuthenticated, setIsAuthenticated] = useState(() => !!window.localStorage.getItem("btc_auth_token"));
  const [currentPrice, setCurrentPrice] = useState(null);
  const [firstPrice, setFirstPrice] = useState(null);
  const [priceHistory, setPriceHistory] = useState([]);
  const [candles, setCandles] = useState([]);
  const [signalPayload, setSignalPayload] = useState(null);
  const [runtime, setRuntime] = useState(null);
  const [tablet, setTablet] = useState(null);
  const [tradeHistory, setTradeHistory] = useState(null);
  const [realBalance, setRealBalance] = useState(null);
  const [configDraft, setConfigDraft] = useState(DEFAULT_CONFIG);
  const [configDirty, setConfigDirty] = useState(false);
  const [apiToken, setApiToken] = useState(() => window.localStorage.getItem("btcApiToken") || "");
  const [toasts, setToasts] = useState([]);
  const lastWsPriceRef = useRef(0);
  const dirtyRef = useRef(false);
  const chartContainerRef = useRef(null);
  const chartInstanceRef = useRef(null);
  const areaSeriesRef = useRef(null); // Used as candlestick series ref now

  const activeSignal = useMemo(() => activeSignalFromPayload(signalPayload), [signalPayload]);
  const signalAmount = useMemo(() => {
    if (!activeSignal) return String(configDraft.amount || DEFAULT_CONFIG.amount);
    return amountForSignal(activeSignal.strategy_id, activeSignal, signalPayload, configDraft);
  }, [activeSignal, configDraft, signalPayload]);

  const safeAmount = useMemo(() => amountForSignal("BTC_10min_SAFE", signalPayload?.BTC_10min_SAFE, signalPayload, configDraft), [configDraft, signalPayload]);
  const takerAmount = useMemo(() => amountForSignal("BTC_10min_TAKER", signalPayload?.BTC_10min_TAKER, signalPayload, configDraft), [configDraft, signalPayload]);
  const safeSignal = signalPayload?.BTC_10min_SAFE || null;
  const takerSignal = signalPayload?.BTC_10min_TAKER || null;

  const priceChange = useMemo(() => {
    if (!currentPrice || !firstPrice) return null;
    const diff = Number(currentPrice) - Number(firstPrice);
    const pct = (diff / Number(firstPrice)) * 100;
    return { diff, pct };
  }, [currentPrice, firstPrice]);

  const notify = useCallback((message, type = "info") => {
    const id = `${Date.now()}-${Math.random()}`;
    setToasts(items => [...items, { id, message, type }].slice(-4));
    setTimeout(() => {
      setToasts(items => items.filter(item => item.id !== id));
    }, 2800);
  }, []);

  const apiFetch = useCallback((url, options = {}) => {
    const headers = { ...(options.headers || {}) };
    const authToken = window.localStorage.getItem("btc_auth_token");
    if (authToken) {
      headers["X-API-Token"] = authToken;
    } else if (apiToken) {
      headers["X-API-Token"] = apiToken;
    }
    return fetch(url, { ...options, headers });
  }, [apiToken]);

  const loadSignals = useCallback(() => {
    apiFetch("/api/signal?source=dashboard")
      .then(res => res.json())
      .then(setSignalPayload)
      .catch(() => {});
  }, [apiFetch]);

  const loadRuntime = useCallback(() => {
    apiFetch("/api/runtime").then(res => res.json()).then(setRuntime).catch(() => {});
  }, [apiFetch]);

  const loadTablet = useCallback(() => {
    apiFetch("/api/tablet-diagnostics").then(res => res.json()).then(setTablet).catch(() => {});
  }, [apiFetch]);

  const loadTradeHistory = useCallback(() => {
    apiFetch("/api/trade-history?limit=120").then(res => res.json()).then(setTradeHistory).catch(() => {});
  }, [apiFetch]);

  const loadConfig = useCallback((force = false) => {
    if (dirtyRef.current && !force) return;
    apiFetch("/api/config")
      .then(res => res.json())
      .then(config => {
        setConfigDraft({
          ...DEFAULT_CONFIG,
          ...config,
          strategyAmounts: { ...DEFAULT_CONFIG.strategyAmounts, ...(config.strategyAmounts || {}) }
        });
        dirtyRef.current = false;
        setConfigDirty(false);
      })
      .catch(() => {});
  }, [apiFetch]);

  const loadPriceFallback = useCallback(() => {
    if (Date.now() - lastWsPriceRef.current < 5000) return;
    apiFetch("/api/price")
      .then(res => res.json())
      .then(data => {
        if (!data?.price) return;
        const price = Number(data.price);
        setCurrentPrice(price);
        setFirstPrice(old => old || price);
        setPriceHistory(history => [...history.slice(-599), { time: Date.now(), price }]);
      })
      .catch(() => {});
  }, [apiFetch]);

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

  const saveConfig = useCallback(() => {
    const { autoTrade, realTradingOverride, ...cleanDraft } = configDraft;
    const payload = {
      ...cleanDraft,
      amount: String(configDraft.amount || DEFAULT_CONFIG.amount),
      duration: String(configDraft.duration || DEFAULT_CONFIG.duration),
      minConfidence: Number(configDraft.minConfidence)
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
        } else {
          notify("配置已保存", "success");
        }
        setConfigDraft({
          ...DEFAULT_CONFIG,
          ...body,
          strategyAmounts: { ...DEFAULT_CONFIG.strategyAmounts, ...(body.strategyAmounts || {}) }
        });
        dirtyRef.current = false;
        setConfigDirty(false);
      })
      .catch(() => notify("保存失败", "error"));
  }, [apiFetch, configDraft, notify]);

  const manualTrade = useCallback(direction => {
    const payload = {
      direction,
      amount: String(configDraft.amount || DEFAULT_CONFIG.amount),
      duration: String(configDraft.duration || DEFAULT_CONFIG.duration)
    };
    apiFetch("/api/manual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(res => res.json())
      .then(cmd => {
        if (cmd.error) notify(`下单指令失败: ${cmd.error}`, "error");
        else notify(`已发布手动看${direction === "UP" ? "涨" : "跌"}指令`, "success");
      })
      .catch(() => notify("发布失败", "error"));
  }, [apiFetch, configDraft, notify]);

  const triggerServerAction = useCallback((url, label, onSuccess) => {
    apiFetch(url, { method: "POST" })
      .then(res => res.json())
      .then(body => {
        if (body.error) notify(`${label}失败: ${body.error}`, "error");
        else {
          notify(`${label}成功`, "success");
          if (onSuccess) onSuccess();
        }
      })
      .catch(() => notify(`${label}请求错误`, "error"));
  }, [apiFetch, notify]);

  const refreshDataNow = useCallback(() => {
    triggerServerAction("/api/data-update/refresh", "数据刷新", () => {
      loadRuntime();
      loadSignals();
    });
  }, [loadRuntime, loadSignals, triggerServerAction]);

  const refreshReportsNow = useCallback(() => {
    triggerServerAction("/api/reports/refresh", "报告刷新");
  }, [triggerServerAction]);

  const refreshAll = useCallback(() => {
    loadSignals();
    loadRuntime();
    loadTablet();
    loadTradeHistory();
    loadConfig(true);
    loadPriceFallback();
    notify("已刷新", "success");
  }, [loadConfig, loadPriceFallback, loadRuntime, loadSignals, loadTablet, loadTradeHistory, notify]);

  useEffect(() => {
    loadSignals();
    loadRuntime();
    loadTablet();
    loadTradeHistory();
    loadConfig();
    loadPriceFallback();
  }, [loadConfig, loadPriceFallback, loadRuntime, loadSignals, loadTablet, loadTradeHistory]);

  useEffect(() => {
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    let closed = false;
    let ws;
    let retryTimer;

    function connect() {
      ws = new WebSocket(`${scheme}//${window.location.host}/ws`);
      ws.onmessage = event => {
        const message = JSON.parse(event.data);
        if (message.type === "init") {
          if (message.price) {
            const price = Number(message.price);
            setCurrentPrice(price);
            setFirstPrice(old => old || price);
            lastWsPriceRef.current = Date.now();
          }
          if (Array.isArray(message.history)) setPriceHistory(message.history);
          if (Array.isArray(message.candles)) setCandles(message.candles);
          if (message.realBalance?.amount !== undefined) setRealBalance(message.realBalance);
        }
        if (message.type === "price") {
          const price = Number(message.price);
          if (Number.isFinite(price)) {
            setCurrentPrice(price);
            setFirstPrice(old => old || price);
            lastWsPriceRef.current = Date.now();
          }
          if (Array.isArray(message.history)) setPriceHistory(message.history);
          if (message.candle) {
            setCandles(old => {
              const copy = [...old];
              if (copy.length === 0) {
                copy.push(message.candle);
              } else {
                const idx = copy.findIndex(c => c.time === message.candle.time);
                if (idx >= 0) {
                  copy[idx] = message.candle;
                } else {
                  copy.push(message.candle);
                }
              }
              return copy.slice(-500);
            });
          }
        }
        if (message.type === "state" && message.realBalance?.amount !== undefined) setRealBalance(message.realBalance);
        if (message.type === "balance" && message.amount !== undefined) setRealBalance(message);
        if (message.type === "trade_update") {
          notify(`订单 #${message.trade?.id || ""} ${message.trade?.status || ""}`, "info");
          loadTradeHistory();
        }
        if (message.type === "error") notify(message.message || "服务端消息错误", "error");
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

  useEffect(() => {
    if (!chartContainerRef.current) return;

    // Initialize TradingView Lightweight Chart
    const chart = createChart(chartContainerRef.current, {
      layout: {
        background: { color: "transparent" },
        textColor: "#7d8792",
      },
      grid: {
        vertLines: { color: "rgba(255, 255, 255, 0.04)" },
        horzLines: { color: "rgba(255, 255, 255, 0.04)" },
      },
      rightPriceScale: {
        borderColor: "rgba(255, 255, 255, 0.1)",
      },
      timeScale: {
        borderColor: "rgba(255, 255, 255, 0.1)",
        timeVisible: true,
        secondsVisible: true,
        tickMarkFormatter: formatChartTime,
      },
      localization: {
        locale: "zh-CN",
        timeFormatter: formatChartTime,
      },
      crosshair: {
        horzLine: {
          labelBackgroundColor: "#1a1e24",
        },
        vertLine: {
          labelBackgroundColor: "#1a1e24",
        },
      },
    });

    const areaSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#27c3a5",
      downColor: "#e45858",
      borderVisible: false,
      wickUpColor: "#27c3a5",
      wickDownColor: "#e45858",
    });

    chartInstanceRef.current = chart;
    areaSeriesRef.current = areaSeries;

    const handleResize = () => {
      if (chartContainerRef.current) {
        chart.resize(
          chartContainerRef.current.clientWidth,
          chartContainerRef.current.clientHeight
        );
      }
    };
    window.addEventListener("resize", handleResize);
    handleResize();

    return () => {
      window.removeEventListener("resize", handleResize);
      chart.removeSeries(areaSeries);
      chart.remove();
      chartInstanceRef.current = null;
      areaSeriesRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!areaSeriesRef.current) return;

    const seenSeconds = new Set();
    const chartData = [];

    // 1. Process standard 1-minute OHLC K-line candles
    for (const item of candles || []) {
      const candleTime = Number(item.time);
      if (Number.isNaN(candleTime)) continue;
      if (seenSeconds.has(candleTime)) continue;
      seenSeconds.add(candleTime);
      chartData.push({
        time: candleTime,
        open: Number(item.open),
        high: Number(item.high),
        low: Number(item.low),
        close: Number(item.close)
      });
    }

    // 2. Add extra trade points to guarantee trade execution times exist as valid series points
    const tradesToMarker = tradeHistory?.recent || [];
    const extraPoints = [];
    for (const trade of tradesToMarker) {
      if (!trade.openTime) continue;
      const openSec = Math.floor(Number(trade.openTime) / 1000);
      const openVal = Number(trade.openPrice);
      
      if (!Number.isNaN(openSec) && Number.isFinite(openVal) && !seenSeconds.has(openSec)) {
        seenSeconds.add(openSec);
        extraPoints.push({
          time: openSec,
          open: openVal,
          high: openVal,
          low: openVal,
          close: openVal
        });
      }
      if (trade.settleTime) {
        const settleSec = Math.floor(Number(trade.settleTime) / 1000);
        const closeVal = Number(trade.closePrice);
        
        if (!Number.isNaN(settleSec) && Number.isFinite(closeVal) && !seenSeconds.has(settleSec)) {
          seenSeconds.add(settleSec);
          extraPoints.push({
            time: settleSec,
            open: closeVal,
            high: closeVal,
            low: closeVal,
            close: closeVal
          });
        }
      }
    }

    const mergedData = [...chartData, ...extraPoints].sort((a, b) => a.time - b.time);
    
    if (mergedData.length > 0) {
      areaSeriesRef.current.setData(mergedData);
    }

    // 3. Build markers list with strict NaN prevention
    const markers = [];
    for (const trade of tradesToMarker) {
      if (!trade.openTime) continue;
      const openSec = Math.floor(Number(trade.openTime) / 1000);
      if (Number.isNaN(openSec)) continue;
      
      // Open marker (CALL or PUT) with execution source prefixes
      const isTablet = trade.source === "autojs";
      const isManual = !trade.strategyId || trade.strategyId === "manual";
      const prefix = isManual
        ? (isTablet ? "[平板手动]" : "[网页手动]")
        : (isTablet ? "[信号实盘]" : "[影子模拟]");
      const stratName = strategyName(trade.strategyId);

      markers.push({
        time: openSec,
        position: trade.direction === "UP" ? "belowBar" : "aboveBar",
        color: trade.direction === "UP" ? "#27c3a5" : "#e45858",
        shape: trade.direction === "UP" ? "arrowUp" : "arrowDown",
        text: `${prefix} ${trade.direction === "UP" ? "买涨" : "买跌"}(${stratName}) ${trade.amount}U`
      });

      // Close marker (WON, LOST, TIE)
      if (trade.status && trade.status !== "pending" && trade.status !== "aborted" && trade.settleTime) {
        const settleSec = Math.floor(Number(trade.settleTime) / 1000);
        if (Number.isNaN(settleSec)) continue;
        markers.push({
          time: settleSec,
          position: "inBar",
          color: trade.status === "won" ? "#27c3a5" : trade.status === "lost" ? "#e45858" : "#f0c94a",
          shape: "circle",
          text: `${trade.status === "won" ? "胜" : trade.status === "lost" ? "负" : "平"} (${trade.pnl > 0 ? "+" : ""}${trade.pnl}U)`
        });
      }
    }

    // Sort markers by time
    markers.sort((a, b) => a.time - b.time);
    createSeriesMarkers(areaSeriesRef.current, markers);

  }, [candles, tradeHistory]);

  useInterval(loadSignals, 3000);
  useInterval(loadPriceFallback, 3000);
  useInterval(loadTradeHistory, 5000);
  useInterval(loadTablet, 15000);
  useInterval(loadRuntime, 30000);
  useInterval(loadConfig, 10000);

  const safeConfidenceTone = Number(safeSignal?.confidence || 0) >= 60 ? "ok" : "";
  const takerConfidenceTone = Number(takerSignal?.confidence || 0) >= 60 ? "ok" : "";
  const priceTone = priceChange?.diff > 0 ? "ok" : priceChange?.diff < 0 ? "fail" : "";
  const username = window.localStorage.getItem("btc_username") || "sl";

  if (!isAuthenticated) {
    return <LoginGate onLoginSuccess={() => setIsAuthenticated(true)} />;
  }

  return (
    <>
      <style>{`
        @keyframes pulse-green {
          0% { box-shadow: 0 0 0 0 rgba(39, 195, 165, 0.7); }
          70% { box-shadow: 0 0 0 6px rgba(39, 195, 165, 0); }
          100% { box-shadow: 0 0 0 0 rgba(39, 195, 165, 0); }
        }
        @keyframes pulse-red {
          0% { box-shadow: 0 0 0 0 rgba(228, 88, 88, 0.7); }
          70% { box-shadow: 0 0 0 6px rgba(228, 88, 88, 0); }
          100% { box-shadow: 0 0 0 0 rgba(228, 88, 88, 0); }
        }
        .pulse-dot.green {
          display: inline-block;
          width: 8px;
          height: 8px;
          border-radius: 50%;
          background-color: var(--green);
          color: transparent;
          overflow: hidden;
          animation: pulse-green 1.5s infinite;
        }
        .pulse-dot.red {
          display: inline-block;
          width: 8px;
          height: 8px;
          border-radius: 50%;
          background-color: var(--red);
          color: transparent;
          overflow: hidden;
          animation: pulse-red 1.5s infinite;
        }
        .strategy-card {
          transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
        }
        .strategy-card:hover {
          transform: translateY(-2px);
          filter: brightness(1.15);
        }
        .preset-btn {
          cursor: pointer;
        }
        .preset-btn:hover {
          filter: brightness(1.25);
        }
        /* Custom Glow Slide Toggle */
        .slide-switch {
          position: relative;
          width: 40px;
          height: 20px;
          background-color: rgba(255, 255, 255, 0.08);
          border: 1px solid var(--line);
          border-radius: 10px;
          cursor: pointer;
          transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
          padding: 0;
          display: inline-block;
          outline: none;
        }
        .slide-switch::after {
          content: "";
          position: absolute;
          top: 1px;
          left: 1px;
          width: 16px;
          height: 16px;
          background-color: var(--text-2);
          border-radius: 50%;
          transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
          box-shadow: 0 1px 3px rgba(0, 0, 0, 0.4);
        }
        .slide-switch.on {
          background-color: var(--green-soft);
          border-color: var(--green);
          box-shadow: 0 0 8px rgba(39, 195, 165, 0.2);
        }
        .slide-switch.on::after {
          left: 21px;
          background-color: var(--green);
          box-shadow: 0 0 5px var(--green);
        }
        .slide-switch.off {
          background-color: rgba(255, 255, 255, 0.02);
          border-color: var(--line);
        }
        .slide-switch.off::after {
          left: 1px;
          background-color: var(--muted);
        }
      `}</style>
      <div className="app-shell">
        <header className="topbar">
          <div className="brand">
            <BadgeCheck size={20} />
            <div>
              <strong style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                BTC 实盘控制台
                <span style={{
                  fontSize: "9px",
                  padding: "1px 6px",
                  borderRadius: "3px",
                  fontWeight: "bold",
                  color: "var(--green)",
                  background: "var(--green-soft)",
                  border: "1px solid rgba(39, 195, 165, 0.25)"
                }}>
                  👤 {username}
                </span>
              </strong>
              <span>{runtime?.managedProcessesEnabled === false ? "本地测试 API" : "服务器实时"}</span>
            </div>
          </div>
          <div className="top-actions">
            <Metric label="BTC 价格" value={fmtPrice(currentPrice)} unit="USDT" tone={priceTone} />
            <Metric label="稳健强度" value={safeSignal?.confidence !== undefined ? fmtPct(safeSignal.confidence, 0) : "--"} tone={safeConfidenceTone} />
            <Metric label="资金流强度" value={takerSignal?.confidence !== undefined ? fmtPct(takerSignal.confidence, 0) : "--"} tone={takerConfidenceTone} />
            <Metric label="账户余额" value={realBalance?.amount !== undefined ? fmt(realBalance.amount, 2) : "--"} unit="USDT" tone={realBalance?.amount !== undefined ? "ok" : ""} />
            <Metric label="稳健投数" value={safeAmount} unit="USDT" />
            <Metric label="资金流投数" value={takerAmount} unit="USDT" />
            <button className="icon-button" type="button" onClick={refreshAll} title="刷新">
              <RefreshCcw size={16} />
            </button>
          </div>
        </header>

        <SignalBanner signalPayload={signalPayload} activeSignal={activeSignal} signalAmount={signalAmount} />

        <section className="strategy-strip">
          <StrategyCard title="推荐稳健" signal={signalPayload?.BTC_10min_SAFE} amount={safeAmount} />
          <StrategyCard title="资金流过滤" signal={signalPayload?.BTC_10min_TAKER} amount={takerAmount} />
        </section>

        <main className="main-grid">
          <section className="workspace">
            <section className="market-panel">
              <header className="market-header">
                <div>
                  <span>BTC / USDT</span>
                  <h1>{fmtPrice(currentPrice)}</h1>
                </div>
                <div className={`price-change ${priceTone}`}>
                  {priceChange ? `${priceChange.diff >= 0 ? "+" : ""}${fmtPrice(priceChange.diff)} (${priceChange.pct >= 0 ? "+" : ""}${priceChange.pct.toFixed(2)}%)` : "--"}
                </div>
              </header>
              <div className="chart-frame" style={{ position: "relative", width: "100%", height: "420px" }}>
                <div ref={chartContainerRef} style={{ width: "100%", height: "100%" }} />
              </div>
            </section>
            <TradeHistory history={tradeHistory} />
          </section>

          <aside className="side-rail">
            <ConfigPanel
              draft={configDraft}
              dirty={configDirty}
              apiToken={apiToken}
              onTokenChange={handleTokenChange}
              onDraftChange={markDraft}
              onToggle={toggleDraft}
              onSave={saveConfig}
            />
            <ManualPanel draft={configDraft} onManualTrade={manualTrade} onAmountPreset={val => markDraft({ amount: String(val) })} />
            <OpsPanel
              runtime={runtime}
              tablet={tablet}
              onRefreshData={refreshDataNow}
              onRefreshReports={refreshReportsNow}
            />
            <section className="panel runtime-panel">
              <header className="panel-header">
                <span><Wifi size={15} /> 运行状态</span>
              </header>
              <div className="runtime-list">
                <span><Server size={14} /> {runtime?.serverId || "--"}</span>
                <span><Database size={14} /> {runtime?.dataDir || "--"}</span>
                <span><Clock size={14} /> 平板 {tablet?.checks?.heartbeatOnline ? `在线 ${ageText(tablet.latestHeartbeatAgeMs)}` : "等待"}</span>
              </div>
            </section>
          </aside>
        </main>
      </div>
      <Toasts items={toasts} />
    </>
  );
}
