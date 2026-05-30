/**
 * 盈亏曲线图 — 使用 Recharts 绘制每日盈亏柱状图
 */
import { useEffect } from 'react';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, ReferenceLine, Cell } from 'recharts';
import { useStore } from '../store';

export default function PnLChart() {
  const { dailyPnl, loadDailyPnl } = useStore();

  useEffect(() => {
    loadDailyPnl();
  }, [loadDailyPnl]);

  const data = dailyPnl.map((d) => ({
    date: d.trade_date.slice(5),
    pnl: parseFloat(d.total_pnl.toFixed(2)),
    trades: d.trade_count,
  })).reverse();

  return (
    <div style={styles.container}>
      <h3 style={styles.title}>每日盈亏</h3>
      {data.length === 0 ? (
        <div style={styles.empty}>暂无数据</div>
      ) : (
        <ResponsiveContainer width="100%" height={280}>
          <BarChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
            <XAxis dataKey="date" stroke="#8b949e" fontSize={11} />
            <YAxis stroke="#8b949e" fontSize={11} />
            <Tooltip
              contentStyle={{ background: '#161b22', border: '1px solid #30363d', borderRadius: 6 }}
              labelStyle={{ color: '#8b949e' }}
            />
            <ReferenceLine y={0} stroke="#30363d" />
            <Bar dataKey="pnl" radius={[4, 4, 0, 0]}>
              {data.map((entry, index) => (
                <Cell key={index} fill={entry.pnl >= 0 ? '#00c853' : '#ff1744'} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    background: '#161b22',
    border: '1px solid #30363d',
    borderRadius: 8,
    padding: 20,
  },
  title: {
    color: '#c9d1d9',
    fontSize: 16,
    marginBottom: 16,
  },
  empty: {
    color: '#8b949e',
    textAlign: 'center',
    padding: 40,
  },
};
