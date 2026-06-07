/**
 * BXM40 量化研究与沙盒系统 — 主页面 (Layout & Routing)
 */
import { useEffect, useState } from 'react';
import StatusBar from './components/StatusBar';
import Sandbox from './views/Sandbox';
import Backtest from './views/Backtest';
import Analysis from './views/Analysis';
import Editor from './views/Editor';
import Collection from './views/Collection';
import HFSandbox from './views/HFSandbox';

export default function App() {
  const [route, setRoute] = useState(window.location.hash || '#/');

  useEffect(() => {
    const handleHashChange = () => {
      setRoute(window.location.hash || '#/');
    };
    window.addEventListener('hashchange', handleHashChange);
    return () => window.removeEventListener('hashchange', handleHashChange);
  }, []);

  const renderView = () => {
    switch (route) {
      case '#/backtest':
        return <Backtest />;
      case '#/analysis':
        return <Analysis />;
      case '#/editor':
        return <Editor />;
      case '#/collection':
        return <Collection />;
      case '#/hft':
        return <HFSandbox />;
      case '#/':
      default:
        return <Sandbox />;
    }
  };

  return (
    <div style={styles.app}>
      <StatusBar />
      <div style={styles.container}>
        {/* 左侧专业侧边导航 */}
        <aside style={styles.sidebar}>
          <div style={styles.navSectionTitle}>📈 交易模拟</div>
          <a
            href="#/"
            style={{
              ...styles.navItem,
              background: route === '#/' || route === '' ? 'rgba(59, 130, 246, 0.15)' : 'transparent',
              color: route === '#/' || route === '' ? 'var(--color-accent)' : 'var(--text-secondary)'
            }}
          >
            💼 模拟交易沙盒
          </a>
          <a
            href="#/hft"
            style={{
              ...styles.navItem,
              background: route === '#/hft' ? 'rgba(59, 130, 246, 0.15)' : 'transparent',
              color: route === '#/hft' ? 'var(--color-accent)' : 'var(--text-secondary)'
            }}
          >
            ⚡ 高频订单薄模拟
          </a>

          <div style={{ ...styles.navSectionTitle, marginTop: '20px' }}>🧠 研究回测</div>
          <a
            href="#/backtest"
            style={{
              ...styles.navItem,
              background: route === '#/backtest' ? 'rgba(59, 130, 246, 0.15)' : 'transparent',
              color: route === '#/backtest' ? 'var(--color-accent)' : 'var(--text-secondary)'
            }}
          >
            ⏳ 历史回测工作室
          </a>
          <a
            href="#/analysis"
            style={{
              ...styles.navItem,
              background: route === '#/analysis' ? 'rgba(59, 130, 246, 0.15)' : 'transparent',
              color: route === '#/analysis' ? 'var(--color-accent)' : 'var(--text-secondary)'
            }}
          >
            📊 量价探索分析
          </a>
          <a
            href="#/editor"
            style={{
              ...styles.navItem,
              background: route === '#/editor' ? 'rgba(59, 130, 246, 0.15)' : 'transparent',
              color: route === '#/editor' ? 'var(--color-accent)' : 'var(--text-secondary)'
            }}
          >
            💻 策略在线 IDE
          </a>

          <div style={{ ...styles.navSectionTitle, marginTop: '20px' }}>📡 运维管理</div>
          <a
            href="#/collection"
            style={{
              ...styles.navItem,
              background: route === '#/collection' ? 'rgba(59, 130, 246, 0.15)' : 'transparent',
              color: route === '#/collection' ? 'var(--color-accent)' : 'var(--text-secondary)'
            }}
          >
            📡 数据采集监控
          </a>
        </aside>

        {/* 右侧主视窗区域 */}
        <main style={styles.main}>
          {renderView()}
        </main>
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  app: {
    minHeight: '100vh',
    background: 'var(--bg-main)',
    color: 'var(--text-primary)',
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    display: 'flex',
    flexDirection: 'column'
  },
  container: {
    display: 'flex',
    flex: 1,
    height: 'calc(100vh - 56px)',
    overflow: 'hidden'
  },
  sidebar: {
    width: '220px',
    background: 'var(--bg-card)',
    borderRight: '1px solid var(--border-color)',
    padding: '20px 12px',
    display: 'flex',
    flexDirection: 'column',
    gap: '4px',
    flexShrink: 0
  },
  navSectionTitle: {
    fontSize: '11px',
    fontWeight: 'bold',
    color: 'var(--text-muted)',
    textTransform: 'uppercase',
    padding: '8px 12px 4px 12px'
  },
  navItem: {
    padding: '10px 12px',
    borderRadius: '4px',
    fontSize: '13px',
    fontWeight: 500,
    textDecoration: 'none',
    transition: 'all 0.2s',
    display: 'flex',
    alignItems: 'center',
    gap: '8px'
  },
  main: {
    flex: 1,
    minWidth: 0,
    height: '100%',
    position: 'relative'
  }
};

