"""Data ingestion and market regime detection for the self-evolving trader.

Fetches historical data from Alpaca and categorizes market conditions
to help the LLM understand the context of strategy performance.
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame

from config import Settings
from memory import MemoryManager

# === Advanced Market Indicators (self-evolving intelligence) ===


def _wilder_rsi(close: pd.Series, window: int = 14) -> pd.Series:
    """Wilder's RSI - smoothed average gain/loss oscillator."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.rolling(window=window, min_periods=1).mean()
    avg_loss = loss.rolling(window=window, min_periods=1).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def _atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Average True Range - volatility measure."""
    high = df['high']
    low = df['low']
    close = df['close'].shift(1)
    tr = np.maximum(high - low, np.maximum((high - close).abs(), (low - close).abs()))
    return tr.rolling(window=window, min_periods=1).mean()


def _adx(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Average Directional Index - trend strength (0-100)."""
    high = df['high']
    low = df['low']
    close = df['close']
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = _atr(df, window=window)
    tr_smooth = tr.rolling(window=window, min_periods=1).sum()
    plus_di = 100 * pd.Series(plus_dm, index=tr.index).rolling(window=window, min_periods=1).sum() / tr_smooth.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=tr.index).rolling(window=window, min_periods=1).sum() / tr_smooth.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.rolling(window=window, min_periods=1).mean()
    return adx


def _ema(close: pd.Series, span: int) -> pd.Series:
    """Exponential Moving Average."""
    return close.ewm(span=span, adjust=False).mean()


