"""
LLM 交易顾问技能模块
结合量化分析与大语言模型 (LLM) 进行自动化交易决策。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

import requests

from .trader import FuturesTrader
from .data_fetcher import FuturesDataFetcher

# 尝试导入 Settings，如果未找到则回退到 None
try:
    from .settings import Settings
except ImportError:
    Settings = None

try:
    from analysis.smart_analyzer import SmartAnalyzer
except ImportError:
    try:
        from src.analysis.smart_analyzer import SmartAnalyzer
    except ImportError:
        SmartAnalyzer = None

logger = logging.getLogger(__name__)

@dataclass
class LLMDecision:
    action: str  # "BUY", "SELL", "HOLD" (买入, 卖出, 持有)
    direction: str  # "LONG", "SHORT", "NONE" (做多, 做空, 无)
    reasoning: str
    confidence: float  # 0.0 - 1.0
    suggested_params: Dict[str, Any]

@dataclass
class _SymbolState:
    initial_qty: Decimal | None = None
    partial_tp_percent: Decimal = Decimal("0.5")
    profit_locked: bool = False
    in_position: bool = False
    last_scalp_add_ts: float = 0.0
    scalp_peak_price: float | None = None
    scalp_trough_price: float | None = None

class LLMAdvisor:
    def __init__(self, trader: FuturesTrader):
        self.trader = trader
        # 尝试从 trader 获取设置，或者加载设置
        if hasattr(trader, "settings"):
            self.settings = trader.settings
        elif Settings:
            self.settings = Settings.load()
        else:
            # 如果绝对必要，使用回退的模拟设置
            class MockSettings:
                llm_api_key = ""
                llm_base_url = "https://api.siliconflow.cn/v1"
                llm_model = "deepseek-ai/DeepSeek-V3"
            self.settings = MockSettings()

        self.api_key = getattr(self.settings, "llm_api_key", "")
        self.base_url = getattr(self.settings, "llm_base_url", "") or "https://api.siliconflow.cn/v1"
        self.model = getattr(self.settings, "llm_model", "") or "deepseek-ai/DeepSeek-V3"
        # 暴露 client 给外部工具 (例如 auto_trader.py) 使用
        self.client = getattr(trader, "_client", None)

        self._symbol_states: dict[str, _SymbolState] = {}
        self._analysis_cache: dict[str, tuple[float, Dict[str, Any]]] = {}
        self._last_entry_times: dict[str, float] = {}  # 记录上次入场时间用于冷却
        self.COOLING_OFF_SECONDS = 300  # 5分钟冷却期
        
        # 设置事件日志
        self._event_log_dir = Path(__file__).resolve().parents[2] / "logs"
        try:
            self._event_log_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def _log_event(self, payload: dict[str, Any]) -> None:
        """记录事件到日志记录器和文件。"""
        try:
            line = json.dumps(payload, ensure_ascii=False)
        except Exception:
            line = str(payload)

        # 记录到文件
        try:
            symbol = payload.get("symbol") if isinstance(payload, dict) else None
        except Exception:
            symbol = None

        if isinstance(symbol, str) and symbol.strip():
            safe_symbol = "".join([c if (c.isalnum() or c in ["_", "-"]) else "_" for c in symbol.strip().upper()])
            date_str = time.strftime("%Y-%m-%d", time.localtime())
            state = self._symbol_states.get(symbol.strip().upper())
            if state is None:
                state = self._symbol_states.get(symbol.strip())
            phase = "pos" if (state is not None and getattr(state, "in_position", False)) else "watch"
            file_path = self._event_log_dir / f"{date_str}_{safe_symbol}_{phase}.log"
            try:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with file_path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass

        # 记录到 logger (info 级别)
        logger.info(f"LLM_EVENT: {line}")

    def fetch_market_data(self, symbol: str) -> Dict[str, Any]:
        """获取分析所需的市场数据。"""
        start = time.time()
        try:
            # 使用 FuturesDataFetcher 获取包含资金流向的 DataFrame
            fetcher = FuturesDataFetcher(self.trader._client)
            
            # 获取 1h K线 (用于主分析)
            klines_1h = fetcher.fetch_klines(symbol, interval="1h", limit=100)
            
            # 获取 15m K线 (用于短期验证)
            klines_15m = fetcher.fetch_klines(symbol, interval="15m", limit=100)

            ticker = self.trader.get_ticker(symbol)
            
            # 尝试获取盘口数据
            orderbook = {}
            if hasattr(self.trader, "_client"):
                 try:
                     orderbook = self.trader._client.futures_order_book(symbol=symbol, limit=20)
                 except Exception:
                     pass
            
            # 尝试获取资金费率
            premium_index = {}
            if hasattr(self.trader, "_client"):
                try:
                    premium_index = self.trader._client.futures_mark_price(symbol=symbol)
                except Exception:
                    pass

            data = {
                "symbol": symbol,
                "klines_1h": klines_1h,
                "klines_15m": klines_15m,
                "ticker": ticker,
                "current_price": float(ticker.get("lastPrice", 0) or ticker.get("price", 0)) if ticker else 0.0,
                "orderbook": orderbook,
                "premium_index": premium_index
            }
            
            self._log_event({
                "event": "market_data_fetched",
                "symbol": symbol,
                "duration_ms": int((time.time() - start) * 1000),
                "kline_count": len(klines_1h) if not klines_1h.empty else 0,
                "current_price": data["current_price"]
            })
            return data
        except Exception as e:
            logger.error(f"获取 {symbol} 市场数据失败: {e}")
            return {"symbol": symbol}

    def get_analysis_report(self, symbol: str) -> Dict[str, Any]:
        """获取或生成技术分析报告。"""
        now = time.time()
        if symbol in self._analysis_cache:
            ts, report = self._analysis_cache[symbol]
            if now - ts < 60:  # 缓存 60秒
                return report

        start = time.time()
        data = self.fetch_market_data(symbol)
        
        # 优先使用 SmartAnalyzer (analyze_symbol 函数)
        try:
            from analysis.smart_analyzer import analyze_symbol
            report = analyze_symbol(data)
        except (ImportError, Exception) as e:
            logger.warning(f"无法使用 SmartAnalyzer 进行分析 ({e})，使用回退逻辑")
            report = {
                "score": 50, 
                "direction_score": 0, 
                "current_price": data.get("current_price", 0),
                "signals": ["Analyzer Missing"],
                "risk_factors": []
            }

        self._log_event(
            {
                "event": "analysis_generated",
                "symbol": symbol,
                "duration_ms": int((time.time() - start) * 1000),
                "score": report.get("score") if isinstance(report, dict) else None,
                "direction_score": report.get("direction_score") if isinstance(report, dict) else None,
                "current_price": report.get("current_price") if isinstance(report, dict) else None,
            }
        )
        self._analysis_cache[symbol] = (now, report)
        return report

    def _calc_default_usdt(self) -> Decimal:
        """根据余额计算动态默认 USDT 金额。"""
        balance = self.trader.get_usdt_balance()
        # 动态默认: 余额的 5%, 最小 20
        calc_amount = balance * Decimal("0.05")
        if calc_amount < 20:
            calc_amount = Decimal("20")
        return calc_amount

    def _clean_llm_response(self, content: str) -> str:
        """清理 LLM 响应内容，移除 markdown 代码块。"""
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]
        return content.strip()

    def _calc_dynamic_partial_tp_price(
        self,
        direction: str,
        entry_price: float,
        current_price: float,
        analysis: Dict[str, Any],
    ) -> Optional[float]:
        """计算用于剥头皮的动态部分止盈价格。"""
        try:
            atr_pct = float(analysis.get("atr_pct") or 0)
            if atr_pct <= 0:
                atr_pct = 0.35
            
            # 目标 ~0.8 * ATR 波动
            target_dist_pct = atr_pct * 0.8
            # 最小 0.5%, 最大 1.5%
            target_dist_pct = max(0.5, min(target_dist_pct, 1.5))
            
            if direction == "LONG":
                target_price = entry_price * (1 + target_dist_pct / 100.0)
                # 确保至少略高于当前价格
                if target_price <= current_price:
                    target_price = current_price * 1.002
            else:
                target_price = entry_price * (1 - target_dist_pct / 100.0)
                if target_price >= current_price:
                    target_price = current_price * 0.998
                    
            return target_price
        except Exception as e:
            logger.error(f"计算动态止盈出错: {e}")
            return None

    def _construct_prompt(self, symbol: str, analysis: Dict[str, Any]) -> str:
        """构建发送给 LLM 的提示词。"""
        balance = self.trader.get_usdt_balance()
        
        prompt = f"""
