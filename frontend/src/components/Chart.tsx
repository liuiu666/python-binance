/**
 * K 线图组件 — TradingView Lightweight Charts
 * 支持叠加买卖标记和均线
 */
import { useEffect, useRef } from 'react';
import { createChart, CandlestickSeries } from 'lightweight-charts';
import type { CandlestickData, Time } from 'lightweight-charts';

interface ChartProps {
  data: CandlestickData<Time>[];
  markers?: Array<{
    time: Time;
    position: 'aboveBar' | 'belowBar';
    color: string;
    shape: 'arrowUp' | 'arrowDown';
    text: string;
  }>;
  height?: number;
}

export default function Chart({ data, markers, height = 400 }: ChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<any>(null);
  const seriesRef = useRef<any>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    // 创建图表
    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height,
      layout: {
        background: { color: '#0d1117' },
        textColor: '#8b949e',
      },
      grid: {
        vertLines: { color: '#21262d' },
        horzLines: { color: '#21262d' },
      },
      crosshair: {
        mode: 0,
      },
      rightPriceScale: {
        borderColor: '#30363d',
      },
      timeScale: {
        borderColor: '#30363d',
        timeVisible: true,
      },
      localization: {
        locale: 'zh-CN',
        timeFormatter: (timestamp: any) => {
          if (typeof timestamp !== 'number') return String(timestamp);
          const date = new Date(timestamp * 1000);
          const y = date.getUTCFullYear();
          const m = String(date.getUTCMonth() + 1).padStart(2, '0');
          const d = String(date.getUTCDate()).padStart(2, '0');
          const hh = String(date.getUTCHours()).padStart(2, '0');
          const mm = String(date.getUTCMinutes()).padStart(2, '0');
          const ss = String(date.getUTCSeconds()).padStart(2, '0');
          return `${y}-${m}-${d} ${hh}:${mm}:${ss}`;
        }
      }
    });

    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#00c853',
      downColor: '#ff1744',
      borderUpColor: '#00c853',
      borderDownColor: '#ff1744',
      wickUpColor: '#00c853',
      wickDownColor: '#ff1744',
    });

    chartRef.current = chart;
    seriesRef.current = series;

    // 响应式
    const handleResize = () => {
      if (containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth });
      }
    };
    window.addEventListener('resize', handleResize);

    return () => {
      window.removeEventListener('resize', handleResize);
      chart.remove();
    };
  }, [height]);

  // 更新数据
  useEffect(() => {
    if (seriesRef.current && data.length > 0) {
      seriesRef.current.setData(data);
    }
  }, [data]);

  // 更新标记
  useEffect(() => {
    if (seriesRef.current && markers && markers.length > 0) {
      seriesRef.current.setMarkers(markers);
    }
  }, [markers]);

  return <div ref={containerRef} style={{ width: '100%' }} />;
}
