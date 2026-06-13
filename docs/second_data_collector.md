# 秒级数据采集服务

这个服务只负责采集数据，不参与当前实盘信号判断。目标是为后面测试秒级入场、秒级正态窗口、秒级资金流过滤准备历史样本。

## 服务

部署后会新增 systemd 服务：

```bash
btc-second-data.service
```

常用命令：

```bash
systemctl status btc-second-data.service --no-pager
journalctl -u btc-second-data.service -n 80 --no-pager
systemctl restart btc-second-data.service
```

健康检查接口：

```bash
curl -s http://127.0.0.1:3000/api/second-data-health
```

## 输出文件

默认写入：

```text
data/btcusdt_1s_trades.csv
data/second_data_status.json
```

CSV 每行是一秒聚合后的 U 本位合约成交数据：

| 字段 | 含义 |
|---|---|
| `timestamp` | 秒级K线时间 |
| `open/high/low/close` | 这一秒内成交价OHLC |
| `volume` | 成交量，BTC |
| `quote_volume` | 成交额，USDT |
| `trades` | 聚合成交笔数 |
| `taker_buy_volume` | 主动买入量 |
| `taker_sell_volume` | 主动卖出量 |
| `taker_buy_quote` | 主动买入额 |
| `taker_sell_quote` | 主动卖出额 |
| `taker_buy_sell_ratio` | 主动买/主动卖比例 |
| `first_agg_trade_id` | 本秒第一笔聚合成交ID |
| `last_agg_trade_id` | 本秒最后一笔聚合成交ID |

`taker_buy_sell_ratio` 的约定：

- `> 1`：主动买更强
- `< 1`：主动卖更强
- `0`：这一秒只有主动卖
- `999`：这一秒只有主动买

## 采集逻辑

服务每秒拉取 Binance U 本位合约 `aggTrades`，按秒聚合后追加写入 CSV。

为了避免当前这一秒还没结束就落盘，服务默认延迟约 2 秒写入，所以健康接口里的 `last_ts` 比当前时间慢几秒是正常的。

## 可配置环境变量

| 变量 | 默认值 | 含义 |
|---|---:|---|
| `SECOND_DATA_SYMBOL` | `BTCUSDT` | 采集交易对 |
| `SECOND_DATA_MARKET` | `futures` | `futures` 或 `spot` |
| `SECOND_DATA_INTERVAL_SEC` | `1` | 拉取间隔 |
| `SECOND_DATA_BACKFILL_MINUTES` | `10` | 首次启动回补分钟数 |
| `SECOND_DATA_FINALIZE_DELAY_SEC` | `2` | 秒K落盘延迟 |
| `SECOND_DATA_RETENTION_DAYS` | `120` | 预留参数，当前追加文件不自动裁剪 |

## 后续回测方向

有了这份数据后，可以测试：

- 信号出现后等待 5/10/15/30/60 秒再入场
- 信号出现后等待价格回撤 N bps 再入场
- 用最近 300/600/900 秒做秒级正态尾部
- 用 10秒/30秒/60秒主动买卖量做资金流过滤

当前主策略仍然保持 2分钟聚合 + 60分钟窗口，秒级采集只是为了后面研究，不会改变实盘下单逻辑。