You are an Execution Trader. The asset {symbol} has ALREADY passed our quantitative screening algorithm (Top candidate from market scan).
Your job is NOT to re-evaluate if it's "worth watching", but to VALIDATE the direction and PLAN the trade execution.

Account Info:
- Available USDT: {balance:.2f}

Technical Analysis Report:
- Score: {analysis.get('score')} (Quantitative Score. >40 is PASS. Higher is better.)
- Direction Score: {analysis.get('direction_score')} (Positive = Bullish, Negative = Bearish)
- Signals: {', '.join(analysis.get('signals', []))}
- Risk Factors: {', '.join(analysis.get('risk_factors', []))}
- Current Price: {analysis.get('current_price')}
- ATR %: {analysis.get('atr_pct', 'N/A')}
- Volume Ratio: {analysis.get('volume_ratio', 'N/A')}
- Net Inflow 24h: {analysis.get('net_inflow_24h', 'N/A')} (USDT)
- Spread (bps): {analysis.get('spread_bps', 'N/A')}
- Orderbook Imbalance: {analysis.get('imbalance', 'N/A')} (>0.15 Bullish, <-0.15 Bearish)
- Funding Rate: {analysis.get('funding_rate', 'N/A')}
- Open Interest Change (30m): {analysis.get('oi_change_30m', 'N/A')}

