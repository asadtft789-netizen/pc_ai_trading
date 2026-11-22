"""Built-in fallback strategy used when the LLM is unavailable.

A simple SMA crossover strategy so the full loop (backtest -> test gate ->
trade -> exit -> lesson) runs end-to-end with real Alpaca data without
needing an LLM key. Swap in LLM-generated strategies once credentials work.
"""

import pandas as pd
import numpy as np

from strategy import BaseStrategy


class SMACrossoverStrategy(BaseStrategy):
    """Buy when fast SMA crosses above slow SMA; sell on cross below."""

    def __init__(self, name: str, params: dict = None):
        super().__init__(name, params or {})
        self.fast = int(self.params.get("fast", 10))
        self.slow = int(self.params.get("slow", 30))

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        res = df.copy()
        grouped = res.groupby(level="symbol")["close"]
        fast_ma = grouped.transform(lambda x: x.rolling(window=self.fast).mean())
        slow_ma = grouped.transform(lambda x: x.rolling(window=self.slow).mean())
        prev_fast = fast_ma.groupby(level="symbol").shift(1)
        prev_slow = slow_ma.groupby(level="symbol").shift(1)

        res["signal"] = 0
        buy = (fast_ma > slow_ma) & (prev_fast <= prev_slow)
        sell = (fast_ma < slow_ma) & (prev_fast >= prev_slow)
        res.loc[buy, "signal"] = 1
        res.loc[sell, "signal"] = -1
        res["signal"] = res["signal"].fillna(0).astype(int)
        return res[["open", "high", "low", "close", "volume", "signal"]]


def get_fallback_strategy() -> dict:
    import uuid

    code = '''import pandas as pd
import numpy as np

from strategy import BaseStrategy


class SMACrossoverStrategy(BaseStrategy):
    """Buy when fast SMA crosses above slow SMA; sell on cross below."""

    def __init__(self, name: str, params: dict = None):
        super().__init__(name, params or {})
        self.fast = int(self.params.get("fast", 10))
        self.slow = int(self.params.get("slow", 30))

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        res = df.copy()
        grouped = res.groupby(level="symbol")["close"]
        fast_ma = grouped.transform(lambda x: x.rolling(window=self.fast).mean())
        slow_ma = grouped.transform(lambda x: x.rolling(window=self.slow).mean())
        prev_fast = fast_ma.groupby(level="symbol").shift(1)
        prev_slow = slow_ma.groupby(level="symbol").shift(1)

        res["signal"] = 0
        buy = (fast_ma > slow_ma) & (prev_fast <= prev_slow)
        sell = (fast_ma < slow_ma) & (prev_fast >= prev_slow)
        res.loc[buy, "signal"] = 1
        res.loc[sell, "signal"] = -1
        res["signal"] = res["signal"].fillna(0).astype(int)
        return res[["open", "high", "low", "close", "volume", "signal"]]
'''
    return {
        "id": str(uuid.uuid4()),
        "name": "BuiltIn_SMACrossover",
        "code": code,
        "parent_id": None,
        "generation": 0,
    }
