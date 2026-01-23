from handlers.binance_client import BinanceClient
from datetime import datetime

def main():
    # 实例化客户端
    client = BinanceClient()
    
    # 目标交易对
    symbol = "RAVEUSDT"
    
    # 获取并打印资金数据
    print(f"\n正在获取 {symbol} 的资金数据...")
    funding_info = client.get_funding_info(symbol)
    
    if funding_info:
        # 格式化时间
        next_funding_time = datetime.fromtimestamp(funding_info['下次资金时间'] / 1000).strftime('%Y-%m-%d %H:%M:%S')
        
        print(f"--- {symbol} 资金数据 ---")
        print(f"标记价格: {funding_info['标记价格']}")
        print(f"当前资金费率: {funding_info['当前资金费率']:.8f} ({funding_info['当前资金费率']*100:.4f}%)")
        print(f"下次资金时间: {next_funding_time}")
    else:
        print("提示: 请检查交易对名称是否正确（例如 'RAREUSDT' 或 'RVNUSDT'）")

    # 获取并打印资金流向
    print(f"\n正在获取 {symbol} 的资金流向...")
    money_flow = client.get_money_flow(symbol, period='1h')
    
    if money_flow:
        print(f"--- {symbol} 资金流向 (最近1小时) ---")
        print(f"主动买入: {money_flow['主动买入量']:,.2f}")
        print(f"主动卖出: {money_flow['主动卖出量']:,.2f}")
        print(f"净流入: {money_flow['净流入量']:,.2f}")
        print(f"买卖比: {money_flow['买卖比']}")
        
        if money_flow['净流入量'] > 0:
            print(">>> 状态: 资金净流入 (看多)")
        else:
            print(">>> 状态: 资金净流出 (看空)")
    else:
        print("无法获取资金流向数据")

if __name__ == "__main__":
    main()