Execution Guidelines:
1. **Role**: You are an Execution Trader. The asset has passed quantitative screening. Your goal is to find the **OPTIMAL ENTRY**.
2. **Direction**: Trust the "Direction Score". If >0 (Bullish) -> Buy Dip / Breakout. If <0 (Bearish) -> Sell Rally / Breakdown.
3. **Timing**: 
   - If the price is good and momentum supports it -> **OPEN** immediately.
   - If the price is overextended or you see a temporary counter-move -> **WAIT** for a better entry (e.g. pullback).
   - If you see a major trend reversal or danger signal -> **WAIT** (do not force a trade).
4. **Risk Management**:
   - Stop Loss: MUST be set (use ATR or Swing Low/High).
   - Take Profit: Target >1.5 Risk/Reward.

Provide the execution plan in JSON format:
- action: "OPEN" or "WAIT"
- direction: "LONG" or "SHORT"
- confidence: float (0.0 - 1.0)
- reasoning: Explain why you are opening NOW or why you are WAITING.
- leverage: Recommended leverage (int, max 20)
- allocation_usdt: Recommended USDT amount (Suggest ~5-10% of balance. If balance is low, min 20.)
- stop_loss: Precise price
- take_profit: Precise price
- partial_tp_price: float | null (FIRST-LADDER limit reduce-only price for scalp)
- partial_tp_percent: float | null (0.05~0.8, default 0.5)

