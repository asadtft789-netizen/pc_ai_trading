"""Common technical indicator helpers for strategy authors and the LLM.

These vectorized functions operate on grouped multi-index DataFrames and are safe
for import inside `exec()` strategy code (only pandas/numpy used).
"""
import numpy as np
import pandas as pd


def sma(grouped_close: pd.Series, window: int) -> pd.Series:
    return grouped_close.transform(lambda x: x.rolling(window=window, min_periods=1).mean())


def ema(grouped_close: pd.Series, window: int) -> pd.Series:
    return grouped_close.transform(lambda x: x.ewm(span=window, adjust=False, min_periods=1).mean())


def rsi(grouped_close: pd.Series, window: int = 14) -> pd.Series:
    delta = grouped_close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.groupby(level="symbol").rolling(window=window, min_periods=1).mean().reset_index(level=0, drop=True)
    avg_loss = loss.groupby(level="symbol").rolling(window=window, min_periods=1).mean().reset_index(level=0, drop=True)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def bollinger(grouped_close: pd.Series, window: int = 20, num_std: float = 2.0):
    ma = sma(grouped_close, window)
    std = grouped_close.transform(lambda x: x.rolling(window=window, min_periods=1).std())
    upper = ma + num_std * std
    lower = ma - num_std * std
    return upper, ma, lower


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.groupby(level="symbol").shift(1)
    tr = np.maximum(high - low, np.maximum((high - prev_close).abs(), (low - prev_close).abs()))
    atr_series = tr.groupby(level="symbol").rolling(window=window, min_periods=1).mean().reset_index(level=0, drop=True)
    return atr_series


def macd(grouped_close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(grouped_close, fast) - ema(grouped_close, slow)
    signal_line = macd_line.groupby(level="symbol").transform(lambda x: x.ewm(span=signal, adjust=False).mean())
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def crossover(a: pd.Series, b: pd.Series) -> pd.Series:
    """True where series a crosses above series b."""
    return (a > b) & (a.shift(1) <= b.shift(1))


def crossunder(a: pd.Series, b: pd.Series) -> pd.Series:
    """True where series a crosses below series b."""
    return (a < b) & (a.shift(1) >= b.shift(1))
