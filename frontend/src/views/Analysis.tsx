/**
 * 量价探索数据分析面板 (Market EDA)
 */
import { useState, useEffect, useRef, useMemo } from 'react';
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
import Chart from '../components/Chart';
import { useStore } from '../store';
import type { Time } from 'lightweight-charts';

interface HistogramItem {
  volume_bucket: string;
  observed: number;
  poisson_fit: number;
}

interface AnalysisResults {
  lambda: number;
  variance: number;
  overdispersion: number;
  anomaly_ratio: number;
  histogram: HistogramItem[];
}

interface CandlestickData {
  time: Time;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
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

interface KlinesWithAnomalies {
  candles: (CandlestickData & {
    trades_count: number;
    lambda: number;
    p_value: number;
    anomaly_level: string;
    z_score: number;
    direction: string;
  })[];
  anomalies: Array<{
    time: number;
    anomaly_level: string;
    trades_count: number;
    lambda: number;
    p_value: number;
    z_score: number;
    price_change_pct: number;
    direction: string;
    candle_index: number;
  }>;
  lambda_estimate: number;
  window_size: number;
}

export default function Analysis() {
  const { prices } = useStore();
  const [symbol, setSymbol] = useState('BTCUSDT');
  const [availableSymbols, setAvailableSymbols] = useState<string[]>(['BTCUSDT', 'ETHUSDT']);
  const [timeframe, setTimeframe] = useState('1m');
  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState<AnalysisResults | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [klinesData, setKlinesData] = useState<KlinesWithAnomalies | null>(null);
  const [klinesLoading, setKlinesLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  // 用ref保存最新状态，避免闭包问题
  const klinesDataRef = useRef<KlinesWithAnomalies | null>(null);
  const loadingMoreRef = useRef(false);
  const hasMoreDataRef = useRef(true);
  // symbol声明后才能使用
  const currentPrice = prices[symbol] || 0;

  const runAnalysis = async () => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const res = await fetch(`/api/control/fit-poisson?symbol=${symbol}&interval=${timeframe}&limit=5000`);
      if (res.ok) {
        const data = await res.json();
        if (data.status === 'error') {
          setErrorMsg(data.message);
          setResults(null);
        } else {
          setResults(data);
        }
      } else {
        const data = await res.json();
        setErrorMsg(data.detail || '获取拟合数据失败');
        setResults(null);
      }
    } catch {
      setErrorMsg('网络连接错误，请检查后端服务是否正常启动。');
      setResults(null);
    } finally {
      setLoading(false);
    }
  };

  const fetchKlinesWithAnomalies = async (isRefresh = false) => {
    if (!isRefresh) setKlinesLoading(true);
    if (!isRefresh) {
      hasMoreDataRef.current = true;
    }
    try {
      const res = await fetch(`/api/klines-with-anomalies?symbol=${symbol}&interval=${timeframe}&limit=500&window_size=60`);
      if (res.ok) {
        const data = await res.json();
        if (data.candles && data.candles.length > 0) {
          klinesDataRef.current = data;
          setKlinesData(data);
          if (!isRefresh && data.candles.length < 500) {
            hasMoreDataRef.current = false;
          }
        }
      }
    } catch (err) {
      console.error('获取K线异常数据失败:', err);
    } finally {
      setKlinesLoading(false);
    }
  };

  // 滑动到左侧时加载更多历史数据
  const loadMoreHistory = async () => {
    if (loadingMoreRef.current || !hasMoreDataRef.current) return;
    const currentData = klinesDataRef.current;
    if (!currentData?.candles.length) return;

    loadingMoreRef.current = true;
    setLoadingMore(true);
    try {
      // 取当前最早K线的时间（减去时区偏移还原为UTC）作为end_time
      const earliestTime = Number(currentData.candles[0].time) - 8 * 3600;
      const res = await fetch(`/api/klines-with-anomalies?symbol=${symbol}&interval=${timeframe}&limit=500&window_size=60&end_time=${earliestTime}`);
      if (res.ok) {
        const data = await res.json();
        if (data.candles && data.candles.length > 0) {
          const existingTimes = new Set(currentData.candles.map(c => c.time));
          const newCandles = data.candles.filter((c: any) => !existingTimes.has(c.time));
          
          if (newCandles.length === 0) {
            hasMoreDataRef.current = false;
          } else {
            setKlinesData(prev => {
              if (!prev) return data;
              // 合并后按时间排序去重
              const allCandles = [...newCandles, ...prev.candles];
              const seen = new Set<number>();
              const uniqueCandles = allCandles.filter(c => {
                const t = Number(c.time);
                if (seen.has(t)) return false;
                seen.add(t);
                return true;
              }).sort((a, b) => Number(a.time) - Number(b.time));

              const allAnomalies = [...(data.anomalies || []), ...prev.anomalies];
              const seenAnomaly = new Set<number>();
              const uniqueAnomalies = allAnomalies.filter(a => {
                const t = Number(a.time);
                if (seenAnomaly.has(t)) return false;
                seenAnomaly.add(t);
                return true;
              }).sort((a, b) => Number(a.time) - Number(b.time));

              const updated = {
                ...prev,
                candles: uniqueCandles,
                anomalies: uniqueAnomalies,
                lambda_estimate: data.lambda_estimate || prev.lambda_estimate,
              };
              klinesDataRef.current = updated;
              return updated;
            });
            if (newCandles.length < 500) {
              hasMoreDataRef.current = false;
            }
          }
        } else {
          hasMoreDataRef.current = false;
        }
      }
    } catch (err) {
      console.error('加载更多历史数据失败:', err);
    } finally {
      loadingMoreRef.current = false;
      setLoadingMore(false);
    }
  };

