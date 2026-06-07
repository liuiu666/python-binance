/**
 * 高频实时订单薄模拟在线交易控制台 (HFT Live Paper Trading Studio)
 */
import { useEffect, useState, useCallback, useRef, useMemo } from 'react';
import { useStore } from '../store';
import { useWebSocket } from '../hooks/useWebSocket';
import Chart from '../components/Chart';
import { fetchKlines, fetchKlinesMore } from '../lib/api';
import type { CandlestickData, Time } from 'lightweight-charts';

interface OrderBookLevel {
  price: number;
  amount: number;
  total: number;
}

interface TradeLog {
  id: string;
  timestamp: string;
  time?: number; // UNIX 毫秒或秒级时间戳，用于 K 线标记对齐
  symbol: string;
  side: 'BUY' | 'SELL';
  action: 'OPEN' | 'CLOSE';
  type: 'MARKET' | 'LIMIT';
  price: number;
  qty: number;
  fee: number;
  pnl: number;
}

interface LimitOrder {
  id: string;
  symbol: string;
  side: 'BUY' | 'SELL';
  price: number;
  qty: number;
  timestamp: string;
}

interface Position {
  symbol: string;
  side: 'LONG' | 'SHORT';
  size: number; // positive float
  entryPrice: number;
  peakPrice?: number; // 记录开仓后的最高价（多仓）或最低价（空仓）
}

const intervalSecondsMap: Record<string, number> = {
  '1m': 60,
  '5m': 300,
  '15m': 900,
  '1h': 3600,
  '4h': 14400,
};

const intervalLimitMap: Record<string, number> = {
  '1m': 500,
  '5m': 500,
  '15m': 500,
  '1h': 500,
  '4h': 500,
};

