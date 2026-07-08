import { useEffect, useMemo, useRef } from "react";
import { CandlestickSeries, createChart, createSeriesMarkers } from "lightweight-charts";
import { directionText, fmt, pnlText, statusClass } from "../utils";

const chartTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  hour12: false,
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit"
});

function formatTime(time) {
  const seconds = typeof time === "number" ? time : Number(time?.timestamp || time);
  if (!Number.isFinite(seconds)) return "";
  return chartTimeFormatter.format(new Date(seconds * 1000));
}

function normalizeCandles(candles) {
  const seen = new Set();
  return (candles || [])
    .map(item => {
      const time = Number(item.time);
      const open = Number(item.open);
      const high = Number(item.high);
      const low = Number(item.low);
      const close = Number(item.close);
      if (![time, open, high, low, close].every(Number.isFinite)) return null;
      if (seen.has(time)) return null;
      seen.add(time);
      return { time, open, high, low, close };
    })
    .filter(Boolean)
    .sort((a, b) => a.time - b.time);
}

function tradeMarkers(trades, chartData) {
  if (!chartData.length) return [];
  const start = Number(chartData[0].time);
  const end = Number(chartData[chartData.length - 1].time);
  return (trades || [])
    .flatMap((trade, index) => {
      const out = [];
      const openSec = Math.floor(Number(trade.openTime) / 1000);
      if (Number.isFinite(openSec) && openSec >= start && openSec <= end) {
        const up = trade.direction === "UP";
        out.push({
          time: openSec,
          position: up ? "belowBar" : "aboveBar",
          color: up ? "#16a085" : "#d94a4a",
          shape: up ? "arrowUp" : "arrowDown",
          text: `${index + 1} ${directionText(trade.direction)} ${fmt(trade.amount, 0)}U`
        });
      }
      const settleSec = Math.floor(Number(trade.settleTime) / 1000);
      if (trade.status && trade.status !== "pending" && Number.isFinite(settleSec) && settleSec >= start && settleSec <= end) {
        const cls = statusClass(trade.status);
        out.push({
          time: settleSec,
          position: "inBar",
          color: cls === "won" ? "#16a085" : cls === "lost" ? "#d94a4a" : "#c99a2e",
          shape: "circle",
          text: pnlText(trade)
        });
      }
      return out;
    })
    .sort((a, b) => a.time - b.time)
    .slice(-120);
}

export default function MarketChart({ candles, trades }) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef(null);
  const markersRef = useRef(null);
  const didInitialFitRef = useRef(false);
  const chartData = useMemo(() => normalizeCandles(candles), [candles]);
  const markers = useMemo(() => tradeMarkers(trades, chartData), [chartData, trades]);

  useEffect(() => {
    if (!containerRef.current || chartRef.current) return undefined;

    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: {
        background: { color: "transparent" },
        textColor: "#8f9bad"
      },
      grid: {
        vertLines: { color: "rgba(255,255,255,0.05)" },
        horzLines: { color: "rgba(255,255,255,0.05)" }
      },
      rightPriceScale: {
        borderColor: "rgba(255,255,255,0.12)",
        scaleMargins: { top: 0.12, bottom: 0.12 }
      },
      timeScale: {
        borderColor: "rgba(255,255,255,0.12)",
        timeVisible: true,
        secondsVisible: false,
        tickMarkFormatter: formatTime
      },
      localization: {
        locale: "zh-CN",
        timeFormatter: formatTime
      },
      crosshair: {
        mode: 0,
        horzLine: { labelBackgroundColor: "#141922" },
        vertLine: { labelBackgroundColor: "#141922" }
      }
    });

    const series = chart.addSeries(CandlestickSeries, {
      upColor: "#16a085",
      downColor: "#d94a4a",
      borderVisible: false,
      wickUpColor: "#16a085",
      wickDownColor: "#d94a4a"
    });

    chartRef.current = chart;
    seriesRef.current = series;
    markersRef.current = createSeriesMarkers(series, []);

    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      markersRef.current = null;
      didInitialFitRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (!seriesRef.current) return;
    seriesRef.current.setData(chartData);
    if (chartData.length && !didInitialFitRef.current) {
      chartRef.current?.timeScale().fitContent();
      didInitialFitRef.current = true;
    }
    if (!chartData.length) didInitialFitRef.current = false;
  }, [chartData]);

  useEffect(() => {
    if (!markersRef.current) return;
    if (typeof markersRef.current.setMarkers === "function") {
      markersRef.current.setMarkers(markers);
    } else {
      createSeriesMarkers(seriesRef.current, markers);
    }
  }, [markers]);

  return (
    <div className="chart-shell">
      <div className="chart-toolbar">
        <span>BTC / USDT</span>
        <strong>北京时间</strong>
      </div>
      {chartData.length ? null : <div className="chart-empty">等待K线数据</div>}
      <div ref={containerRef} className="chart-canvas" />
    </div>
  );
}
