/**
 * K 线图组件 — TradingView Lightweight Charts
 * 支持叠加买卖标记和均线
 */
import { useEffect, useRef } from 'react';
import { createChart, IChartApi, ISeriesApi, CandlestickData, Time } from 'lightweight-charts';

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
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);

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
    });

    const series = chart.addCandlestickSeries({
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
