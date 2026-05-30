/**
 * 持仓卡片列表 — 显示当前所有持仓
 */
import { useEffect } from 'react';
import { useStore } from '../store';
import type { Position } from '../lib/api';

export default function PositionCards() {
  const { positions, prices, loadPositions } = useStore();

  useEffect(() => {
    loadPositions();
    const timer = setInterval(loadPositions, 10000);
    return () => clearInterval(timer);
  }, [loadPositions]);

  if (positions.length === 0) {
    return (
      <div style={styles.empty}>
        <span style={{ fontSize: 32 }}>📊</span>
        <p>当前无持仓</p>
      </div>
    );
  }

  return (
    <div style={styles.grid}>
      {positions.map((pos) => (
        <PositionCardItem key={pos.symbol} position={pos} currentPrice={prices[pos.symbol]} />
      ))}
    </div>
  );
}

function PositionCardItem({ position, currentPrice }: { position: Position; currentPrice?: number }) {
  const isLong = position.side === 'BUY';
  const price = currentPrice ?? position.entry_price;
  const livePnl = (price - position.entry_price) * position.quantity * (isLong ? 1 : -1);
  const pnlPct = ((price - position.entry_price) / position.entry_price * 100) * (isLong ? 1 : -1);

  return (
    <div style={styles.card}>
      <div style={styles.cardHeader}>
        <span style={styles.symbol}>{position.symbol}</span>
        <span style={{
          ...styles.badge,
          background: isLong ? 'rgba(0,200,83,0.15)' : 'rgba(255,23,68,0.15)',
          color: isLong ? '#00c853' : '#ff1744',
        }}>
          {isLong ? '做多 📈' : '做空 📉'}
        </span>
      </div>
      <div style={styles.cardBody}>
        <div style={styles.row}>
          <span style={styles.label}>数量</span>
          <span style={styles.val}>{position.quantity}</span>
        </div>
        <div style={styles.row}>
          <span style={styles.label}>入场价</span>
          <span style={styles.val}>{position.entry_price.toFixed(2)}</span>
        </div>
        <div style={styles.row}>
          <span style={styles.label}>当前价</span>
          <span style={styles.val}>{price.toFixed(2)}</span>
        </div>
        <div style={styles.row}>
          <span style={styles.label}>浮动盈亏</span>
          <span style={{
            ...styles.val,
            color: livePnl >= 0 ? '#00c853' : '#ff1744',
            fontWeight: 700,
            fontSize: 16,
          }}>
            {livePnl >= 0 ? '+' : ''}{livePnl.toFixed(2)} USDT ({pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(2)}%)
          </span>
        </div>
        {position.leverage > 1 && (
          <div style={styles.row}>
            <span style={styles.label}>杠杆</span>
            <span style={styles.val}>{position.leverage}x</span>
          </div>
        )}
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  grid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
    gap: 16,
  },
  empty: {
    textAlign: 'center',
    padding: 40,
    color: '#8b949e',
  },
  card: {
    background: '#161b22',
    border: '1px solid #30363d',
    borderRadius: 8,
    padding: 16,
  },
  cardHeader: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 12,
  },
  symbol: {
    fontSize: 16,
    fontWeight: 700,
    color: '#c9d1d9',
  },
  badge: {
    fontSize: 12,
    padding: '2px 8px',
    borderRadius: 4,
  },
  cardBody: {
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
  },
  row: {
    display: 'flex',
    justifyContent: 'space-between',
  },
  label: {
    fontSize: 13,
    color: '#8b949e',
  },
  val: {
    fontSize: 13,
    color: '#c9d1d9',
    fontWeight: 500,
  },
};