Respond ONLY with the JSON.
"""
        return prompt.strip()

    def ask_llm(self, symbol: str) -> Optional[LLMDecision]:
        """获取 LLM 的交易建议。"""
        analysis = self.get_analysis_report(symbol)
        prompt = self._construct_prompt(symbol, analysis)
        
        self._log_event({
            "event": "llm_entry_request",
            "symbol": symbol,
            "analysis_score": analysis.get("score"),
            "current_price": analysis.get("current_price")
        })

        if not self.api_key:
            logger.error("在设置中未找到 LLM API Key。")
            return None

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are an aggressive but calculated Execution Trader. You trust the quantitative screening and focus on optimal entry/exit parameters."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 500
        }

        try:
            # 禁用代理请求
            response = requests.post(
                f"{self.base_url}/chat/completions", 
                headers=headers, 
                json=payload, 
                timeout=30,
                proxies={"http": None, "https": None}
            )
            response.raise_for_status()
            result = response.json()
            content = result['choices'][0]['message']['content']
            
            # 清理内容
            content = self._clean_llm_response(content)
            data = json.loads(content)
            
            self._log_event({"event": "llm_response_parsed", "symbol": symbol, "data": data})

            # 映射 OPEN/LONG -> BUY, OPEN/SHORT -> SELL
            action = "HOLD"
            if data.get("action") == "OPEN":
                if data.get("direction") == "LONG":
                    action = "BUY"
                elif data.get("direction") == "SHORT":
                    action = "SELL"
            
            direction = data.get("direction", "NONE")
            if action == "HOLD": 
                direction = "NONE"

            return LLMDecision(
                action=action,
                direction=direction,
                reasoning=data.get("reasoning", ""),
                confidence=float(data.get("confidence", 0)),
                suggested_params={
                    "leverage": int(data.get("leverage", 1)),
                    "usdt_amount": float(data.get("allocation_usdt", 0)),
                    "stop_loss": float(data.get("stop_loss", 0)) if data.get("stop_loss") else None,
                    "take_profit": float(data.get("take_profit", 0)) if data.get("take_profit") else None,
                    "partial_tp_price": float(data.get("partial_tp_price", 0)) if data.get("partial_tp_price") else None,
                    "partial_tp_percent": float(data.get("partial_tp_percent", 0)) if data.get("partial_tp_percent") else None,
                }
            )
            
        except Exception as e:
            logger.error(f"{symbol} LLM API 调用失败: {e}")
            return None

    def execute_trade(self, symbol: str, decision: LLMDecision) -> bool:
        """根据 LLM 决策执行交易。"""
        if decision.action not in ["BUY", "SELL"]:
            return False

        # 检查冷却期
        last_entry = self._last_entry_times.get(symbol, 0)
        if time.time() - last_entry < self.COOLING_OFF_SECONDS:
            logger.warning(f"跳过 {symbol} 交易: 处于冷却期中")
            return False

        try:
            side = decision.action
            params = decision.suggested_params
            
            # 1. 设置杠杆
            lev = params.get("leverage", 1)
            self.trader.set_leverage(symbol, lev)

            # 2. 计算数量
            usdt_amount = params.get("usdt_amount", 0)
            if usdt_amount <= 0:
                usdt_amount = self._calc_default_usdt()
            
            # 3. 下市价单
            self._log_event({"event": "trade_execution_start", "symbol": symbol, "side": side, "usdt": usdt_amount})
            order_result = self.trader.place_market_entry_by_usdt(symbol=symbol, side=side, usdt_amount=usdt_amount)
            
            if not order_result or not getattr(order_result, "order_id", None):
                logger.error(f"下单失败 {symbol}")
                return False

            executed_qty = float(getattr(order_result, "executed_qty", 0))
            avg_price = float(getattr(order_result, "avg_price", 0))
            
            if executed_qty <= 0:
                logger.error(f"订单已执行但 {symbol} 数量为 0")
                return False

            self._last_entry_times[symbol] = time.time()

            # 更新状态
            state = _SymbolState(
                initial_qty=Decimal(str(executed_qty)),
                profit_locked=False,
                in_position=True,
                scalp_peak_price=avg_price if side == "BUY" else None,
                scalp_trough_price=avg_price if side == "SELL" else None
            )
            self._symbol_states[symbol] = state

            # 4. 设置止损/止盈
            sl_price = params.get("stop_loss")
            tp_price = params.get("take_profit")

            if sl_price:
                sl_res = self.trader.place_stop_loss_market(symbol=symbol, entry_side=side, stop_price=Decimal(str(sl_price)), close_position=True)
                logger.info(f"设置止损 {symbol}: {sl_price} (ID: {sl_res.stop_order_id})")
            
            if tp_price:
                tp_res = self.trader.place_take_profit_market(symbol=symbol, entry_side=side, take_profit_price=Decimal(str(tp_price)), close_position=True)
                logger.info(f"设置止盈 {symbol}: {tp_price} (ID: {tp_res.tp_order_id})")

            # 5. 设置部分止盈 (限价)
            ptp_price = params.get("partial_tp_price")
            ptp_pct = params.get("partial_tp_percent") or 0.5
            
            if ptp_price and ptp_price > 0:
                ptp_qty = executed_qty * ptp_pct
                exit_side = "SELL" if side == "BUY" else "BUY"
                self.trader.place_limit_reduce_order(
                    symbol, exit_side, Decimal(str(ptp_qty)), Decimal(str(ptp_price))
                )

            return True

        except Exception as e:
            logger.error(f"{symbol} 交易执行失败: {e}")
            return False

    def clear_symbol_state(self, symbol: str) -> None:
        """清除币种状态 (例如平仓后)。"""
        if symbol in self._symbol_states:
            self._symbol_states.pop(symbol)
        if symbol in self._analysis_cache:
            self._analysis_cache.pop(symbol)

    def monitor_position(self, symbol: str, pos: Dict[str, Any] | None = None) -> bool:
        """监控和管理现有持仓。如果持仓已关闭则返回 True。"""
        # 1. 获取持仓
        if pos is None:
            pos = self.trader.get_position(symbol)
        
        if not pos:
             self._symbol_states.pop(symbol, None)
             return True

        amt_dec = Decimal(str(pos.get("positionAmt", 0)))
        if amt_dec == 0:
            self._symbol_states.pop(symbol, None)
            return True

        direction = "LONG" if amt_dec > 0 else "SHORT"
        abs_amt_dec = abs(amt_dec)
        
        # 2. 获取分析
        analysis = self.get_analysis_report(symbol)
        current_price = float(analysis.get("current_price") or pos.get("markPrice", 0))
        
        # 更新状态
        state = self._symbol_states.get(symbol)
        if state is None:
            state = _SymbolState()
            self._symbol_states[symbol] = state
        
        state.in_position = True
        if state.initial_qty is None or state.initial_qty <= 0:
            state.initial_qty = abs_amt_dec
        
        # 稳健逻辑：如果当前持仓显著大于初始持仓，更新初始值
        if abs_amt_dec > state.initial_qty * Decimal("1.05"):
             state.initial_qty = abs_amt_dec

        initial_qty = state.initial_qty
        partial_pct = state.partial_tp_percent
        scalp_qty = initial_qty * partial_pct
        core_qty = initial_qty - scalp_qty

        # 利润锁定检查
        entry_price = float(pos.get('entryPrice', 0))
        if not state.profit_locked and initial_qty > 0:
            filled_ratio = abs_amt_dec / initial_qty
            # 如果持仓显著减少（例如触发了部分止盈），锁定利润
            if filled_ratio <= (Decimal("1") - partial_pct + Decimal("0.05")):
                state.profit_locked = True
        
        # 或者如果 PnL 良好
        pnl_pct = 0
        if entry_price > 0:
            if direction == "LONG":
                pnl_pct = (current_price - entry_price) / entry_price * 100
            else:
                pnl_pct = (entry_price - current_price) / entry_price * 100
        
        if pnl_pct > 1.5:
            state.profit_locked = True

        # 更新用于剥头皮的峰值/谷值
        if state.profit_locked:
            if direction == "LONG":
                if state.scalp_peak_price is None or current_price > state.scalp_peak_price:
                    state.scalp_peak_price = current_price
            else:
                if state.scalp_trough_price is None or current_price < state.scalp_trough_price:
                    state.scalp_trough_price = current_price

        # 3. 获取挂单并验证
        try:
            open_orders = self.trader.list_open_orders(symbol)
        except AttributeError:
             # 如果 list_open_orders 不存在则回退（如果 trader.py 正确则不应发生）
             open_orders = []

        current_sl_price = None
        current_tp_price = None
        current_limit_tp_price = None
        
        exit_side = "SELL" if direction == "LONG" else "BUY"

        # 验证循环
        for o in open_orders:
            oid = o.get('orderId')
            otype = o.get('type')
            oside = o.get('side')
            oprice = float(o.get('stopPrice', 0) or o.get('price', 0))
            
            is_valid = True
            
            # 方向检查
            if oside != exit_side:
                 is_valid = False
            
            # 价格逻辑检查 (放宽检查，避免稍微一点波动就认为无效)
            if is_valid:
                if direction == "LONG":
                    # 只有当止损价明显高于当前价时才认为无效 (容忍 0.5% 的波动)
                    if otype in ['STOP_MARKET', 'STOP', 'STOP_LOSS'] and oprice >= current_price * 1.005: is_valid = False
                    # 只有当止盈价明显低于当前价时才认为无效
                    elif otype in ['TAKE_PROFIT_MARKET', 'TAKE_PROFIT'] and oprice <= current_price * 0.995: is_valid = False
                    elif otype == 'LIMIT' and oprice <= current_price * 0.995: is_valid = False
                else:
                    if otype in ['STOP_MARKET', 'STOP', 'STOP_LOSS'] and oprice <= current_price * 0.995: is_valid = False
                    elif otype in ['TAKE_PROFIT_MARKET', 'TAKE_PROFIT'] and oprice >= current_price * 1.005: is_valid = False
                    elif otype == 'LIMIT' and oprice >= current_price * 1.005: is_valid = False

            if not is_valid:
                # 只有当确实偏离很远时才取消，避免误杀 (例如价格剧烈波动瞬间)
                # 这里先不取消，只是不记录为有效 SL/TP
                pass
            else:
                if otype in ['STOP_MARKET', 'STOP', 'STOP_LOSS', 'STOP_LOSS_LIMIT']: 
                    current_sl_price = oprice
                elif otype in ['TAKE_PROFIT_MARKET', 'TAKE_PROFIT', 'TAKE_PROFIT_LIMIT']: 
                    current_tp_price = oprice
                elif otype == 'LIMIT': 
                    current_limit_tp_price = oprice

        has_sl = (current_sl_price is not None)
        need_partial_tp = (not state.profit_locked) or (abs_amt_dec > core_qty + (initial_qty * Decimal("0.01")))

        # 4. 自动加仓逻辑
        if state.profit_locked and has_sl and not need_partial_tp and (current_limit_tp_price is None):
            self._attempt_auto_scalp(symbol, state, direction, current_price, initial_qty, scalp_qty, analysis, abs_amt_dec)

        # 5. LLM 监控
        # 不要过于频繁地询问 LLM
        if time.time() - state.last_scalp_add_ts < 10:
            return False

        self._run_llm_monitor(
            symbol, pos, analysis, direction, 
            current_sl_price, current_tp_price, current_limit_tp_price, 
            state.profit_locked, need_partial_tp, abs_amt_dec, initial_qty, core_qty, scalp_qty
        )
        
        return False

    def _attempt_auto_scalp(
        self,
        symbol: str,
        state: _SymbolState,
        direction: str,
        current_price: float,
        initial_qty: Decimal,
        scalp_qty: Decimal,
        analysis: Dict[str, Any],
        current_qty: Decimal
    ):
        """执行自动加仓逻辑 (Auto-scalping)。"""
        now_ts = time.time()
        if state.last_scalp_add_ts > 0 and now_ts - state.last_scalp_add_ts < 240:
            return

        gap_qty = initial_qty - current_qty
        if gap_qty <= initial_qty * Decimal("0.02"):
            return

        desired_add_qty = gap_qty
        if desired_add_qty > initial_qty:
            desired_add_qty = initial_qty

        dir_score = float(analysis.get("direction_score") or 0)
        atr_pct = float(analysis.get("atr_pct") or 0.35)
        if atr_pct <= 0: atr_pct = 0.35
        
        pullback_dist = max(current_price * (atr_pct / 100.0) * 0.6, current_price * 0.002)
        
        should_add = False
        if direction == "LONG":
            peak = state.scalp_peak_price or current_price
            if dir_score >= 0.3 and current_price <= peak - pullback_dist:
                should_add = True
        else:
            trough = state.scalp_trough_price or current_price
            if dir_score <= -0.3 and current_price >= trough + pullback_dist:
                should_add = True

        if should_add:
            side = "BUY" if direction == "LONG" else "SELL"
            usdt_amt = desired_add_qty * Decimal(str(current_price))
            
            self._log_event({"event": "auto_scalp_add", "symbol": symbol, "side": side, "qty": str(desired_add_qty)})
            
            res = self.trader.place_market_entry_by_usdt(symbol=symbol, side=side, usdt_amount=usdt_amt)
            exec_qty = float(getattr(res, "executed_qty", 0)) if res else 0
            
            if exec_qty > 0:
                state.last_scalp_add_ts = now_ts
                # 立即设置部分止盈
                exit_side = "SELL" if side == "BUY" else "BUY"
                tp_price = self._calc_dynamic_partial_tp_price(direction, current_price, current_price, analysis)
                if not tp_price:
                    tp_price = current_price * (1.008 if direction == "LONG" else 0.992)
                
                self.trader.place_limit_reduce_order(
                    symbol=symbol, side=exit_side, quantity=Decimal(str(exec_qty)), price=Decimal(str(tp_price))
                )

    def _run_llm_monitor(
        self,
        symbol: str,
        pos: Dict[str, Any],
        analysis: Dict[str, Any],
        direction: str,
        current_sl: Optional[float],
        current_tp: Optional[float],
        current_limit_tp_price: Optional[float],
        profit_locked: bool,
        need_partial_tp: bool,
        abs_amt_dec: Decimal,
        initial_qty: Decimal,
        core_qty: Decimal,
        scalp_qty: Decimal
    ):
        entry_price = float(pos.get('entryPrice', 0))
        unrealized_profit = float(pos.get('unRealizedProfit', 0))
        leverage = float(pos.get('leverage', 1))
        initial_margin = (entry_price * abs(float(pos.get('positionAmt', 0)))) / leverage if leverage else 0
        pnl_pct = (unrealized_profit / initial_margin * 100) if initial_margin else 0
        current_price = float(analysis.get('current_price') or 0)

        # 智能建议逻辑
        suggested_sl = None
        suggestion_reason = ""
        atr = float(analysis.get('atr') or 0)
        if not atr and analysis.get('atr_pct') and current_price:
             atr = current_price * (float(analysis.get('atr_pct')) / 100)

        if pnl_pct > 1.5:
             if direction == "LONG":
                 be_price = entry_price * 1.002 
                 if not current_sl or current_sl < be_price:
                     suggested_sl = be_price
                     suggestion_reason = "Protect Profit (Break Even)"
             else:
                 be_price = entry_price * 0.998
                 if not current_sl or current_sl > be_price:
                     suggested_sl = be_price
                     suggestion_reason = "Protect Profit (Break Even)"
        
        # 2. 移动止损 (Trailing Stop): 盈利超过 3% 时，建议使用 ATR 跟踪
        if pnl_pct > 3.0 and atr:
             if direction == "LONG":
                 ts_price = current_price - (2 * atr)
                 # 只有当跟踪止损比当前止损和开仓价都高时才建议
                 if ts_price > entry_price and (not current_sl or ts_price > current_sl):
                     suggested_sl = ts_price
                     suggestion_reason = "Trailing Stop (Lock Profit)"
             else:
                 ts_price = current_price + (2 * atr)
                 if ts_price < entry_price and (not current_sl or ts_price < current_sl):
                     suggested_sl = ts_price
                     suggestion_reason = "Trailing Stop (Lock Profit)"

        prompt = f"""