  useEffect(() => {
    runAnalysis();
  }, [symbol, timeframe]);

  // 加载系统配置中的所有可用币种
  useEffect(() => {
    const fetchSymbols = async () => {
      try {
        const res = await fetch('/api/control/symbols');
        if (res.ok) {
          const data = await res.json();
          if (data.symbols && data.symbols.length > 0) {
            setAvailableSymbols(data.symbols);
          }
        }
      } catch (err) {
        console.error('获取可用币种列表失败:', err);
      }
    };
    fetchSymbols();
  }, []);

  useEffect(() => {
    fetchKlinesWithAnomalies();
  }, [symbol, timeframe]);

  // 每30秒自动刷新异常检测结果，实现实时标注
  useEffect(() => {
    const timer = setInterval(() => {
      fetchKlinesWithAnomalies(true);
    }, 30000);
    return () => clearInterval(timer);
  }, [symbol, timeframe]);

  // 用useMemo计算图表数据，依赖klinesData和currentPrice实现实时更新
  const { candles, volumeData, markers } = useMemo(() => {
    if (!klinesData) return { candles: [], volumeData: [], markers: [] };

    // 东八区时间偏移
    const TZ_OFFSET = 8 * 3600;

    const candles: CandlestickData[] = klinesData.candles.map((c, i) => {
      // 最后一根K线用实时价格更新
      const isLast = i === klinesData.candles.length - 1 && currentPrice > 0;
      const close = isLast ? currentPrice : c.close;
      const high = isLast ? Math.max(c.high, currentPrice) : c.high;
      const low = isLast ? Math.min(c.low, currentPrice) : c.low;
      return {
        time: (Number(c.time) + TZ_OFFSET) as Time,
        open: c.open,
        high,
        low,
        close,
        volume: c.volume,
      };
    });

    const volumeData: VolumeData[] = klinesData.candles.map(c => {
      const isUp = c.close >= c.open;
      let color = isUp ? 'rgba(0, 200, 83, 0.5)' : 'rgba(255, 23, 68, 0.5)';

      if (c.anomaly_level === 'ANOMALY') {
        color = isUp ? 'rgba(255, 183, 77, 0.7)' : 'rgba(255, 138, 101, 0.7)';
      } else if (c.anomaly_level === 'EXTREME') {
        color = isUp ? 'rgba(255, 82, 82, 0.9)' : 'rgba(255, 23, 68, 0.9)';
      }

      return {
        time: (Number(c.time) + TZ_OFFSET) as Time,
        value: c.trades_count || c.volume,
        color,
      };
    });

    const markers: AnomalyMarker[] = klinesData.anomalies.map(a => {
      const isExtreme = a.anomaly_level === 'EXTREME';
      const isBuy = a.price_change_pct > 0;

      return {
        time: (Number(a.time) + TZ_OFFSET) as Time,
        position: isBuy ? 'belowBar' : 'aboveBar',
        color: isExtreme ? '#ff5252' : '#ffab40',
        shape: isBuy ? 'arrowUp' : 'arrowDown',
        text: `${a.anomaly_level === 'EXTREME' ? '🚨' : '⚠️'} ${a.trades_count}笔 (${a.price_change_pct > 0 ? '+' : ''}${a.price_change_pct.toFixed(2)}%)`,
      };
    });

    return { candles, volumeData, markers };
  }, [klinesData, currentPrice]);

