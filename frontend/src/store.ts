/**
 * 全局状态管理 — Zustand Store
 */
import { create } from 'zustand';
import {
  fetchAccount,
  fetchPositions,
  fetchStats,
  fetchDailyPnl,
} from './lib/api';
import type {
  AccountInfo,
  Position,
  Stats,
  DailyPnl,
} from './lib/api';

interface AppState {
  // 账户
  account: AccountInfo | null;
  positions: Position[];
  stats: Stats | null;
  dailyPnl: DailyPnl[];
  // 实时价格
  prices: Record<string, number>;
  // 连接状态
  wsConnected: boolean;
  strategyPaused: boolean;
  // 加载状态
  loading: boolean;
  error: string | null;

  // Actions
  loadAccount: () => Promise<void>;
  loadPositions: () => Promise<void>;
  loadStats: () => Promise<void>;
  loadDailyPnl: () => Promise<void>;
  updatePrice: (symbol: string, price: number) => void;
  setWsConnected: (v: boolean) => void;
  setStrategyPaused: (v: boolean) => void;
}

export const useStore = create<AppState>((set) => ({
  account: null,
  positions: [],
  stats: null,
  dailyPnl: [],
  prices: {},
  wsConnected: false,
  strategyPaused: false,
  loading: false,
  error: null,

  loadAccount: async () => {
    try {
      const account = await fetchAccount();
      set({ account });
    } catch (e: any) {
      set({ error: e.message });
    }
  },

  loadPositions: async () => {
    try {
      const positions = await fetchPositions();
      set({ positions });
    } catch (e: any) {
      set({ error: e.message });
    }
  },

  loadStats: async () => {
    try {
      const stats = await fetchStats();
      set({ stats });
    } catch (e: any) {
      set({ error: e.message });
    }
  },

  loadDailyPnl: async () => {
    try {
      const dailyPnl = await fetchDailyPnl();
      set({ dailyPnl });
    } catch (e: any) {
      set({ error: e.message });
    }
  },

  updatePrice: (symbol, price) =>
    set((state) => ({ prices: { ...state.prices, [symbol]: price } })),

  setWsConnected: (v) => set({ wsConnected: v }),
  setStrategyPaused: (v) => set({ strategyPaused: v }),
}));