Current Position: {symbol} ({direction})
- Entry: {entry_price}
- PnL: {pnl_pct:.2f}%
- SL: {current_sl}
- TP: {current_tp}
- Partial TP: {current_limit_tp_price} (Need: {need_partial_tp})
- Profit Locked: {profit_locked}

Analysis:
- Score: {analysis.get('score')}
- Direction Score: {analysis.get('direction_score')}

Smart Suggestions:
- Suggested SL: {round(suggested_sl, 4) if suggested_sl else "None"}
- Reason: {suggestion_reason}

Decide Action (JSON):
- action: "CLOSE", "REDUCE", "ADD", "ADJUST_TP_SL", "HOLD"
- reasoning: ...
- new_stop_loss: ...
- new_take_profit: ...
- partial_tp_price: ... (if adjusting)

Guidelines:
1. If PnL is bad AND trend is invalid -> CLOSE.
2. If SL/TP is missing -> PREFER 'ADJUST_TP_SL' to set them immediately (do not CLOSE just because they are missing, unless the trade is bad).
3. If action is 'ADJUST_TP_SL':
    - You MUST provide 'new_stop_loss' and 'new_take_profit' values.
    - If 'Need Partial TP' is True (see Context), you MUST provide 'partial_tp_price'. It should be a limit price between Entry and Take Profit to secure some profit.
