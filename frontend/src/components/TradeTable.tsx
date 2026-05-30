/**
 * 交易记录表格
 */
import { useEffect, useState } from 'react';
import { fetchTrades } from '../lib/api';
import type { TradeRecord, TradeListResponse } from '../lib/api';

export default function TradeTable() {
  const [data, setData] = useState<TradeListResponse>({ total: 0, page: 1, size: 20, data: [] });
  const [page, setPage] = useState(1);

  useEffect(() => {
    fetchTrades(page).then(setData).catch(() => {});
  }, [page]);

  return (
    <div style={styles.container}>
      <h3 style={styles.title}>交易记录</h3>
      {data.data.length === 0 ? (
        <div style={styles.empty}>暂无交易记录</div>
      ) : (
        <>
          <div style={styles.tableWrap}>
            <table style={styles.table}>
              <thead>
                <tr>
                  <th style={styles.th}>时间</th>
                  <th style={styles.th}>交易对</th>
                  <th style={styles.th}>方向</th>
                  <th style={styles.th}>操作</th>
                  <th style={styles.th}>数量</th>
                  <th style={styles.th}>入场价</th>
                  <th style={styles.th}>出场价</th>
                  <th style={styles.th}>盈亏</th>
                  <th style={styles.th}>策略</th>
                  <th style={styles.th}>状态</th>
                </tr>
              </thead>
              <tbody>
                {data.data.map((t) => (
                  <tr key={t.signal_id} style={styles.tr}>
                    <td style={styles.td}>{t.opened_at?.slice(5, 16) || '-'}</td>
                    <td style={styles.td}>{t.symbol}</td>
                    <td style={{ ...styles.td, color: t.side === 'BUY' ? '#00c853' : '#ff1744' }}>
                      {t.side === 'BUY' ? '多' : '空'}
                    </td>
                    <td style={styles.td}>{t.action}</td>
                    <td style={styles.td}>{t.quantity}</td>
                    <td style={styles.td}>{t.entry_price?.toFixed(2) || '-'}</td>
                    <td style={styles.td}>{t.exit_price?.toFixed(2) || '-'}</td>
                    <td style={{
                      ...styles.td,
                      color: (t.pnl ?? 0) > 0 ? '#00c853' : (t.pnl ?? 0) < 0 ? '#ff1744' : '#8b949e',
                      fontWeight: 600,
                    }}>
                      {t.pnl != null ? `${t.pnl >= 0 ? '+' : ''}${t.pnl.toFixed(2)}` : '-'}
                    </td>
                    <td style={styles.td}>{t.strategy}</td>
                    <td style={styles.td}>
                      <span style={{
                        padding: '2px 6px',
                        borderRadius: 4,
                        fontSize: 11,
                        background: t.status === 'OPENED' ? 'rgba(88,166,255,0.15)' : 'rgba(139,148,158,0.15)',
                        color: t.status === 'OPENED' ? '#58a6ff' : '#8b949e',
                      }}>
                        {t.status}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {/* 分页 */}
          <div style={styles.pagination}>
            <button
              style={{ ...styles.pageBtn, opacity: page <= 1 ? 0.4 : 1 }}
              disabled={page <= 1}
              onClick={() => setPage(page - 1)}
            >
              上一页
            </button>
            <span style={styles.pageInfo}>
              第 {page} 页 / 共 {Math.ceil(data.total / data.size)} 页 ({data.total} 条)
            </span>
            <button
              style={{ ...styles.pageBtn, opacity: page * data.size >= data.total ? 0.4 : 1 }}
              disabled={page * data.size >= data.total}
              onClick={() => setPage(page + 1)}
            >
              下一页
            </button>
          </div>
        </>
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
  tableWrap: {
    overflowX: 'auto',
  },
  table: {
    width: '100%',
    borderCollapse: 'collapse',
    fontSize: 13,
  },
  th: {
    textAlign: 'left',
    padding: '8px 12px',
    color: '#8b949e',
    borderBottom: '1px solid #30363d',
    fontSize: 11,
    textTransform: 'uppercase' as const,
  },
  tr: {
    borderBottom: '1px solid #21262d',
  },
  td: {
    padding: '8px 12px',
    color: '#c9d1d9',
  },
  pagination: {
    display: 'flex',
    justifyContent: 'center',
    alignItems: 'center',
    gap: 16,
    marginTop: 16,
  },
  pageBtn: {
    background: '#21262d',
    border: '1px solid #30363d',
    color: '#c9d1d9',
    padding: '6px 16px',
    borderRadius: 6,
    cursor: 'pointer',
    fontSize: 13,
  },
  pageInfo: {
    color: '#8b949e',
    fontSize: 13,
  },
};
