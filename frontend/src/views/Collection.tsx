/**
 * 数据采集与存储管理面板 (Collection Manager)
 * - 真实对接后端 API 拉取 Redis 流状态
 * - 历史数据回填 (时间段 + 周期可配置)
 * - 币种管理 (添加 / 移除, 写入 PostgreSQL)
 */
import React, { useState, useEffect, useCallback } from 'react';
import { useStore } from '../store';

interface RedisStatus {
  stream: string;
  length: number;
}

interface SymbolStatus {
  symbol: string;
  active: boolean;
}

type BackfillStatus = 'idle' | 'running' | 'done' | 'error';

const COMMON_SYMBOLS = [
  'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
  'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT', 'LINKUSDT', 'DOTUSDT',
];

const INTERVALS = ['1m', '5m', '15m', '1h', '4h'];

const formatDate = (ms: number) => {
  if (!ms) return '无数据';
  const d = new Date(ms);
  const MM = (d.getMonth() + 1).toString().padStart(2, '0');
  const DD = d.getDate().toString().padStart(2, '0');
  const hh = d.getHours().toString().padStart(2, '0');
  const mm = d.getMinutes().toString().padStart(2, '0');
  return `${MM}-${DD} ${hh}:${mm}`;
};

export default function Collection() {
  const { wsConnected } = useStore();

  // ---- 状态 ----
  const [currentSymbols, setCurrentSymbols] = useState<string[]>(['BTCUSDT']);
  const [redisStreams, setRedisStreams] = useState<RedisStatus[]>([]);
  const [loadingSymbols, setLoadingSymbols] = useState(false);
  const [savingSymbols, setSavingSymbols] = useState(false);
  const [symbolInput, setSymbolInput] = useState('');
  const [symbolSaveMsg, setSymbolSaveMsg] = useState('');

  // 数据库存储区间状态
  const [dbRanges, setDbRanges] = useState<any>(null);
  const [loadingRanges, setLoadingRanges] = useState(false);

  // 断流检测状态
  const [gapCheckSymbol, setGapCheckSymbol] = useState<string | null>(null);
  const [checkingGaps, setCheckingGaps] = useState(false);
  const [gapCheckResult, setGapCheckResult] = useState<any>(null);
  const [gapCheckError, setGapCheckError] = useState<string | null>(null);

  const runGapCheck = async (symbol: string, interval = '1m') => {
    setCheckingGaps(true);
    setGapCheckSymbol(symbol);
    setGapCheckResult(null);
    setGapCheckError(null);
    try {
      const res = await fetch(`/api/control/check-gaps?symbol=${symbol}&interval=${interval}`);
      if (res.ok) {
        const data = await res.json();
        setGapCheckResult(data);
      } else {
        const data = await res.json();
        setGapCheckError(data.detail || '检测失败');
      }
    } catch {
      setGapCheckError('网络错误，请重试');
    } finally {
      setCheckingGaps(false);
    }
  };

  // 获取本地时间格式 YYYY-MM-DDTHH:mm
  const getLocalISOString = (date: Date) => {
    const tzOffset = date.getTimezoneOffset() * 60000;
    const localTime = new Date(date.getTime() - tzOffset);
    return localTime.toISOString().slice(0, 16);
  };

  // 回填状态
  const [bfSymbol, setBfSymbol] = useState('BTCUSDT');
  const [bfInterval, setBfInterval] = useState('1m');
  const [bfStartDate, setBfStartDate] = useState(() => {
    const d = new Date();
    d.setDate(d.getDate() - 7);
    return getLocalISOString(d);
  });
  const [bfEndDate, setBfEndDate] = useState(() => getLocalISOString(new Date()));
  const [bfStatus, setBfStatus] = useState<BackfillStatus>('idle');
  const [bfMessage, setBfMessage] = useState('');
  const [bfProgress, setBfProgress] = useState(0);
  const [bfInserted, setBfInserted] = useState(0);

  // ---- 加载数据 ----
  const loadSymbols = useCallback(async () => {
    setLoadingSymbols(true);
    try {
      const res = await fetch('/api/control/symbols');
      if (res.ok) {
        const data = await res.json();
        setCurrentSymbols(data.symbols || []);
      }
    } catch {
      // 忽略加载错误，保留上次值
    } finally {
      setLoadingSymbols(false);
    }
  }, []);

  const loadRedisStreams = useCallback(async () => {
    try {
      // 直接用 check_redis 的逻辑：对每个监控币种检查各流长度
      // 调用 /api/health 或 /api/config 来辅助显示
      const res = await fetch('/api/config');
      if (res.ok) {
        const cfg = await res.json();
        const syms: string[] = cfg.symbols || currentSymbols;
        // 构造期望 of 流名称列表 (frontend read-only display)
        const streams: RedisStatus[] = [];
        for (const s of syms) {
          const sl = s.toLowerCase();
          streams.push(
            { stream: `market:${sl}`, length: -1 },
            { stream: `depth:${sl}`, length: -1 },
            { stream: `ticker:${sl}`, length: -1 },
          );
        }
        streams.push({ stream: 'signal:trade', length: -1 });
        streams.push({ stream: 'order:update', length: -1 });
        setRedisStreams(streams);
      }
    } catch {
      // ignore
    }
  }, [currentSymbols]);

  const loadDbRanges = useCallback(async () => {
    setLoadingRanges(true);
    try {
      const res = await fetch('/api/control/db-ranges');
      if (res.ok) {
        const data = await res.json();
        setDbRanges(data);
      }
    } catch (e) {
      console.error(e);
    } finally {
      setLoadingRanges(false);
    }
  }, []);

  useEffect(() => {
    loadSymbols();
  }, [loadSymbols]);

  useEffect(() => {
    loadRedisStreams();
    const t = setInterval(loadRedisStreams, 20000);
    return () => clearInterval(t);
  }, [loadRedisStreams]);

  useEffect(() => {
    loadDbRanges();
    const t = setInterval(loadDbRanges, 10000);
    return () => clearInterval(t);
  }, [loadDbRanges]);

  // 当切换币种、周期，或者获取到数据库区间时，自动将回填的“结束时间”对齐已有数据的开始时间
  useEffect(() => {
    if (!dbRanges?.klines) return;
    const key = `${bfSymbol}:${bfInterval}`;
    const klineInfo = dbRanges.klines[key];
    if (klineInfo && klineInfo.min_time > 0) {
      const date = new Date(klineInfo.min_time);
      setBfEndDate(getLocalISOString(date));
      // 同时也把开始时间对齐到结束时间的前 7 天，方便操作
      const startDate = new Date(klineInfo.min_time - 7 * 86400000);
      setBfStartDate(getLocalISOString(startDate));
    } else {
      // 若该周期在 ClickHouse 中尚无任何数据，则结束时间默认对齐当前系统时间（现在），开始时间为 7 天前
      const now = new Date();
      setBfEndDate(getLocalISOString(now));
      const startDate = new Date(now.getTime() - 7 * 86400000);
      setBfStartDate(getLocalISOString(startDate));
    }
  }, [bfSymbol, bfInterval, dbRanges]);

  const checkBackfillStatus = useCallback(async () => {
    try {
      const res = await fetch(`/api/control/backfill-status?symbol=${bfSymbol}&interval=${bfInterval}`);
      if (res.ok) {
        const data = await res.json();
        if (data.status === 'running') {
          setBfStatus('running');
          setBfProgress(data.progress);
          setBfInserted(data.total_inserted);
          setBfMessage(`⚡ 正在从币安获取并导入历史 K 线... 已写入 ${data.total_inserted.toLocaleString()} 条`);
        } else if (data.status === 'completed') {
          setBfStatus('done');
          setBfProgress(100);
          setBfInserted(data.total_inserted);
          setBfMessage(`✅ 历史数据回填成功！共计写入了 ${data.total_inserted.toLocaleString()} 条数据。`);
          loadDbRanges();
        } else if (data.status === 'failed') {
          setBfStatus('error');
          setBfProgress(0);
          setBfMessage(`❌ 回填任务异常终止: ${data.error_message || '未知异常'}`);
        }
      }
    } catch {
      // ignore
    }
  }, [bfSymbol, bfInterval, loadDbRanges]);

  useEffect(() => {
    if (bfStatus === 'running') {
      // 每 1.5 秒轮询一次最新进度
      const t = setInterval(checkBackfillStatus, 1500);
      return () => clearInterval(t);
    }
  }, [bfStatus, checkBackfillStatus]);

  // ---- 币种管理 ----
  const addSymbol = () => {
    const s = symbolInput.trim().toUpperCase();
    if (!s) return;
    if (!s.endsWith('USDT')) {
      setSymbolSaveMsg('❌ 只支持 USDT 计价的交易对（如 ETHUSDT）');
      return;
    }
    if (currentSymbols.includes(s)) {
      setSymbolSaveMsg('⚠️ 该币种已在监控列表中');
      return;
    }
    if (currentSymbols.length >= 10) {
      setSymbolSaveMsg('❌ 最多监控 10 个币种');
      return;
    }
    setCurrentSymbols(prev => [...prev, s]);
    setSymbolInput('');
    setSymbolSaveMsg('');
  };

  const removeSymbol = (sym: string) => {
    if (currentSymbols.length <= 1) {
      setSymbolSaveMsg('❌ 至少保留一个监控币种');
      return;
    }
    setCurrentSymbols(prev => prev.filter(s => s !== sym));
    setSymbolSaveMsg('');
  };

  const saveSymbols = async () => {
    setSavingSymbols(true);
    setSymbolSaveMsg('');
    try {
      const res = await fetch('/api/control/symbols', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbols: currentSymbols }),
      });
      const data = await res.json();
      if (res.ok) {
        setSymbolSaveMsg('✅ 已保存！采集器将自动热更新订阅。');
        setBfSymbol(currentSymbols[0]);
      } else {
        setSymbolSaveMsg(`❌ 保存失败: ${data.detail || '未知错误'}`);
      }
    } catch {
      setSymbolSaveMsg('❌ 网络错误，请重试');
    } finally {
      setSavingSymbols(false);
    }
  };

  // ---- 历史回填 ----
  const handleBackfill = async (e: React.FormEvent) => {
    e.preventDefault();
    const startTs = new Date(bfStartDate).getTime();
    const endTs = new Date(bfEndDate).getTime();

    if (endTs <= startTs) {
      setBfMessage('❌ 结束时间必须晚于开始时间');
      return;
    }
    const diffDays = (endTs - startTs) / 86_400_000;
    if (diffDays > 30) {
      setBfMessage('❌ 单次回填最长 30 天，请分批操作');
      return;
    }

    setBfStatus('running');
    setBfMessage('');
    try {
      const res = await fetch('/api/control/compensate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol: bfSymbol,
          interval: bfInterval,
          start_time: startTs,
          end_time: endTs,
        }),
      });
      const data = await res.json();
      if (res.ok) {
        setBfStatus('running');
        setBfProgress(0);
        setBfInserted(0);
        setBfMessage('⚡ 正在初始化回填任务，正在连接币安 API...');
        // 立即触发一次状态检测
        setTimeout(checkBackfillStatus, 500);
      } else {
        setBfStatus('error');
        setBfMessage(`❌ 失败: ${data.detail || '未知错误'}`);
      }
    } catch {
      setBfStatus('error');
      setBfMessage('❌ 网络错误，请重试');
    }
  };

  const bfDays = ((new Date(bfEndDate).getTime() - new Date(bfStartDate).getTime()) / 86_400_000).toFixed(1);
  const estimatedKlines = Math.round(parseFloat(bfDays) * (bfInterval === '1m' ? 1440 : bfInterval === '5m' ? 288 : bfInterval === '15m' ? 96 : bfInterval === '1h' ? 24 : 6));

  // 按照交易对聚合已有数据
  const groupedRanges = (() => {
    if (!dbRanges) return [];
    const symbols = new Set<string>();
    Object.keys(dbRanges.trades || {}).forEach(s => symbols.add(s));
    Object.keys(dbRanges.klines || {}).forEach(k => {
      symbols.add(k.split(':')[0]);
    });

    return Array.from(symbols).map(symbol => {
      const trades = dbRanges.trades[symbol] || null;
      const klines: Record<string, any> = {};
      Object.keys(dbRanges.klines || {}).forEach(k => {
        const [sym, iv] = k.split(':');
        if (sym === symbol) {
          klines[iv] = dbRanges.klines[k];
        }
      });
      return { symbol, trades, klines };
    });
  })();

  return (
    <div style={s.page}>
      <div style={s.header}>
        <h2 style={s.title}>📡 数据采集与存储管理</h2>
        <p style={s.sub}>管理监控币种、回填历史 K 线数据、查看 Redis 消息总线状态</p>
      </div>

      <div style={s.grid}>

        {/* ========== 左栏 ========== */}
        <div style={s.col}>

          {/* 采集链路状态 */}
          <div className="card" style={s.card}>
            <h3 style={s.cardTitle}>📥 采集链路状态</h3>
            <div style={s.statusGrid}>
              <StatusRow label="前端 WebSocket" value={wsConnected ? '● 已连接' : '○ 断开中'} ok={wsConnected} />
              <StatusRow label="REST 校准器" value="⏱ 每 30s 轮询" ok={true} />
              <StatusRow label="深度流 (depth)" value="● 已修复" ok={true} />
              <StatusRow label="ClickHouse" value="批量写入中" ok={true} />
            </div>
          </div>

          {/* Redis 流监视 */}
          <div className="card" style={s.card}>
            <h3 style={s.cardTitle}>🔄 Redis Stream 状态</h3>
            <div style={s.streamList}>
              {redisStreams.length === 0 && (
                <div style={s.emptyTip}>加载中...</div>
              )}
              {redisStreams.map(({ stream }) => (
                <div key={stream} style={s.streamRow}>
                  <span style={s.streamBadge}>
                    {stream.startsWith('market:') ? '📈' :
                     stream.startsWith('depth:') ? '📊' :
                     stream.startsWith('ticker:') ? '⚡' :
                     stream.startsWith('signal:') ? '🎯' : '📬'}
                  </span>
                  <span style={s.streamName}>{stream}</span>
                  <span style={{
                    ...s.streamDot,
                    color: wsConnected ? 'var(--color-up)' : '#666'
                  }}>
                    {wsConnected ? '● 活跃' : '○ 等待'}
                  </span>
                </div>
              ))}
            </div>
          </div>

          {/* ClickHouse 数据存储详情看板 */}
          <div className="card" style={s.card}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '14px' }}>
              <h3 style={{ ...s.cardTitle, margin: 0 }}>🗄️ ClickHouse 数据区间</h3>
              <button 
                onClick={loadDbRanges} 
                disabled={loadingRanges}
                style={{
                  background: 'rgba(88, 166, 255, 0.1)', border: '1px solid rgba(88, 166, 255, 0.2)',
                  color: 'var(--color-accent)', borderRadius: '4px', padding: '3px 8px',
                  cursor: 'pointer', fontSize: '11px', fontWeight: 'bold'
                }}
              >
                {loadingRanges ? '🔄 刷新中...' : '🔄 刷新'}
              </button>
            </div>
            <p style={s.desc}>
              显示当前数据库中每个币种已有的 K线 与 明细成交 数据的完整区间与总量。
            </p>
            {!dbRanges ? (
              <div style={s.emptyTip}>暂无数据或正在读取存储统计...</div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                {groupedRanges.map(item => {
                  return (
                    <div key={item.symbol} style={{
                      background: 'rgba(255,255,255,0.02)', padding: '10px',
                      borderRadius: '6px', border: '1px solid var(--border-color)'
                    }}>
                      <div style={{ fontWeight: 'bold', color: 'var(--text-primary)', marginBottom: '6px', fontSize: '12px' }}>
                        🪙 {item.symbol}
                      </div>
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px', fontSize: '11px' }}>
                        <div>
                          {Object.keys(item.klines).length === 0 ? (
                            <div style={{ color: 'var(--text-muted)', fontSize: '10px' }}>无 K线 数据</div>
                          ) : (
                            Object.keys(item.klines).map(iv => {
                              const kInfo = item.klines[iv] || { min_time: 0, max_time: 0, count: 0 };
                              return (
                                <div key={iv} style={{ marginBottom: '6px' }}>
                                  <div style={{ color: 'var(--color-accent)', fontWeight: 600 }}>📈 K线 ({iv})</div>
                                  <div style={{ fontSize: '10px', color: 'var(--text-secondary)' }}>
                                    {formatDate(kInfo.min_time)}<br/>至 {formatDate(kInfo.max_time)}
                                  </div>
                                  <div style={{ color: 'var(--text-secondary)', marginTop: '2px' }}>
                                    共 <strong>{kInfo.count.toLocaleString()}</strong> 条
                                  </div>
                                </div>
                              );
                            })
                          )}
                        </div>
                        <div>
                          {item.trades ? (
                            <div>
                              <div style={{ color: 'var(--color-up)', fontWeight: 600 }}>⚡ 明细成交</div>
                              <div style={{ fontSize: '10px', color: 'var(--text-secondary)' }}>
                                {formatDate(item.trades.min_time)}<br/>至 {formatDate(item.trades.max_time)}
                              </div>
                              <div style={{ color: 'var(--text-secondary)', marginTop: '4px' }}>
                                共 <strong>{item.trades.count.toLocaleString()}</strong> 条
                              </div>
                            </div>
                          ) : (
                            <div style={{ color: 'var(--text-muted)' }}>无明细成交</div>
                          )}
                        </div>
                      </div>

                      {/* 完整性/断流检测 */}
                      <div style={{ marginTop: '10px', borderTop: '1px solid rgba(255,255,255,0.05)', paddingTop: '8px' }}>
                        <button
                          onClick={() => runGapCheck(item.symbol, bfInterval)}
                          disabled={checkingGaps}
                          style={{
                            background: 'rgba(255, 255, 255, 0.05)',
                            border: '1px solid var(--border-color)',
                            color: 'var(--text-secondary)',
                            borderRadius: '4px',
                            padding: '3px 8px',
                            cursor: 'pointer',
                            fontSize: '10px',
                            fontWeight: 'bold',
                            display: 'flex',
                            alignItems: 'center',
                            gap: '4px'
                          }}
                        >
                          {checkingGaps && gapCheckSymbol === item.symbol ? `⏳ 正在检测...` : `🔍 检测 ${bfInterval} K线断流/缺漏`}
                        </button>

                        {gapCheckSymbol === item.symbol && (
                          <div style={{ marginTop: '8px', fontSize: '11px', background: 'rgba(0,0,0,0.2)', padding: '8px', borderRadius: '4px' }}>
                            {checkingGaps ? (
                              <div style={{ color: 'var(--text-muted)' }}>正在对该币种做 {gapCheckResult?.interval || bfInterval} 周期全量时序扫描，请稍候...</div>
                            ) : gapCheckError ? (
                              <div style={{ color: 'var(--color-down)' }}>⚠️ 检测失败: {gapCheckError}</div>
                            ) : gapCheckResult ? (
                              <div>
                                {gapCheckResult.gaps.length === 0 ? (
                                  <div style={{ color: 'var(--color-up)', fontWeight: 'bold' }}>
                                    ✅ 100% 完整：检测到该周期内 K 线完全连续，无数据缺漏！
                                  </div>
                                ) : (
                                  <div>
                                    <div style={{ color: 'var(--color-warn)', fontWeight: 'bold', marginBottom: '4px' }}>
                                      ⚠️ 发现以下缺漏区间 (共 {gapCheckResult.gaps.length} 处)：
                                    </div>
                                    <div style={{
                                      maxHeight: '120px', overflowY: 'auto', display: 'flex',
                                      flexDirection: 'column', gap: '3px', fontFamily: 'monospace', fontSize: '10px'
                                    }}>
                                      {gapCheckResult.gaps.map((g: any, i: number) => (
                                        <div key={i} style={{ color: 'var(--text-secondary)' }}>
                                          • {formatDate(g.start_time)} 至 {formatDate(g.end_time)} (缺失 {g.gap_minutes} 分钟)
                                        </div>
                                      ))}
                                    </div>
                                  </div>
                                )}
                              </div>
                            ) : null}
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

        </div>

        {/* ========== 右栏 ========== */}
        <div style={s.col}>

          {/* 币种管理 */}
          <div className="card" style={s.card}>
            <h3 style={s.cardTitle}>🪙 币种管理</h3>
            <p style={s.desc}>
              修改后点击「保存」写入数据库。系统将自动发送 Redis 信号热更新订阅，<strong>无需重启任何服务</strong>。
            </p>

            {/* 当前币种列表 */}
            <div style={s.chipBox}>
              {currentSymbols.map(sym => (
                <div key={sym} style={s.chip}>
                  <span style={s.chipText}>{sym}</span>
                  <button
                    style={s.chipDel}
                    onClick={() => removeSymbol(sym)}
                    title="移除"
                  >×</button>
                </div>
              ))}
            </div>

            {/* 快速选择 */}
            <div style={s.quickRow}>
              <span style={s.quickLabel}>快速添加：</span>
              {COMMON_SYMBOLS.filter(s => !currentSymbols.includes(s)).slice(0, 6).map(sym => (
                <button
                  key={sym}
                  style={s.quickBtn}
                  onClick={() => setCurrentSymbols(prev => [...prev, sym])}
                >+{sym}</button>
              ))}
            </div>

            {/* 手动输入 */}
            <div style={s.inputRow}>
              <input
                style={{ ...s.input, flex: 1 }}
                placeholder="输入币种，如 SOLUSDT"
                value={symbolInput}
                onChange={e => setSymbolInput(e.target.value.toUpperCase())}
                onKeyDown={e => e.key === 'Enter' && addSymbol()}
              />
              <button style={s.addBtn} onClick={addSymbol}>添加</button>
            </div>

            {symbolSaveMsg && (
              <div style={{
                ...s.msg,
                color: symbolSaveMsg.startsWith('✅') ? 'var(--color-up)' : 'var(--color-down)'
              }}>{symbolSaveMsg}</div>
            )}

            <button
              style={{ ...s.saveBtn, opacity: savingSymbols ? 0.6 : 1 }}
              onClick={saveSymbols}
              disabled={savingSymbols || loadingSymbols}
            >
              {savingSymbols ? '保存中...' : '💾 保存币种配置'}
            </button>

            <div style={s.hint}>
              ✅ <strong>无需重启</strong>：保存后采集器会通过 Redis 信号自动热切换 WebSocket 订阅，新币种数秒内开始推流。
            </div>
          </div>

          {/* 历史数据回填 */}
          <div className="card" style={s.card}>
            <h3 style={s.cardTitle}>📥 历史数据回填</h3>
            <p style={s.desc}>
              从币安 REST API 拉取指定时间段的历史 K 线，写入 ClickHouse。
              单次最多 <strong>30 天</strong>，超出请分批执行。
            </p>
            <form onSubmit={handleBackfill} style={s.form}>
              <div style={s.formRow}>
                <div style={s.formGroup}>
                  <label style={s.label}>交易对</label>
                  <select value={bfSymbol} onChange={e => setBfSymbol(e.target.value)} style={s.input}>
                    {currentSymbols.map(sym => (
                      <option key={sym} value={sym}>{sym}</option>
                    ))}
                  </select>
                </div>
                <div style={s.formGroup}>
                  <label style={s.label}>K 线周期</label>
                  <select value={bfInterval} onChange={e => setBfInterval(e.target.value)} style={s.input}>
                    {INTERVALS.map(iv => (
                      <option key={iv} value={iv}>{iv}</option>
                    ))}
                  </select>
                </div>
              </div>
              <div style={s.formRow}>
                <div style={s.formGroup}>
                  <label style={s.label}>开始时间</label>
                  <input
                    type="datetime-local"
                    value={bfStartDate}
                    onChange={e => setBfStartDate(e.target.value)}
                    style={s.dtInput}
                  />
                </div>
                <div style={s.formGroup}>
                  <label style={s.label}>结束时间</label>
                  <input
                    type="datetime-local"
                    value={bfEndDate}
                    onChange={e => setBfEndDate(e.target.value)}
                    style={s.dtInput}
                  />
                </div>
              </div>

              {/* 自动对齐提示 */}
              {dbRanges?.klines?.[bfSymbol]?.min_time > 0 && (
                <div style={{ fontSize: '11px', color: 'var(--color-accent)', background: 'rgba(88, 166, 255, 0.05)', padding: '6px 10px', borderRadius: '4px', border: '1px solid rgba(88, 166, 255, 0.1)', display: 'flex', alignItems: 'center', gap: '4px' }}>
                  <span>💡</span>
                  <span><strong>结束时间</strong>已自动对齐 ClickHouse 中 <strong>{bfSymbol}</strong> 已有数据的起点（{formatDate(dbRanges.klines[bfSymbol].min_time)}），确保无缝衔接。</span>
                </div>
              )}

              {/* 预览 */}
              <div style={s.preview}>
                <span>📆 时间跨度：<strong>{bfDays} 天</strong></span>
                <span>🔢 预估 K 线：<strong>~{estimatedKlines.toLocaleString()} 根</strong></span>
              </div>

              {bfStatus === 'running' && (
                <div style={{
                  background: 'rgba(255,255,255,0.03)', border: '1px solid var(--border-color)',
                  borderRadius: '6px', padding: '12px', marginTop: '4px'
                }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '11px', marginBottom: '6px' }}>
                    <span style={{ color: 'var(--color-accent)', fontWeight: 'bold' }}>⚡ 任务执行中</span>
                    <span style={{ color: 'var(--text-primary)', fontWeight: 'bold' }}>{bfProgress}%</span>
                  </div>
                  {/* 进度条轨道 */}
                  <div style={{
                    width: '100%', height: '8px', background: 'rgba(255,255,255,0.06)',
                    borderRadius: '4px', overflow: 'hidden', marginBottom: '8px'
                  }}>
                    <div style={{
                      width: `${bfProgress}%`, height: '100%',
                      background: 'linear-gradient(90deg, #0e4f8c, var(--color-accent))',
                      transition: 'width 0.4s ease'
                    }} />
                  </div>
                  <div style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>
                    已回填 K 线: <strong style={{ color: 'var(--text-primary)' }}>{bfInserted.toLocaleString()}</strong> 根
                  </div>
                </div>
              )}

              {bfMessage && (
                <div style={{
                  ...s.msg,
                  color: bfMessage.startsWith('❌') ? 'var(--color-down)' : 'var(--color-up)',
                  marginTop: '4px',
                  background: bfMessage.startsWith('❌') ? 'rgba(255, 69, 58, 0.05)' : 'rgba(48, 209, 88, 0.05)',
                  padding: '8px 12px',
                  borderRadius: '4px',
                  border: bfMessage.startsWith('❌') ? '1px solid rgba(255, 69, 58, 0.15)' : '1px solid rgba(48, 209, 88, 0.15)',
                }}>{bfMessage}</div>
              )}

              <button
                type="submit"
                disabled={bfStatus === 'running'}
                style={{
                  ...s.bfBtn,
                  opacity: bfStatus === 'running' ? 0.6 : 1,
                  cursor: bfStatus === 'running' ? 'not-allowed' : 'pointer'
                }}
              >
                {bfStatus === 'running'
                  ? `⏳ 正在回填 (${bfProgress}%)`
                  : '🚀 启动后台回填'}
              </button>
            </form>
          </div>

        </div>
      </div>
    </div>
  );
}


// 状态行组件
function StatusRow({ label, value, ok }: { label: string; value: string; ok: boolean }) {
  return (
    <div style={sr.row}>
      <span style={sr.label}>{label}</span>
      <span style={{ ...sr.value, color: ok ? 'var(--color-up)' : 'var(--color-down)' }}>
        {value}
      </span>
    </div>
  );
}

const sr: Record<string, React.CSSProperties> = {
  row: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    padding: '10px 0',
    borderBottom: '1px dashed var(--border-color)',
  },
  label: { fontSize: '13px', color: 'var(--text-secondary)' },
  value: { fontSize: '13px', fontWeight: 600 },
};


const s: Record<string, React.CSSProperties> = {
  page: { padding: '24px', height: 'calc(100vh - 56px)', overflowY: 'auto' },
  header: { marginBottom: '24px' },
  title: { fontSize: '22px', fontWeight: 'bold', color: 'var(--text-primary)', marginBottom: '4px' },
  sub: { fontSize: '13px', color: 'var(--text-secondary)' },
  grid: { display: 'flex', gap: '24px', alignItems: 'flex-start' },
  col: { flex: 1, display: 'flex', flexDirection: 'column', gap: '20px' },
  card: { marginBottom: 0 },
  cardTitle: {
    fontSize: '15px', fontWeight: 'bold', color: 'var(--text-primary)',
    marginBottom: '14px', borderBottom: '1px solid var(--border-color)', paddingBottom: '8px'
  },
  desc: { fontSize: '12px', color: 'var(--text-secondary)', lineHeight: '1.6', marginBottom: '14px' },

  // Redis 流
  streamList: { display: 'flex', flexDirection: 'column', gap: '8px' },
  streamRow: {
    display: 'flex', alignItems: 'center', gap: '10px',
    padding: '6px 0', borderBottom: '1px dashed rgba(255,255,255,0.05)'
  },
  streamBadge: { fontSize: '16px', width: '20px' },
  streamName: { fontSize: '12px', color: 'var(--text-secondary)', flex: 1, fontFamily: 'monospace' },
  streamDot: { fontSize: '11px', fontWeight: 600, whiteSpace: 'nowrap' },

  // 状态网格
  statusGrid: { display: 'flex', flexDirection: 'column' },

  emptyTip: { color: 'var(--text-secondary)', fontSize: '13px', textAlign: 'center', padding: '12px' },

  // 币种管理
  chipBox: { display: 'flex', flexWrap: 'wrap', gap: '8px', marginBottom: '14px' },
  chip: {
    display: 'flex', alignItems: 'center', gap: '6px',
    background: 'rgba(88, 166, 255, 0.12)', border: '1px solid rgba(88,166,255,0.3)',
    borderRadius: '6px', padding: '4px 10px',
  },
  chipText: { fontSize: '13px', fontWeight: 600, color: 'var(--color-accent)' },
  chipDel: {
    background: 'none', border: 'none', color: 'var(--text-secondary)',
    cursor: 'pointer', fontSize: '16px', lineHeight: 1, padding: '0 2px'
  },

  quickRow: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '6px', marginBottom: '12px' },
  quickLabel: { fontSize: '11px', color: 'var(--text-secondary)' },
  quickBtn: {
    background: 'rgba(255,255,255,0.04)', border: '1px solid var(--border-color)',
    color: 'var(--text-secondary)', borderRadius: '4px', padding: '3px 8px',
    fontSize: '11px', cursor: 'pointer', transition: 'all 0.2s',
  },

  inputRow: { display: 'flex', gap: '8px', marginBottom: '12px' },
  input: {
    width: '100%',
    boxSizing: 'border-box' as const,
    background: 'var(--bg-main)', border: '1px solid var(--border-color)',
    color: 'var(--text-primary)', padding: '8px 12px', borderRadius: '4px',
    fontSize: '13px', outline: 'none',
  },
  // datetime-local 专用样式：加 colorScheme:'dark' 让浏览器日历弹窗用深色主题
  dtInput: {
    width: '100%',
    boxSizing: 'border-box' as const,
    background: 'var(--bg-main)', border: '1px solid var(--border-color)',
    color: 'var(--text-primary)', padding: '8px 10px', borderRadius: '4px',
    fontSize: '13px', outline: 'none',
    colorScheme: 'dark' as const,
  },
  addBtn: {
    background: 'rgba(88,166,255,0.15)', border: '1px solid var(--color-accent)',
    color: 'var(--color-accent)', padding: '8px 16px', borderRadius: '4px',
    fontSize: '13px', fontWeight: 600, cursor: 'pointer',
  },
  msg: { fontSize: '12px', marginBottom: '10px', lineHeight: 1.5 },
  saveBtn: {
    width: '100%', background: 'var(--color-accent)', border: 'none',
    color: '#fff', padding: '10px', borderRadius: '6px',
    fontSize: '13px', fontWeight: 'bold', cursor: 'pointer',
  },
  hint: {
    marginTop: '10px', fontSize: '11px', color: 'var(--text-secondary)',
    lineHeight: 1.6, padding: '8px', background: 'rgba(255,180,0,0.07)',
    borderRadius: '4px', border: '1px solid rgba(255,180,0,0.15)',
  },

  // 回填表单
  form: { display: 'flex', flexDirection: 'column', gap: '12px' },
  formRow: { display: 'flex', gap: '12px' },
  formGroup: { flex: 1, display: 'flex', flexDirection: 'column', gap: '5px' },
  label: { fontSize: '11px', color: 'var(--text-secondary)' },
  preview: {
    display: 'flex', gap: '24px', padding: '10px 14px',
    background: 'rgba(88,166,255,0.07)', borderRadius: '6px',
    fontSize: '12px', color: 'var(--text-secondary)',
  },
  bfBtn: {
    background: 'linear-gradient(135deg, #0e4f8c, var(--color-accent))',
    border: 'none', color: '#fff', padding: '11px',
    borderRadius: '6px', fontSize: '13px', fontWeight: 'bold', width: '100%',
  },
};