4. If profit > 1.5% -> Consider moving SL to Break Even.
"""
        messages = [
            {"role": "system", "content": "You are a Risk Manager. Protect capital and maximize trend profits."},
            {"role": "user", "content": prompt}
        ]
        
        if not self.api_key: return

        # Log monitor request
        self._log_event({
            "event": "llm_monitor_request", 
            "symbol": symbol, 
            "pnl_pct": pnl_pct, 
            "has_sl": current_sl is not None
        })

        try:
            headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
            payload = {"model": self.model, "messages": messages, "temperature": 0.1}
            response = requests.post(f"{self.base_url}/chat/completions", headers=headers, json=payload, timeout=30, proxies={"http": None, "https": None})
            response.raise_for_status()
            content = self._clean_llm_response(response.json()['choices'][0]['message']['content'])
            data = json.loads(content)
            
            self._log_event({"event": "llm_monitor_response", "symbol": symbol, "data": data})
            
        except Exception as e:
            logger.error(f"{symbol} LLM Monitor 失败: {e}")
            return

        action = data.get("action")
        
        if action == "CLOSE":
            logger.info(f"LLM 建议平仓 {symbol}: {data.get('reasoning')}")
            self.trader.close_position(symbol)
            
        elif action == "REDUCE":
            reduce_usdt = float(data.get("allocation_usdt") or 0) # Fallback to 50% if not specified? 
            # 简化: 如果没有指定金额，则减少一半
            if reduce_usdt <= 0:
                self.trader.reduce_position(symbol, abs_amt_dec * Decimal("0.5"))
            else:
                side = "SELL" if direction == "LONG" else "BUY"
                self.trader.place_market_entry_by_usdt(symbol=symbol, side=side, usdt_amount=Decimal(str(reduce_usdt)))

        elif action == "ADD":
            if profit_locked:
                add_usdt = float(data.get("allocation_usdt") or 0)
                if add_usdt > 0:
                    side = "BUY" if direction == "LONG" else "SELL"
                    self.trader.place_market_entry_by_usdt(symbol=symbol, side=side, usdt_amount=Decimal(str(add_usdt)))

        elif action == "ADJUST_TP_SL" or (action == "HOLD" and (not current_sl or not current_tp)):
            new_sl = data.get("new_stop_loss")
            new_tp = data.get("new_take_profit")
            new_ptp = data.get("partial_tp_price")

            # Fallback: 如果没有止损且 LLM 也没给，强制计算一个
            if not current_sl and not new_sl:
                atr_val = float(analysis.get("atr") or 0)
                if not atr_val and analysis.get("atr_pct"):
                     atr_val = current_price * (float(analysis.get("atr_pct")) / 100)
                
                if atr_val > 0:
                    if direction == "LONG":
                        new_sl = current_price - (3 * atr_val)
                        if new_sl >= current_price: new_sl = current_price * 0.98 # Safety
                    else:
                        new_sl = current_price + (3 * atr_val)
                        if new_sl <= current_price: new_sl = current_price * 1.02 # Safety
                    logger.info(f"强制设置默认止损: {new_sl} (3*ATR)")

            # Fallback: 如果没有止盈且 LLM 也没给，强制计算一个 (Risk:Reward 1:2)
            if not current_tp and not new_tp:
                 if direction == "LONG":
                     dist = current_price * 0.02 # 默认2%
                     if new_sl:
                        dist = abs(current_price - new_sl) * 2
                     new_tp = current_price + dist
                 else:
                     dist = current_price * 0.02
                     if new_sl:
                        dist = abs(current_price - new_sl) * 2
                     new_tp = current_price - dist
                 logger.info(f"强制设置默认止盈: {new_tp}")

            # 验证: 如果利润已锁定，不要放宽止损
            if profit_locked and current_sl:
                if direction == "LONG" and new_sl and new_sl < current_sl: return
                if direction == "SHORT" and new_sl and new_sl > current_sl: return
            
            # 只有当新的 SL/TP 与当前的显著不同时，才进行调整
            should_update = False
            
            if new_sl:
                 if not current_sl or abs(new_sl - current_sl) / current_sl > 0.001: # 0.1% 差异
                     should_update = True
            
            if new_tp:
                 if not current_tp or abs(new_tp - current_tp) / current_tp > 0.001:
                     should_update = True

            if need_partial_tp and new_ptp:
                 if not current_limit_tp_price or abs(new_ptp - current_limit_tp_price) / current_limit_tp_price > 0.001:
                     should_update = True

            if should_update:
                 try:
                     self.trader.cancel_all_open_orders(symbol)
                     time.sleep(1.0) # 等待撤单生效，防止状态未同步
                 except Exception as e:
                     logger.error(f"取消订单失败: {e}")
                 
                 entry_side = "BUY" if direction == "LONG" else "SELL"
                 if new_sl:
                     # 检查是否会立即触发
                     is_invalid = False
                     if direction == "LONG" and new_sl >= current_price: is_invalid = True
                     if direction == "SHORT" and new_sl <= current_price: is_invalid = True
                     
                     if is_invalid:
                         logger.warning(f"跳过止损设置: 价格 {new_sl} 过于接近或劣于现价 {current_price}，可能导致立即触发")
                     else:
                         # Retry logic for SL
                         max_retries = 3
                         for i in range(max_retries):
                             try:
                                self.trader.place_stop_loss_market(symbol=symbol, entry_side=entry_side, stop_price=Decimal(str(new_sl)), close_position=True)
                                break
                             except Exception as e:
                                if i == max_retries - 1:
                                    logger.error(f"设置新止损失败 {new_sl} (重试{max_retries}次后): {e}")
                                else:
                                    logger.warning(f"设置止损失败，准备重试 ({i+1}/{max_retries}): {e}")
                                    time.sleep(1.0 * (i + 1))
                        
                 if new_tp:
                     # 检查是否会立即触发
                     is_invalid = False
                     if direction == "LONG" and new_tp <= current_price: is_invalid = True
                     if direction == "SHORT" and new_tp >= current_price: is_invalid = True

                     if is_invalid:
                         logger.warning(f"跳过止盈设置: 价格 {new_tp} 过于接近或劣于现价 {current_price}，可能导致立即触发")
                     else:
                         # Retry logic for TP
                         max_retries = 3
                         for i in range(max_retries):
                             try:
                                self.trader.place_take_profit_market(symbol=symbol, entry_side=entry_side, take_profit_price=Decimal(str(new_tp)), close_position=True)
                                break
                             except Exception as e:
                                if i == max_retries - 1:
                                    logger.error(f"设置新止盈失败 {new_tp} (重试{max_retries}次后): {e}")
                                else:
                                    logger.warning(f"设置止盈失败，准备重试 ({i+1}/{max_retries}): {e}")
                                    time.sleep(1.0 * (i + 1))
                 
                 # 如果需要，重新设置部分止盈
                 if need_partial_tp:
                     # 部分止盈是限价平仓单，需要使用 exit_side
                     exit_side = "SELL" if direction == "LONG" else "BUY"
                     
                     target_ptp = new_ptp if new_ptp else current_limit_tp_price
                     # 如果 LLM 没有给出一个并且我们也没有，计算默认值
                     if not target_ptp:
                         dyn = self._calc_dynamic_partial_tp_price(direction, entry_price, current_price, analysis)
                         target_ptp = dyn if dyn else (current_price * (1.015 if direction == "LONG" else 0.985))
                     
                     qty_to_sell = abs_amt_dec - core_qty
                     if qty_to_sell > 0:
                         try:
                            self.trader.place_limit_reduce_order(symbol=symbol, side=exit_side, quantity=qty_to_sell, price=Decimal(str(target_ptp)))
                         except Exception as e:
                            logger.error(f"设置部分止盈失败: {e}")