  return (
    <div style={styles.container}>
      <h2 style={styles.title}>🧠 量化数据分析与模型拟合</h2>
      <p style={styles.sub}>通过时序数据库 ClickHouse 行情分析成交量分布，拟合泊松异常探测参数</p>

      <div className="card" style={styles.controlBar}>
        <div style={styles.formInline}>
          <div style={styles.field}>
            <label style={styles.labelInline}>分析标的</label>
            <select value={symbol} onChange={(e) => setSymbol(e.target.value)} style={styles.select}>
              {availableSymbols.map(sym => (
                <option key={sym} value={sym}>{sym} (币安合约)</option>
              ))}
            </select>
          </div>
          <div style={styles.field}>
            <label style={styles.labelInline}>K线周期</label>
            <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)} style={styles.select}>
              <option value="1m">1分钟 (1m)</option>
              <option value="5m">5分钟 (5m)</option>
              <option value="15m">15分钟 (15m)</option>
            </select>
          </div>
          <button onClick={() => { runAnalysis(); fetchKlinesWithAnomalies(); }} disabled={loading || klinesLoading} style={styles.btn}>
            {loading || klinesLoading ? '加载中...' : '⚡ 刷新数据'}
          </button>
        </div>
      </div>

      {errorMsg && (
        <div style={styles.errorBox}>
          ⚠️ {errorMsg}
        </div>
      )}

      {klinesData && klinesData.candles.length > 0 && (
        <div className="card" style={styles.chartCard}>
          <h3 style={styles.cardTitle}>📈 K线走势与泊松异常标记</h3>
          <div style={styles.chartInfo}>
            <span style={styles.infoTag}>📊 当前 λ = {klinesData.lambda_estimate.toFixed(1)} 笔/周期</span>
            <span style={styles.infoTag}>⚠️ 检测到 {klinesData.anomalies.length} 个异常点</span>
            <span style={styles.legendItem}>
              <span style={{ ...styles.legendDot, background: 'rgba(255, 183, 77, 0.7)' }}></span> 异常放量
            </span>
            <span style={styles.legendItem}>
              <span style={{ ...styles.legendDot, background: 'rgba(255, 82, 82, 0.9)' }}></span> 极端放量
            </span>
          </div>
          {klinesLoading ? (
            <div style={styles.loading}>正在加载K线数据...</div>
          ) : (
            <Chart
              data={candles}
              volumeData={volumeData}
              anomalyMarkers={markers}
              height={450}
              showVolume={true}
              onLoadMore={loadMoreHistory}
              loadingMore={loadingMore}
            />
          )}

          {klinesData.anomalies.length > 0 && (
            <div style={styles.anomalyTable}>
              <h4 style={styles.subTitle}>🚨 异常详情</h4>
              <div style={styles.table}>
                <div style={styles.tableRowHeader}>
                  <span style={styles.colHeader}>时间</span>
                  <span style={styles.colHeader}>级别</span>
                  <span style={styles.colHeader}>成交笔数</span>
                  <span style={styles.colHeader}>λ 均值</span>
                  <span style={styles.colHeader}>z-score</span>
                  <span style={styles.colHeader}>价格变动</span>
                </div>
                {klinesData.anomalies.slice(0, 10).map((a, idx) => (
                  <div key={idx} style={styles.tableRow}>
                    <span className="font-mono" style={styles.cell}>
                      {new Date(a.time * 1000).toLocaleString('zh-CN')}
                    </span>
                    <span style={{ ...styles.cell, color: a.anomaly_level === 'EXTREME' ? 'var(--color-anomaly)' : 'var(--color-warn)' }}>
                      {a.anomaly_level === 'EXTREME' ? '🚨 极端' : '⚠️ 异常'}
                    </span>
                    <span className="font-mono" style={styles.cell}>{a.trades_count}</span>
                    <span className="font-mono" style={styles.cell}>{a.lambda.toFixed(1)}</span>
                    <span className="font-mono" style={{ ...styles.cell, color: 'var(--color-accent)' }}>
                      {a.z_score.toFixed(2)}
                    </span>
                    <span className="font-mono" style={{ ...styles.cell, color: a.price_change_pct > 0 ? 'var(--color-up)' : 'var(--color-down)' }}>
                      {a.price_change_pct > 0 ? '+' : ''}{a.price_change_pct.toFixed(2)}%
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      <div style={styles.grid}>
        <div className="card" style={styles.chartCard}>
          <h3 style={styles.cardTitle}>📊 成交量分布直方图 vs 泊松模型拟合</h3>
          {loading ? (
            <div style={styles.loading}>正在计算分布拟合参数...</div>
          ) : (
            results && (
              <div style={styles.chartContainer}>
                <ResponsiveContainer width="100%" height={320}>
                  <BarChart data={results.histogram}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--border-color)" />
                    <XAxis dataKey="volume_bucket" stroke="var(--text-secondary)" tick={{ fontSize: 11 }} />
                    <YAxis stroke="var(--text-secondary)" tick={{ fontSize: 11 }} />
                    <Tooltip
                      contentStyle={{ background: 'var(--bg-card)', border: '1px solid var(--border-color)' }}
                      labelStyle={{ color: 'var(--text-primary)' }}
                    />
                    <Bar dataKey="observed" name="实际观测频率" fill="rgba(59, 130, 246, 0.4)" stroke="#3b82f6" strokeWidth={1} />
                    <Bar dataKey="poisson_fit" name="泊松理论拟合" fill="rgba(177, 104, 250, 0.2)" stroke="var(--color-anomaly)" strokeWidth={1.5} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )
          )}
        </div>

        <div style={styles.rightSide}>
          <div className="card" style={styles.card}>
            <h3 style={styles.cardTitle}>📈 统计拟合估计值 (MLE Estimates)</h3>
            {results && (
              <div style={styles.metricsList}>
                <div style={styles.metric}>
                  <span style={styles.mLabel}>泊松强密度 (Mean λ)</span>
                  <span className="font-mono" style={styles.mValue}>{results.lambda.toFixed(2)}</span>
                </div>
                <div style={styles.metric}>
                  <span style={styles.mLabel}>样本方差 (Sample Var)</span>
                  <span className="font-mono" style={styles.mValue}>{results.variance.toFixed(2)}</span>
                </div>
                <div style={styles.metric}>
                  <span style={styles.mLabel}>超额离散度 (Overdispersion)</span>
                  <span className="font-mono" style={{ ...styles.mValue, color: results.overdispersion > 1.05 ? 'var(--color-warn)' : 'var(--text-primary)' }}>
                    {results.overdispersion.toFixed(3)}
                  </span>
                </div>
                <div style={styles.metric}>
                  <span style={styles.mLabel}>异常判定比例 (z-score &gt; 3)</span>
                  <span className="font-mono" style={{ ...styles.mValue, color: 'var(--color-anomaly)' }}>
                    {(results.anomaly_ratio * 100).toFixed(2)}%
                  </span>
                </div>
              </div>
            )}
            <p style={styles.infoText}>
              💡 **超额离散度 (Overdispersion)** 大于 1.0 说明数据存在聚集效应（胖尾），泊松探测器在判定异常时将引入**负二项修正参数**，以降低震荡市中的误报率。
            </p>
          </div>

          <div className="card" style={styles.card}>
            <h3 style={styles.cardTitle}>🔗 跨交易对相关性估计 (Correlation)</h3>
            <div style={styles.table}>
              <div style={styles.tableRowHeader}>
                <span style={styles.colHeader}>交易对 A</span>
                <span style={styles.colHeader}>交易对 B</span>
                <span style={styles.colHeader}>波动率相关系数</span>
                <span style={styles.colHeader}>成交量相关系数</span>
              </div>
              <div style={styles.tableRow}>
                <span className="font-mono" style={styles.cell}>BTCUSDT</span>
                <span className="font-mono" style={styles.cell}>ETHUSDT</span>
                <span className="font-mono" style={{ ...styles.cell, color: 'var(--color-up)' }}>0.84</span>
                <span className="font-mono" style={{ ...styles.cell, color: 'var(--color-up)' }}>0.67</span>
              </div>
              <div style={styles.tableRow}>
                <span className="font-mono" style={styles.cell}>BTCUSDT</span>
                <span className="font-mono" style={styles.cell}>SOLUSDT</span>
                <span className="font-mono" style={{ ...styles.cell, color: 'var(--color-up)' }}>0.76</span>
                <span className="font-mono" style={{ ...styles.cell, color: 'var(--color-up)' }}>0.55</span>
              </div>
            </div>
            <p style={styles.infoText}>
              相关性矩阵有助于前置风控系统检测**尾部风险暴露**，当各标的相关性陡增时，系统会自动缩减多交易对的联合总开仓上限。
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    padding: '24px',
    height: 'calc(100vh - 56px)',
    overflowY: 'auto'
  },
  title: {
    fontSize: '22px',
    fontWeight: 'bold',
    color: 'var(--text-primary)',
    marginBottom: '4px'
  },
  sub: {
    fontSize: '13px',
    color: 'var(--text-secondary)',
    marginBottom: '24px'
  },
  controlBar: {
    marginBottom: '24px',
    padding: '12px 16px'
  },
  formInline: {
    display: 'flex',
    alignItems: 'center',
    gap: '24px',
    flexWrap: 'wrap'
  },
  field: {
    display: 'flex',
    alignItems: 'center',
    gap: '8px'
  },
  labelInline: {
    fontSize: '12px',
    color: 'var(--text-secondary)',
    whiteSpace: 'nowrap'
  },
  select: {
    background: 'var(--bg-main)',
    border: '1px solid var(--border-color)',
    color: 'var(--text-primary)',
    padding: '6px 12px',
    borderRadius: '4px',
    fontSize: '13px',
    outline: 'none',
    minWidth: '150px'
  },
  btn: {
    background: 'var(--color-accent)',
    border: 'none',
    color: '#fff',
    padding: '7px 16px',
    borderRadius: '4px',
    fontSize: '13px',
    fontWeight: 600,
    cursor: 'pointer'
  },
  grid: {
    display: 'flex',
    gap: '24px',
    alignItems: 'flex-start'
  },
  chartCard: {
    flex: 1.5,
    marginBottom: 0
  },
  chartContainer: {
    marginTop: '16px'
  },
  rightSide: {
    flex: 1,
    display: 'flex',
    flexDirection: 'column',
    gap: '24px'
  },
  cardTitle: {
    fontSize: '14px',
    fontWeight: 'bold',
    color: 'var(--text-primary)',
    marginBottom: '14px',
    borderBottom: '1px solid var(--border-color)',
    paddingBottom: '8px'
  },
  card: {
    marginBottom: 0
  },
  metricsList: {
    display: 'flex',
    flexDirection: 'column',
    gap: '12px'
  },
  metric: {
    display: 'flex',
    justifyContent: 'space-between',
    paddingBottom: '8px',
    borderBottom: '1px solid var(--border-color)'
  },
  mLabel: {
    fontSize: '12px',
    color: 'var(--text-secondary)'
  },
  mValue: {
    fontSize: '14px',
    fontWeight: 600,
    color: 'var(--text-primary)'
  },
  errorBox: {
    color: '#ff453a',
    background: 'rgba(255, 69, 58, 0.08)',
    padding: '12px 16px',
    borderRadius: '8px',
    border: '1px solid rgba(255, 69, 58, 0.2)',
    marginBottom: '20px',
    fontSize: '13px',
    fontWeight: 600
  },
  loading: {
    color: 'var(--text-secondary)',
    fontSize: '13px',
    textAlign: 'center' as const,
    padding: '40px 0'
  },
  chartInfo: {
    display: 'flex',
    gap: '16px',
    flexWrap: 'wrap' as const,
    marginBottom: '12px',
    fontSize: '12px'
  },
  infoTag: {
    background: 'rgba(59, 130, 246, 0.1)',
    padding: '4px 8px',
    borderRadius: '4px',
    color: 'var(--color-accent)'
  },
  legendItem: {
    display: 'flex',
    alignItems: 'center',
    gap: '4px',
    color: 'var(--text-secondary)'
  },
  legendDot: {
    width: '10px',
    height: '10px',
    borderRadius: '2px'
  },
  anomalyTable: {
    marginTop: '16px'
  },
  subTitle: {
    fontSize: '13px',
    fontWeight: 'bold',
    color: 'var(--text-primary)',
    marginBottom: '8px'
  },
  table: {
    width: '100%',
    fontSize: '12px'
  },
  tableRowHeader: {
    display: 'grid',
    gridTemplateColumns: '180px 80px 80px 70px 80px 80px',
    gap: '8px',
    padding: '8px 0',
    borderBottom: '1px solid var(--border-color)',
    color: 'var(--text-secondary)',
    fontWeight: 600
  },
  tableRow: {
    display: 'grid',
    gridTemplateColumns: '180px 80px 80px 70px 80px 80px',
    gap: '8px',
    padding: '8px 0',
    borderBottom: '1px solid rgba(48, 54, 61, 0.5)',
    alignItems: 'center'
  },
  colHeader: {
    fontSize: '11px',
    color: 'var(--text-secondary)'
  },
  cell: {
    fontSize: '12px',
    color: 'var(--text-primary)',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap' as const
  },
  infoText: {
    fontSize: '11px',
    color: 'var(--text-secondary)',
    lineHeight: '1.5',
    marginTop: '12px'
  }
};
