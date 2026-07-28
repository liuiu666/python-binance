"""Runtime paths used by the live BTC signal service."""

import os


APP_DIR = os.environ.get("APP_DIR", "E:/codex")
OUT = os.environ.get("DATA_DIR", os.path.join(APP_DIR, "data"))

SIGNAL_FILE = os.path.join(OUT, "live_signals.json")
CONFIG_FILE = os.path.join(OUT, "prod_config.json")
SIGNAL_AUDIT_FILE = os.path.join(OUT, "signal_audit.jsonl")
SIGNAL_STATE_FILE = os.path.join(OUT, "signal_state.json")
LOCK_FILE = os.path.join(OUT, "signal_btc.lock")
LOCK_DIR = os.path.join(OUT, "signal_btc.lockdir")

HISTORY_1M_FILE = os.path.join(OUT, "btcusdt_1m.csv")
TAKER_FILE = os.path.join(OUT, "btcusdt_taker.csv")
LS_RATIO_FILE = os.path.join(OUT, "btcusdt_lsratio.csv")
FUNDING_FILE = os.path.join(OUT, "btcusdt_funding.csv")
OPEN_INTEREST_FILE = os.path.join(OUT, "btcusdt_open_interest.csv")
GLOBAL_LS_RATIO_FILE = os.path.join(OUT, "btcusdt_global_lsratio.csv")
TOP_ACCOUNT_LS_RATIO_FILE = os.path.join(OUT, "btcusdt_top_account_lsratio.csv")
SECOND_TRADES_FILE = os.path.join(OUT, "btcusdt_1s_trades.csv")
ORDERBOOK_FILE = os.path.join(OUT, "btcusdt_orderbook_1s.csv")
AUCTION_ROOT = os.path.join(OUT, "auction", "BTCUSDT", "futures")
AUCTION_STATUS_FILE = os.path.join(OUT, "auction_data_status.json")
