/**
 * K 线图组件 — TradingView Lightweight Charts
 * 支持叠加成交量柱状图、泊松异常标记、滑动加载历史数据
 */
import { useEffect, useRef } from 'react';
import { createChart, CandlestickSeries, HistogramSeries, createSeriesMarkers } from 'lightweight-charts';
import type { Time } from 'lightweight-charts';

interface CandlestickData {
  time: Time;
  open: number;
  high: number;
  low: number;
  close: number;
}

interface VolumeData {
  time: Time;
  value: number;
  color?: string;
}

interface AnomalyMarker {
  time: Time;
  position: 'aboveBar' | 'belowBar';
  color: string;
  shape: 'arrowUp' | 'arrowDown';
  text: string;
}

interface ChartProps {
  data: CandlestickData[];
  volumeData?: VolumeData[];
  anomalyMarkers?: AnomalyMarker[];
  height?: number;
  showVolume?: boolean;
  /** 滑动到最左侧时触发，请求更早的历史数据 */
  onLoadMore?: () => void;
  /** 是否正在加载更多数据 */
  loadingMore?: boolean;
}

export default function Chart({ 
  data, 
  volumeData, 
  anomalyMarkers, 
  height = 500,
  showVolume = true,
  onLoadMore,
  loadingMore = false,
}: ChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<any>(null);
  const candleSeriesRef = useRef<any>(null);
  const volumeSeriesRef = useRef<any>(null);
  const markersRef = useRef<any>(null);
  const loadingMoreRef = useRef(loadingMore);
  const onLoadMoreRef = useRef(onLoadMore);

  useEffect(() => {
    loadingMoreRef.current = loadingMore;
  }, [loadingMore]);

  useEffect(() => {
    onLoadMoreRef.current = onLoadMore;
  }, [onLoadMore]);

  // chart初始化（只在height/showVolume变化时重建）
  useEffect(() => {
    if (!containerRef.current) return;

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
        rightOffset: 5,
        barSpacing: 8,
        minBarSpacing: 2,
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

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#00c853',
      downColor: '#ff1744',
      borderUpColor: '#00c853',
      borderDownColor: '#ff1744',
      wickUpColor: '#00c853',
      wickDownColor: '#ff1744',
    });

    let volumeSeries: any = null;
    if (showVolume) {
      volumeSeries = chart.addSeries(HistogramSeries, {
        priceFormat: { type: 'volume' },
        priceScaleId: '',
      });
      volumeSeries.priceScale().applyOptions({
        scaleMargins: { top: 0.8, bottom: 0 },
      });
    }

    // 监听滑动到最左侧时加载更多历史数据
    chart.timeScale().subscribeVisibleLogicalRangeChange((range: any) => {
      if (!range || loadingMoreRef.current) return;
      if (range.from < 5 && onLoadMoreRef.current) {
        onLoadMoreRef.current();
      }
    });

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;
    // chart重建后markersRef失效，需要重置
    markersRef.current = null;

    const handleResize = () => {
      if (containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth });
      }
    };
    window.addEventListener('resize', handleResize);

    return () => {
      window.removeEventListener('resize', handleResize);
      chart.remove();
      markersRef.current = null;
    };
  }, [height, showVolume]);

  // 数据更新：K线 + 成交量 + 标记统一处理
  useEffect(() => {
    if (!candleSeriesRef.current || data.length === 0) return;

    // 先设置K线数据
    candleSeriesRef.current.setData(data);

    // 再设置成交量数据
    if (volumeSeriesRef.current && volumeData && volumeData.length > 0) {
      volumeSeriesRef.current.setData(volumeData);
    }

    // 最后设置异常标记（必须在setData之后）
    if (anomalyMarkers && anomalyMarkers.length > 0) {
      if (markersRef.current) {
        markersRef.current.setMarkers(anomalyMarkers);
      } else {
        markersRef.current = createSeriesMarkers(candleSeriesRef.current, anomalyMarkers);
      }
    }
  }, [data, volumeData, anomalyMarkers]);

  return (
    <div style={{ position: 'relative' }}>
      <div ref={containerRef} style={{ width: '100%' }} />
      {loadingMore && (
        <div style={{
          position: 'absolute',
          top: 8,
          left: '50%',
          transform: 'translateX(-50%)',
          background: 'rgba(0,0,0,0.7)',
          color: '#8b949e',
          padding: '4px 12px',
          borderRadius: 4,
          fontSize: 12,
          zIndex: 10,
        }}>
          加载更多历史数据...
        </div>
      )}
    </div>
  );
}
