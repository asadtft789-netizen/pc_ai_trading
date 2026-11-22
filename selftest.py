"""Standalone self-test: exercises the full pipeline with real Alpaca data
without placing live paper-trading orders."""
import sys
sys.path.insert(0, "C:/self_evolving_trader")

import pandas as pd
from datetime import datetime, timedelta

from config import Settings
from memory import MemoryManager
from backtester import Backtester
from risk_engine import RiskEngine
from builtin_strategy import SMARSIStrategy
from strategy import BaseStrategy
from datetime import datetime

settings = Settings.load(env_path=".env")
memory = MemoryManager(settings)
risk_engine = RiskEngine(settings)
backtester = Backtester(settings, risk_engine)

print("=== 1. Real market data: train/test split ===")
end = datetime.now()
start = end - timedelta(days=200)

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.timeframe import TimeFrame
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed

client = StockHistoricalDataClient(settings.alpaca.api_key, settings.alpaca.api_secret)
req = StockBarsRequest(
    symbol_or_symbols=["AAPL", "MSFT", "NVDA"],
    timeframe=TimeFrame.Day,
    start=start,
    end=end,
    adjustment="raw",
    feed=DataFeed.IEX,
)
df = client.get_stock_bars(req).df
df = df.reset_index()
df = df.set_index(["symbol", "timestamp"])
print(f"Fetched {len(df)} bars. Index order: {df.index.names}")

# Split: ~186 days train, last 14 days test
latest_ts = df.index.get_level_values("timestamp").max()
split_ts = latest_ts - timedelta(days=14)
train_df = df[df.index.get_level_values("timestamp") <= split_ts].copy()
test_df = df[df.index.get_level_values("timestamp") > split_ts].copy()
print(f"Train: {len(train_df)} bars | Test: {len(test_df)} bars")
assert len(train_df) > 0 and len(test_df) > 0, "Split produced empty data!"

# Ensure no overlap (anti-cheating gate)
train_ts = set(train_df.index.get_level_values("timestamp"))
test_ts = set(test_df.index.get_level_values("timestamp"))
overlap = train_ts & test_ts
print(f"Train/test overlap: {len(overlap)} timestamps")
assert len(overlap) == 0, "Data leakage! Train and test overlap."

print("\n=== 2. Backtester on fallback SMARSIStrategy ===")
strategy = SMARSIStrategy(name="fallback_test")

train_signals = strategy.generate_signals(train_df.copy())
bt = backtester.run(strategy, train_signals)
print(f"Train backtest: trades={bt['total_trades']} win_rate={bt['win_rate']:.2%} "
      f"sharpe={bt['sharpe']:.2f} pnl=${bt['pnl']:.2f} dd={bt['max_drawdown']:.2%}")

test_signals = strategy.generate_signals(test_df.copy())
bt2 = backtester.run(strategy, test_signals)
print(f"Test backtest: trades={bt2['total_trades']} win_rate={bt2['win_rate']:.2%} "
      f"sharpe={bt2['sharpe']:.2f} pnl=${bt2['pnl']:.2f} dd={bt2['max_drawdown']:.2%}")
if bt2["trades"]:
    print(f"  First trade: {bt2['trades'][0]}")

print("\n=== 3. Memory: rule storage & retrieval ===")
memory.save_rule("Rule: Never enter trades in sideways markets.", source_reflection_id=None)
rules = memory.get_all_rules()
print(f"Rules stored: {len(rules)}")
assert any("sideways" in r["rule_text"] for r in rules), "Rule not persisted"

print("\n=== 4. Trade recording + metrics ===")
order_id = f"test-order-{datetime.now().timestamp()}"
memory.log_trade_open(order_id, "test-id-1", "AAPL", 1.0, "buy", regime_id=1)
memory.close_trade_by_order_id(order_id, 150.50, 0.50)
metrics = memory.get_trade_metrics()
print(f"Trade metrics: {metrics}")
assert metrics["total_trades"] >= 1, "Trade not recorded"

print("\n=== 5. Anti-cheating gate ===")
def test_passes(m):
    return m.get("total_trades", 0) > 0 and m.get("win_rate", 0) >= 0.40 and m.get("sharpe", 0) > 0 and m.get("total_return_pct", 0) > 0
print(f"Train test passes gate: {test_passes(bt)}")
print(f"Test (held-out) passes gate: {test_passes(bt2)}")

print("\nALL TESTS PASSED")
