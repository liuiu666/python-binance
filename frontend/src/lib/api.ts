/**
 * API 请求封装
 * 统一调用后端 REST API
 */

const API_BASE = '/api';

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!resp.ok) {
    throw new Error(`API Error: ${resp.status} ${resp.statusText}`);
  }
  return resp.json();
}

// 账户信息
export interface AccountInfo {
  total_wallet_balance: number;
  total_unrealized_profit: number;
  total_margin_balance: number;
  available_balance: number;
}

export const fetchAccount = () => request<AccountInfo>('/account');

// 持仓
export interface Position {
  symbol: string;
  side: string;
  quantity: number;
  entry_price: number;
  unrealized_pnl: number;
  leverage: number;
  opened_at: string;
}

export const fetchPositions = () => request<Position[]>('/positions');

// 交易记录
export interface TradeRecord {
  signal_id: string;
  symbol: string;
  side: string;
  action: string;
  quantity: number;
  entry_price: number;
  exit_price: number;
  pnl: number;
  fee: number;
  strategy: string;
  reason: string;
  status: string;
  opened_at: string;
  closed_at: string;
}

export interface TradeListResponse {
  total: number;
  page: number;
  size: number;
  data: TradeRecord[];
}

export const fetchTrades = (page = 1, size = 20) =>
  request<TradeListResponse>(`/trades?page=${page}&size=${size}`);

// 统计
export interface Stats {
  total_trades: number;
  win_trades: number;
  loss_trades: number;
  win_rate: number;
  profit_factor: number;
  total_pnl: number;
  max_drawdown: number;
}

export const fetchStats = () => request<Stats>('/stats');

// 每日盈亏
export interface DailyPnl {
  trade_date: string;
  total_pnl: number;
  total_fee: number;
  trade_count: number;
  win_count: number;
  loss_count: number;
}

export const fetchDailyPnl = (days = 30) =>
  request<DailyPnl[]>(`/daily-pnl?days=${days}`);

// 控制操作
export const closeAll = () =>
  request<{ status: string }>('/close-all', { method: 'POST' });

export const togglePause = (paused: boolean) =>
  request<{ status: string }>('/pause', {
    method: 'POST',
    body: JSON.stringify({ paused }),
  });

export const emergencyOrder = (symbol: string, side: string, quantity: number) =>
  request<{ status: string }>('/emergency-order', {
    method: 'POST',
    body: JSON.stringify({ symbol, side, quantity }),
  });

export interface KLineData {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export const fetchKlines = (symbol: string, interval = '1m', limit = 100) =>
  request<KLineData[]>(`/klines?symbol=${symbol}&interval=${interval}&limit=${limit}`);

/** 加载更早的历史K线数据（end_time之前的） */
export const fetchKlinesMore = (symbol: string, interval = '1m', limit = 500, endTime: number) =>
  request<KLineData[]>(`/klines?symbol=${symbol}&interval=${interval}&limit=${limit}&end_time=${endTime}`);
