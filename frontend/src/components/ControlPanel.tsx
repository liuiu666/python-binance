/**
 * 控制面板 — 一键全平 / 暂停恢复 / 紧急下单
 */
import { useState } from 'react';
import { closeAll, togglePause, emergencyOrder } from '../lib/api';
import { useStore } from '../store';

export default function ControlPanel() {
  const { strategyPaused, setStrategyPaused } = useStore();
  const [confirmCloseAll, setConfirmCloseAll] = useState(false);
  const [emergencyForm, setShowEmergency] = useState(false);
  const [emergencySymbol, setEmergencySymbol] = useState('BTCUSDT');
  const [emergencySide, setEmergencySide] = useState('BUY');
  const [emergencyQty, setEmergencyQty] = useState('0.001');
  const [loading, setLoading] = useState(false);

  const handleCloseAll = async () => {
    if (!confirmCloseAll) {
      setConfirmCloseAll(true);
      setTimeout(() => setConfirmCloseAll(false), 3000);
      return;
    }
    setLoading(true);
    try {
      await closeAll();
      setConfirmCloseAll(false);
    } catch {}
    setLoading(false);
  };

  const handleTogglePause = async () => {
    try {
      await togglePause(!strategyPaused);
      setStrategyPaused(!strategyPaused);
    } catch {}
  };

  const handleEmergency = async () => {
    setLoading(true);
    try {
      await emergencyOrder(emergencySymbol, emergencySide, parseFloat(emergencyQty));
      setShowEmergency(false);
    } catch {}
    setLoading(false);
  };

  return (
    <div style={styles.container}>
      <h3 style={styles.title}>控制面板</h3>
      <div style={styles.buttons}>
        <button
          style={{
            ...styles.btn,
            background: confirmCloseAll ? '#d32f2f' : '#c62828',
          }}
          onClick={handleCloseAll}
          disabled={loading}
        >
          {confirmCloseAll ? '⚠️ 确认全平?' : '🔴 一键全平'}
        </button>

        <button
          style={{
            ...styles.btn,
            background: strategyPaused ? '#2e7d32' : '#e65100',
          }}
          onClick={handleTogglePause}
          disabled={loading}
        >
          {strategyPaused ? '▶️ 恢复策略' : '⏸ 暂停策略'}
        </button>

        <button
          style={{ ...styles.btn, background: '#1565c0' }}
          onClick={() => setShowEmergency(!emergencyForm)}
        >
          🚨 紧急下单
        </button>
      </div>

      {emergencyForm && (
        <div style={styles.form}>
          <select
            value={emergencySymbol}
            onChange={(e) => setEmergencySymbol(e.target.value)}
            style={styles.select}
          >
            <option value="BTCUSDT">BTCUSDT</option>
            <option value="ETHUSDT">ETHUSDT</option>
          </select>
          <select
            value={emergencySide}
            onChange={(e) => setEmergencySide(e.target.value)}
            style={styles.select}
          >
            <option value="BUY">BUY (做多)</option>
            <option value="SELL">SELL (做空)</option>
          </select>
          <input
            type="number"
            value={emergencyQty}
            onChange={(e) => setEmergencyQty(e.target.value)}
            style={styles.input}
            placeholder="数量"
            step="0.001"
          />
          <button style={{ ...styles.btn, background: '#d32f2f' }} onClick={handleEmergency} disabled={loading}>
            确认下单
          </button>
        </div>
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
  buttons: {
    display: 'flex',
    gap: 12,
    flexWrap: 'wrap',
  },
  btn: {
    padding: '10px 20px',
    borderRadius: 6,
    border: 'none',
    color: '#fff',
    fontSize: 14,
    fontWeight: 600,
    cursor: 'pointer',
    transition: 'opacity 0.2s',
  },
  form: {
    display: 'flex',
    gap: 12,
    marginTop: 16,
    flexWrap: 'wrap',
    alignItems: 'center',
  },
  select: {
    background: '#0d1117',
    border: '1px solid #30363d',
    color: '#c9d1d9',
    padding: '8px 12px',
    borderRadius: 6,
    fontSize: 13,
  },
  input: {
    background: '#0d1117',
    border: '1px solid #30363d',
    color: '#c9d1d9',
    padding: '8px 12px',
    borderRadius: 6,
    fontSize: 13,
    width: 120,
  },
};
