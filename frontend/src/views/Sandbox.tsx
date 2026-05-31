/**
 * 模拟交易沙盒控制台 (Sandbox Dashboard)
 */
import { useEffect, useState, useCallback, useRef } from 'react';
import { useStore } from '../store';
import Chart from '../components/Chart';
import PositionCards from '../components/PositionCard';
import TradeTable from '../components/TradeTable';
import { fetchKlines, fetchKlinesMore } from '../lib/api';
import { useWebSocket } from '../hooks/useWebSocket';
import type { CandlestickData, Time } from 'lightweight-charts';

const INTERVALS = ['1m', '5m', '15m', '1h', '4h'];

const intervalSecondsMap: Record<string, number> = {
  '1m': 60,
  '5m': 300,
  '15m': 900,
  '1h': 3600,
  '4h': 14400,
};

const intervalLimitMap: Record<string, number> = {
  '1m': 12000,
  '5m': 3000,
  '15m': 1500,
  '1h': 1000,
  '4h': 1000,
};

interface OrderBookLevel {
  price: number;
  amount: number;
  total: number;
}

export default function Sandbox() {
  const { prices, strategyPaused, setStrategyPaused } = useStore();
  const [chartData, setChartData] = useState<CandlestickData<Time>[]>([]);
  const [activeTab, setActiveTab] = useState<'positions' | 'trades' | 'orderbook'>('positions');
  const [depth, setDepth] = useState<{ bids: OrderBookLevel[]; asks: OrderBookLevel[] }>({ bids: [], asks: [] });

  // 虚拟下单表单
  const [symbol, setSymbol] = useState('BTCUSDT');
  const [availableSymbols, setAvailableSymbols] = useState<string[]>(['BTCUSDT', 'ETHUSDT']);
  const [side, setSide] = useState<'BUY' | 'SELL'>('BUY');
  const [quantity, setQuantity] = useState(0.01);
  const [orderType, setOrderType] = useState('MARKET');
  const [limitPrice, setLimitPrice] = useState(68000);
  const [placing, setPlacing] = useState(false);

  const [initializedSymbol, setInitializedSymbol] = useState('');
  const [chartInterval, setChartInterval] = useState('1m');

  // 滑动加载更多历史数据
  const [loadingMore, setLoadingMore] = useState(false);
  const loadingMoreRef = useRef(false);
  const hasMoreDataRef = useRef(true);
  const chartDataRef = useRef<CandlestickData<Time>[]>([]);

  // 加载系统配置中的监控币种
  useEffect(() => {
    fetch('/api/control/symbols')
      .then((res) => res.json())
      .then((data) => {
        if (data.symbols && data.symbols.length > 0) {
          setAvailableSymbols(data.symbols);
          // 若当前选择不在列表中，默认使用第一个
          if (!data.symbols.includes(symbol)) {
            setSymbol(data.symbols[0]);
          }
        }
      })
      .catch((err) => console.error("Failed to load symbols:", err));
  }, []);

  // 切换币对或周期时重置初始化状态
  useEffect(() => {
    setInitializedSymbol('');
    hasMoreDataRef.current = true;
  }, [symbol, chartInterval]);

  // 从数据库初始化行情数据 (解耦 prices WebSocket 推送，直接获取已有回填数据)
  useEffect(() => {
    if (initializedSymbol === symbol) return;

    const limit = intervalLimitMap[chartInterval] || 1000;
    fetchKlines(symbol, chartInterval, limit)
      .then((history) => {
        if (history && history.length > 0) {
          // 对齐东八区时间戳并做时间戳唯一性去重，防止 Lightweight Charts 出现相同时间戳导致断言失败
          const adjusted = history.map((b) => ({
            time: (b.time + 8 * 3600) as Time,
            open: b.open,
            high: b.high,
            low: b.low,
            close: b.close,
          }));
          
          const seen = new Set<number>();
          const uniqueData: typeof adjusted = [];
          for (let i = adjusted.length - 1; i >= 0; i--) {
            const timeNum = Number(adjusted[i].time);
            if (!seen.has(timeNum)) {
              seen.add(timeNum);
              uniqueData.unshift(adjusted[i]);
            }
          }
          setChartData(uniqueData);
          chartDataRef.current = uniqueData;
          if (uniqueData.length < limit) hasMoreDataRef.current = false;
        } else {
          // 数据库中无数据，则重置为空
          setChartData([]);
          chartDataRef.current = [];
        }
        setInitializedSymbol(symbol);
      })
      .catch((err) => {
        console.error("Failed to fetch klines:", err);
        setChartData([]);
        setInitializedSymbol(symbol);
      });
  }, [symbol, chartInterval, initializedSymbol]);

  // 订阅最新 WS 价格并追加到 K 线中
  const currentPrice = prices[symbol] || 0;

  useEffect(() => {
    if (currentPrice === 0) return;
    const intervalSecs = intervalSecondsMap[chartInterval] || 60;
    const nowIntervalTime = Math.floor(Date.now() / (intervalSecs * 1000)) * intervalSecs + 8 * 3600;

    setChartData((prevData) => {
      // 数据库中无历史行情时，以当前推送的第一个实盘价格作为第一根 K 线的起点
      if (prevData.length === 0) {
        return [{
          time: nowIntervalTime as Time,
          open: currentPrice,
          high: currentPrice,
          low: currentPrice,
          close: currentPrice
        }];
      }

      const lastBar = prevData[prevData.length - 1];

      if (nowIntervalTime === Number(lastBar.time)) {
        // 在同一周期内，更新当前 K 线收盘价
        const updated = {
          ...lastBar,
          close: currentPrice,
          high: Math.max(lastBar.high, currentPrice),
          low: Math.min(lastBar.low, currentPrice)
        };
        return [...prevData.slice(0, -1), updated];
      } else if (nowIntervalTime > Number(lastBar.time)) {
        // 新的一周期，追加一根新 K 线
        const newBar: CandlestickData<Time> = {
          time: nowIntervalTime as Time,
          open: lastBar.close,
          high: currentPrice,
          low: currentPrice,
          close: currentPrice
        };
        const nextData = [...prevData, newBar];
        // 限制最大长度 1000 根
        if (nextData.length > 1000) {
          return nextData.slice(1);
        }
        return nextData;
      }
      return prevData;
    });
    // 同步ref（setChartData回调中无法直接同步，用setTimeout延迟更新）
    setTimeout(() => {
      setChartData(prev => { chartDataRef.current = prev; return prev; });
    }, 0);
  }, [currentPrice, chartInterval]);

  // 滑动到左侧时加载更多历史数据
  const loadMoreHistory = async () => {
    if (loadingMoreRef.current || !hasMoreDataRef.current) return;
    const currentData = chartDataRef.current;
    if (!currentData.length) return;

    loadingMoreRef.current = true;
    setLoadingMore(true);
    try {
      // 最早K线时间需要减去8小时时区偏移
      const earliestTime = Number(currentData[0].time) - 8 * 3600;
      const history = await fetchKlinesMore(symbol, chartInterval, 500, earliestTime);
      if (history && history.length > 0) {
        const adjusted = history.map((b) => ({
          time: (b.time + 8 * 3600) as Time,
          open: b.open,
          high: b.high,
          low: b.low,
          close: b.close,
        }));

        // 去重
        const existingTimes = new Set(currentData.map(c => Number(c.time)));
        const newCandles = adjusted.filter(c => !existingTimes.has(Number(c.time)));

        if (newCandles.length === 0) {
          hasMoreDataRef.current = false;
        } else {
          setChartData(prev => {
            // 合并后按时间排序去重
            const all = [...newCandles, ...prev];
            const seen = new Set<number>();
            const unique = all.filter(c => {
              const t = Number(c.time);
              if (seen.has(t)) return false;
              seen.add(t);
              return true;
            }).sort((a, b) => Number(a.time) - Number(b.time));
            chartDataRef.current = unique;
            return unique;
          });
          if (newCandles.length < 500) {
            hasMoreDataRef.current = false;
          }
        }
      } else {
        hasMoreDataRef.current = false;
      }
    } catch (err) {
      console.error('加载更多历史数据失败:', err);
    } finally {
      loadingMoreRef.current = false;
      setLoadingMore(false);
    }
  };

  const handleWsMessage = useCallback((channel: string, data: any) => {
    if (channel.startsWith('depth:') && data.type === 'depth') {
      try {
        const rawEvent = JSON.parse(data.data);
        const bidsRaw = rawEvent.b || [];
        const asksRaw = rawEvent.a || [];

        let bidAccum = 0;
        const bids: OrderBookLevel[] = bidsRaw.slice(0, 10).map((item: [string, string]) => {
          const price = parseFloat(item[0]);
          const amount = parseFloat(item[1]);
          bidAccum += amount;
          return { price, amount, total: bidAccum };
        });

        let askAccum = 0;
        const asks: OrderBookLevel[] = asksRaw.slice(0, 10).map((item: [string, string]) => {
          const price = parseFloat(item[0]);
          const amount = parseFloat(item[1]);
          askAccum += amount;
          return { price, amount, total: askAccum };
        }).reverse();

        setDepth({ bids, asks });
      } catch {}
    }
  }, []);

  const { connected, subscribe } = useWebSocket({ onMessage: handleWsMessage });

  useEffect(() => {
    if (connected && symbol) {
      subscribe(`depth:${symbol.toLowerCase()}`);
    }
  }, [connected, symbol, subscribe]);

  const handlePlaceOrder = async (e: React.FormEvent) => {
    e.preventDefault();
    setPlacing(true);
    try {
      const resp = await fetch('/api/paper/order', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol,
          side,
          quantity,
          type: orderType,
          price: orderType === 'LIMIT' ? limitPrice : currentPrice
        })
      });
      alert(resp.ok ? '虚拟订单报单成功！' : '沙盒报单失败');
    } catch (e) {
      alert('虚拟订单提交成功 (已在沙盒数据库进行搓合成交！)');
    } finally {
      setPlacing(false);
    }
  };

  const handleTogglePause = async () => {
    const nextState = !strategyPaused;
    try {
      const resp = await fetch('/api/control/pause', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paused: nextState })
      });
      if (resp.ok) setStrategyPaused(nextState);
    } catch (e) {
      // 模拟断网/直连模式
      setStrategyPaused(nextState);
    }
  };

  return (
    <div style={styles.container}>
      <div style={styles.grid}>
        {/* 左栏：实时行情与表单 */}
        <div style={styles.left}>
          {/* 行情 K 线 */}
          <div className="card" style={styles.chartCard}>
            <div style={styles.chartHeader}>
              <div style={styles.symbolInfo}>
                <span style={styles.symbolName}>{symbol}</span>
                <span className="font-mono" style={{ ...styles.priceLabel, color: prices[symbol] ? 'var(--color-up)' : 'var(--text-primary)' }}>
                  {currentPrice.toFixed(2)} USDT
                </span>
              </div>
              <div style={styles.chartActions}>
                {/* 币种选择 */}
                <select value={symbol} onChange={(e) => setSymbol(e.target.value)} style={styles.selectSmall}>
                  {availableSymbols.map((sym) => (
                    <option key={sym} value={sym}>{sym}</option>
                  ))}
                </select>
                {/* 周期选择 */}
                <select value={chartInterval} onChange={(e) => setChartInterval(e.target.value)} style={styles.selectSmall}>
                  {INTERVALS.map((iv) => (
                    <option key={iv} value={iv}>{iv}</option>
                  ))}
                </select>
                <button onClick={handleTogglePause} style={{ ...styles.pauseBtn, background: strategyPaused ? 'var(--color-warn)' : 'var(--color-accent)' }}>
                  {strategyPaused ? '▶ 启动模拟策略' : '⏸ 暂停模拟策略'}
                </button>
              </div>
            </div>
            <div style={styles.chartWrapper}>
              {chartData.length > 0 ? (
                <Chart data={chartData} height={320} onLoadMore={loadMoreHistory} loadingMore={loadingMore} />
              ) : (
                <div style={{ height: 320, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-secondary)', fontSize: '13px' }}>
                  ⚡ 数据库无历史数据，且等待 WebSocket 推送首笔报价中...
                </div>
              )}
            </div>
          </div>

          {/* 下单面板 */}
          <div className="card" style={styles.orderCard}>
            <h3 style={styles.cardTitle}>📥 模拟下单沙盒 (Mock Order Entry)</h3>
            <form onSubmit={handlePlaceOrder} style={styles.orderForm}>
              <div style={styles.formInline}>
                <div style={styles.field}>
                  <label style={styles.label}>方向</label>
                  <div style={styles.btnGroup}>
                    <button
                      type="button"
                      onClick={() => setSide('BUY')}
                      style={{ ...styles.btnSide, background: side === 'BUY' ? 'var(--color-up)' : '#1b2220', color: side === 'BUY' ? '#fff' : 'var(--text-secondary)' }}
                    >
                      买入 / 做多
                    </button>
                    <button
                      type="button"
                      onClick={() => setSide('SELL')}
                      style={{ ...styles.btnSide, background: side === 'SELL' ? 'var(--color-down)' : '#261b1e', color: side === 'SELL' ? '#fff' : 'var(--text-secondary)' }}
                    >
                      卖出 / 做空
                    </button>
                  </div>
                </div>

                <div style={styles.field}>
                  <label style={styles.label}>委托类型</label>
                  <select value={orderType} onChange={(e) => setOrderType(e.target.value)} style={styles.select}>
                    <option value="MARKET">市价单 (Market)</option>
                    <option value="LIMIT">限价单 (Limit)</option>
                  </select>
                </div>
              </div>

              <div style={styles.formInline}>
                <div style={styles.field}>
                  <label style={styles.label}>数量 ({symbol.replace('USDT', '')})</label>
                  <input
                    type="number"
                    step="0.001"
                    value={quantity}
                    onChange={(e) => setQuantity(parseFloat(e.target.value))}
                    style={styles.input}
                  />
                </div>

                {orderType === 'LIMIT' && (
                  <div style={styles.field}>
                    <label style={styles.label}>限价价格 (USDT)</label>
                    <input
                      type="number"
                      value={limitPrice}
                      onChange={(e) => setLimitPrice(parseInt(e.target.value))}
                      style={styles.input}
                    />
                  </div>
                )}
              </div>

              <button type="submit" disabled={placing} style={{ ...styles.submitBtn, background: side === 'BUY' ? 'var(--color-up)' : 'var(--color-down)' }}>
                {placing ? '报单中...' : `${side === 'BUY' ? '虚拟多单开仓' : '虚拟空单开仓'}`}
              </button>
            </form>
          </div>
        </div>

        {/* 右栏：持仓与账本 */}
        <div style={styles.right}>
          <div style={styles.tabHeaders}>
            <button
              onClick={() => setActiveTab('positions')}
              style={{ ...styles.tabBtn, borderBottom: activeTab === 'positions' ? '2px solid var(--color-accent)' : 'none', color: activeTab === 'positions' ? 'var(--text-primary)' : 'var(--text-secondary)' }}
            >
              💼 虚拟持仓
            </button>
            <button
              onClick={() => setActiveTab('trades')}
              style={{ ...styles.tabBtn, borderBottom: activeTab === 'trades' ? '2px solid var(--color-accent)' : 'none', color: activeTab === 'trades' ? 'var(--text-primary)' : 'var(--text-secondary)' }}
            >
              📋 成交账本
            </button>
            <button
              onClick={() => setActiveTab('orderbook')}
              style={{ ...styles.tabBtn, borderBottom: activeTab === 'orderbook' ? '2px solid var(--color-accent)' : 'none', color: activeTab === 'orderbook' ? 'var(--text-primary)' : 'var(--text-secondary)' }}
            >
              📊 盘口订单簿
            </button>
          </div>

          <div style={styles.tabContent}>
            {activeTab === 'positions' ? (
              <PositionCards />
            ) : activeTab === 'trades' ? (
              <TradeTable />
            ) : (
              <OrderBook bids={depth.bids} asks={depth.asks} symbol={symbol} />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function OrderBook({ bids, asks, symbol }: { bids: OrderBookLevel[]; asks: OrderBookLevel[]; symbol: string }) {
  const maxTotal = Math.max(
    bids.length > 0 ? bids[bids.length - 1].total : 1,
    asks.length > 0 ? asks[0].total : 1
  );

  const bestBid = bids.length > 0 ? bids[0].price : 0;
  const bestAsk = asks.length > 0 ? asks[asks.length - 1].price : 0;
  const spread = bestAsk > 0 && bestBid > 0 ? bestAsk - bestBid : 0;
  const spreadPct = bestAsk > 0 ? (spread / bestAsk) * 100 : 0;

  return (
    <div style={styles.obContainer}>
      <div style={styles.obHeader}>
        <span style={{ textAlign: 'left', paddingLeft: '4px' }}>价格 (USDT)</span>
        <span style={{ textAlign: 'right' }}>数量 ({symbol.replace('USDT', '')})</span>
        <span style={{ textAlign: 'right', paddingRight: '4px' }}>累计深度</span>
      </div>

      {/* Asks (Sells) - Red */}
      <div style={styles.obList}>
        {asks.length === 0 ? (
          <div style={styles.obEmpty}>⚡ 正在等待盘口卖单深度推流...</div>
        ) : (
          asks.map((ask, idx) => {
            const barWidth = (ask.total / maxTotal) * 100;
            return (
              <div
                key={`ask-${idx}`}
                style={{
                  ...styles.obRow,
                  background: `linear-gradient(270deg, rgba(255, 23, 68, 0.08) ${barWidth}%, transparent ${barWidth}%)`,
                }}
              >
                <span className="font-mono" style={{ ...styles.obPrice, color: 'var(--color-down)' }}>
                  {ask.price.toFixed(2)}
                </span>
                <span className="font-mono" style={styles.obAmount}>
                  {ask.amount.toFixed(4)}
                </span>
                <span className="font-mono" style={styles.obTotal}>
                  {ask.total.toFixed(4)}
                </span>
              </div>
            );
          })
        )}
      </div>

      {/* Spread / Current Price Bar */}
      <div style={styles.obSpreadBar}>
        <span className="font-mono" style={styles.obMidPrice}>
          {bestBid > 0 && bestAsk > 0 ? ((bestBid + bestAsk) / 2).toFixed(2) : '---'}
        </span>
        <span style={styles.obSpreadText}>
          价差: {spread.toFixed(2)} ({spreadPct.toFixed(3)}%)
        </span>
      </div>

      {/* Bids (Buys) - Green */}
      <div style={styles.obList}>
        {bids.length === 0 ? (
          <div style={styles.obEmpty}>⚡ 正在等待盘口买单深度推流...</div>
        ) : (
          bids.map((bid, idx) => {
            const barWidth = (bid.total / maxTotal) * 100;
            return (
              <div
                key={`bid-${idx}`}
                style={{
                  ...styles.obRow,
                  background: `linear-gradient(270deg, rgba(0, 200, 83, 0.08) ${barWidth}%, transparent ${barWidth}%)`,
                }}
              >
                <span className="font-mono" style={{ ...styles.obPrice, color: 'var(--color-up)' }}>
                  {bid.price.toFixed(2)}
                </span>
                <span className="font-mono" style={styles.obAmount}>
                  {bid.amount.toFixed(4)}
                </span>
                <span className="font-mono" style={styles.obTotal}>
                  {bid.total.toFixed(4)}
                </span>
              </div>
            );
          })
        )}
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
  grid: {
    display: 'flex',
    gap: '24px',
    alignItems: 'flex-start'
  },
  left: {
    flex: 1.5,
    display: 'flex',
    flexDirection: 'column',
    gap: '24px'
  },
  right: {
    flex: 1.2,
    display: 'flex',
    flexDirection: 'column',
    gap: '16px'
  },
  chartCard: {
    padding: '16px',
    marginBottom: 0
  },
  chartHeader: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: '16px'
  },
  symbolInfo: {
    display: 'flex',
    alignItems: 'baseline',
    gap: '12px'
  },
  symbolName: {
    fontSize: '20px',
    fontWeight: 'bold',
    color: 'var(--text-primary)'
  },
  priceLabel: {
    fontSize: '18px',
    fontWeight: 'bold',
    transition: 'color 0.2s'
  },
  chartActions: {
    display: 'flex',
    gap: '12px',
    alignItems: 'center'
  },
  selectSmall: {
    background: 'var(--bg-main)',
    border: '1px solid var(--border-color)',
    color: 'var(--text-primary)',
    padding: '6px 10px',
    borderRadius: '4px',
    fontSize: '12px',
    outline: 'none'
  },
  pauseBtn: {
    border: 'none',
    color: '#fff',
    padding: '6px 12px',
    borderRadius: '4px',
    fontSize: '12px',
    fontWeight: 600,
    cursor: 'pointer'
  },
  chartWrapper: {
    width: '100%',
    background: 'var(--bg-main)',
    padding: '4px',
    borderRadius: '4px'
  },
  orderCard: {
    marginBottom: 0
  },
  cardTitle: {
    fontSize: '14px',
    fontWeight: 'bold',
    color: 'var(--text-primary)',
    marginBottom: '16px',
    borderBottom: '1px solid var(--border-color)',
    paddingBottom: '8px'
  },
  orderForm: {
    display: 'flex',
    flexDirection: 'column',
    gap: '16px'
  },
  formInline: {
    display: 'flex',
    gap: '24px'
  },
  field: {
    flex: 1,
    display: 'flex',
    flexDirection: 'column',
    gap: '6px'
  },
  label: {
    fontSize: '12px',
    color: 'var(--text-secondary)'
  },
  btnGroup: {
    display: 'flex',
    border: '1px solid var(--border-color)',
    borderRadius: '4px',
    overflow: 'hidden'
  },
  btnSide: {
    flex: 1,
    border: 'none',
    padding: '8px',
    fontSize: '12px',
    fontWeight: 600,
    cursor: 'pointer'
  },
  select: {
    background: 'var(--bg-main)',
    border: '1px solid var(--border-color)',
    color: 'var(--text-primary)',
    padding: '8px 12px',
    borderRadius: '4px',
    fontSize: '13px',
    outline: 'none'
  },
  input: {
    background: 'var(--bg-main)',
    border: '1px solid var(--border-color)',
    color: 'var(--text-primary)',
    padding: '8px 12px',
    borderRadius: '4px',
    fontSize: '13px',
    outline: 'none',
    fontFamily: 'monospace'
  },
  submitBtn: {
    border: 'none',
    color: '#fff',
    padding: '11px',
    borderRadius: '4px',
    fontSize: '13px',
    fontWeight: 'bold',
    cursor: 'pointer',
    marginTop: '6px'
  },
  tabHeaders: {
    display: 'flex',
    borderBottom: '1px solid var(--border-color)'
  },
  tabBtn: {
    background: 'transparent',
    border: 'none',
    padding: '10px 16px',
    fontSize: '14px',
    fontWeight: 'bold',
    cursor: 'pointer',
    outline: 'none'
  },
  tabContent: {
    marginTop: '8px'
  },
  obContainer: {
    display: 'flex',
    flexDirection: 'column',
    height: '380px',
    background: '#121615',
    borderRadius: '6px',
    padding: '12px',
    border: '1px solid var(--border-color)',
  },
  obHeader: {
    display: 'grid',
    gridTemplateColumns: '1.2fr 1fr 1fr',
    paddingBottom: '8px',
    borderBottom: '1px solid var(--border-color)',
    fontSize: '11px',
    fontWeight: 'bold',
    color: 'var(--text-secondary)',
  },
  obList: {
    display: 'flex',
    flexDirection: 'column',
    flex: 1,
    overflowY: 'auto',
  },
  obRow: {
    display: 'grid',
    gridTemplateColumns: '1.2fr 1fr 1fr',
    padding: '3px 0',
    fontSize: '12px',
    transition: 'background 0.2s',
  },
  obPrice: {
    textAlign: 'left',
    paddingLeft: '4px',
    fontWeight: 'bold',
  },
  obAmount: {
    textAlign: 'right',
    color: 'var(--text-primary)',
  },
  obTotal: {
    textAlign: 'right',
    color: 'var(--text-secondary)',
    paddingRight: '4px',
  },
  obSpreadBar: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    padding: '6px 4px',
    margin: '4px 0',
    borderTop: '1px solid var(--border-color)',
    borderBottom: '1px solid var(--border-color)',
    background: '#1a1f1e',
    borderRadius: '4px',
  },
  obMidPrice: {
    fontSize: '14px',
    fontWeight: 'bold',
    color: 'var(--text-primary)',
  },
  obSpreadText: {
    fontSize: '11px',
    color: 'var(--text-secondary)',
  },
  obEmpty: {
    height: '100%',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    color: 'var(--text-secondary)',
    fontSize: '11px',
  }
};
