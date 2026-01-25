"""
脚本功能：管理止盈止损订单
主要作用：
1. 查看当前挂单（普通单和算法单）
2. 查看当前持仓详情
3. 撤销指定或全部挂单
4. 为现有持仓设置或更新止损单（支持按价格或比例）
5. 为现有持仓设置或更新止盈单（支持按价格或比例）
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_skills import FuturesTrader, Settings, create_client
from trading_skills.account_data import FuturesAccountData


def _pos_entry_side(position_amt: Decimal) -> str:
    if position_amt > 0:
        return "BUY"
    if position_amt < 0:
        return "SELL"
    raise RuntimeError("当前无持仓")


def _find_position_qty(acct: FuturesAccountData, symbol: str, position_side: str = "") -> tuple[str, str, Decimal]:
    df = acct.fetch_positions(symbol)
    if df.empty:
        raise RuntimeError("未获取到持仓信息")

    if position_side:
        ps_req = position_side.strip().upper()
        col = df.get("positionSide")
        if col is not None:
            df_ps = df[col.astype(str).str.upper() == ps_req]
            if df_ps.empty and ps_req in {"LONG", "SHORT"}:
                df_ps = df[col.astype(str).str.upper() == "BOTH"]
            df = df_ps
        if df.empty:
            raise RuntimeError("未找到对应 positionSide 的持仓")

    rows = []
    for _, r in df.iterrows():
        amt = Decimal(str(r.get("positionAmt", "0")))
        if amt != 0:
            rows.append((r, amt))

    if not rows:
        raise RuntimeError("当前无持仓")

    if len(rows) > 1 and not position_side:
        sides = [str(x[0].get("positionSide")) for x in rows]
        raise RuntimeError(f"检测到多方向持仓：{sides}，请用 --position-side LONG 或 SHORT 指定")

    row, amt = max(rows, key=lambda x: abs(x[1]))
    entry_side = _pos_entry_side(amt)
    qty = abs(amt)
    ps = str(row.get("positionSide") or "BOTH")
    return entry_side, ps, qty


def _mark_price(client, symbol: str) -> Decimal:
    r = client.futures_mark_price(symbol=symbol)
    return Decimal(str(r.get("markPrice")))


def _is_stop_kind(v: str) -> bool:
    s = (v or "").upper()
    return s in {"STOP", "STOP_MARKET"}


def _is_take_profit_kind(v: str) -> bool:
    s = (v or "").upper()
    return s in {"TAKE_PROFIT", "TAKE_PROFIT_MARKET"}


def _cancel_existing_algo_orders(
    trader: FuturesTrader,
    *,
    symbol: str,
    position_side: str,
    kinds: set[str],
) -> int:
    ps = position_side.strip().upper() or "BOTH"
    algo_orders = trader.list_open_algo_orders(symbol)
    canceled = 0
    for o in algo_orders:
        oid = o.get("algoId")
        if oid is None:
            continue
        o_ps = str(o.get("positionSide") or "BOTH").upper()
        if o_ps != ps:
            continue
        ot = str(o.get("orderType") or "").upper()
        if ("STOP" in kinds and _is_stop_kind(ot)) or ("TP" in kinds and _is_take_profit_kind(ot)):
            trader.cancel_algo_order(symbol, int(oid))
            canceled += 1
    return canceled


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", type=str, default="XRPUSDT")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--trigger", type=str, default="MARK_PRICE")
    parser.add_argument("--position-side", type=str, default="")
    parser.add_argument("--show-positions", action="store_true")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--show-algo-all", action="store_true")
    parser.add_argument("--cancel-id", type=int, default=0)
    parser.add_argument("--cancel-all", action="store_true")
    parser.add_argument("--cancel-all-algo", action="store_true")
    parser.add_argument("--replace", action="store_true")

    parser.add_argument("--set-stop", action="store_true")
    parser.add_argument("--stop-price", type=str, default="")
    parser.add_argument("--stop-ratio", type=str, default="")

    parser.add_argument("--set-tp", action="store_true")
    parser.add_argument("--tp-price", type=str, default="")
    parser.add_argument("--tp-ratio", type=str, default="")

    args = parser.parse_args()

    settings = Settings.load(ROOT)
    client = create_client(settings)
    trader = FuturesTrader(client)
    acct = FuturesAccountData(client)

    if args.show_positions:
        df = acct.fetch_positions(args.symbol)
        if df.empty:
            print("未获取到持仓信息")
            return 0
        cols = [c for c in ["symbol", "positionSide", "positionAmt", "entryPrice", "markPrice", "unRealizedProfit", "leverage", "marginType"] if c in df.columns]
        if cols:
            print(df[cols].to_string(index=False))
        else:
            print(df.to_string(index=False))
        return 0

    if args.show:
        orders = trader.list_open_orders(args.symbol)
        algo_orders = trader.list_open_algo_orders(args.symbol)
        if not orders and not algo_orders:
            print("无挂单")
            return 0
        if orders:
            print("普通挂单：")
            for o in orders:
                oid = o.get("orderId")
                t = o.get("type")
                side = o.get("side")
                sp = o.get("stopPrice")
                p = o.get("price")
                ro = o.get("reduceOnly")
                cp = o.get("closePosition")
                q = o.get("origQty")
                st = o.get("status")
                print(f"{oid} {t} {side} qty={q} stop={sp} price={p} reduceOnly={ro} closePosition={cp} status={st}")
        if algo_orders:
            print("Algo挂单：")
            for o in algo_orders:
                oid = o.get("algoId")
                t = o.get("orderType")
                side = o.get("side")
                sp = o.get("triggerPrice")
                q = o.get("quantity")
                ro = o.get("reduceOnly")
                cp = o.get("closePosition")
                st = o.get("algoStatus")
                wt = o.get("workingType")
                print(f"{oid} {t} {side} qty={q} trigger={sp} workingType={wt} reduceOnly={ro} closePosition={cp} status={st}")
        return 0

    if args.show_algo_all:
        orders = trader.list_all_algo_orders(args.symbol)
        if not orders:
            print("无Algo历史")
            return 0
        for o in orders[:50]:
            oid = o.get("algoId")
            t = o.get("orderType")
            side = o.get("side")
            ps = o.get("positionSide")
            sp = o.get("triggerPrice")
            st = o.get("algoStatus")
            wt = o.get("workingType")
            cp = o.get("closePosition")
            tif = o.get("timeInForce")
            print(f"{oid} {t} {side} ps={ps} trigger={sp} workingType={wt} tif={tif} closePosition={cp} status={st}")
        return 0

    if args.cancel_all:
        if not args.confirm:
            print("未执行撤单：加 --confirm 才会撤单")
            return 0
        trader.cancel_all_open_orders(args.symbol)
        trader.cancel_all_open_algo_orders(args.symbol)
        print("已撤销该交易对全部挂单")
        return 0

    if args.cancel_all_algo:
        if not args.confirm:
            print("未执行撤单：加 --confirm 才会撤单")
            return 0
        trader.cancel_all_open_algo_orders(args.symbol)
        print("已撤销该交易对全部Algo挂单")
        return 0

    if args.cancel_id:
        if not args.confirm:
            print("未执行撤单：加 --confirm 才会撤单")
            return 0
        ok = False
        try:
            trader.cancel_order(args.symbol, args.cancel_id)
            ok = True
        except Exception:
            ok = False
        if not ok:
            trader.cancel_algo_order(args.symbol, args.cancel_id)
        print(f"已撤销挂单：{args.cancel_id}")
        return 0

    if args.set_stop or args.set_tp:
        entry_side, position_side, qty = _find_position_qty(acct, args.symbol, args.position_side)
        mp = _mark_price(client, args.symbol)
        print(f"当前持仓方向：{entry_side} positionSide：{position_side} 数量：{qty} 标记价：{mp}")

        if args.set_stop:
            if args.stop_price:
                sp = Decimal(args.stop_price)
            elif args.stop_ratio:
                ratio = Decimal(args.stop_ratio)
                sp = mp * (Decimal("1") - ratio) if entry_side == "BUY" else mp * (Decimal("1") + ratio)
            else:
                raise RuntimeError("set-stop 需要 stop-price 或 stop-ratio")

            print(f"将设置止损价：{sp}")
            if not args.confirm:
                print("未执行真实挂单：加 --confirm 才会提交")
            else:
                if args.replace:
                    n = _cancel_existing_algo_orders(
                        trader,
                        symbol=args.symbol,
                        position_side=position_side,
                        kinds={"STOP"},
                    )
                    if n:
                        print(f"已撤销旧止损Algo单：{n} 个")
                r = trader.place_stop_loss_market(
                    symbol=args.symbol,
                    entry_side=entry_side,
                    position_side=position_side,
                    quantity=qty,
                    stop_price=sp,
                    trigger_type=args.trigger,
                )
                print(f"已挂止损单：{r.stop_order_id} closePosition={r.close_position}")

        if args.set_tp:
            if args.tp_price:
                tp = Decimal(args.tp_price)
            elif args.tp_ratio:
                ratio = Decimal(args.tp_ratio)
                tp = mp * (Decimal("1") + ratio) if entry_side == "BUY" else mp * (Decimal("1") - ratio)
            else:
                raise RuntimeError("set-tp 需要 tp-price 或 tp-ratio")

            print(f"将设置止盈价：{tp}")
            if not args.confirm:
                print("未执行真实挂单：加 --confirm 才会提交")
            else:
                if args.replace:
                    n = _cancel_existing_algo_orders(
                        trader,
                        symbol=args.symbol,
                        position_side=position_side,
                        kinds={"TP"},
                    )
                    if n:
                        print(f"已撤销旧止盈Algo单：{n} 个")
                r = trader.place_take_profit_market(
                    symbol=args.symbol,
                    entry_side=entry_side,
                    position_side=position_side,
                    quantity=qty,
                    take_profit_price=tp,
                    trigger_type=args.trigger,
                )
                print(f"已挂止盈单：{r.tp_order_id} closePosition={r.close_position}")

        return 0

    print("未指定操作：可用 --show / --cancel-id / --cancel-all / --set-stop / --set-tp")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
