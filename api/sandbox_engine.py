"""
高频沙盒模拟交易引擎 (HFT Sandbox Trading Engine)
在后台持续监听实时行情 (来自 Redis Streams)，运行策略并撮合委托
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Dict, List, Optional

from common.db import db
from common.logger import get_logger
from common.redis_client import redis_client

logger = get_logger(__name__)


class SandboxEngine:
    def __init__(self) -> None:
        # 沙盒运行状态（内存单例）
        self.state: Dict[str, Any] = {
            "balance": 100000.0,
            "position": None,  # dict or None
            "limitOrders": [],
            "tradeLogs": [],
            "autoStrategy": "NONE",
            "autoQty": "1",
            "autoInterval": 3,
            "tpPercent": 0.8,
            "slPercent": 0.4,
            "tsActivation": 0.6,
            "tsCallback": 0.2,
            "symbol": "BTCUSDT",
        }
        self.running_task: Optional[asyncio.Task] = None
        self._running = False
        self.db_save_pending = False
        self.last_auto_trade_time = 0.0

        # 行情状态缓存
        self.current_price = 0.0
        self.best_bid = 0.0
        self.best_ask = 0.0
        self.depth_bids: List[Dict[str, float]] = []
        self.depth_asks: List[Dict[str, float]] = []

    async def start(self) -> None:
        """启动沙盒引擎"""
        self._running = True
        await self.load_state()
        self.running_task = asyncio.create_task(self._loop())
        logger.info("sandbox.engine.started", symbol=self.state["symbol"])

    async def stop(self) -> None:
        """停止沙盒引擎"""
        self._running = False
        if self.running_task:
            self.running_task.cancel()
            try:
                await self.running_task
            except asyncio.CancelledError:
                pass
        await self.save_state()
        logger.info("sandbox.engine.stopped")

    async def load_state(self) -> None:
        """从数据库恢复状态"""
        try:
            async with db.pool.acquire() as conn:
                val = await conn.fetchval(
                    "SELECT value FROM system_config WHERE key = 'sandbox:state'"
                )
                if val is not None:
                    loaded = json.loads(val)
                    if isinstance(loaded, dict):
                        # 保留默认值字段以防加载历史空字段
                        self.state.update(loaded)
                        logger.info("sandbox.engine.state_loaded", state=self.state)
        except Exception:
            logger.exception("sandbox.engine.load_state_failed")

    async def save_state(self) -> None:
        """保存状态到数据库"""
        try:
            async with db.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO system_config (key, value, description, updated_at)
                    VALUES ('sandbox:state', $1, '沙盒高频实时交易模拟器状态', NOW())
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                    """,
                    json.dumps(self.state),
                )
            self.db_save_pending = False
            logger.debug("sandbox.engine.saved_to_db")
        except Exception:
            logger.exception("sandbox.engine.save_state_failed")

    async def on_state_changed(self) -> None:
        """状态变更处理，广播并排队保存"""
        # 1. 广播给前端 WebSocket
        try:
            from api.ws_handler import manager
            await manager.broadcast("sandbox:state", self.state)
        except Exception:
            pass

        # 2. 异步延迟去重保存到数据库
        if not self.db_save_pending:
            self.db_save_pending = True
            asyncio.create_task(self._debounce_save())

    async def _debounce_save(self) -> None:
        await asyncio.sleep(1.0)
        if self.db_save_pending:
            await self.save_state()

    async def update_config(self, config: Dict[str, Any]) -> None:
        """更新策略配置"""
        old_symbol = self.state.get("symbol", "BTCUSDT").upper()
        
        # 允许更新的参数
        for k in ["autoStrategy", "autoQty", "autoInterval", "tpPercent", "slPercent", "tsActivation", "tsCallback", "symbol"]:
            if k in config:
                self.state[k] = config[k]
                
        new_symbol = self.state.get("symbol", "BTCUSDT").upper()
        if old_symbol != new_symbol:
            logger.info("sandbox.engine.symbol_changed", old=old_symbol, new=new_symbol)
            # 切币时清空限价单与持仓以防止跨币种订单逻辑混乱
            self.state["limitOrders"] = []
            self.state["position"] = None
            self.current_price = 0.0
            self.best_bid = 0.0
            self.best_ask = 0.0
            self.depth_bids = []
            self.depth_asks = []
            
        await self.on_state_changed()

    async def reset_account(self) -> None:
        """重置模拟账户"""
        self.state["balance"] = 100000.0
        self.state["position"] = None
        self.state["limitOrders"] = []
        self.state["tradeLogs"] = []
        self.state["autoStrategy"] = "NONE"
        await self.on_state_changed()
        logger.info("sandbox.engine.account_reset")

    async def place_manual_order(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """手动下单接口"""
        symbol = req.get("symbol", "").upper()
        side = req.get("side", "").upper()
        order_type = req.get("type", "").upper()
        qty = float(req.get("qty", 0))
        price = float(req.get("price", 0))

        if symbol and symbol != self.state["symbol"].upper():
            await self.update_config({"symbol": symbol})

        if side not in ("BUY", "SELL"):
            return {"status": "error", "message": "Invalid side"}
        if qty <= 0:
            return {"status": "error", "message": "Invalid quantity"}

        if order_type == "MARKET":
            await self.execute_market_order(side, qty)
            return {"status": "success", "message": "Market order executed"}
        elif order_type == "LIMIT":
            if price <= 0:
                return {"status": "error", "message": "Invalid limit price"}
            # 挂限价单
            order_item = {
                "id": uuid.uuid4().hex[:8],
                "symbol": self.state["symbol"],
                "side": side,
                "price": price,
                "qty": qty,
                "timestamp": time.strftime("%H:%M:%S")
            }
            self.state["limitOrders"].insert(0, order_item)
            await self.on_state_changed()
            return {"status": "success", "message": "Limit order placed"}
        
        return {"status": "error", "message": "Invalid order type"}

    async def cancel_limit_order(self, order_id: str) -> None:
        """取消限价单"""
        orders = self.state.get("limitOrders", [])
        filtered = [o for o in orders if o.get("id") != order_id]
        if len(orders) != len(filtered):
            self.state["limitOrders"] = filtered
            await self.on_state_changed()
            logger.info("sandbox.engine.order_cancelled", id=order_id)

    async def _loop(self) -> None:
        """实时行情轮询与撮合主循环"""
        logger.info("sandbox.engine.loop_started")
        
        while self._running:
            try:
                symbol_lower = self.state["symbol"].lower()
                depth_stream = f"depth:{symbol_lower}"
                ticker_stream = f"ticker:{symbol_lower}"
                
                # 初始化读取流的指针，直接定位到当前末尾，防止读取堆积历史数据
                last_ids = {depth_stream: "$", ticker_stream: "$"}
                
                # 执行首次试探以获取最新的 message_id
                init_res = await redis_client.client.xread(streams=last_ids, count=1, block=10)
                if init_res:
                    for stream, msgs in init_res:
                        if msgs:
                            last_ids[stream] = msgs[-1][0]
                
                logger.info("sandbox.engine.consuming_redis", streams=list(last_ids.keys()))

                # 开始消费
                while self._running and symbol_lower == self.state["symbol"].lower():
                    # 轮询获取实时行情
                    res = await redis_client.client.xread(streams=last_ids, count=10, block=200)
                    if not res:
                        await asyncio.sleep(0.01)
                        continue
                        
                    depth_updated = False
                    ticker_updated = False
                    latest_depth = None
                    latest_ticker = None
                    
                    for stream, msgs in res:
                        if msgs:
                            last_ids[stream] = msgs[-1][0]
                            for _, fields in msgs:
                                data_str = fields.get("data")
                                if not data_str:
                                    continue
                                try:
                                    parsed_data = json.loads(data_str)
                                    if stream.startswith("depth:"):
                                        latest_depth = parsed_data
                                        depth_updated = True
                                    elif stream.startswith("ticker:"):
                                        latest_ticker = parsed_data
                                        ticker_updated = True
                                except Exception:
                                    pass
                    
                    # 优先计算最优买卖价与限价撮合，再做策略深度运算
                    if ticker_updated and latest_ticker:
                        await self.handle_ticker_tick(latest_ticker)
                    if depth_updated and latest_depth:
                        await self.handle_depth_tick(latest_depth)
                    
                    await asyncio.sleep(0.01)
                    
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("sandbox.engine.loop_error")
                await asyncio.sleep(1.0)

    async def handle_ticker_tick(self, ticker: Dict[str, Any]) -> None:
        """处理最优报价"""
        try:
            bid = float(ticker.get("b", 0))
            ask = float(ticker.get("a", 0))
            if bid <= 0 or ask <= 0:
                return
            
            self.best_bid = bid
            self.best_ask = ask
            self.current_price = (bid + ask) / 2.0
            
            # 1. 撮合限价单
            await self.check_limit_orders()
            # 2. 检查持仓的风控（止盈止损/移动锁盈）
            await self.check_tp_sl()
        except Exception:
            logger.exception("sandbox.engine.ticker_tick_error")

    async def handle_depth_tick(self, depth: Dict[str, Any]) -> None:
        """处理盘口深度，驱动高频策略"""
        try:
            bids_raw = depth.get("b", [])
            asks_raw = depth.get("a", [])
            
            self.depth_bids = [{"price": float(item[0]), "amount": float(item[1])} for item in bids_raw]
            self.depth_asks = [{"price": float(item[0]), "amount": float(item[1])} for item in asks_raw]
            
            # 3. 运行自动交易策略模型
            await self.check_strategy()
        except Exception:
            logger.exception("sandbox.engine.depth_tick_error")

    async def check_limit_orders(self) -> None:
        """撮合挂单列表"""
        limit_orders = self.state.get("limitOrders", [])
        if not limit_orders or self.current_price <= 0:
            return
        
        remaining = []
        state_changed = False
        order_filled = False
        
        new_balance = self.state["balance"]
        new_position = self.state["position"]
        
        for order in limit_orders:
            # 每次 tick 最多只成交一个单，防止由于多空并发挂单导致仓位逻辑错乱
            if order_filled:
                remaining.append(order)
                continue
                
            fill = False
            execution_price = float(order["price"])
            qty = float(order["qty"])
            side = order["side"]
            
            if side == "BUY" and self.best_ask > 0 and self.best_ask <= execution_price:
                fill = True
            elif side == "SELL" and self.best_bid > 0 and self.best_bid >= execution_price:
                fill = True
                
            if fill:
                order_filled = True
                state_changed = True
                fee = qty * execution_price * 0.0002  # Maker 手续费 0.02%
                trade_pnl = 0.0
                
                if side == "BUY":
                    if new_position and new_position["side"] == "SHORT":
                        # 平空
                        size_diff = new_position["size"] - qty
                        trade_pnl = qty * (new_position["entryPrice"] - execution_price) - fee
                        new_balance += qty * (new_position["entryPrice"] - execution_price) - fee
                        
                        if size_diff > 0.00001:
                            new_position["size"] = size_diff
                        elif abs(size_diff) <= 0.00001:
                            new_position = None
                        else:
                            # 反手开多
                            new_position = {
                                "symbol": self.state["symbol"],
                                "side": "LONG",
                                "size": abs(size_diff),
                                "entryPrice": execution_price,
                                "peakPrice": execution_price
                            }
                            new_balance -= fee
                    elif new_position and new_position["side"] == "LONG":
                        # 加多
                        old_total = new_position["size"] * new_position["entryPrice"]
                        new_total = qty * execution_price
                        new_position["size"] += qty
                        new_position["entryPrice"] = (old_total + new_total) / new_position["size"]
                        new_balance -= fee
                    else:
                        # 新开多
                        new_position = {
                            "symbol": self.state["symbol"],
                            "side": "LONG",
                            "size": qty,
                            "entryPrice": execution_price,
                            "peakPrice": execution_price
                        }
                        new_balance -= fee
                else:  # SELL
                    if new_position and new_position["side"] == "LONG":
                        # 平多
                        size_diff = new_position["size"] - qty
                        trade_pnl = qty * (execution_price - new_position["entryPrice"]) - fee
                        new_balance += qty * (execution_price - new_position["entryPrice"]) - fee
                        
                        if size_diff > 0.00001:
                            new_position["size"] = size_diff
                        elif abs(size_diff) <= 0.00001:
                            new_position = None
                        else:
                            # 反手开空
                            new_position = {
                                "symbol": self.state["symbol"],
                                "side": "SHORT",
                                "size": abs(size_diff),
                                "entryPrice": execution_price,
                                "peakPrice": execution_price
                            }
                            new_balance -= fee
                    elif new_position and new_position["side"] == "SHORT":
                        # 加空
                        old_total = new_position["size"] * new_position["entryPrice"]
                        new_total = qty * execution_price
                        new_position["size"] += qty
                        new_position["entryPrice"] = (old_total + new_total) / new_position["size"]
                        new_balance -= fee
                    else:
                        # 新开空
                        new_position = {
                            "symbol": self.state["symbol"],
                            "side": "SHORT",
                            "size": qty,
                            "entryPrice": execution_price,
                            "peakPrice": execution_price
                        }
                        new_balance -= fee
                
                # 记录交易明细
                log_item = {
                    "id": uuid.uuid4().hex[:8],
                    "timestamp": time.strftime("%H:%M:%S"),
                    "time": int(time.time()),
                    "symbol": self.state["symbol"],
                    "side": side,
                    "action": "OPEN" if new_position and (new_position["side"] == side) else "CLOSE",
                    "type": "LIMIT",
                    "price": execution_price,
                    "qty": qty,
                    "fee": fee,
                    "pnl": trade_pnl
                }
                self.state["tradeLogs"].insert(0, log_item)
                self.state["tradeLogs"] = self.state["tradeLogs"][:200]
                logger.info("sandbox.engine.limit_filled", log=log_item)
            else:
                remaining.append(order)
                
        if state_changed:
            self.state["balance"] = new_balance
            self.state["position"] = new_position
            self.state["limitOrders"] = remaining
            await self.on_state_changed()

    async def check_tp_sl(self) -> None:
        """检查移动止损、止盈、止损规则"""
        position = self.state.get("position")
        if not position or self.current_price <= 0:
            return
            
        entry = position["entryPrice"]
        side = position["side"]
        
        # 1. 刷新开仓以来的价格极值 (最高/最低)
        current_peak = position.get("peakPrice", entry)
        next_peak = current_peak
        
        if side == "LONG":
            if self.current_price > current_peak:
                next_peak = self.current_price
        else:
            if self.current_price < current_peak:
                next_peak = self.current_price
                
        if next_peak != position.get("peakPrice"):
            position["peakPrice"] = next_peak
            self.state["position"] = position
            # 记录变更，但不在此高频循环写入DB，由防抖完成
            self.db_save_pending = True
            
        # 2. 计算当前收益率与极值收益率
        pnl_pct = ((self.current_price - entry) / entry) * 100 if side == "LONG" else ((entry - self.current_price) / entry) * 100
        max_profit_pct = ((next_peak - entry) / entry) * 100 if side == "LONG" else ((entry - next_peak) / entry) * 100
        
        sl_percent = self.state["slPercent"]
        tp_percent = self.state["tpPercent"]
        ts_activation = self.state["tsActivation"]
        ts_callback = self.state["tsCallback"]
        
        trigger_close = False
        reason = ""
        
        # A. 硬止损规则 (对冲暴跌风险)
        if pnl_pct <= -sl_percent:
            trigger_close = True
            reason = f"[智能风控] 触发硬止损! 当前盈亏: {pnl_pct:.3f}% (止损限制: -{sl_percent}%)"
            
        # B. 移动锁盈规则 (Trailing Stop-Loss)
        elif max_profit_pct >= ts_activation:
            # 计算回撤幅度百分比
            retraction = ((next_peak - self.current_price) / next_peak) * 100 if side == "LONG" else ((self.current_price - next_peak) / next_peak) * 100
            if retraction >= ts_callback:
                trigger_close = True
                reason = f"[智能风控] 触发移动锁盈! 最高收益: {max_profit_pct:.3f}%, 回撤: {retraction:.3f}% (回调限制: {ts_callback}%)"
                
        # C. 固定硬止盈规则 (超高爆发平仓)
        elif pnl_pct >= tp_percent:
            trigger_close = True
            reason = f"[智能风控] 触发固定止盈! 当前盈亏: {pnl_pct:.3f}% (止盈设置: +{tp_percent}%)"
            
        if trigger_close:
            logger.warning("sandbox.engine.risk_triggered", symbol=self.state["symbol"], reason=reason)
            # 记录平仓原因，暂时推送到 tradeLogs 后续打印在前端
            await self.execute_market_close()
            
            # 给最后的 tradeLog 加上平仓原因为了可视化直观显示
            if self.state["tradeLogs"]:
                self.state["tradeLogs"][0]["reason"] = reason
                await self.on_state_changed()

    async def check_strategy(self) -> None:
        """检查策略信号 (IMBALANCE / MICROPRICE / SCALPING)"""
        auto_strategy = self.state.get("autoStrategy", "NONE")
        if auto_strategy == "NONE" or self.current_price <= 0 or not self.depth_bids or not self.depth_asks:
            return
            
        # 策略开仓约束：已有仓位时不重复开仓
        if self.state.get("position") is not None:
            return
            
        # 冷却期限制
        now = time.time()
        if now - self.last_auto_trade_time < self.state["autoInterval"]:
            return
            
        usdt_val = float(self.state["autoQty"])
        if usdt_val <= 0:
            return
        qty_val = usdt_val / self.current_price
        
        best_bid_price = self.depth_bids[0]["price"]
        best_ask_price = self.depth_asks[0]["price"]
        
        trigger_side = None
        
        if auto_strategy == "IMBALANCE":
            # 计算买卖盘前 5 档挂单的深度比例 (Order Imbalance)
            bid_sum = sum(b["amount"] for b in self.depth_bids[:5])
            ask_sum = sum(a["amount"] for a in self.depth_asks[:5])
            total = bid_sum + ask_sum
            if total > 0:
                imbalance = (bid_sum - ask_sum) / total
                if imbalance > 0.35:
                    trigger_side = "BUY"
                elif imbalance < -0.35:
                    trigger_side = "SELL"
                    
        elif auto_strategy == "MICROPRICE":
            # 计算微观价格动量差异 (Micro-Price)
            bid_qty = sum(b["amount"] for b in self.depth_bids[:3])
            ask_qty = sum(a["amount"] for a in self.depth_asks[:3])
            total_qty = bid_qty + ask_qty
            if total_qty > 0:
                micro_price = (best_bid_price * ask_qty + best_ask_price * bid_qty) / total_qty
                mid_price = (best_bid_price + best_ask_price) / 2.0
                diff_percent = ((micro_price - mid_price) / mid_price) * 100
                if diff_percent > 0.015:
                    trigger_side = "BUY"
                elif diff_percent < -0.015:
                    trigger_side = "SELL"
                    
        elif auto_strategy == "SCALPING":
            # 网格套利策略 (若没有挂单，则在盘口两侧挂出买卖双单进行高频双向套利)
            if self.state.get("limitOrders"):
                return
            spread_pct = ((best_ask_price - best_bid_price) / best_ask_price) * 100
            # 必须有点点价差才有利润空间
            if spread_pct > 0.04:
                buy_price = round(best_bid_price + 0.01, 2)
                sell_price = round(best_ask_price - 0.01, 2)
                buy_qty = usdt_val / buy_price
                sell_qty = usdt_val / sell_price
                
                self.state["limitOrders"] = [
                    {
                        "id": uuid.uuid4().hex[:8],
                        "symbol": self.state["symbol"],
                        "side": "BUY",
                        "price": buy_price,
                        "qty": buy_qty,
                        "timestamp": time.strftime("%H:%M:%S")
                    },
                    {
                        "id": uuid.uuid4().hex[:8],
                        "symbol": self.state["symbol"],
                        "side": "SELL",
                        "price": sell_price,
                        "qty": sell_qty,
                        "timestamp": time.strftime("%H:%M:%S")
                    }
                ]
                self.last_auto_trade_time = now
                await self.on_state_changed()
                logger.info("sandbox.engine.scalping_grid_placed", buy_price=buy_price, sell_price=sell_price)
                return
                
        if trigger_side:
            self.last_auto_trade_time = now
            logger.info("sandbox.engine.strategy_triggered", strategy=auto_strategy, side=trigger_side)
            await self.execute_market_order(trigger_side, qty_val)

    async def execute_market_order(self, side: str, qty: float) -> None:
        """撮合高频市价单 (穿透深度表以逼近真实的滑点成交价格)"""
        if qty <= 0 or self.current_price <= 0:
            return
            
        filled_value = 0.0
        remaining_qty = qty
        
        if side == "BUY":
            levels = self.depth_asks if self.depth_asks else [{"price": self.best_ask or self.current_price, "amount": qty}]
            for lvl in levels:
                if remaining_qty <= 0:
                    break
                fill = min(remaining_qty, lvl["amount"])
                filled_value += fill * lvl["price"]
                remaining_qty -= fill
            if remaining_qty > 0:
                fallback = self.best_ask or self.current_price
                filled_value += remaining_qty * fallback
        else:  # SELL
            levels = self.depth_bids if self.depth_bids else [{"price": self.best_bid or self.current_price, "amount": qty}]
            for lvl in levels:
                if remaining_qty <= 0:
                    break
                fill = min(remaining_qty, lvl["amount"])
                filled_value += fill * lvl["price"]
                remaining_qty -= fill
            if remaining_qty > 0:
                fallback = self.best_bid or self.current_price
                filled_value += remaining_qty * fallback
                
        avg_price = filled_value / qty
        fee = qty * avg_price * 0.0004  # Taker 手续费 0.04%
        
        trade_pnl = 0.0
        new_balance = self.state["balance"]
        new_position = self.state["position"]
        
        if side == "BUY":
            if new_position and new_position["side"] == "SHORT":
                # 平空
                size_diff = new_position["size"] - qty
                trade_pnl = qty * (new_position["entryPrice"] - avg_price) - fee
                new_balance += qty * (new_position["entryPrice"] - avg_price) - fee
                
                if size_diff > 0.00001:
                    new_position["size"] = size_diff
                elif abs(size_diff) <= 0.00001:
                    new_position = None
                else:
                    # 反手开多
                    new_position = {
                        "symbol": self.state["symbol"],
                        "side": "LONG",
                        "size": abs(size_diff),
                        "entryPrice": avg_price,
                        "peakPrice": avg_price
                    }
                    new_balance -= fee
            elif new_position and new_position["side"] == "LONG":
                # 加多
                old_total = new_position["size"] * new_position["entryPrice"]
                new_total = qty * avg_price
                new_position["size"] += qty
                new_position["entryPrice"] = (old_total + new_total) / new_position["size"]
                new_balance -= fee
            else:
                # 新开多
                new_position = {
                    "symbol": self.state["symbol"],
                    "side": "LONG",
                    "size": qty,
                    "entryPrice": avg_price,
                    "peakPrice": avg_price
                }
                new_balance -= fee
        else:  # SELL
            if new_position and new_position["side"] == "LONG":
                # 平多
                size_diff = new_position["size"] - qty
                trade_pnl = qty * (avg_price - new_position["entryPrice"]) - fee
                new_balance += qty * (avg_price - new_position["entryPrice"]) - fee
                
                if size_diff > 0.00001:
                    new_position["size"] = size_diff
                elif abs(size_diff) <= 0.00001:
                    new_position = None
                else:
                    # 反手开空
                    new_position = {
                        "symbol": self.state["symbol"],
                        "side": "SHORT",
                        "size": abs(size_diff),
                        "entryPrice": avg_price,
                        "peakPrice": avg_price
                    }
                    new_balance -= fee
            elif new_position and new_position["side"] == "SHORT":
                # 加空
                old_total = new_position["size"] * new_position["entryPrice"]
                new_total = qty * avg_price
                new_position["size"] += qty
                new_position["entryPrice"] = (old_total + new_total) / new_position["size"]
                new_balance -= fee
            else:
                # 新开空
                new_position = {
                    "symbol": self.state["symbol"],
                    "side": "SHORT",
                    "size": qty,
                    "entryPrice": avg_price,
                    "peakPrice": avg_price
                }
                new_balance -= fee
                
        self.state["balance"] = new_balance
        self.state["position"] = new_position
        
        # 记录交易明细
        log_item = {
            "id": uuid.uuid4().hex[:8],
            "timestamp": time.strftime("%H:%M:%S"),
            "time": int(time.time()),
            "symbol": self.state["symbol"],
            "side": side,
            "action": "OPEN" if new_position and (new_position["side"] == side) else "CLOSE",
            "type": "MARKET",
            "price": avg_price,
            "qty": qty,
            "fee": fee,
            "pnl": trade_pnl
        }
        self.state["tradeLogs"].insert(0, log_item)
        self.state["tradeLogs"] = self.state["tradeLogs"][:200]
        
        logger.info("sandbox.engine.market_filled", log=log_item)
        await self.on_state_changed()

    async def execute_market_close(self) -> None:
        """一键平仓"""
        pos = self.state.get("position")
        if not pos:
            return
        opposite = "SELL" if pos["side"] == "LONG" else "BUY"
        await self.execute_market_order(opposite, pos["size"])


# 全局单例沙盒引擎
sandbox_engine = SandboxEngine()

