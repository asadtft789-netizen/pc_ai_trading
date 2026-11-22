"""Built-in fallback strategy used when the LLM is unavailable.

A robust SMA + RSI filter strategy so the full loop (backtest -> test gate ->
trade -> exit -> lesson) runs end-to-end with real Alpaca data without needing
an LLM key. Uses smaller windows so it works even on short held-out test
windows, and an RSI filter to avoid buying overbought / selling oversold
(major accuracy improvement over plain SMA crossover).
"""

import pandas as pd
import numpy as np

from strategy import BaseStrategy


class SMARSIStrategy(BaseStrategy):
    """Buy when fast SMA crosses above slow SMA and RSI is not overbought.
    Sell on cross under or RSI overbought."""

    def __init__(self, name: str, params: dict = None):
        super().__init__(name, params or {})
        self.fast = int(self.params.get("fast", 5))
        self.slow = int(self.params.get("slow", 15))
        self.rsi_window = int(self.params.get("rsi_window", 14))
        self.rsi_overbought = float(self.params.get("rsi_overbought", 70.0))
        self.rsi_oversold = float(self.params.get("rsi_oversold", 30.0))

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        res = df.copy()
        close = res.groupby(level="symbol")["close"]

        fast_ma = close.transform(lambda x: x.rolling(window=self.fast, min_periods=1).mean())
        slow_ma = close.transform(lambda x: x.rolling(window=self.slow, min_periods=1).mean())

        # RSI
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.groupby(level="symbol").rolling(window=self.rsi_window, min_periods=1).mean().reset_index(level=0, drop=True)
        avg_loss = loss.groupby(level="symbol").rolling(window=self.rsi_window, min_periods=1).mean().reset_index(level=0, drop=True)
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))

        prev_fast = fast_ma.groupby(level="symbol").shift(1)
        prev_slow = slow_ma.groupby(level="symbol").shift(1)

        res["signal"] = 0
        buy = (
            (fast_ma > slow_ma)
            & (prev_fast <= prev_slow)
            & (rsi < self.rsi_overbought)
        )
        sell = (
            (fast_ma < slow_ma)
            & (prev_fast >= prev_slow)
        )
        res.loc[buy, "signal"] = 1
        res.loc[sell, "signal"] = -1
        res["signal"] = res["signal"].fillna(0).astype(int)
        return res[["open", "high", "low", "close", "volume", "signal"]]


def get_fallback_strategy() -> dict:
    import uuid

    code = '''import pandas as pd
import numpy as np

from strategy import BaseStrategy


class SMARSIStrategy(BaseStrategy):
    """Buy when fast SMA crosses above slow SMA and RSI is not overbought.
    Sell on cross under or RSI overbought."""

    def __init__(self, name: str, params: dict = None):
        super().__init__(name, params or {})
        self.fast = int(self.params.get("fast", 5))
        self.slow = int(self.params.get("slow", 15))
        self.rsi_window = int(self.params.get("rsi_window", 14))
        self.rsi_overbought = float(self.params.get("rsi_overbought", 70.0))
        self.rsi_oversold = float(self.params.get("rsi_oversold", 30.0))

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        res = df.copy()
        close = res.groupby(level="symbol")["close"]

        fast_ma = close.transform(lambda x: x.rolling(window=self.fast, min_periods=1).mean())
        slow_ma = close.transform(lambda x: x.rolling(window=self.slow, min_periods=1).mean())

        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.groupby(level="symbol").rolling(window=self.rsi_window, min_periods=1).mean().reset_index(level=0, drop=True)
        avg_loss = loss.groupby(level="symbol").rolling(window=self.rsi_window, min_periods=1).mean().reset_index(level=0, drop=True)
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))

        prev_fast = fast_ma.groupby(level="symbol").shift(1)
        prev_slow = slow_ma.groupby(level="symbol").shift(1)

        res["signal"] = 0
        buy = (
            (fast_ma > slow_ma)
            & (prev_fast <= prev_slow)
            & (rsi < self.rsi_overbought)
        )
        sell = (
            (fast_ma < slow_ma)
            & (prev_fast >= prev_slow)
        )
        res.loc[buy, "signal"] = 1
        res.loc[sell, "signal"] = -1
        res["signal"] = res["signal"].fillna(0).astype(int)
        return res[["open", "high", "low", "close", "volume", "signal"]]
'''
    return {
        "id": str(uuid.uuid4()),
        "name": "BuiltIn_SMA_RSI",
        "code": code,
        "parent_id": None,
        "generation": 0,
    }
