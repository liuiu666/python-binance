#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
期货 BTCUSDT — 基于区间订单量变化的交易信号检测类。

使用方式：在 futures_orderbook_live_text.py 获取到聚合区间快照后，
将 [(interval_start, interval_end, bid_volume, ask_volume), ...] 与动态窗口大小传入本类。

核心逻辑：
  - 为每个价格区间维护滑动窗口（历史样本缓冲）。
  - 计算“当前区间量 / 历史平均量”的变化率（买、卖分别计算）。
  - 单一区间信号：
      * 买入：低价区买量变化率 >= 1.2、相邻高价区卖量变化率 <= 0.9；
      * 卖出：高价区卖量变化率 >= 1.2、相邻低价区买量变化率 <= 0.9。
  - 组合信号：
      * 强买入：满足买入且相邻高价区卖压下降；
      * 强卖出：满足卖出且相邻低价区买压下降。
  - 假信号过滤：需连续满足 ≥3 个数据点，且区间内 aggTrade 真实成交量的变化率达到确认阈值。

说明：无模拟数据；WebSocket 使用传入的 AsyncClient 启动 aggTrade 监听做成交确认。
"""

import asyncio
import time
import os
from collections import deque, defaultdict
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple

from binance.async_client import AsyncClient
from binance.ws.streams import BinanceSocketManager
from binance.enums import FuturesType


# ======= 参数（可按需求调整，默认遵循你的阈值说明） =======
PERSIST_SAMPLES = 2           # 连续满足次数（噪声过滤）

BUY_RATE_THRESHOLD = 1.4      # 买单变化率阈值（如20%增加）
ASK_DROP_THRESHOLD = 0.7      # 卖单变化率不足（如<10%增加，或下降）
SELL_RATE_THRESHOLD = 1.4     # 卖单变化率阈值（如20%增加）
BID_DROP_THRESHOLD = 0.7      # 买单变化率不足（如<10%增加，或下降）

CONFIRM_WINDOW_SEC = 45.0      # 成交量确认窗口（秒）
CONFIRM_TRADE_RATE_THRESHOLD = 1.2  # 成交确认变化率阈值（当前/历史均值）

MAX_BUFFER_SIZE = 200         # 每区间最多保留的历史样本数（用于动态窗口取后N）


@dataclass
class IntervalWindow:
    bid_vols: Deque[float]
    ask_vols: Deque[float]
    last_bid: float = 0.0
    last_ask: float = 0.0

    def push(self, bid: float, ask: float) -> None:
        self.last_bid = bid
        self.last_ask = ask
        self.bid_vols.append(bid)
        self.ask_vols.append(ask)

    def avg_bid_excluding_last_n(self, n: int) -> float:
        if len(self.bid_vols) <= 1 or n <= 1:
            return 0.0
        k = min(n, len(self.bid_vols) - 1)
        return sum(list(self.bid_vols)[-k:-1]) / (k - 1)

    def avg_ask_excluding_last_n(self, n: int) -> float:
        if len(self.ask_vols) <= 1 or n <= 1:
            return 0.0
        k = min(n, len(self.ask_vols) - 1)
        return sum(list(self.ask_vols)[-k:-1]) / (k - 1)


class SignalDetector:
    def __init__(self, symbol: str = "BTCUSDT"):
        self.symbol = symbol
        self._windows: Dict[float, IntervalWindow] = {}
        self._persist_buy: Dict[float, int] = defaultdict(int)
        self._persist_sell: Dict[float, int] = defaultdict(int)

        # 真实成交量确认缓冲（aggTrade）
        self._aggtrades: Deque[Tuple[float, float]] = deque()  # (ts_sec, qty)
        # 最近一次快照的区间列表与按区间的成交量缓冲
        self._last_intervals: List[Tuple[float, float]] = []  # [(start, end)]
        self._agg_by_interval: Dict[float, Deque[Tuple[float, float]]] = defaultdict(deque)  # start -> deque[(ts_sec, qty)]
        # 按方向的区间成交量缓冲（用于方向确认）
        self._agg_buy_by_interval: Dict[float, Deque[Tuple[float, float]]] = defaultdict(deque)  # start -> deque[(ts_sec, qty)]
        self._agg_sell_by_interval: Dict[float, Deque[Tuple[float, float]]] = defaultdict(deque)  # start -> deque[(ts_sec, qty)]
        # 区间成交量历史（旧：总量，保留诊断）
        self._trade_hist: Dict[float, Deque[float]] = {}
        # 按方向的区间成交量历史（用于方向确认变化率）
        self._trade_hist_buy: Dict[float, Deque[float]] = {}
        self._trade_hist_sell: Dict[float, Deque[float]] = {}

        # Binance 客户端与WS
        self._client: Optional[AsyncClient] = None
        self._bsm: Optional[BinanceSocketManager] = None
        self._trade_task: Optional[asyncio.Task] = None

        # 本地调试日志文件路径（examples目录下）
        self._log_path = os.path.join(os.path.dirname(__file__), 'signal_detector.log')

    def _write_log(self, msg: str) -> None:
        try:
            with open(self._log_path, 'a', encoding='utf-8') as f:
                f.write(msg + "\n")
        except Exception:
            # 日志失败不影响主逻辑
            ...

    # --------- WebSocket：期货 aggTrade 成交量确认 ---------
    async def _trade_listener(self):
        assert self._bsm is not None
        sock = self._bsm.aggtrade_futures_socket(self.symbol, futures_type=FuturesType.USD_M)
        async with sock as s:
            while True:
                msg = await s.recv()
                if not msg:
                    continue
                try:
                    # 期货组合流为 {"stream": "...", "data": {...}}，需解包 data
                    payload = msg.get("data", msg)
                    qty = float(payload.get("q", "0"))
                    p_str = payload.get("p")
                    if p_str is None:
                        # 缺少价格无法归属区间，跳过本条
                        continue
                    price = float(p_str)
                    ts_ms = float(payload.get("T", payload.get("E", time.time() * 1000)))
                    ts_sec = ts_ms / 1000.0
                    now = time.time()
                    # 方向：m=True 表示买家是做市商 => 主动卖；m=False => 主动买
                    is_sell = bool(payload.get("m", False))
                    # 将成交分配到最近一次快照的对应区间（左闭右开）
                    assigned = False
                    for start, end in self._last_intervals:
                        if start <= price < end:
                            if is_sell:
                                dq = self._agg_sell_by_interval[start]
                            else:
                                dq = self._agg_buy_by_interval[start]
                            dq.append((ts_sec, qty))
                            assigned = True
                            # 裁剪该区间的滚动窗口
                            while dq and (now - dq[0][0] > CONFIRM_WINDOW_SEC):
                                dq.popleft()
                            break
                    if not assigned:
                        # 若未能归属到任何区间，保留全局缓冲以备诊断
                        self._aggtrades.append((ts_sec, qty))
                        while self._aggtrades and (now - self._aggtrades[0][0] > CONFIRM_WINDOW_SEC):
                            self._aggtrades.popleft()
                except Exception:
                    continue

    def _recent_trade_volume(self) -> float:
        # 保留原方法用于诊断（全局成交量）
        now = time.time()
        return sum(q for ts, q in self._aggtrades if now - ts <= CONFIRM_WINDOW_SEC)

    def _recent_trade_volume_for(self, start_price: float) -> float:
        now = time.time()
        dq = self._agg_by_interval.get(start_price)
        if not dq:
            return 0.0
        return sum(q for ts, q in dq if now - ts <= CONFIRM_WINDOW_SEC)

    def _current_interval_step(self) -> Optional[float]:
        """返回最近一次快照的区间步长（end - start）。若不可用则返回 None。"""
        if not self._last_intervals:
            return None
        s0, e0 = self._last_intervals[0]
        step = e0 - s0
        return step if step > 0 else None

    def _recent_buy_volume_for(self, start_price: float) -> float:
        """按当前区间起点检索最近买成交量；若精确键未命中，则回退到最近键（距离<=step/2）。"""
        now = time.time()
        dq = self._agg_buy_by_interval.get(start_price)
        if dq:
            return sum(q for ts, q in dq if now - ts <= CONFIRM_WINDOW_SEC)
        step = self._current_interval_step()
        if step is None or not self._agg_buy_by_interval:
            return 0.0
        # 寻找最近的区间键
        try:
            nearest_key = min(self._agg_buy_by_interval.keys(), key=lambda k: abs(k - start_price))
        except ValueError:
            return 0.0
        if abs(nearest_key - start_price) <= (step / 2.0):
            dq2 = self._agg_buy_by_interval.get(nearest_key)
            if dq2:
                return sum(q for ts, q in dq2 if now - ts <= CONFIRM_WINDOW_SEC)
        return 0.0

    def _recent_sell_volume_for(self, start_price: float) -> float:
        """按当前区间起点检索最近卖成交量；若精确键未命中，则回退到最近键（距离<=step/2）。"""
        now = time.time()
        dq = self._agg_sell_by_interval.get(start_price)
        if dq:
            return sum(q for ts, q in dq if now - ts <= CONFIRM_WINDOW_SEC)
        step = self._current_interval_step()
        if step is None or not self._agg_sell_by_interval:
            return 0.0
        try:
            nearest_key = min(self._agg_sell_by_interval.keys(), key=lambda k: abs(k - start_price))
        except ValueError:
            return 0.0
        if abs(nearest_key - start_price) <= (step / 2.0):
            dq2 = self._agg_sell_by_interval.get(nearest_key)
            if dq2:
                return sum(q for ts, q in dq2 if now - ts <= CONFIRM_WINDOW_SEC)
        return 0.0

    # 已移除绝对量阈值确认方法，成交确认改为变化率逻辑

    # --------- 信号判定核心 ---------
    def _ensure_window(self, start_price: float) -> IntervalWindow:
        win = self._windows.get(start_price)
        if not win:
            win = IntervalWindow(deque(maxlen=MAX_BUFFER_SIZE), deque(maxlen=MAX_BUFFER_SIZE))
            self._windows[start_price] = win
        return win

    def _compute_rates(self, win: IntervalWindow, window_size: int) -> Tuple[float, float]:
        avg_bid = win.avg_bid_excluding_last_n(window_size)
        avg_ask = win.avg_ask_excluding_last_n(window_size)
        # 防止除零：若历史均值为0，则在当前有正量时视为极大变化（inf），无量视为0
        bid_rate = (win.last_bid / avg_bid) if avg_bid > 0 else (float("inf") if win.last_bid > 0 else 0.0)
        ask_rate = (win.last_ask / avg_ask) if avg_ask > 0 else (float("inf") if win.last_ask > 0 else 0.0)
        return bid_rate, ask_rate

    def _neighbors(self, sorted_starts: List[float], idx: int) -> Tuple[Optional[float], Optional[float]]:
        left = sorted_starts[idx - 1] if idx - 1 >= 0 else None
        right = sorted_starts[idx + 1] if idx + 1 < len(sorted_starts) else None
        return left, right

    def _check_and_emit(self, starts: List[float], window_size: int, interval_step: float) -> None:
        for idx, start in enumerate(starts):
            win = self._windows.get(start)
            if not win:
                continue
            bid_rate, ask_rate = self._compute_rates(win, window_size)
            # 邻近区间（用于方向成交量合并确认：买合并左邻，卖合并右邻）
            left, right = self._neighbors(starts, idx)

            # 单一区间条件
            buy_ok = (
                bid_rate >= BUY_RATE_THRESHOLD and
                ask_rate <= ASK_DROP_THRESHOLD
            )
            sell_ok = (
                ask_rate >= SELL_RATE_THRESHOLD and
                bid_rate <= BID_DROP_THRESHOLD
            )

            # 连续满足计数（噪声过滤）
            if buy_ok:
                self._persist_buy[start] += 1
            else:
                self._persist_buy[start] = 0

            if sell_ok:
                self._persist_sell[start] += 1
            else:
                self._persist_sell[start] = 0

            # 方向成交量（用于日志与确认）：合并邻近区间
            tv_buy_cur = self._recent_buy_volume_for(start)
            tv_sell_cur = self._recent_sell_volume_for(start)
            tv_buy_adj = self._recent_buy_volume_for(left) if left is not None else 0.0
            tv_sell_adj = self._recent_sell_volume_for(right) if right is not None else 0.0
            trade_vol_buy_combined = tv_buy_cur + tv_buy_adj
            trade_vol_sell_combined = tv_sell_cur + tv_sell_adj

            # 候选阶段调试日志（仅当满足单区间条件时记录）
            if buy_ok:
                self._write_log(
                    f"CANDIDATE BUY start={start:.3f} step={interval_step:.3f} "
                    f"bid_rate={bid_rate:.3f} ask_rate={ask_rate:.3f} bid_vol={win.last_bid:.6f} "
                    f"persist={self._persist_buy[start]}/{PERSIST_SAMPLES} trade_vol={trade_vol_buy_combined:.6f}"
                )
            if sell_ok:
                self._write_log(
                    f"CANDIDATE SELL start={start:.3f} step={interval_step:.3f} "
                    f"ask_rate={ask_rate:.3f} bid_rate={bid_rate:.3f} ask_vol={win.last_ask:.6f} "
                    f"persist={self._persist_sell[start]}/{PERSIST_SAMPLES} trade_vol={trade_vol_sell_combined:.6f}"
                )
            left_win = self._windows.get(left) if left is not None else None
            right_win = self._windows.get(right) if right is not None else None

            # 组合条件（增强可信度）
            adj_up_ok = (right_win is not None and self._compute_rates(right_win, window_size)[1] <= ASK_DROP_THRESHOLD)
            adj_dn_ok = (left_win is not None and self._compute_rates(left_win, window_size)[0] <= BID_DROP_THRESHOLD)
            strong_buy = buy_ok and adj_up_ok
            strong_sell = sell_ok and adj_dn_ok

            ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            trade_vol_buy = trade_vol_buy_combined
            trade_vol_sell = trade_vol_sell_combined
            # 成交确认采用方向变化率：当前该方向成交量 / 该方向历史均值
            confirm_ok_buy = False
            confirm_ok_sell = False
            hist_b = self._trade_hist_buy.get(start)
            if hist_b is not None and len(hist_b) > 1:
                k = min(window_size, len(hist_b))
                prev_samples = list(hist_b)[-k:-1]
                if len(prev_samples) > 0:
                    avg_prev = sum(prev_samples) / len(prev_samples)
                    trade_rate_b = (trade_vol_buy / avg_prev) if avg_prev > 0 else 0.0
                    confirm_ok_buy = trade_rate_b >= CONFIRM_TRADE_RATE_THRESHOLD
            hist_s = self._trade_hist_sell.get(start)
            if hist_s is not None and len(hist_s) > 1:
                k = min(window_size, len(hist_s))
                prev_samples = list(hist_s)[-k:-1]
                if len(prev_samples) > 0:
                    avg_prev = sum(prev_samples) / len(prev_samples)
                    trade_rate_s = (trade_vol_sell / avg_prev) if avg_prev > 0 else 0.0
                    confirm_ok_sell = trade_rate_s >= CONFIRM_TRADE_RATE_THRESHOLD

            # 单行合并格式日志（中文输出）：仅在出现候选或可能发出时记录
            if buy_ok or sell_ok:
                # 发出类型（可能为无、买入/强买入、卖出/强卖出；若两者同时满足则以买入优先显示）
                emit = "无"
                # 根据方向选择要显示的成交量和确认标志（买入优先）
                display_trade_vol = trade_vol_buy if buy_ok else (trade_vol_sell if sell_ok else 0.0)
                display_confirm = confirm_ok_buy if buy_ok else (confirm_ok_sell if sell_ok else False)
                if self._persist_buy[start] >= PERSIST_SAMPLES and confirm_ok_buy:
                    emit = "强买入" if strong_buy else "买入"
                    display_trade_vol = trade_vol_buy
                    display_confirm = True
                elif self._persist_sell[start] >= PERSIST_SAMPLES and confirm_ok_sell:
                    emit = "强卖出" if strong_sell else "卖出"
                    display_trade_vol = trade_vol_sell
                    display_confirm = True

                self._write_log(
                    (
                        f"[{ts_str}] 合约={self.symbol} 范围=[{start:.3f}-{start + interval_step:.3f}] 窗口={window_size} "
                        f"买变率={bid_rate:.3f} 卖变率={ask_rate:.3f} 买量={win.last_bid:.6f} 卖量={win.last_ask:.6f} "
                        f"买条件={'是' if buy_ok else '否'} 卖条件={'是' if sell_ok else '否'} "
                        f"上邻卖压下降={'是' if (right_win is not None and adj_up_ok) else ('无' if right_win is None else '否')} "
                        f"下邻买压下降={'是' if (left_win is not None and adj_dn_ok) else ('无' if left_win is None else '否')} "
                        f"买持续={self._persist_buy[start]}/{PERSIST_SAMPLES} 卖持续={self._persist_sell[start]}/{PERSIST_SAMPLES} "
                        f"成交量={display_trade_vol:.6f} 成交确认={'是' if display_confirm else '否'} 发出={emit}"
                    )
                )

            # 发出信号：需连续满足且真实成交量确认
            if self._persist_buy[start] >= PERSIST_SAMPLES and confirm_ok_buy:
                level = "STRONG_BUY" if strong_buy else "BUY"
                print(
                    f"[{ts_str}] {level} @ [{start:.3f} - {start + interval_step:.3f}] "
                    f"bid_rate={bid_rate:.3f} ask_rate={ask_rate:.3f} bid_vol={win.last_bid:.6f}"
                )
                self._persist_buy[start] = 0

            if self._persist_sell[start] >= PERSIST_SAMPLES and confirm_ok_sell:
                level = "STRONG_SELL" if strong_sell else "SELL"
                print(
                    f"[{ts_str}] {level} @ [{start:.3f} - {start + interval_step:.3f}] "
                    f"ask_rate={ask_rate:.3f} bid_rate={bid_rate:.3f} ask_vol={win.last_ask:.6f}"
                )
                self._persist_sell[start] = 0

    # --------- 对外接口 ---------
    async def start(self, client: AsyncClient) -> None:
        """使用已存在的 AsyncClient 启动期货 aggTrade 监听（用于成交量确认）。"""
        self._client = client
        self._bsm = BinanceSocketManager(self._client)
        self._trade_task = asyncio.create_task(self._trade_listener())

    async def stop(self) -> None:
        try:
            if self._trade_task:
                self._trade_task.cancel()
                try:
                    await self._trade_task
                except Exception:
                    ...
        except Exception:
            ...

    def process_snapshot(
        self,
        snapshot_rows: List[Tuple[float, float, float, float]],
        window_size: int,
        timestamp: Optional[float] = None,
    ) -> None:
        """处理一次聚合快照并进行信号判定。

        :param snapshot_rows: [(start, end, bid_vol, ask_vol), ...]
        :param window_size: 动态滑窗大小（由调用方计算并传入）
        :param timestamp: 可选外部时间戳，不参与逻辑，仅保留接口兼容
        """
        if not snapshot_rows or window_size <= 1:
            return
        snapshot_rows.sort(key=lambda x: x[0])
        starts: List[float] = []
        interval_step: float = 0.0
        for start, end, bid, ask in snapshot_rows:
            starts.append(start)
            interval_step = max(interval_step, end - start)
            win = self._ensure_window(start)
            win.push(bid, ask)

            # 记录该区间最近 1 秒真实成交量到历史（用于变化率确认）
            tv_buy = self._recent_buy_volume_for(start)
            hist_b = self._trade_hist_buy.get(start)
            if hist_b is None:
                hist_b = deque(maxlen=MAX_BUFFER_SIZE)
                self._trade_hist_buy[start] = hist_b
            hist_b.append(tv_buy)

            tv_sell = self._recent_sell_volume_for(start)
            hist_s = self._trade_hist_sell.get(start)
            if hist_s is None:
                hist_s = deque(maxlen=MAX_BUFFER_SIZE)
                self._trade_hist_sell[start] = hist_s
            hist_s.append(tv_sell)

        # 更新最近一次快照的区间列表，并同步清理过期区间的成交量缓冲
        self._last_intervals = [(s, s + interval_step) for s in starts]
        # 仅按时间裁剪方向缓冲；当队列为空时删除键（不因快照区间变化而直接删除）
        now = time.time()
        for key, dq in list(self._agg_buy_by_interval.items()):
            while dq and (now - dq[0][0] > CONFIRM_WINDOW_SEC):
                dq.popleft()
            if not dq:
                del self._agg_buy_by_interval[key]
        for key, dq in list(self._agg_sell_by_interval.items()):
            while dq and (now - dq[0][0] > CONFIRM_WINDOW_SEC):
                dq.popleft()
            if not dq:
                del self._agg_sell_by_interval[key]

        self._check_and_emit(starts, window_size=window_size, interval_step=interval_step)