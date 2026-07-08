import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Activity, ArrowDown, ArrowUp, Clock, Database, Gauge, RefreshCcw } from "lucide-react";
import { createChart, createSeriesMarkers, HistogramSeries, LineSeries } from "lightweight-charts";

const shanghaiTime = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  hour12: false,
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit"
});

const shanghaiClock = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  hour12: false,
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit"
});

const HORIZON_SEC = 600;
const Z70 = 1.04;
const Z95 = 1.96;

function normalCdf(x) {
  const sign = x < 0 ? -1 : 1;
  const value = Math.abs(x) / Math.sqrt(2);
  const t = 1 / (1 + 0.3275911 * value);
  const a1 = 0.254829592;
  const a2 = -0.284496736;
  const a3 = 1.421413741;
  const a4 = -1.453152027;
  const a5 = 1.061405429;
  const erf = 1 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * Math.exp(-value * value);
  return 0.5 * (1 + sign * erf);
}

function fmt(value, digits = 2) {
  return Number.isFinite(value) ? value.toFixed(digits) : "--";
}

function fmtTime(iso) {
  if (!iso) return "--";
  return shanghaiTime.format(new Date(iso));
}

function fmtClock(iso) {
  if (!iso) return "--";
  return shanghaiClock.format(new Date(iso));
}