def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add RSI, ATR, ADX, EMA20/EMA50 to every symbol group in a multi-index DataFrame."""
    res = df.copy()
    close = res.groupby(level="symbol")["close"]

    res["rsi"] = close.transform(_wilder_rsi)
    res["atr"] = res.groupby(level="symbol").apply(lambda g: _atr(g), include_groups=False).reset_index(level=0, drop=True)
    res["adx"] = res.groupby(level="symbol").apply(lambda g: _adx(g), include_groups=False).reset_index(level=0, drop=True)
    res["ema20"] = close.transform(lambda x: _ema(x, span=20))
    res["ema50"] = close.transform(lambda x: _ema(x, span=50))
    return res


class DataIngestor:
    """Handles fetching market data and identifying market regimes."""

    def __init__(self, settings: Settings, memory: MemoryManager):
        self.settings = settings
        self.memory = memory
        self.stock_client = StockHistoricalDataClient(
            settings.alpaca.api_key, settings.alpaca.api_secret
        )
        self.crypto_client = CryptoHistoricalDataClient(
            settings.alpaca.api_key, settings.alpaca.api_secret
        )

    def fetch_bars(
        self, symbols: List[str], timeframe: TimeFrame, start: datetime, end: datetime
    ) -> pd.DataFrame:
        """Fetches historical bars for a mix of equities and cryptos."""
        equities = [s for s in symbols if s in self.settings.universe.equities]
        cryptos = [s for s in symbols if s in self.settings.universe.cryptos]

        dfs = []

        if equities:
            req = StockBarsRequest(
                symbol_or_symbols=equities,
                timeframe=timeframe,
                start=start,
                end=end,
                adjustment='raw',
                feed=DataFeed.IEX
            )
            stock_bars = self.stock_client.get_stock_bars(req)
            if not stock_bars.df.empty:
                dfs.append(stock_bars.df)

        if cryptos:
            # Alpaca crypto symbols in bars request often don't want the slash or want specific formatting
            # alpaca-py handles the symbols as provided usually
            req = CryptoBarsRequest(
                symbol_or_symbols=cryptos,
                timeframe=timeframe,
                start=start,
                end=end
            )
            crypto_bars = self.crypto_client.get_crypto_bars(req)
            if not crypto_bars.df.empty:
                dfs.append(crypto_bars.df)

        if not dfs:
            return pd.DataFrame()

        return pd.concat(dfs)

    def get_latest_data(self) -> pd.DataFrame:
        """Fetches the most recent market data for the trade universe."""
        end = datetime.now()
        start = end - timedelta(days=30)
        return self.fetch_bars(self.settings.universe.symbols, TimeFrame.Day, start, end)

    def detect_regime(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Detects the current market regime based on recent price action.
        Uses a heuristic approach: Trend (Moving Average) + Volatility (ATR/StdDev).
        """
        if df.empty:
            return {"label": "unknown", "indicators": {}}

        # Use the first symbol or an index proxy if available.
        # For simplicity, we'll average the returns of all symbols to get a "market" view.
        # In a real system, one might use SPY or BTC as the regime anchor.

        # Reshape to have symbols as columns for easier market averaging
        pivoted = df.reset_index().pivot(index='timestamp', columns='symbol', values='close')
        returns = pivoted.pct_change().dropna()
        market_return = returns.mean(axis=1)

        # Indicators
        # 1. Trend: 20-period moving average of market returns
        trend_series = market_return.rolling(window=20).mean()
        trend = float(trend_series.iloc[-1]) if len(trend_series.dropna()) > 0 else 0.0

        # 2. Volatility: 20-period standard deviation of returns
        vol_series = market_return.rolling(window=20).std()
        vol = float(vol_series.iloc[-1]) if len(vol_series.dropna()) > 0 else 0.0

        # 3. Momentum: Last 5 days vs last 20 days
        short_ma_series = market_return.rolling(window=5).mean()
        long_ma_series = market_return.rolling(window=20).mean()
        short_ma = float(short_ma_series.iloc[-1]) if len(short_ma_series.dropna()) > 0 else 0.0
        long_ma = float(long_ma_series.iloc[-1]) if len(long_ma_series.dropna()) > 0 else 0.0

        indicators = {
            "avg_return_20d": float(trend),
            "volatility_20d": float(vol),
            "momentum_ratio": float(short_ma / long_ma) if long_ma != 0 else 1.0,
        }

        # Heuristic labeling
        if trend > 0.001 and vol < 0.01:
            label = "bull_quiet"
        elif trend > 0.001 and vol >= 0.01:
            label = "bull_volatile"
        elif trend < -0.001 and vol < 0.01:
            label = "bear_quiet"
        elif trend < -0.001 and vol >= 0.01:
            label = "bear_volatile"
        else:
            label = "sideways"

        return {"label": label, "indicators": indicators}

    def update_market_context(self):
        """Fetches recent data, detects regime, and persists it."""
        end = datetime.now()
        start = end - timedelta(days=30)

        # Use a representative subset for regime detection
        symbols = self.settings.universe.symbols
        df = self.fetch_bars(symbols, TimeFrame.Day, start, end)

        if not df.empty:
            regime = self.detect_regime(data=df)
            regime_id = self.memory.save_regime(regime["label"], regime["indicators"])
            return regime_id
        return None

    def get_train_test_split(self, train_days: int = 60, test_days: int = 14) -> tuple:
        """
        Fetch real market data and split into train (older) and test (recent held-out).

        Train data is used to backtest candidate strategies; test data is held out
        and used only for out-of-sample validation. This prevents the cheating where
        a strategy is tested on the same data it was generated for.

        Returns (train_df, test_df) — both multi-index (timestamp, symbol) DataFrames.
        """
        end = datetime.now()
        # Start far enough back to cover both train and test windows
        start = end - timedelta(days=train_days + test_days + 5)

        symbols = self.settings.universe.symbols
        df = self.fetch_bars(symbols, TimeFrame.Day, start, end)

        if df.empty:
            return pd.DataFrame(), pd.DataFrame()

        # Sort by timestamp to ensure chronological split
        df = df.sort_index()

        # Split point: test = last test_days, train = everything before
        latest_ts = df.index.get_level_values('timestamp').max()
        split_ts = latest_ts - timedelta(days=test_days)

        train_mask = df.index.get_level_values('timestamp') <= split_ts
        test_mask = df.index.get_level_values('timestamp') > split_ts

        train_df = df.loc[train_mask].copy()
        test_df = df.loc[test_mask].copy()

        # Enrich with advanced indicators so strategies see RSI/ATR/ADX/EMA features.
        if not train_df.empty:
            train_df = _add_indicators(train_df)
        if not test_df.empty:
            test_df = _add_indicators(test_df)

        return train_df, test_df