export default function HFSandbox() {
  const { updatePrice } = useStore();
  const [symbol, setSymbol] = useState(() => localStorage.getItem('hft_symbol') || 'BTCUSDT');
  const [currentPrice, setCurrentPrice] = useState<number>(0);
  const lastPriceRef = useRef<number>(0);
  const [availableSymbols, setAvailableSymbols] = useState<string[]>(['BTCUSDT', 'ETHUSDT']);
  const [chartInterval, setChartInterval] = useState('1m');
  const [chartData, setChartData] = useState<CandlestickData<Time>[]>([]);
  const [depth, setDepth] = useState<{ bids: OrderBookLevel[]; asks: OrderBookLevel[] }>({ bids: [], asks: [] });
  const [activeTab, setActiveTab] = useState<'position' | 'orders' | 'history'>('position');

  // 模拟交易账户状态 (从 localStorage 恢复)
  const [balance, setBalance] = useState<number>(() => {
    const val = localStorage.getItem('hft_balance');
    return val ? parseFloat(val) : 100000; // 默认 10 万刀
  });
  const [position, setPosition] = useState<Position | null>(() => {
    const val = localStorage.getItem('hft_position');
    return val ? JSON.parse(val) : null;
  });
  const [limitOrders, setLimitOrders] = useState<LimitOrder[]>(() => {
    const val = localStorage.getItem('hft_limit_orders');
    return val ? JSON.parse(val) : [];
  });
  const [tradeLogs, setTradeLogs] = useState<TradeLog[]>(() => {
    const val = localStorage.getItem('hft_trade_logs');
    return val ? JSON.parse(val) : [];
  });

  // 优化性能：使用 buffer 节流盘口更新，防止前端每秒重绘几十次卡死
  const depthBufferRef = useRef<{ bids: OrderBookLevel[]; asks: OrderBookLevel[] }>({ bids: [], asks: [] });
  
  useEffect(() => {
    const timer = setInterval(() => {
      if (depthBufferRef.current.bids.length > 0) {
        setDepth(depthBufferRef.current);
      }
    }, 200); // 限制每秒最多刷新 5 次界面
    return () => clearInterval(timer);
  }, []);

  // 切换币种时重置 buffer 与价格
  useEffect(() => {
    setDepth({ bids: [], asks: [] });
    depthBufferRef.current = { bids: [], asks: [] };
    lastPriceRef.current = 0;
    setCurrentPrice(0);
  }, [symbol]);

  // 自动交易策略状态 (从 localStorage 恢复，防止刷新页面被重置)
  const [autoStrategy, setAutoStrategy] = useState<'NONE' | 'IMBALANCE' | 'MICROPRICE' | 'SCALPING'>(() => {
    return (localStorage.getItem('hft_auto_strategy') as any) || 'NONE';
  });
  const [autoQty, setAutoQty] = useState<string>(() => {
    return localStorage.getItem('hft_auto_qty') || '1';
  });
  const [autoInterval, setAutoInterval] = useState<number>(() => {
    const val = localStorage.getItem('hft_auto_interval');
    return val ? parseInt(val) : 3;
  });
  const [tpPercent, setTpPercent] = useState<number>(() => {
    const val = localStorage.getItem('hft_tp_percent');
    return val ? parseFloat(val) : 0.8;
  });
  const [slPercent, setSlPercent] = useState<number>(() => {
    const val = localStorage.getItem('hft_sl_percent');
    return val ? parseFloat(val) : 0.4;
  });
  const [tsActivation, setTsActivation] = useState<number>(() => {
    const val = localStorage.getItem('hft_ts_activation');
    return val ? parseFloat(val) : 0.6;
  });
  const [tsCallback, setTsCallback] = useState<number>(() => {
    const val = localStorage.getItem('hft_ts_callback');
    return val ? parseFloat(val) : 0.2;
  });

  // 交易面板表单
  const [orderType, setOrderType] = useState<'MARKET' | 'LIMIT'>('MARKET');
  const [tradeSide, setTradeSide] = useState<'BUY' | 'SELL'>('BUY');
  const [quantity, setQuantity] = useState<string>('0.1');
  const [limitPrice, setLimitPrice] = useState<string>('68000');
  const [takerFee, setTakerFee] = useState<number>(0.04); // 0.04%
  const [makerFee, setMakerFee] = useState<number>(0.02); // 0.02%

  const [loadingMore, setLoadingMore] = useState(false);
  const loadingMoreRef = useRef(false);
  const hasMoreDataRef = useRef(true);
  const chartDataRef = useRef<CandlestickData<Time>[]>([]);

  const hasLoadedFromBackendRef = useRef<boolean>(false);
  const saveStateTimeoutRef = useRef<any>(null);

  // 从后台获取沙盒状态并合并
  useEffect(() => {
    fetch('/api/paper/sandbox/state')
      .then((res) => res.json())
      .then((data) => {
        if (data.status === 'success' && data.state) {
          const s = data.state;
          if (typeof s.balance === 'number') setBalance(s.balance);
          if (s.position !== undefined) setPosition(s.position);
          if (Array.isArray(s.limitOrders)) setLimitOrders(s.limitOrders);
          if (Array.isArray(s.tradeLogs)) setTradeLogs(s.tradeLogs);
          if (s.autoStrategy) setAutoStrategy(s.autoStrategy);
          if (s.autoQty) setAutoQty(s.autoQty);
          if (typeof s.autoInterval === 'number') setAutoInterval(s.autoInterval);
          if (typeof s.tpPercent === 'number') setTpPercent(s.tpPercent);
          if (typeof s.slPercent === 'number') setSlPercent(s.slPercent);
          if (typeof s.tsActivation === 'number') setTsActivation(s.tsActivation);
          if (typeof s.tsCallback === 'number') setTsCallback(s.tsCallback);
        }
      })
      .catch((err) => console.error('从后台加载沙盒状态失败:', err))
      .finally(() => {
        hasLoadedFromBackendRef.current = true;
      });
  }, []);

  const saveSandboxStateToBackend = useCallback((currentState: Record<string, any>) => {
    if (saveStateTimeoutRef.current) {
      clearTimeout(saveStateTimeoutRef.current);
    }
    saveStateTimeoutRef.current = setTimeout(() => {
      fetch('/api/paper/sandbox/state', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ state: currentState })
      }).catch((err) => console.error('保存沙盒状态到后台失败:', err));
    }, 1000); // 延迟 1 秒保存，防抖合并高频成交
  }, []);

  // 仅在策略配置改变时，才同步保存至后台 PostgreSQL (过滤交易成交产生的高频数据流，解决无效循环调用问题)
  useEffect(() => {
    if (hasLoadedFromBackendRef.current) {
      const configObj = {
        autoStrategy,
        autoQty,
        autoInterval,
        tpPercent,
        slPercent,
        tsActivation,
        tsCallback,
        symbol
      };
      saveSandboxStateToBackend(configObj);
    }
  }, [
    autoStrategy,
    autoQty,
    autoInterval,
    tpPercent,
    slPercent,
    tsActivation,
    tsCallback,
    symbol,
    saveSandboxStateToBackend
  ]);

  // 同步本地 localStorage (即时本地响应与降级，对性能与网络无任何影响)
  useEffect(() => {
    if (hasLoadedFromBackendRef.current) {
      localStorage.setItem('hft_balance', balance.toString());
      localStorage.setItem('hft_position', position ? JSON.stringify(position) : '');
      localStorage.setItem('hft_limit_orders', JSON.stringify(limitOrders));
      localStorage.setItem('hft_trade_logs', JSON.stringify(tradeLogs));
      localStorage.setItem('hft_auto_strategy', autoStrategy);
      localStorage.setItem('hft_auto_qty', autoQty);
      localStorage.setItem('hft_auto_interval', autoInterval.toString());
      localStorage.setItem('hft_tp_percent', tpPercent.toString());
      localStorage.setItem('hft_sl_percent', slPercent.toString());
      localStorage.setItem('hft_ts_activation', tsActivation.toString());
      localStorage.setItem('hft_ts_callback', tsCallback.toString());
      localStorage.setItem('hft_symbol', symbol);
    }
  }, [
    balance,
    position,
    limitOrders,
    tradeLogs,
    autoStrategy,
    autoQty,
    autoInterval,
    tpPercent,
    slPercent,
    tsActivation,
    tsCallback,
    symbol
  ]);

  // 加载配置可用币种
  useEffect(() => {
    fetch('/api/control/symbols')
      .then((res) => res.json())
      .then((data) => {
        if (data.symbols && data.symbols.length > 0) {
          setAvailableSymbols(data.symbols);
          if (!data.symbols.includes(symbol)) {
            setSymbol(data.symbols[0]);
          }
        }
      })
      .catch((err) => console.error("加载可用币种失败:", err));
  }, []);

  // 获取 K 线历史数据 (支持防抖与过期异步响应拦截)
  useEffect(() => {
    const limit = intervalLimitMap[chartInterval] || 500;
    let active = true;
    hasMoreDataRef.current = true;

    // 每次切换时先清空老 K 线，防止切币时老 K 线残留闪烁
    setChartData([]);
    chartDataRef.current = [];

    fetchKlines(symbol, chartInterval, limit)
      .then((history) => {
        if (!active) return;
        if (history && history.length > 0) {
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
          const lastClose = uniqueData[uniqueData.length - 1]?.close || 0;
          lastPriceRef.current = lastClose;
          displayedPriceRef.current = lastClose;
          setCurrentPrice(lastClose);
          if (uniqueData.length < limit) hasMoreDataRef.current = false;
        } else {
          setChartData([]);
          chartDataRef.current = [];
          hasMoreDataRef.current = false;
        }
      })
      .catch((err) => {
        if (!active) return;
        console.error("加载 K 线失败:", err);
        setChartData([]);
        chartDataRef.current = [];
        hasMoreDataRef.current = false;
      });

    return () => {
      active = false;
    };
  }, [symbol, chartInterval]);

  // 实时价格更新 (从 state 读取，不订阅 store 以免高频刷新整页)
  const bestBid = depth.bids.length > 0 ? depth.bids[0].price : currentPrice;
  const bestAsk = depth.asks.length > 0 ? depth.asks[depth.asks.length - 1].price : currentPrice;
  const spread = Math.max(0, bestAsk - bestBid);
  const spreadPercent = bestAsk > 0 ? (spread / bestAsk) * 100 : 0;

  const displayedPriceRef = useRef<number>(0);

  // 限制 K 线与界面价格每 500ms 最多刷新一次，防止每秒几十次重绘导致浏览器卡死
  useEffect(() => {
    const timer = setInterval(() => {
      const price = lastPriceRef.current;
      if (price === 0 || price === displayedPriceRef.current) return;
      
      displayedPriceRef.current = price;
      setCurrentPrice(price);

      // 将最新价格推入 K 线
      const intervalSecs = intervalSecondsMap[chartInterval] || 60;
      const nowIntervalTime = Math.floor(Date.now() / (intervalSecs * 1000)) * intervalSecs + 8 * 3600;

      setChartData((prevData) => {
        let nextData = prevData;
        if (prevData.length === 0) {
          nextData = [{
            time: nowIntervalTime as Time,
            open: price,
            high: price,
            low: price,
            close: price
          }];
        } else {
          const lastBar = prevData[prevData.length - 1];
          if (nowIntervalTime === Number(lastBar.time)) {
            const updated = {
              ...lastBar,
              close: price,
              high: Math.max(lastBar.high, price),
              low: Math.min(lastBar.low, price)
            };
            nextData = [...prevData.slice(0, -1), updated];
          } else if (nowIntervalTime > Number(lastBar.time)) {
            const newBar: CandlestickData<Time> = {
              time: nowIntervalTime as Time,
              open: lastBar.close,
              high: price,
              low: price,
              close: price
            };
            nextData = [...prevData, newBar];
            if (nextData.length > 1000) {
              nextData = nextData.slice(1);
            }
          }
        }
        chartDataRef.current = nextData;
        return nextData;
      });
    }, 500);

    return () => clearInterval(timer);
  }, [chartInterval]);

  // 滑动加载更多
  const loadMoreHistory = async () => {
    if (loadingMoreRef.current || !hasMoreDataRef.current) return;
    const currentData = chartDataRef.current;
    if (!currentData.length) return;

    loadingMoreRef.current = true;
    setLoadingMore(true);
    try {
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

        const existingTimes = new Set(currentData.map(c => Number(c.time)));
        const newCandles = adjusted.filter(c => !existingTimes.has(Number(c.time)));

        if (newCandles.length === 0) {
          hasMoreDataRef.current = false;
        } else {
          setChartData(prev => {
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

  // 订阅 WebSocket
  const handleWsMessage = useCallback((channel: string, data: any) => {
    if (channel === 'sandbox:state') {
      try {
        const s = data;
        if (typeof s.balance === 'number') setBalance(s.balance);
        if (s.position !== undefined) setPosition(s.position);
        if (Array.isArray(s.limitOrders)) setLimitOrders(s.limitOrders);
        if (Array.isArray(s.tradeLogs)) setTradeLogs(s.tradeLogs);
        if (s.autoStrategy) setAutoStrategy(s.autoStrategy);
        if (s.autoQty) setAutoQty(s.autoQty);
        if (typeof s.autoInterval === 'number') setAutoInterval(s.autoInterval);
        if (typeof s.tpPercent === 'number') setTpPercent(s.tpPercent);
        if (typeof s.slPercent === 'number') setSlPercent(s.slPercent);
        if (typeof s.tsActivation === 'number') setTsActivation(s.tsActivation);
        if (typeof s.tsCallback === 'number') setTsCallback(s.tsCallback);
      } catch (e) {
        console.error('解析后端推送的沙盒状态失败:', e);
      }
      return;
    }

    if (channel.startsWith('depth:') && data.type === 'depth') {
      const msgSymbol = channel.split(':')[1]?.toUpperCase();
      if (msgSymbol !== symbol) return; // 解决多币种订阅数据冲突跳转问题
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

        depthBufferRef.current = { bids, asks };
      } catch {}
    }

    if (channel.startsWith('ticker:') && data.type === 'bookTicker') {
      try {
        const ticker = JSON.parse(data.data);
        const bid = parseFloat(ticker.b);
        const ask = parseFloat(ticker.a);
        if (!isNaN(bid) && !isNaN(ask)) {
          const mid = (bid + ask) / 2;
          if (ticker.s === symbol) {
            lastPriceRef.current = mid;
          }
          updatePrice(ticker.s, mid);
        }
      } catch {}
    }
  }, [updatePrice, symbol]);

  const { connected, subscribe, unsubscribe } = useWebSocket({ onMessage: handleWsMessage });

  const prevSymbolRef = useRef(symbol);
  useEffect(() => {
    if (connected && symbol) {
      const prev = prevSymbolRef.current;
      if (prev && prev !== symbol) {
        unsubscribe(`depth:${prev.toLowerCase()}`);
        unsubscribe(`ticker:${prev.toLowerCase()}`);
      }
      prevSymbolRef.current = symbol;
      subscribe(`depth:${symbol.toLowerCase()}`);
      subscribe(`ticker:${symbol.toLowerCase()}`);
      subscribe('sandbox:state');
    }
  }, [connected, symbol, subscribe, unsubscribe]);

  // 计算当前仓位的未实现盈亏 (考虑点差：多仓在买一平，空仓在卖一平)
  const getUnrealizedPnL = useCallback(() => {
    if (!position) return 0;
    if (position.side === 'LONG') {
      const exitPrice = bestBid || currentPrice;
      return position.size * (exitPrice - position.entryPrice);
    } else {
      const exitPrice = bestAsk || currentPrice;
      return position.size * (position.entryPrice - exitPrice);
    }
  }, [position, bestBid, bestAsk, currentPrice]);

  const unrealizedPnL = getUnrealizedPnL();
  const currentEquity = balance + unrealizedPnL;




  // 手动执行市价下单 (调用后端沙盒引擎)
  const executeMarketOrder = (side: 'BUY' | 'SELL', qty: number) => {
    if (qty <= 0) return;
    if (currentPrice === 0) {
      alert('行情尚未加载，无法下单。');
      return;
    }

    fetch('/api/paper/sandbox/order', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, side, type: 'MARKET', qty })
    })
    .then(res => res.json())
    .then(data => {
      if (data.status !== 'success') {
        alert(data.detail || data.message || '市价下单失败');
      }
    })
    .catch(err => console.error('手动市价下单API请求失败:', err));
  };

  // 手动下单表单提交 (支持市价与限价)
  const handleTradeSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const qtyVal = parseFloat(quantity);
    if (isNaN(qtyVal) || qtyVal <= 0) {
      alert('请输入正确的下单数量');
      return;
    }

    if (orderType === 'MARKET') {
      executeMarketOrder(tradeSide, qtyVal);
    } else {
      const priceVal = parseFloat(limitPrice);
      if (isNaN(priceVal) || priceVal <= 0) {
        alert('请输入正确的限价价格');
        return;
      }
      fetch('/api/paper/sandbox/order', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol, side: tradeSide, type: 'LIMIT', qty: qtyVal, price: priceVal })
      })
      .then(res => res.json())
      .then(data => {
        if (data.status !== 'success') {
          alert(data.detail || data.message || '限价单挂单失败');
        }
      })
      .catch(err => console.error('手动限价下单API请求失败:', err));
    }
  };

  // 仓位一键平仓
  const handleMarketClose = () => {
    fetch('/api/paper/sandbox/close', {
      method: 'POST'
    })
    .then(res => res.json())
    .then(data => {
      if (data.status !== 'success') {
        alert(data.detail || data.message || '平仓失败');
      }
    })
    .catch(err => console.error('平仓API请求失败:', err));
  };

  // 仓位一键翻转 (做多翻转为做空，做空翻转为做多)
  const handleReversePosition = () => {
    if (!position) return;
    const oldSize = position.size;
    const oppositeSide = position.side === 'LONG' ? 'SELL' : 'BUY';
    
    // 平仓原方向仓位
    executeMarketOrder(position.side === 'LONG' ? 'SELL' : 'BUY', oldSize);
    
    // 反向开仓相同大小
    setTimeout(() => {
      executeMarketOrder(oppositeSide, oldSize);
    }, 300);
  };

  // 一键双倍仓位
  const handleDoublePosition = () => {
    if (!position) return;
    executeMarketOrder(position.side === 'LONG' ? 'BUY' : 'SELL', position.size);
  };

  // 取消限价挂单
  const handleCancelOrder = (id: string) => {
    fetch('/api/paper/sandbox/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id })
    })
    .then(res => res.json())
    .then(data => {
      if (data.status !== 'success') {
        alert(data.detail || data.message || '取消挂单失败');
      }
    })
    .catch(err => console.error('取消挂单API请求失败:', err));
  };

  // 重置模拟账户
  const handleResetAccount = () => {
    if (window.confirm('确定要重置资金并清空仓位和历史明细吗？')) {
      fetch('/api/paper/sandbox/reset', {
        method: 'POST'
      })
      .then(res => res.json())
      .then(data => {
        if (data.status !== 'success') {
          alert(data.detail || data.message || '重置失败');
        }
      })
      .catch(err => console.error('重置账户API请求失败:', err));
    }
  };


  // 使用 useMemo 缓存买卖点标记，只在相关数据改变时重新计算
  const markers = useMemo(() => {
    const markersList: any[] = [];
    const intervalSecs = intervalSecondsMap[chartInterval] || 60;
    
    [...tradeLogs].reverse().forEach((log) => {
      if (log.symbol !== symbol) return;
      
      // 使用交易成交时的实际时间戳，防止历史交易点在图表上全部垂直堆叠在最新的 K 线上
      const tradeTime = log.time || Math.floor(Date.now() / 1000);
      const alignedTime = Math.floor(tradeTime / intervalSecs) * intervalSecs + 8 * 3600;

      // 动态格式化价格：低价币（如几美分）用多位小数，高价币（如BTC）用 1 位或 2 位小数，防止低价币显示为 B 0.0
      const formattedPrice = log.price < 0.1 
        ? log.price.toFixed(5) 
        : (log.price < 1 
          ? log.price.toFixed(4) 
          : (log.price < 100 ? log.price.toFixed(2) : log.price.toFixed(1)));

      markersList.push({
        time: alignedTime as Time,
        position: log.side === 'BUY' ? 'belowBar' : 'aboveBar',
        color: log.side === 'BUY' ? '#00c853' : '#ff1744',
        shape: log.side === 'BUY' ? 'arrowUp' : 'arrowDown',
        text: `${log.side === 'BUY' ? 'B' : 'S'} ${formattedPrice}`
      });
    });

    return markersList;
  }, [tradeLogs, symbol, chartInterval]);

  // 缓存整个 Chart 绘图区域的 DOM/Canvas diff 渲染树，防止订单薄频繁高频重绘导致 Chart 闪烁卡顿
  const memoizedChart = useMemo(() => (
    <Chart
      data={chartData}
      anomalyMarkers={markers}
      onLoadMore={loadMoreHistory}
      loadingMore={loadingMore}
      height={400}
    />
  ), [chartData, markers, loadingMore]);

  return (
    <div style={styles.container}>
      {/* 顶部行情展示条 */}
      <div style={styles.topbar}>
        <div style={styles.topbarLeft}>
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)} style={styles.symbolSelect}>
            {availableSymbols.map(sym => (
              <option key={sym} value={sym}>{sym}</option>
            ))}
          </select>
          <div style={styles.priceSection}>
            <span style={styles.label}>当前中间价</span>
            <span style={{
              ...styles.valuePrice,
              color: currentPrice > 0 ? 'var(--color-up)' : 'var(--text-primary)'
            }}>
              {currentPrice ? currentPrice.toFixed(2) : '--'}
            </span>
          </div>
          <div style={styles.statGroup}>
            <span style={styles.label}>卖一价</span>
            <span style={{ ...styles.value, color: 'var(--color-down)' }}>{bestAsk ? bestAsk.toFixed(2) : '--'}</span>
          </div>
          <div style={styles.statGroup}>
            <span style={styles.label}>买一价</span>
            <span style={{ ...styles.value, color: 'var(--color-up)' }}>{bestBid ? bestBid.toFixed(2) : '--'}</span>
          </div>
          <div style={styles.statGroup}>
            <span style={styles.label}>点差</span>
            <span style={styles.value}>{spread.toFixed(2)} ({spreadPercent.toFixed(3)}%)</span>
          </div>
        </div>
        <div style={styles.topbarRight}>
          <div style={styles.wsStatus}>
            <span style={{
              ...styles.statusDot,
              background: connected ? '#00c853' : '#ff1744'
            }} />
            <span style={styles.statusText}>{connected ? '行情已连接 (Live)' : '行情连接已断开'}</span>
          </div>
          <button onClick={handleResetAccount} style={styles.resetBtn}>Reset 账户</button>
        </div>
      </div>

      {/* 主格栅布局：左边订单薄，中间 K 线图，右边交易面板 */}
      <div style={styles.mainGrid}>
        
        {/* 左栏：高频实时订单薄 */}
        <div className="card" style={styles.leftPane}>
          <h3 style={styles.paneTitle}>⚡ 实时盘口订单薄</h3>
          <div style={styles.orderbookWrapper}>
            {/* 卖盘 asks (从高到低排列) */}
            <div style={styles.asksList}>
              {depth.asks.map((ask, i) => (
                <div key={i} style={styles.depthRow} onClick={() => setLimitPrice(ask.price.toString())}>
                  <span style={{ ...styles.depthPrice, color: 'var(--color-down)' }}>{ask.price.toFixed(2)}</span>
                  <span style={styles.depthAmount}>{ask.amount.toFixed(3)}</span>
                  <div style={{
                    ...styles.depthBar,
                    width: `${Math.min(100, (ask.total / 10) * 100)}%`,
                    background: 'rgba(255, 23, 68, 0.08)'
                  }} />
                </div>
              ))}
              {depth.asks.length === 0 && <span style={styles.empty}>等待盘口数据...</span>}
            </div>

            {/* 中间折射价 / Spread */}
            <div style={styles.midSpreadRow}>
              <span style={{ ...styles.midPrice, color: currentPrice > 0 ? 'var(--color-up)' : 'var(--text-primary)' }}>
                {currentPrice ? currentPrice.toFixed(2) : '--'}
              </span>
              <span style={styles.spreadVal}>Spread: {spread.toFixed(2)} ({spreadPercent.toFixed(3)}%)</span>
            </div>

            {/* 买盘 bids (从高到低排列) */}
            <div style={styles.bidsList}>
              {depth.bids.map((bid, i) => (
                <div key={i} style={styles.depthRow} onClick={() => setLimitPrice(bid.price.toString())}>
                  <span style={{ ...styles.depthPrice, color: 'var(--color-up)' }}>{bid.price.toFixed(2)}</span>
                  <span style={styles.depthAmount}>{bid.amount.toFixed(3)}</span>
                  <div style={{
                    ...styles.depthBar,
                    width: `${Math.min(100, (bid.total / 10) * 100)}%`,
                    background: 'rgba(0, 200, 83, 0.08)'
                  }} />
                </div>
              ))}
              {depth.bids.length === 0 && <span style={styles.empty}>等待盘口数据...</span>}
            </div>
          </div>
        </div>

        {/* 中间栏：K 线走势与成交标记 */}
        <div style={styles.centerPane}>
          <div className="card" style={styles.chartCard}>
            <div style={styles.chartHeader}>
              <span style={styles.chartTitle}>📈 交易信号与高频实盘 K 线图</span>
              <div style={styles.intervalSelectors}>
                {['1m', '5m', '15m', '1h'].map(int => (
                  <button
                    key={int}
                    onClick={() => setChartInterval(int)}
                    style={{
                      ...styles.intervalBtn,
                      background: chartInterval === int ? 'var(--color-accent)' : 'transparent',
                      borderColor: chartInterval === int ? 'var(--color-accent)' : 'var(--border-color)',
                      color: chartInterval === int ? '#fff' : 'var(--text-secondary)'
                    }}
                  >
                    {int}
                  </button>
                ))}
              </div>
            </div>
            <div style={styles.chartWrapper}>
              {memoizedChart}
            </div>
          </div>

          {/* 下部面板：仓位、挂单与成交历史 */}
          <div className="card" style={styles.bottomStatsCard}>
            <div style={styles.tabs}>
              <button
                onClick={() => setActiveTab('position')}
                style={{
                  ...styles.tabBtn,
                  borderBottomColor: activeTab === 'position' ? 'var(--color-accent)' : 'transparent',
                  color: activeTab === 'position' ? 'var(--text-primary)' : 'var(--text-secondary)'
                }}
              >
                💼 当前仓位
              </button>
              <button
                onClick={() => setActiveTab('orders')}
                style={{
                  ...styles.tabBtn,
                  borderBottomColor: activeTab === 'orders' ? 'var(--color-accent)' : 'transparent',
                  color: activeTab === 'orders' ? 'var(--text-primary)' : 'var(--text-secondary)'
                }}
              >
                ⏳ 活跃挂单 ({limitOrders.length})
              </button>
              <button
                onClick={() => setActiveTab('history')}
                style={{
                  ...styles.tabBtn,
                  borderBottomColor: activeTab === 'history' ? 'var(--color-accent)' : 'transparent',
                  color: activeTab === 'history' ? 'var(--text-primary)' : 'var(--text-secondary)'
                }}
              >
                📋 成交明细 ({tradeLogs.length})
              </button>
            </div>

            <div style={styles.tabContent}>
              {activeTab === 'position' && (
                <div style={styles.positionGrid}>
                  {position ? (
                    <div style={styles.positionRow}>
                      <div style={styles.posItem}>
                        <span style={styles.posLabel}>仓位标的</span>
                        <span style={styles.posValName}>{position.symbol}</span>
                      </div>
                      <div style={styles.posItem}>
                        <span style={styles.posLabel}>方向</span>
                        <span style={{
                          ...styles.posVal,
                          color: position.side === 'LONG' ? 'var(--color-up)' : 'var(--color-down)'
                        }}>
                          {position.side === 'LONG' ? '做多 (LONG)' : '做空 (SHORT)'}
                        </span>
                      </div>
                      <div style={styles.posItem}>
                        <span style={styles.posLabel}>持有数量</span>
                        <span className="font-mono" style={styles.posVal}>{position.size.toFixed(4)}</span>
                      </div>
                      <div style={styles.posItem}>
                        <span style={styles.posLabel}>开仓均价</span>
                        <span className="font-mono" style={styles.posVal}>{position.entryPrice.toFixed(2)}</span>
                      </div>
                      <div style={styles.posItem}>
                        <span style={styles.posLabel}>标记价格</span>
                        <span className="font-mono" style={styles.posVal}>{currentPrice ? currentPrice.toFixed(2) : '--'}</span>
                      </div>
                      <div style={styles.posItem}>
                        <span style={styles.posLabel}>未实现盈亏 (uPnL)</span>
                        <span className="font-mono" style={{
                          ...styles.posValPrice,
                          color: unrealizedPnL >= 0 ? 'var(--color-up)' : 'var(--color-down)'
                        }}>
                          {unrealizedPnL >= 0 ? '+' : ''}{unrealizedPnL.toFixed(2)} USDT
                        </span>
                      </div>
                      <div style={styles.posActions}>
                        <button onClick={handleDoublePosition} style={styles.btnSecondary}>双倍</button>
                        <button onClick={handleReversePosition} style={styles.btnSecondary}>反向</button>
                        <button onClick={handleMarketClose} style={styles.btnClose}>市价平仓</button>
                      </div>
                    </div>
                  ) : (
                    <div style={styles.empty}>❌ 当前没有持仓，请在右侧下单。</div>
                  )}
                </div>
              )}

              {activeTab === 'orders' && (
                <div style={styles.tableWrapper}>
                  <table style={styles.table}>
                    <thead>
                      <tr style={styles.trHeader}>
                        <th style={styles.th}>时间</th>
                        <th style={styles.th}>标的</th>
                        <th style={styles.th}>类型</th>
                        <th style={styles.th}>买卖</th>
                        <th style={styles.th}>价格</th>
                        <th style={styles.th}>数量</th>
                        <th style={styles.th}>操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {limitOrders.map(o => (
                        <tr key={o.id} style={styles.trBody}>
                          <td style={styles.td}>{o.timestamp}</td>
                          <td className="font-mono" style={styles.td}>{o.symbol}</td>
                          <td style={styles.td}>LIMIT</td>
                          <td style={{ ...styles.td, color: o.side === 'BUY' ? 'var(--color-up)' : 'var(--color-down)' }}>
                            {o.side === 'BUY' ? '做多 (LONG)' : '做空 (SHORT)'}
                          </td>
                          <td className="font-mono" style={styles.td}>{o.price}</td>
                          <td className="font-mono" style={styles.td}>{o.qty}</td>
                          <td style={styles.td}>
                            <button onClick={() => handleCancelOrder(o.id)} style={styles.cancelBtn}>撤单</button>
                          </td>
                        </tr>
                      ))}
                      {limitOrders.length === 0 && (
                        <tr>
                          <td colSpan={7} style={styles.noData}>无活跃限价单</td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              )}

              {activeTab === 'history' && (
                <div style={styles.tableWrapper}>
                  <table style={styles.table}>
                    <thead>
                      <tr style={styles.trHeader}>
                        <th style={styles.th}>ID</th>
                        <th style={styles.th}>时间</th>
                        <th style={styles.th}>方向</th>
                        <th style={styles.th}>成交方式</th>
                        <th style={styles.th}>成交价</th>
                        <th style={styles.th}>数量</th>
                        <th style={styles.th}>手续费</th>
                        <th style={styles.th}>已实现盈亏</th>
                      </tr>
                    </thead>
                    <tbody>
                      {tradeLogs.map(log => (
                        <tr key={log.id} style={styles.trBody}>
                          <td className="font-mono" style={styles.td}>{log.id}</td>
                          <td style={styles.td}>{log.timestamp}</td>
                          <td style={{ ...styles.td, color: log.side === 'BUY' ? 'var(--color-up)' : 'var(--color-down)' }}>
                            {log.side === 'BUY' ? '买入' : '卖出'}
                          </td>
                          <td style={styles.td}>{log.type}</td>
                          <td className="font-mono" style={styles.td}>{log.price.toFixed(2)}</td>
                          <td className="font-mono" style={styles.td}>{log.qty}</td>
                          <td className="font-mono" style={{ ...styles.td, color: '#ff453a' }}>-{log.fee.toFixed(4)}</td>
                          <td className="font-mono" style={{
                            ...styles.td,
                            color: log.pnl > 0 ? 'var(--color-up)' : (log.pnl < 0 ? 'var(--color-down)' : 'var(--text-primary)')
                          }}>
                            {log.pnl > 0 ? '+' : ''}{log.pnl.toFixed(2)}
                          </td>
                        </tr>
                      ))}
                      {tradeLogs.length === 0 && (
                        <tr>
                          <td colSpan={8} style={styles.noData}>无历史成交记录</td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        </div>

        {/* 右栏：模拟下单终端与资金状态 */}
        <div style={styles.rightPane}>
          {/* 资产统计 */}
          <div className="card" style={styles.accountCard}>
            <h3 style={styles.paneTitle}>💼 模拟资产钱包</h3>
            <div style={styles.accountMetric}>
              <span style={styles.metricLabel}>可用余额 (USDT)</span>
              <span className="font-mono" style={styles.metricValue}>{balance.toFixed(2)}</span>
            </div>
            <div style={styles.accountMetric}>
              <span style={styles.metricLabel}>未平仓盈亏 (uPnL)</span>
              <span className="font-mono" style={{
                ...styles.metricValue,
                color: unrealizedPnL >= 0 ? 'var(--color-up)' : 'var(--color-down)'
              }}>
                {unrealizedPnL >= 0 ? '+' : ''}{unrealizedPnL.toFixed(2)}
              </span>
            </div>
            <div style={styles.accountMetric}>
              <span style={styles.metricLabel}>账户净资产 (Equity)</span>
              <span className="font-mono" style={{ ...styles.metricValue, color: 'var(--color-accent)' }}>
                {currentEquity.toFixed(2)}
              </span>
            </div>
          </div>

          {/* 自动交易机器人面板 */}
          <div className="card" style={{ ...styles.accountCard, border: autoStrategy !== 'NONE' ? '1px solid var(--color-up)' : '1px solid var(--border-color)', gap: '10px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <h3 style={styles.paneTitle}>🤖 订单薄自动交易机器人</h3>
              <span style={{
                fontSize: '11px',
                fontWeight: 'bold',
                padding: '2px 6px',
                borderRadius: '4px',
                background: autoStrategy !== 'NONE' ? 'rgba(0, 200, 83, 0.15)' : 'rgba(255, 69, 58, 0.15)',
                color: autoStrategy !== 'NONE' ? 'var(--color-up)' : '#ff453a'
              }}>
                {autoStrategy !== 'NONE' ? '● 自动运行中' : '● 已停止'}
              </span>
            </div>
            
            <div style={styles.inputGroup}>
              <label style={styles.inputLabel}>量化策略模型</label>
              <select
                value={autoStrategy}
                onChange={(e: any) => setAutoStrategy(e.target.value)}
                style={styles.tradeInput}
              >
                <option value="NONE">停止自动交易 (手动模式)</option>
                <option value="IMBALANCE">盘口深度失衡策略 (Order Imbalance)</option>
                <option value="MICROPRICE">微观价格动量策略 (Micro-Price)</option>
                <option value="SCALPING">盘口双向套利策略 (Spread Scalping)</option>
              </select>
            </div>

            {autoStrategy !== 'NONE' && (
              <>
                <div style={styles.feeGroup}>
                  <div style={styles.inputGroup}>
                    <label style={styles.inputLabel}>自动单笔金额 (USDT)</label>
                    <input
                      type="number"
                      step="0.1"
                      value={autoQty}
                      onChange={(e) => setAutoQty(e.target.value)}
                      style={styles.tradeInput}
                    />
                  </div>
                  <div style={styles.inputGroup}>
                    <label style={styles.inputLabel}>冷却周期 (秒)</label>
                    <input
                      type="number"
                      step="1"
                      value={autoInterval}
                      onChange={(e) => setAutoInterval(parseInt(e.target.value) || 1)}
                      style={styles.tradeInput}
                    />
                  </div>
                </div>

                <div style={styles.feeGroup}>
                  <div style={styles.inputGroup}>
                    <label style={styles.inputLabel}>自动止盈 (TP)</label>
                    <div style={{ position: 'relative' }}>
                      <input
                        type="number"
                        step="0.05"
                        value={tpPercent}
                        onChange={(e) => setTpPercent(parseFloat(e.target.value) || 0.1)}
                        style={{ ...styles.tradeInput, width: '100%' }}
                      />
                      <span style={{ position: 'absolute', right: '8px', bottom: '8px', fontSize: '12px', color: 'var(--text-secondary)' }}>%</span>
                    </div>
                  </div>
                  <div style={styles.inputGroup}>
                    <label style={styles.inputLabel}>硬性止损 (Max SL)</label>
                    <div style={{ position: 'relative' }}>
                      <input
                        type="number"
                        step="0.05"
                        value={slPercent}
                        onChange={(e) => setSlPercent(parseFloat(e.target.value) || 0.1)}
                        style={{ ...styles.tradeInput, width: '100%' }}
                      />
                      <span style={{ position: 'absolute', right: '8px', bottom: '8px', fontSize: '12px', color: 'var(--text-secondary)' }}>%</span>
                    </div>
                  </div>
                </div>

                <div style={styles.feeGroup}>
                  <div style={styles.inputGroup}>
                    <label style={styles.inputLabel}>移动锁盈触发</label>
                    <div style={{ position: 'relative' }}>
                      <input
                        type="number"
                        step="0.05"
                        value={tsActivation}
                        onChange={(e) => setTsActivation(parseFloat(e.target.value) || 0.05)}
                        style={{ ...styles.tradeInput, width: '100%' }}
                      />
                      <span style={{ position: 'absolute', right: '8px', bottom: '8px', fontSize: '12px', color: 'var(--text-secondary)' }}>%</span>
                    </div>
                  </div>
                  <div style={styles.inputGroup}>
                    <label style={styles.inputLabel}>移动回撤回调</label>
                    <div style={{ position: 'relative' }}>
                      <input
                        type="number"
                        step="0.05"
                        value={tsCallback}
                        onChange={(e) => setTsCallback(parseFloat(e.target.value) || 0.05)}
                        style={{ ...styles.tradeInput, width: '100%' }}
                      />
                      <span style={{ position: 'absolute', right: '8px', bottom: '8px', fontSize: '12px', color: 'var(--text-secondary)' }}>%</span>
                    </div>
                  </div>
                </div>
              </>
            )}
          </div>

          {/* 下单面板 */}
          <div className="card" style={styles.tradeCard}>
            <h3 style={styles.paneTitle}>📥 新建快捷委托</h3>
            <form onSubmit={handleTradeSubmit} style={styles.tradeForm}>
              
              <div style={styles.btnGroupType}>
                <button
                  type="button"
                  onClick={() => setOrderType('MARKET')}
                  style={{
                    ...styles.typeBtn,
                    background: orderType === 'MARKET' ? 'rgba(59,130,246,0.15)' : 'transparent',
                    borderColor: orderType === 'MARKET' ? 'var(--color-accent)' : 'var(--border-color)',
                    color: orderType === 'MARKET' ? 'var(--color-accent)' : 'var(--text-secondary)'
                  }}
                >
                  市价委托
                </button>
                <button
                  type="button"
                  onClick={() => setOrderType('LIMIT')}
                  style={{
                    ...styles.typeBtn,
                    background: orderType === 'LIMIT' ? 'rgba(59,130,246,0.15)' : 'transparent',
                    borderColor: orderType === 'LIMIT' ? 'var(--color-accent)' : 'var(--border-color)',
                    color: orderType === 'LIMIT' ? 'var(--color-accent)' : 'var(--text-secondary)'
                  }}
                >
                  限价委托
                </button>
              </div>

              <div style={styles.btnGroupSide}>
                <button
                  type="button"
                  onClick={() => setTradeSide('BUY')}
                  style={{
                    ...styles.sideBtn,
                    background: tradeSide === 'BUY' ? 'var(--color-up)' : 'transparent',
                    borderColor: tradeSide === 'BUY' ? 'var(--color-up)' : 'var(--border-color)',
                    color: tradeSide === 'BUY' ? '#fff' : 'var(--text-secondary)'
                  }}
                >
                  做多 / 买入 (LONG)
                </button>
                <button
                  type="button"
                  onClick={() => setTradeSide('SELL')}
                  style={{
                    ...styles.sideBtn,
                    background: tradeSide === 'SELL' ? 'var(--color-down)' : 'transparent',
                    borderColor: tradeSide === 'SELL' ? 'var(--color-down)' : 'var(--border-color)',
                    color: tradeSide === 'SELL' ? '#fff' : 'var(--text-secondary)'
                  }}
                >
                  做空 / 卖出 (SHORT)
                </button>
              </div>

              {orderType === 'LIMIT' && (
                <div style={styles.inputGroup}>
                  <label style={styles.inputLabel}>委托限价 (USDT)</label>
                  <input
                    type="number"
                    step="0.01"
                    value={limitPrice}
                    onChange={(e) => setLimitPrice(e.target.value)}
                    style={styles.tradeInput}
                  />
                </div>
              )}

              <div style={styles.inputGroup}>
                <label style={styles.inputLabel}>委托数量</label>
                <input
                  type="number"
                  step="0.0001"
                  value={quantity}
                  onChange={(e) => setQuantity(e.target.value)}
                  style={styles.tradeInput}
                />
              </div>

              {/* 手续费费率控制 */}
              <div style={styles.feeGroup}>
                <div style={styles.feeItem}>
                  <label style={styles.feeLabel}>吃单费率 (Taker)</label>
                  <input
                    type="number"
                    step="0.01"
                    value={takerFee}
                    onChange={(e) => setTakerFee(parseFloat(e.target.value))}
                    style={styles.feeInput}
                  />
                  <span style={styles.percentText}>%</span>
                </div>
                <div style={styles.feeItem}>
                  <label style={styles.feeLabel}>挂单费率 (Maker)</label>
                  <input
                    type="number"
                    step="0.01"
                    value={makerFee}
                    onChange={(e) => setMakerFee(parseFloat(e.target.value))}
                    style={styles.feeInput}
                  />
                  <span style={styles.percentText}>%</span>
                </div>
              </div>

              <button type="submit" style={{
                ...styles.submitBtn,
                background: tradeSide === 'BUY' ? 'var(--color-up)' : 'var(--color-down)'
              }}>
                立即发送委托指令
              </button>
            </form>
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
    overflowY: 'auto',
    display: 'flex',
    flexDirection: 'column',
    gap: '16px',
    backgroundColor: '#070a0e'
  },
  topbar: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    background: 'var(--bg-card)',
    padding: '12px 20px',
    borderRadius: '8px',
    border: '1px solid var(--border-color)',
  },
  topbarLeft: {
    display: 'flex',
    alignItems: 'center',
    gap: '24px',
  },
  symbolSelect: {
    background: 'var(--bg-main)',
    border: '1px solid var(--border-color)',
    color: 'var(--text-primary)',
    padding: '8px 16px',
    borderRadius: '4px',
    fontSize: '15px',
    fontWeight: 'bold',
    outline: 'none',
  },
  priceSection: {
    display: 'flex',
    flexDirection: 'column',
  },
  label: {
    fontSize: '11px',
    color: 'var(--text-secondary)',
    textTransform: 'uppercase',
  },
  valuePrice: {
    fontSize: '18px',
    fontWeight: 'bold',
    fontFamily: 'monospace',
  },
  statGroup: {
    display: 'flex',
    flexDirection: 'column',
  },
  value: {
    fontSize: '14px',
    fontWeight: 'bold',
    fontFamily: 'monospace',
    color: 'var(--text-primary)',
  },
  topbarRight: {
    display: 'flex',
    alignItems: 'center',
    gap: '16px',
  },
  wsStatus: {
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
  },
  statusDot: {
    width: '8px',
    height: '8px',
    borderRadius: '50%',
  },
  statusText: {
    fontSize: '12px',
    color: 'var(--text-secondary)',
  },
  resetBtn: {
    background: 'transparent',
    border: '1px solid #ff453a',
    color: '#ff453a',
    padding: '6px 12px',
    borderRadius: '4px',
    cursor: 'pointer',
    fontSize: '12px',
    fontWeight: 'bold',
  },
  mainGrid: {
    display: 'grid',
    gridTemplateColumns: '300px 1fr 320px',
    gap: '16px',
    alignItems: 'start',
  },
  leftPane: {
    padding: '16px',
    height: '680px',
    display: 'flex',
    flexDirection: 'column',
    gap: '12px',
    marginBottom: 0
  },
  paneTitle: {
    fontSize: '14px',
    fontWeight: 'bold',
    color: 'var(--text-primary)',
    borderBottom: '1px solid var(--border-color)',
    paddingBottom: '8px',
    marginBottom: 0
  },
  orderbookWrapper: {
    display: 'flex',
    flexDirection: 'column',
    flex: 1,
    overflow: 'hidden',
  },
  asksList: {
    display: 'flex',
    flexDirection: 'column',
    justifyContent: 'flex-end',
    flex: 1,
    overflowY: 'auto',
    gap: '3px',
  },
  bidsList: {
    display: 'flex',
    flexDirection: 'column',
    flex: 1,
    overflowY: 'auto',
    gap: '3px',
  },
  depthRow: {
    position: 'relative',
    display: 'flex',
    justifyContent: 'space-between',
    padding: '4px 8px',
    fontSize: '12px',
    cursor: 'pointer',
    borderRadius: '2px',
  },
  depthPrice: {
    zIndex: 2,
    fontFamily: 'monospace',
    fontWeight: 'bold',
  },
  depthAmount: {
    zIndex: 2,
    color: 'var(--text-secondary)',
    fontFamily: 'monospace',
  },
  depthBar: {
    position: 'absolute',
    top: 0,
    right: 0,
    bottom: 0,
    zIndex: 1,
    transition: 'width 0.1s ease',
  },
  midSpreadRow: {
    padding: '10px 8px',
    borderTop: '1px solid var(--border-color)',
    borderBottom: '1px solid var(--border-color)',
    margin: '8px 0',
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    gap: '2px',
  },
  midPrice: {
    fontSize: '18px',
    fontWeight: 'bold',
    fontFamily: 'monospace',
  },
  spreadVal: {
    fontSize: '11px',
    color: 'var(--text-secondary)',
  },
  empty: {
    fontSize: '12px',
    color: 'var(--text-secondary)',
    textAlign: 'center',
    padding: '24px 0',
    display: 'block',
  },
  centerPane: {
    display: 'flex',
    flexDirection: 'column',
    gap: '16px',
    minWidth: 0,
  },
  chartCard: {
    padding: '16px',
    marginBottom: 0
  },
  chartHeader: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: '12px',
  },
  chartTitle: {
    fontSize: '14px',
    fontWeight: 'bold',
    color: 'var(--text-primary)',
  },
  intervalSelectors: {
    display: 'flex',
    gap: '6px',
  },
  intervalBtn: {
    border: '1px solid',
    padding: '4px 8px',
    borderRadius: '4px',
    fontSize: '12px',
    cursor: 'pointer',
  },
  chartWrapper: {
    width: '100%',
  },
  bottomStatsCard: {
    padding: '16px',
    height: '248px',
    display: 'flex',
    flexDirection: 'column',
    marginBottom: 0
  },
  tabs: {
    display: 'flex',
    borderBottom: '1px solid var(--border-color)',
    gap: '16px',
  },
  tabBtn: {
    background: 'none',
    border: 'none',
    borderBottom: '2px solid transparent',
    padding: '8px 4px',
    cursor: 'pointer',
    fontSize: '13px',
    fontWeight: 'bold',
    outline: 'none',
    transition: 'border-color 0.2s',
  },
  tabContent: {
    flex: 1,
    overflowY: 'auto',
    paddingTop: '12px',
  },
  positionGrid: {
    display: 'flex',
    alignItems: 'center',
    height: '100%',
  },
  positionRow: {
    display: 'flex',
    width: '100%',
    justifyContent: 'space-between',
    alignItems: 'center',
    background: 'var(--bg-main)',
    padding: '16px 20px',
    borderRadius: '6px',
    border: '1px solid var(--border-color)',
  },
  posItem: {
    display: 'flex',
    flexDirection: 'column',
    gap: '4px',
  },
  posLabel: {
    fontSize: '11px',
    color: 'var(--text-secondary)',
  },
  posValName: {
    fontSize: '14px',
    fontWeight: 'bold',
    color: 'var(--text-primary)',
  },
  posVal: {
    fontSize: '14px',
    fontWeight: 'bold',
  },
  posValPrice: {
    fontSize: '15px',
    fontWeight: 'bold',
  },
  posActions: {
    display: 'flex',
    gap: '8px',
  },
  btnSecondary: {
    background: 'var(--bg-main)',
    border: '1px solid var(--border-color)',
    color: 'var(--text-primary)',
    padding: '6px 12px',
    borderRadius: '4px',
    cursor: 'pointer',
    fontSize: '12px',
    fontWeight: 'bold',
  },
  btnClose: {
    background: '#ff453a',
    border: 'none',
    color: '#fff',
    padding: '6px 14px',
    borderRadius: '4px',
    cursor: 'pointer',
    fontSize: '12px',
    fontWeight: 'bold',
  },
  rightPane: {
    display: 'flex',
    flexDirection: 'column',
    gap: '16px',
  },
  accountCard: {
    padding: '16px',
    display: 'flex',
    flexDirection: 'column',
    gap: '12px',
    marginBottom: 0
  },
  accountMetric: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    borderBottom: '1px dashed var(--border-color)',
    paddingBottom: '8px',
  },
  metricLabel: {
    fontSize: '12px',
    color: 'var(--text-secondary)',
  },
  metricValue: {
    fontSize: '16px',
    fontWeight: 'bold',
  },
  tradeCard: {
    padding: '16px',
    marginBottom: 0
  },
  tradeForm: {
    display: 'flex',
    flexDirection: 'column',
    gap: '14px',
    marginTop: '12px',
  },
  btnGroupType: {
    display: 'grid',
    gridTemplateColumns: '1fr 1fr',
    gap: '8px',
  },
  typeBtn: {
    border: '1px solid',
    padding: '8px',
    borderRadius: '4px',
    fontSize: '12px',
    fontWeight: 'bold',
    cursor: 'pointer',
    textAlign: 'center',
  },
  btnGroupSide: {
    display: 'grid',
    gridTemplateColumns: '1fr 1fr',
    gap: '8px',
  },
  sideBtn: {
    border: '1px solid',
    padding: '8px',
    borderRadius: '4px',
    fontSize: '12px',
    fontWeight: 'bold',
    cursor: 'pointer',
    textAlign: 'center',
  },
  inputGroup: {
    display: 'flex',
    flexDirection: 'column',
    gap: '6px',
  },
  inputLabel: {
    fontSize: '11px',
    color: 'var(--text-secondary)',
  },
  tradeInput: {
    background: 'var(--bg-main)',
    border: '1px solid var(--border-color)',
    color: 'var(--text-primary)',
    padding: '8px 12px',
    borderRadius: '4px',
    fontSize: '13px',
    fontFamily: 'monospace',
    outline: 'none',
  },
  feeGroup: {
    display: 'grid',
    gridTemplateColumns: '1fr 1fr',
    gap: '8px',
  },
  feeItem: {
    display: 'flex',
    flexDirection: 'column',
    gap: '4px',
    position: 'relative',
  },
  feeLabel: {
    fontSize: '11px',
    color: 'var(--text-secondary)',
  },
  feeInput: {
    background: 'var(--bg-main)',
    border: '1px solid var(--border-color)',
    color: 'var(--text-primary)',
    padding: '8px 24px 8px 12px',
    borderRadius: '4px',
    fontSize: '13px',
    fontFamily: 'monospace',
    outline: 'none',
  },
  percentText: {
    position: 'absolute',
    right: '8px',
    bottom: '8px',
    fontSize: '12px',
    color: 'var(--text-secondary)',
  },
  submitBtn: {
    border: 'none',
    color: '#fff',
    padding: '10px',
    borderRadius: '4px',
    fontSize: '13px',
    fontWeight: 'bold',
    cursor: 'pointer',
    marginTop: '6px',
  },
  tableWrapper: {
    overflowX: 'auto',
    maxHeight: '175px',
  },
  table: {
    width: '100%',
    borderCollapse: 'collapse',
    fontSize: '12px',
  },
  trHeader: {
    borderBottom: '1px solid var(--border-color)',
    textAlign: 'left',
  },
  th: {
    padding: '6px 8px',
    color: 'var(--text-secondary)',
    fontWeight: 600,
  },
  trBody: {
    borderBottom: '1px dashed var(--border-color)',
  },
  td: {
    padding: '6px 8px',
    color: 'var(--text-primary)',
  },
  cancelBtn: {
    background: 'none',
    border: '1px solid #ff453a',
    color: '#ff453a',
    padding: '2px 6px',
    borderRadius: '3px',
    cursor: 'pointer',
    fontSize: '10px',
  },
  noData: {
    textAlign: 'center',
    padding: '24px 0',
    color: 'var(--text-secondary)',
  },
};
