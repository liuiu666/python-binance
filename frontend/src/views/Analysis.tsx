/**
 * 量价探索数据分析面板 (Market EDA)
 */
import { useState, useEffect } from 'react';
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';

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

export default function Analysis() {
  const [symbol, setSymbol] = useState('BTCUSDT');
  const [timeframe, setTimeframe] = useState('1m');
  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState<AnalysisResults | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

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

  useEffect(() => {
    runAnalysis();
  }, [symbol, timeframe]);

  return (
    <div style={styles.container}>
      <h2 style={styles.title}>🧠 量化数据分析与模型拟合</h2>
      <p style={styles.sub}>通过时序数据库 ClickHouse 行情分析成交量分布，拟合泊松异常探测参数</p>

      {/* 控制条 */}
      <div className="card" style={styles.controlBar}>
        <div style={styles.formInline}>
          <div style={styles.field}>
            <label style={styles.labelInline}>分析标的</label>
            <select value={symbol} onChange={(e) => setSymbol(e.target.value)} style={styles.select}>
              <option value="BTCUSDT">BTCUSDT (币安合约)</option>
              <option value="ETHUSDT">ETHUSDT (币安合约)</option>
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
          <button onClick={runAnalysis} disabled={loading} style={styles.btn}>
            {loading ? '运行分析中...' : '⚡ 重新载入数据分析'}
          </button>
        </div>
      </div>

      {errorMsg && (
        <div style={{
          color: '#ff453a',
          background: 'rgba(255, 69, 58, 0.08)',
          padding: '12px 16px',
          borderRadius: '8px',
          border: '1px solid rgba(255, 69, 58, 0.2)',
          marginBottom: '20px',
          fontSize: '13px',
          fontWeight: 600
        }}>
          ⚠️ {errorMsg}
        </div>
      )}

      <div style={styles.grid}>
        {/* 左栏：直方图与分布 */}
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

        {/* 右栏：统计结果与量能判定 */}
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
  infoText: {
    fontSize: '11px',
    color: 'var(--text-secondary)',
    lineHeight: '1.6',
    marginTop: '12px'
  },
  table: {
    display: 'flex',
    flexDirection: 'column',
    gap: '8px'
  },
  tableRowHeader: {
    display: 'grid',
    gridTemplateColumns: '1.2fr 1.2fr 1.5fr 1.5fr',
    borderBottom: '1px solid var(--border-color)',
    paddingBottom: '6px',
    fontSize: '11px',
    color: 'var(--text-secondary)',
    fontWeight: 600
  },
  tableRow: {
    display: 'grid',
    gridTemplateColumns: '1.2fr 1.2fr 1.5fr 1.5fr',
    paddingBottom: '6px',
    borderBottom: '1px dashed var(--border-color)',
    fontSize: '13px'
  },
  colHeader: {
    textAlign: 'left'
  },
  cell: {
    textAlign: 'left'
  },
  loading: {
    textAlign: 'center',
    padding: '40px 0',
    color: 'var(--text-secondary)',
    fontSize: '13px'
  }
};
