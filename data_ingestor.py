"""Data ingestion and market regime detection for the self-evolving trader.

Fetches historical data from Alpaca and categorizes market conditions
to help the LLM understand the context of strategy performance.
"""

from __future__ import annotations
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any
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

    def __init__(self, settings: Settings, memory: MemoryManager, observation_path=None):
        from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
        self.settings = settings
        self.memory = memory
        self.observation_path = observation_path
        self.diagnostics = []
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
        from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
        from alpaca.data.enums import DataFeed
        equities = [s for s in symbols if s in self.settings.universe.equities or s in ('SPY', 'QQQ')]
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
        from alpaca.data.timeframe import TimeFrame
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=30)
        return self.fetch_bars(self.settings.universe.symbols, TimeFrame.Hour, start, end)

    def detect_regime(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Report the latest observed equity regime from the frozen SPY anchor."""
        from regime_detector import point_in_time_regimes

        if df.empty:
            return {'label': 'unknown', 'indicators': {}}
        return point_in_time_regimes(df)[1]

    def update_market_context(self):
        """Fetches recent data, detects regime, and persists it."""
        from alpaca.data.timeframe import TimeFrame
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=30)

        # Use a representative subset for regime detection
        symbols = self.settings.universe.symbols
        df = self.fetch_bars(symbols, TimeFrame.Hour, start, end)

        if not df.empty:
            regime = self.detect_regime(df)
            regime_id = self.memory.save_regime(regime["label"], regime["indicators"])
            return regime_id
        return None

    def _cached_intervals(self, start, end):
        from paper_store import db

        path = getattr(self, 'observation_path', None)
        if path is None:
            return pd.DataFrame()
        with db(path) as c:
            rows = pd.read_sql_query(
                """SELECT symbol,t AS timestamp,o AS open,h AS high,l AS low,c AS close,v AS volume,
                          interval_start,interval_end,interval_kind,feed,received_at
                   FROM usable_bars WHERE t BETWEEN ? AND ? AND julianday(received_at)<=julianday(?)
                     AND interval_kind IN ('XNYS_1m','UTC_1h')""",
                c, params=(start.isoformat(), end.isoformat(), end.isoformat()))
        if rows.empty:
            return rows
        for col in ('timestamp', 'interval_start', 'interval_end'):
            rows[col] = pd.to_datetime(rows[col], utc=True, format='ISO8601')
        return rows.set_index(['symbol', 'timestamp']).sort_index()

    def get_train_test_split(self, train_days: int = 60, test_days: int = 14, *, as_of=None) -> tuple:
        """Construct completed equity and crypto intervals with independent feed failure scopes."""
        from alpaca.data.timeframe import TimeFrame
        from market_time import aggregate_equity_minutes
        from hourly_features import enrich, validate_raw

        end = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp.now(tz='UTC')
        if end.tz is None:
            raise ValueError('as_of requires an explicit offset')
        end = end.tz_convert('UTC')
        start = (end - pd.Timedelta(days=train_days + test_days + 5)).normalize()
        cache = self._cached_intervals(start, end)
        self.diagnostics = []
        parts = []
        equities = list(dict.fromkeys([*self.settings.universe.equities, 'SPY', 'QQQ']))
        for symbols, equity in ((equities, True), (self.settings.universe.cryptos, False)):
            if not symbols:
                continue
            cohort_cache = cache
            if not cache.empty:
                cohort_cache = cache.loc[cache.index.get_level_values('symbol').isin(symbols)]
            cursor = start
            if not cohort_cache.empty:
                parts.append(cohort_cache)
                latest = cohort_cache.reset_index().groupby('symbol').timestamp.max()
                if all(symbol in latest for symbol in symbols):
                    cursor = max(start, latest.min().normalize() - pd.Timedelta(days=1))
            while cursor < end:
                # Provider SDKs collect paginated results in memory. Bound each minute request.
                stop = min(cursor + pd.Timedelta(days=3 if equity else 30), end)
                try:
                    bars = self.fetch_bars(symbols, TimeFrame.Minute if equity else TimeFrame.Hour,
                                           cursor.to_pydatetime(), stop.to_pydatetime())
                except Exception as exc:
                    self.diagnostics.append({'scope': symbols, 'error': type(exc).__name__,
                                             'start': cursor.isoformat(), 'end': stop.isoformat()})
                    break
                if not bars.empty:
                    bars = bars.reorder_levels(['symbol', 'timestamp']).sort_index()
                    timestamps = bars.index.get_level_values('timestamp')
                    if timestamps.tz is None:
                        raise ValueError('expected timezone-aware provider bars')
                    bars = bars.loc[(timestamps >= cursor) & (timestamps < stop)]
                for symbol in symbols:
                    if bars.empty or symbol not in bars.index.get_level_values('symbol'):
                        self.diagnostics.append({'scope': symbol, 'error': 'no_rows', 'start': cursor.isoformat()})
                        continue
                    try:
                        raw = bars.xs(symbol, level='symbol', drop_level=False)
                        validate_raw(raw)
                        if equity:
                            frame = aggregate_equity_minutes(raw, end)
                        else:
                            frame = raw.copy()
                            opening = frame.index.get_level_values('timestamp').tz_convert('UTC')
                            if (opening != opening.floor('h')).any():
                                raise ValueError('invalid crypto hourly boundary')
                            closing = opening + pd.Timedelta(hours=1)
                            frame['interval_start'], frame['interval_end'] = opening, closing
                            frame['interval_kind'] = 'UTC_1h'
                            frame.index = pd.MultiIndex.from_arrays(
                                [frame.index.get_level_values('symbol'), closing], names=['symbol', 'timestamp'])
                            frame = frame.loc[closing <= end]
                        if not frame.empty:
                            frame = frame.assign(feed='alpaca:iex:raw' if equity else 'alpaca:crypto:us',
                                                 received_at=pd.Timestamp.now(tz='UTC').isoformat())
                            parts.append(frame)
                    except (ValueError, TypeError, KeyError) as exc:
                        self.diagnostics.append({'scope': symbol, 'error': str(exc)[:120],
                                                 'start': cursor.isoformat(), 'end': stop.isoformat()})
                cursor = stop
        if not parts:
            return pd.DataFrame(), pd.DataFrame()
        combined = pd.concat(parts)
        # Cache rows precede freshly received values. Persistence will retain revisions and quarantine conflicts.
        combined = combined.loc[~combined.index.duplicated(keep='last')].sort_index()
        frame = enrich(combined)
        cut = end - pd.Timedelta(days=test_days)
        times = frame.index.get_level_values('timestamp')
        return frame.loc[times <= cut], frame.loc[times > cut]