function chartTime(time) {
  const seconds = typeof time === "number" ? time : Number(time?.timestamp || time);
  if (!Number.isFinite(seconds)) return "";
  return fmtTime(new Date(seconds * 1000).toISOString());
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function statsFromPrefix(prefix, prefixSq, start, end) {
  const count = Math.max(end - start + 1, 1);
  const sum = prefix[end + 1] - prefix[start];
  const sumSq = prefixSq[end + 1] - prefixSq[start];
  const avg = sum / count;
  const variance = count > 1 ? Math.max((sumSq - (sum * sum) / count) / (count - 1), 0) : 0;
  return { avg, sigma: Math.sqrt(variance), count };
}

function toSeconds(row) {
  return Math.floor(new Date(row.t).getTime() / 1000);
}

function directionFromZ(z) {
  if (z <= -Z70) return "UP";
  if (z >= Z70) return "DOWN";
  return "WAIT";
}

function directionLabel(direction) {
  if (direction === "UP") return "买涨 UP";
  if (direction === "DOWN") return "买跌 DOWN";
  return "不下单";
}

function actualLabel(actual) {
  if (actual === "UP") return "到期涨";
  if (actual === "DOWN") return "到期跌";
  return "到期平";
}

function NormalVisualInner({ realData }) {
  const rows = realData.rows || [];
  const maxIndex = Math.max(rows.length - HORIZON_SEC - 1, 0);
  const [windowMin, setWindowMin] = useState(180);
  const [index, setIndex] = useState(maxIndex);
  const [signalsOnly, setSignalsOnly] = useState(true);
  const [hoverInfo, setHoverInfo] = useState(null);
  const [manualMarks, setManualMarks] = useState([]);
  const [overlayTick, setOverlayTick] = useState(0);
  const chartRef = useRef(null);
  const lastDoubleClickRef = useRef(0);
  const clickTimerRef = useRef(null);
  const containerRef = useRef(null);
  const seriesRef = useRef({});
  const markersRef = useRef(null);
  const timeToIndexRef = useRef(new Map());
  const lastChartWindowKeyRef = useRef("");
  const userChangedChartRangeRef = useRef(false);
  const programmaticRangeChangeRef = useRef(false);

  const prefix = useMemo(() => {
    const sum = [0];
    const sumSq = [0];
    rows.forEach(row => {
      const price = Number(row.p) || 0;
      sum.push(sum[sum.length - 1] + price);
      sumSq.push(sumSq[sumSq.length - 1] + price * price);
    });
    return { sum, sumSq };
  }, [rows]);

  const model = useMemo(() => {
    const entryIndex = clamp(index, 0, maxIndex);
    const windowSec = windowMin * 60;
    const windowStart = clamp(entryIndex - windowSec + 1, 0, entryIndex);
    const settleIndex = Math.min(entryIndex + HORIZON_SEC, rows.length - 1);
    const chartStart = windowStart;
    const chartEnd = settleIndex;
    const windowRows = rows.slice(windowStart, entryIndex + 1);
    const chartRows = rows.slice(chartStart, chartEnd + 1);
    const entryRow = rows[entryIndex] || {};
    const settleRow = rows[settleIndex] || {};
    const entry = Number(entryRow.p);
    const settle = Number(settleRow.p);
    const { avg, sigma } = statsFromPrefix(prefix.sum, prefix.sumSq, windowStart, entryIndex);
    const z = sigma > 0 ? (entry - avg) / sigma : 0;
    const zone70 = Z70 * sigma;
    const zone95 = Z95 * sigma;
    const prices = windowRows.map(row => Number(row.p)).filter(Number.isFinite);
    const low = Math.min(...prices);
    const high = Math.max(...prices);
    const rangeBps = low > 0 ? (high / low - 1) * 10000 : 0;
    const logReturns = [];
    for (let i = 1; i < prices.length; i += 1) {
      if (prices[i - 1] > 0 && prices[i] > 0) logReturns.push(Math.log(prices[i] / prices[i - 1]));
    }
    const mu = logReturns.reduce((sum, value) => sum + value, 0) / Math.max(logReturns.length, 1);
    const variance = logReturns.reduce((sum, value) => sum + (value - mu) ** 2, 0) / Math.max(logReturns.length - 1, 1);
    const sigmaSec = Math.sqrt(Math.max(variance, 0));
    const directionZ = sigmaSec > 0 ? (HORIZON_SEC * mu) / (Math.sqrt(HORIZON_SEC) * sigmaSec) : 0;
    const pUp = normalCdf(directionZ);
    const signal = directionFromZ(z);
    const actual = settle > entry ? "UP" : settle < entry ? "DOWN" : "FLAT";
    const won = signal !== "WAIT" && signal === actual;
    const settleMove = entry > 0 ? (settle / entry - 1) * 10000 : 0;
    const state = Math.abs(z) <= Z70 ? "震荡区" : Math.abs(z) <= Z95 ? "回归观察区" : "突破/新区间";
    const observedCount = windowRows.filter(row => row.o).length;
    return {
      entry,
      settle,
      entryRow,
      entryIndex,
      settleIndex,
      entryTime: entryRow.t,
      settleTime: settleRow.t,
      avg,
      sigma,
      z,
      zone70,
      zone95,
      low,
      high,
      rangeBps,
      pUp,
      signal,
      actual,
      won,
      settleMove,
      state,
      chartRows,
      chartStart,
      chartEnd,
      observedPct: windowRows.length ? (observedCount / windowRows.length) * 100 : 0
    };
  }, [index, maxIndex, prefix, rows, windowMin]);

  const orderbookAxis = useMemo(() => {
    const entry = Number(model.entry);
    if (!Number.isFinite(entry) || entry <= 0) return [];
    const spreadBps = Math.max(Number(model.entryRow?.sp) || 0, 0.01);
    const bidQty = Number(model.entryRow?.b5) || 0;
    const askQty = Number(model.entryRow?.a5) || 0;
    const buyWallQty = Number(model.entryRow?.bwq) || 0;
    const sellWallQty = Number(model.entryRow?.awq) || 0;
    const buyWallBps = Math.max(Number(model.entryRow?.bwb) || 0, spreadBps / 2);
    const sellWallBps = Math.max(Number(model.entryRow?.awb) || 0, spreadBps / 2);
    const levels = [
      { side: "ask", name: "卖5档", price: entry * (1 + spreadBps / 20000), qty: askQty, bps: spreadBps / 2 },
      { side: "bid", name: "买5档", price: entry * (1 - spreadBps / 20000), qty: bidQty, bps: spreadBps / 2 },
      { side: "ask", name: "卖墙", price: entry * (1 + sellWallBps / 10000), qty: sellWallQty, bps: sellWallBps },
      { side: "bid", name: "买墙", price: entry * (1 - buyWallBps / 10000), qty: buyWallQty, bps: buyWallBps }
    ].filter(item => Number.isFinite(item.price) && item.qty > 0);
    const chartPrices = model.chartRows.map(row => Number(row.p)).filter(Number.isFinite);
    const priceMin = Math.min(...chartPrices, model.avg - model.zone95, ...levels.map(item => item.price));
    const priceMax = Math.max(...chartPrices, model.avg + model.zone95, ...levels.map(item => item.price));
    const priceRange = Math.max(priceMax - priceMin, 1);
    const maxQty = Math.max(...levels.map(item => item.qty), 1);
    return levels.map(item => ({
      ...item,
      top: clamp(((priceMax - item.price) / priceRange) * 76 + 2, 3, 76),
      width: clamp((item.qty / maxQty) * 100, 12, 100)
    }));
  }, [model]);

  const candidateIndexes = useMemo(() => {
    const out = [];
    const windowSec = windowMin * 60;
    for (let i = windowSec; i <= maxIndex; i += 5) {
      const start = i - windowSec + 1;
      const { avg, sigma } = statsFromPrefix(prefix.sum, prefix.sumSq, start, i);
      const price = Number(rows[i]?.p);
      const z = sigma > 0 ? (price - avg) / sigma : 0;
      if (Math.abs(z) >= Z70) out.push(i);
    }
    return out;
  }, [maxIndex, prefix, rows, windowMin]);

  const timelineIndexes = signalsOnly ? candidateIndexes : rows.map((_row, idx) => idx).filter(idx => idx <= maxIndex);
  const selectedPosition = Math.max(0, timelineIndexes.findIndex(item => item >= index));
  const nearestSignal = useMemo(() => {
    if (!candidateIndexes.length) return maxIndex;
    return candidateIndexes.reduce((best, item) => Math.abs(item - index) < Math.abs(best - index) ? item : best, candidateIndexes[0]);
  }, [candidateIndexes, index, maxIndex]);

  const visibleMarkOverlays = useMemo(() => {
    const chart = chartRef.current;
    if (!chart) return [];
    const latestMark = manualMarks[manualMarks.length - 1];
    return (latestMark ? [latestMark] : [])
      .filter(mark => mark.settleIndex >= model.chartStart && mark.entryIndex <= model.chartEnd)
      .map(mark => {
        const entryRow = rows[mark.entryIndex];
        const settleRow = rows[mark.settleIndex];
        if (!entryRow || !settleRow) return null;
        const calculatedEntryLeft = chart.timeScale().timeToCoordinate(toSeconds(entryRow));
        const calculatedSettleLeft = chart.timeScale().timeToCoordinate(toSeconds(settleRow));
        if (!Number.isFinite(calculatedEntryLeft) || !Number.isFinite(calculatedSettleLeft)) return null;
        const entryLeft = Number.isFinite(mark.visualEntryLeft) ? mark.visualEntryLeft : calculatedEntryLeft;
        const settleLeft = entryLeft + (calculatedSettleLeft - calculatedEntryLeft);
        const movedUp = mark.actual === "UP";
        const movedDown = mark.actual === "DOWN";
        return {
          ...mark,
          entryLeft,
          settleLeft,
          width: Math.max(settleLeft - entryLeft, 2),
          resultClass: movedUp ? "up" : movedDown ? "down" : "flat",
          resultText: movedUp ? "涨" : movedDown ? "跌" : "平"
        };
      })
      .filter(Boolean);
  }, [manualMarks, model.chartEnd, model.chartStart, overlayTick, rows]);

  const createMark = useCallback(rowIndex => {
    const entryIndex = clamp(rowIndex, windowMin * 60, maxIndex);
    const settleIndex = Math.min(entryIndex + HORIZON_SEC, rows.length - 1);
    const entryRow = rows[entryIndex];
    const settleRow = rows[settleIndex];
    const start = clamp(entryIndex - windowMin * 60 + 1, 0, entryIndex);
    const { avg, sigma } = statsFromPrefix(prefix.sum, prefix.sumSq, start, entryIndex);
    const entry = Number(entryRow?.p);
    const settle = Number(settleRow?.p);
    const z = sigma > 0 ? (entry - avg) / sigma : 0;
    const direction = directionFromZ(z);
    const actual = settle > entry ? "UP" : settle < entry ? "DOWN" : "FLAT";
    const won = direction !== "WAIT" && direction === actual;
    const resultText = actual === "UP" ? "涨" : actual === "DOWN" ? "跌" : "平";
    return {
      id: `${entryRow.t}-${settleRow.t}-${Date.now()}`,
      entryIndex,
      settleIndex,
      direction,
      actual,
      won,
      entry,
      settle,
      z,
      resultText,
      entryTime: entryRow.t,
      settleTime: settleRow.t,
      moveBps: entry > 0 ? (settle / entry - 1) * 10000 : 0
    };
  }, [maxIndex, prefix, rows, windowMin]);

  const addManualMark = useCallback((rowIndex, visualEntryLeft = null) => {
    if (clickTimerRef.current) {
      window.clearTimeout(clickTimerRef.current);
      clickTimerRef.current = null;
    }
    const now = Date.now();
    if (now - lastDoubleClickRef.current < 260) return;
    lastDoubleClickRef.current = now;
    const mark = { ...createMark(rowIndex), visualEntryLeft };
    setManualMarks(items => [...items, mark].slice(-50));
  }, [createMark]);

  const nearestIndexFromChartX = useCallback(x => {
    const chart = chartRef.current;
    if (!chart) return undefined;
    const paneWidth = chart.timeScale().width();
    const chartTimeValue = chart.timeScale().coordinateToTime(clamp(x, 0, paneWidth));
    const directTime = Number(chartTimeValue?.timestamp || chartTimeValue);
    if (Number.isFinite(directTime)) {
      const directIndex = timeToIndexRef.current.get(directTime);
      if (directIndex !== undefined) return directIndex;
      let nearestTime;
      for (const time of timeToIndexRef.current.keys()) {
        if (nearestTime === undefined || Math.abs(time - directTime) < Math.abs(nearestTime - directTime)) {
          nearestTime = time;
        }
      }
      if (nearestTime !== undefined) return timeToIndexRef.current.get(nearestTime);
    }
    const ratio = paneWidth > 0 ? clamp(x, 0, paneWidth) / paneWidth : 0;
    return Math.round(model.chartStart + ratio * (model.chartEnd - model.chartStart));
  }, [model.chartEnd, model.chartStart]);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout: { background: { color: "transparent" }, textColor: "#98a2b3" },
      grid: {
        vertLines: { color: "rgba(255,255,255,0.05)" },
        horzLines: { color: "rgba(255,255,255,0.05)" }
      },
      rightPriceScale: { borderColor: "rgba(255,255,255,0.12)" },
      timeScale: {
        borderColor: "rgba(255,255,255,0.12)",
        timeVisible: true,
        secondsVisible: true,
        tickMarkFormatter: chartTime
      },
      localization: { locale: "zh-CN", timeFormatter: chartTime },
      crosshair: {
        mode: 0,
        vertLine: { labelBackgroundColor: "#12161f" },
        horzLine: { labelBackgroundColor: "#12161f" }
      }
    });
    const price = chart.addSeries(LineSeries, { color: "#d9edf7", lineWidth: 2 });
    const meanLine = chart.addSeries(LineSeries, { color: "rgba(255,255,255,0.6)", lineWidth: 1, lineStyle: 2 });
    const upper70 = chart.addSeries(LineSeries, { color: "#27c3a5", lineWidth: 1, lineStyle: 2 });
    const lower70 = chart.addSeries(LineSeries, { color: "#27c3a5", lineWidth: 1, lineStyle: 2 });
    const upper95 = chart.addSeries(LineSeries, { color: "#f0c94a", lineWidth: 1, lineStyle: 3 });
    const lower95 = chart.addSeries(LineSeries, { color: "#f0c94a", lineWidth: 1, lineStyle: 3 });
    const volume = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "",
      base: 0,
      lastValueVisible: false,
      priceLineVisible: false
    });
    chart.priceScale("").applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } });
    markersRef.current = createSeriesMarkers(price, []);
    chartRef.current = chart;
    seriesRef.current = { price, meanLine, upper70, lower70, upper95, lower95, volume };

    const syncOverlay = () => {
      if (!programmaticRangeChangeRef.current && lastChartWindowKeyRef.current) {
        userChangedChartRangeRef.current = true;
      }
      setOverlayTick(value => value + 1);
    };
    const resize = () => {
      chart.resize(containerRef.current.clientWidth, containerRef.current.clientHeight);
      syncOverlay();
    };
    window.addEventListener("resize", resize);
    chart.timeScale().subscribeVisibleLogicalRangeChange(syncOverlay);
    chart.timeScale().subscribeVisibleTimeRangeChange(syncOverlay);
    resize();
    return () => {
      window.removeEventListener("resize", resize);
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(syncOverlay);
      chart.timeScale().unsubscribeVisibleTimeRangeChange(syncOverlay);
      chart.remove();
      chartRef.current = null;
      seriesRef.current = {};
      markersRef.current = null;
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    const series = seriesRef.current;
    if (!chart || !series.price || !model.chartRows.length) return;
    const timeMap = new Map();
    const priceData = model.chartRows.map((row, offset) => {
      const time = toSeconds(row);
      timeMap.set(time, model.chartStart + offset);
      return { time, value: Number(row.p) };
    });
    timeToIndexRef.current = timeMap;
    const band = model.chartRows.map(row => ({ time: toSeconds(row) }));
    series.price.setData(priceData);
    series.meanLine.setData(band.map(item => ({ ...item, value: model.avg })));
    series.upper70.setData(band.map(item => ({ ...item, value: model.avg + model.zone70 })));
    series.lower70.setData(band.map(item => ({ ...item, value: model.avg - model.zone70 })));
    series.upper95.setData(band.map(item => ({ ...item, value: model.avg + model.zone95 })));
    series.lower95.setData(band.map(item => ({ ...item, value: model.avg - model.zone95 })));
    series.volume.setData(model.chartRows.map(row => ({
      time: toSeconds(row),
      value: Number(row.v) || 0,
      color: Number(row.b) >= Number(row.s) ? "rgba(39,195,165,0.42)" : "rgba(255,74,90,0.42)"
    })));
    const chartWindowKey = `${model.chartStart}:${model.chartEnd}:${windowMin}`;
    const chartWindowChanged = lastChartWindowKeyRef.current !== chartWindowKey;
    if (chartWindowChanged || !userChangedChartRangeRef.current) {
      programmaticRangeChangeRef.current = true;
      chart.timeScale().fitContent();
      window.setTimeout(() => {
        programmaticRangeChangeRef.current = false;
      }, 0);
      lastChartWindowKeyRef.current = chartWindowKey;
      userChangedChartRangeRef.current = false;
    }
    setOverlayTick(value => value + 1);
  }, [model, windowMin]);

  useEffect(() => {
    if (!markersRef.current) return;
    markersRef.current.setMarkers([]);
  }, [manualMarks]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return undefined;
    const onMove = param => {
      if (!param?.time) {
        setHoverInfo(null);
        return;
      }
      const rowIndex = timeToIndexRef.current.get(Number(param.time));
      const row = rowIndex !== undefined ? rows[rowIndex] : null;
      setHoverInfo(row ? { row, index: rowIndex } : null);
    };
    const onDblClick = param => {
      if (clickTimerRef.current) {
        window.clearTimeout(clickTimerRef.current);
        clickTimerRef.current = null;
      }
      const eventTime = Number(param?.time?.timestamp || param?.time);
      let rowIndex = Number.isFinite(eventTime) ? timeToIndexRef.current.get(eventTime) : undefined;
      if (rowIndex === undefined && Number.isFinite(eventTime)) {
        let nearestTime;
        for (const time of timeToIndexRef.current.keys()) {
          if (nearestTime === undefined || Math.abs(time - eventTime) < Math.abs(nearestTime - eventTime)) {
            nearestTime = time;
          }
        }
        if (nearestTime !== undefined) rowIndex = timeToIndexRef.current.get(nearestTime);
      }
      if (rowIndex === undefined) {
        const x = Number(param?.point?.x);
        if (Number.isFinite(x)) rowIndex = nearestIndexFromChartX(x);
      }
      const visualX = Number(param?.point?.x);
      if (rowIndex !== undefined) addManualMark(rowIndex, Number.isFinite(visualX) ? visualX : null);
    };
    chart.subscribeCrosshairMove(onMove);
    chart.subscribeDblClick(onDblClick);
    return () => {
      if (clickTimerRef.current) {
        window.clearTimeout(clickTimerRef.current);
        clickTimerRef.current = null;
      }
      chart.unsubscribeCrosshairMove(onMove);
      chart.unsubscribeDblClick(onDblClick);
    };
  }, [addManualMark, maxIndex, nearestIndexFromChartX, rows, windowMin]);

  const jumpToPosition = nextPosition => {
    if (!timelineIndexes.length) return;
    setIndex(timelineIndexes[clamp(nextPosition, 0, timelineIndexes.length - 1)]);
  };

  const jumpByMinutes = minutes => {
    const target = clamp(index + minutes * 60, 0, maxIndex);
    if (!signalsOnly || !candidateIndexes.length) {
      setIndex(target);
      return;
    }
    const next = candidateIndexes.reduce((best, item) => Math.abs(item - target) < Math.abs(best - target) ? item : best, candidateIndexes[0]);
    setIndex(next);
  };

  const signalClass = model.signal === "UP" ? "up" : model.signal === "DOWN" ? "down" : "wait";
  const signalText = directionLabel(model.signal);
  const actualText = actualLabel(model.actual);
  const liquiditySide = Number(model.entryRow?.im20) > 0.12 ? "买盘更厚" : Number(model.entryRow?.im20) < -0.12 ? "卖盘更厚" : "流动性均衡";
  const hoverRow = hoverInfo?.row;

  return (
    <div className="normal-page">
      <header className="normal-topbar">
        <div>
          <span>TradingView Lightweight Charts</span>
          <h1>真实秒级价格、成交量、正态区间和订单薄</h1>
        </div>
        <a href="/dashboard/" className="normal-back">返回控制台</a>
      </header>

      <main className="normal-layout normal-layout-wide">
        <section className="normal-stage">
          <div className="normal-stage-head">
            <div>
              <span>这一单</span>
              <strong>{signalText}</strong>
            </div>
            <div className={`normal-signal ${signalClass}`}>
              {model.signal === "UP" ? <ArrowUp size={18} /> : model.signal === "DOWN" ? <ArrowDown size={18} /> : <Gauge size={18} />}
              {model.signal === "WAIT" ? "区间内等待" : model.won ? "10分钟后命中" : "10分钟后未命中"}
            </div>
          </div>

          <div className="binary-strip">
            <div><span>入场</span><strong>{fmtTime(model.entryTime)}</strong><small>价格 {fmt(model.entry, 2)}</small></div>
            <div><span>预测</span><strong className={signalClass}>{signalText}</strong><small>z={fmt(model.z, 2)}，{model.state}</small></div>
            <div><span>10分钟到期</span><strong>{fmtTime(model.settleTime)}</strong><small>价格 {fmt(model.settle, 2)}</small></div>
            <div><span>真实结果</span><strong className={model.actual === "UP" ? "up" : model.actual === "DOWN" ? "down" : ""}>{actualText}</strong><small>{fmt(model.settleMove, 2)} bps，{model.signal === "WAIT" ? "未下单" : model.won ? "赢" : "输"}</small></div>
          </div>

          <div
            className="tv-chart-frame"
          >
            <div ref={containerRef} className="tv-chart" />
            <div className="orderbook-axis" aria-label="订单薄价格轴">
              <div className="orderbook-axis-title">订单薄</div>
              {orderbookAxis.map(item => (
                <div
                  className={`orderbook-axis-row ${item.side}`}
                  key={`${item.name}-${item.price}`}
                  style={{ top: `${item.top}%` }}
                  title={`${item.name} ${fmt(item.price, 2)} / ${fmt(item.qty, 2)} BTC / ${fmt(item.bps, 3)} bps`}
                >
                  <span>{item.name}</span>
                  <b style={{ width: `${item.width}%` }} />
                  <small>{fmt(item.price, 1)}</small>
                </div>
              ))}
            </div>
            <div className="chart-mark-overlay" aria-label="图表打标层">
              {visibleMarkOverlays.map(mark => (
                <div className={`chart-mark-group ${mark.resultClass}`} key={mark.id}>
                  <div
                    className="chart-mark-span"
                    style={{ left: `${mark.entryLeft}px`, width: `${mark.width}px` }}
                  >
                    <span>10分钟</span>
                  </div>
                  <div className="chart-mark-line entry" style={{ left: `${mark.entryLeft}px` }}>
                    <b>打标 {fmtClock(mark.entryTime)}</b>
                  </div>
                  <div className="chart-mark-line settle" style={{ left: `${mark.settleLeft}px` }}>
                    <b>10分钟后{mark.resultText} {fmtClock(mark.settleTime)}</b>
                  </div>
                </div>
              ))}
            </div>
            <div className="tv-chart-tip">
              {hoverRow ? `${fmtTime(hoverRow.t)}  价 ${fmt(Number(hoverRow.p), 2)}  量 ${fmt(Number(hoverRow.v), 3)}` : "点击或按住拖动图表选择入场点，双击打点并标记后面第10分钟"}
            </div>
          </div>
        </section>

        <aside className="normal-side">
          <section className="panel normal-card">
            <header className="panel-header"><span><Activity size={15} /> 参数</span></header>
            <div className="drag-tip">点击图表选入场点；按住左键拖动可连续切换。</div>
            <div className="drag-tip">双击图表打点，会自动标记后面第10分钟到期点。</div>
            <label><span>滚动窗口</span><select value={windowMin} onChange={event => setWindowMin(Number(event.target.value))}><option value={60}>60分钟</option><option value={120}>120分钟</option><option value={180}>180分钟</option><option value={360}>360分钟</option><option value={720}>720分钟</option><option value={1440}>24小时</option><option value={2160}>36小时</option><option value={2400}>全部</option></select><strong>{windowMin}分</strong></label>
            <label><span>浏览模式</span><select value={signalsOnly ? "signals" : "all"} onChange={event => setSignalsOnly(event.target.value === "signals")}><option value="signals">只看触发点</option><option value="all">全部秒级点</option></select><strong>{signalsOnly ? `${candidateIndexes.length}个` : `${maxIndex + 1}秒`}</strong></label>
            <label><span>入场点</span><input type="range" min="0" max={Math.max(timelineIndexes.length - 1, 0)} step="1" value={selectedPosition} onChange={event => jumpToPosition(Number(event.target.value))} /><strong>{fmtTime(model.entryTime)}</strong></label>
            <button className="normal-reset" type="button" onClick={() => setIndex(maxIndex)}><RefreshCcw size={15} /> 回到最新可结算点</button>
            <div className="time-jump-grid">
              <button type="button" onClick={() => jumpToPosition(selectedPosition - 1)}>上一单</button>
              <button type="button" onClick={() => jumpToPosition(selectedPosition + 1)}>下一单</button>
              <button type="button" onClick={() => jumpByMinutes(-10)}>-10分钟</button>
              <button type="button" onClick={() => jumpByMinutes(10)}>+10分钟</button>
              <button type="button" onClick={() => setIndex(nearestSignal)}>最近触发</button>
              <button type="button" onClick={() => setIndex(candidateIndexes[candidateIndexes.length - 1] || maxIndex)}>最后触发</button>
            </div>
          </section>

          <section className="panel normal-card mark-card">
            <header className="panel-header">
              <span>打点记录</span>
              <button type="button" onClick={() => setManualMarks([])}>清空</button>
            </header>
            <div className="mark-list">
              {manualMarks.length ? manualMarks.slice().reverse().map(mark => (
                <button type="button" key={mark.id} onClick={() => setIndex(mark.entryIndex)}>
                  <strong className={mark.actual === "UP" ? "up" : mark.actual === "DOWN" ? "down" : "warn"}>10分钟后{mark.resultText || (mark.actual === "UP" ? "涨" : mark.actual === "DOWN" ? "跌" : "平")}</strong>
                  <span>{fmtTime(mark.entryTime)} 到 {fmtTime(mark.settleTime)}</span>
                  <small>{fmt(mark.entry, 2)} 到 {fmt(mark.settle, 2)}，{fmt(mark.moveBps, 2)} bps，z={fmt(mark.z, 2)}</small>
                </button>
              )) : <div className="empty-marks">双击图表添加打点</div>}
            </div>
          </section>

          <section className="panel normal-card liquidity-card">
            <header className="panel-header"><span><Database size={15} /> 右侧流动性</span></header>
            <div className="liquidity-summary">
              <strong>{liquiditySide}</strong>
              <span>价差 {fmt(Number(model.entryRow?.sp), 4)} bps</span>
              <span>微价格偏移 {fmt(Number(model.entryRow?.mp), 4)} bps</span>
            </div>
            <div className="depth-bars">
              <div><span>5档买盘</span><b>{fmt(Number(model.entryRow?.b5), 2)}</b><i style={{ width: `${clamp((Number(model.entryRow?.b5) || 0) / Math.max((Number(model.entryRow?.b5) || 0) + (Number(model.entryRow?.a5) || 0), 1) * 100, 4, 100)}%` }} /></div>
              <div><span>5档卖盘</span><b>{fmt(Number(model.entryRow?.a5), 2)}</b><i className="ask" style={{ width: `${clamp((Number(model.entryRow?.a5) || 0) / Math.max((Number(model.entryRow?.b5) || 0) + (Number(model.entryRow?.a5) || 0), 1) * 100, 4, 100)}%` }} /></div>
              <div><span>20档买盘</span><b>{fmt(Number(model.entryRow?.b20), 2)}</b><i style={{ width: `${clamp((Number(model.entryRow?.b20) || 0) / Math.max((Number(model.entryRow?.b20) || 0) + (Number(model.entryRow?.a20) || 0), 1) * 100, 4, 100)}%` }} /></div>
              <div><span>20档卖盘</span><b>{fmt(Number(model.entryRow?.a20), 2)}</b><i className="ask" style={{ width: `${clamp((Number(model.entryRow?.a20) || 0) / Math.max((Number(model.entryRow?.b20) || 0) + (Number(model.entryRow?.a20) || 0), 1) * 100, 4, 100)}%` }} /></div>
            </div>
            <div className="normal-formula">
              <span>5档不平衡：{fmt(Number(model.entryRow?.im5), 4)}</span>
              <span>20档不平衡：{fmt(Number(model.entryRow?.im20), 4)}</span>
              <span>买墙：{fmt(Number(model.entryRow?.bwq), 2)} @ {fmt(Number(model.entryRow?.bwb), 3)} bps</span>
              <span>卖墙：{fmt(Number(model.entryRow?.awq), 2)} @ {fmt(Number(model.entryRow?.awb), 3)} bps</span>
            </div>
          </section>

          <section className="panel normal-card">
            <header className="panel-header"><span><Clock size={15} /> 计算结果</span></header>
            <div className="normal-formula">
              <strong>正态区间</strong>
              <span>入场价：{fmt(model.entry, 2)}</span>
              <span>均值：{fmt(model.avg, 2)}</span>
              <span>标准差 sigma：{fmt(model.sigma, 2)} USDT</span>
              <span>70% 区间：{fmt(model.avg - model.zone70, 2)} - {fmt(model.avg + model.zone70, 2)}</span>
              <span>95% 区间：{fmt(model.avg - model.zone95, 2)} - {fmt(model.avg + model.zone95, 2)}</span>
              <strong>10分钟二元期权</strong>
              <span>p_up 约 {fmt(model.pUp * 100, 1)}%，p_down 约 {fmt((1 - model.pUp) * 100, 1)}%</span>
              <span>结果：{signalText}，10分钟后{actualText}，{model.signal === "WAIT" ? "不计胜负" : model.won ? "命中" : "未命中"}</span>
            </div>
          </section>
        </aside>
      </main>
    </div>
  );
}

export default function NormalVisual() {
  const [realData, setRealData] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    fetch("/api/normal-visual-data")
      .then(res => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then(data => {
        if (!alive) return;
        setRealData(data && Array.isArray(data.rows) ? data : { rows: [] });
        setError("");
      })
      .catch(err => {
        if (!alive) return;
        setError(String(err?.message || err));
      });
    return () => {
      alive = false;
    };
  }, []);

  if (!realData && !error) {
    return (
      <div className="normal-page">
        <header className="normal-topbar">
          <div>
            <span>TradingView Lightweight Charts</span>
            <h1>正在加载真实秒级数据...</h1>
          </div>
          <a href="/dashboard/" className="normal-back">返回控制台</a>
        </header>
      </div>
    );
  }

  if (error) {
    return (
      <div className="normal-page">
        <header className="normal-topbar">
          <div>
            <span>TradingView Lightweight Charts</span>
            <h1>真实数据加载失败</h1>
          </div>
          <a href="/dashboard/" className="normal-back">返回控制台</a>
        </header>
        <main className="normal-layout">
          <section className="panel normal-card" style={{ padding: 16, color: "var(--red)" }}>
            {error}
          </section>
        </main>
      </div>
    );
  }

  return <NormalVisualInner realData={realData} />;
}
