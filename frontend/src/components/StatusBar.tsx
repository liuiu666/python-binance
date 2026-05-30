/**
 * 顶部状态栏 — 显示账户权益、今日盈亏、胜率、连接状态
 */
import { useEffect } from 'react';
import { useStore } from '../store';
import { useWebSocket } from '../hooks/useWebSocket';

export default function StatusBar() {
  const { account, stats, wsConnected, strategyPaused, loadAccount, loadStats, updatePrice, setWsConnected } = useStore();

  const handleWsMessage = (channel: string, data: any) => {
    // 更新实时价格
    if (channel.startsWith('market:') && data.close_price) {
      const symbol = data.symbol || channel.split(':')[1]?.toUpperCase();
      if (symbol) updatePrice(symbol, parseFloat(data.close_price));
    }
  };

  const { connected } = useWebSocket({ onMessage: handleWsMessage });

  useEffect(() => {
    setWsConnected(connected);
  }, [connected, setWsConnected]);

  useEffect(() => {
    loadAccount();
    loadStats();
    const timer = setInterval(() => {
      loadAccount();
      loadStats();
    }, 10000);
    return () => clearInterval(timer);
  }, [loadAccount, loadStats]);

  const balance = account?.total_wallet_balance ?? 0;
  const unrealized = account?.total_unrealized_profit ?? 0;
  const winRate = stats?.win_rate ?? 0;

  return (
    <header style={styles.bar}>
      <div style={styles.logo}>BXM40 量化交易</div>

      <div style={styles.metrics}>
        <div style={styles.metric}>
          <span style={styles.label}>账户权益</span>
          <span style={styles.value}>{balance.toFixed(2)} USDT</span>
        </div>
        <div style={styles.metric}>
          <span style={styles.label}>未实现盈亏</span>
          <span style={{ ...styles.value, color: unrealized >= 0 ? '#00c853' : '#ff1744' }}>
            {unrealized >= 0 ? '+' : ''}{unrealized.toFixed(2)}
          </span>
        </div>
        <div style={styles.metric}>
          <span style={styles.label}>胜率</span>
          <span style={styles.value}>{(winRate * 100).toFixed(1)}%</span>
        </div>
        <div style={styles.metric}>
          <span style={styles.label}>状态</span>
          <span style={{
            ...styles.value,
            color: strategyPaused ? '#ff9800' : wsConnected ? '#00c853' : '#ff1744'
          }}>
            {strategyPaused ? '⏸ 已暂停' : wsConnected ? '● 运行中' : '○ 已断开'}
          </span>
        </div>
      </div>
    </header>
  );
}

const styles: Record<string, React.CSSProperties> = {
  bar: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '0 24px',
    height: 56,
    background: '#161b22',
    borderBottom: '1px solid #30363d',
  },
  logo: {
    fontSize: 18,
    fontWeight: 700,
    color: '#58a6ff',
  },
  metrics: {
    display: 'flex',
    gap: 32,
  },
  metric: {
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'flex-end',
  },
  label: {
    fontSize: 11,
    color: '#8b949e',
    textTransform: 'uppercase',
  },
  value: {
    fontSize: 14,
    fontWeight: 600,
    color: '#c9d1d9',
  },
};
