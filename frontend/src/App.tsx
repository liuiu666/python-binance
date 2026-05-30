/**
 * BXM40 量化交易系统 — 主页面
 * 深色主题, 布局: 顶栏 + 左侧看板 + 右侧交易
 */
import StatusBar from './components/StatusBar';
import PositionCards from './components/PositionCard';
import PnLChart from './components/PnLChart';
import TradeTable from './components/TradeTable';
import ControlPanel from './components/ControlPanel';

export default function App() {
  return (
    <div style={styles.app}>
      <StatusBar />
      <main style={styles.main}>
        <div style={styles.left}>
          <section style={styles.section}>
            <PositionCards />
          </section>
          <section style={styles.section}>
            <PnLChart />
          </section>
          <section style={styles.section}>
            <TradeTable />
          </section>
        </div>
        <div style={styles.right}>
          <section style={styles.section}>
            <ControlPanel />
          </section>
        </div>
      </main>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  app: {
    minHeight: '100vh',
    background: '#0d1117',
    color: '#c9d1d9',
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
  },
  main: {
    display: 'flex',
    gap: 20,
    padding: 20,
    maxWidth: 1600,
    margin: '0 auto',
  },
  left: {
    flex: 1,
    display: 'flex',
    flexDirection: 'column',
    gap: 20,
    minWidth: 0,
  },
  right: {
    width: 320,
    flexShrink: 0,
  },
  section: {
    marginBottom: 0,
  },
};
