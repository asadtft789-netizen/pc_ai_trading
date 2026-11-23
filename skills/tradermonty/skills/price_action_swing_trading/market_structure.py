"""Market Structure detection module for the self-evolving trader.

Implements swing points, BOS (break of structure), CHoCH (change of character),
and impulse+pullback rhythm detection as described in the YouTube course.
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Any


def _ts_label(idx) -> str:
    """Render a (timestamp, symbol) index entry as a readable string."""
    if isinstance(idx, tuple):
        return str(idx[0])
    return str(idx)


class MarketStructureDetector:
    """Detects market structure elements including swing points, BOS, CHoCH, and impulse patterns."""

    def __init__(self, swing_lookback: int = 5, extreme_lookback: int = 20):
        self.swing_lookback = swing_lookback
        self.extreme_lookback = extreme_lookback

    def _mark_swings(self, group: pd.DataFrame) -> pd.DataFrame:
        """Mark fractal swing highs/lows (2 bars either side)."""
        g = group.copy()
        g['swing_high'] = np.nan
        g['swing_low'] = np.nan

        highs = g['high'].to_numpy()
        lows = g['low'].to_numpy()

        for i in range(2, len(g) - 2):
            if (highs[i] > highs[i - 2] and highs[i] > highs[i - 1]
                    and highs[i] > highs[i + 1] and highs[i] > highs[i + 2]):
                g.iloc[i, g.columns.get_loc('swing_high')] = highs[i]
            if (lows[i] < lows[i - 2] and lows[i] < lows[i - 1]
                    and lows[i] < lows[i + 1] and lows[i] < lows[i + 2]):
                g.iloc[i, g.columns.get_loc('swing_low')] = lows[i]

        return g

    def detect_swing_points(self, df: pd.DataFrame) -> Dict[str, List[Dict]]:
        """
        Detect swing points (HH, HL, LH, LL) - fractal points where price reverses.

        Returns:
            Dict with swing point categories (HH, HL, LH, LL) containing list of points
        """
        swing_points = {'HH': [], 'HL': [], 'LH': [], 'LL': []}

        for symbol, group in df.groupby(level='symbol'):
            g = self._mark_swings(group)

            last_high = None
            last_low = None

            for i in range(len(g)):
                row = g.iloc[i]
                ts = _ts_label(row.name)

                if pd.notna(row['swing_high']):
                    price = float(row['swing_high'])
                    label = 'HH' if (last_high is not None and price > last_high) else 'LH'
                    swing_points[label].append({
                        'symbol': symbol,
                        'timestamp': ts,
                        'price': price,
                        'type': label,
                    })
                    last_high = price

                if pd.notna(row['swing_low']):
                    price = float(row['swing_low'])
                    label = 'LL' if (last_low is not None and price < last_low) else 'HL'
                    swing_points[label].append({
                        'symbol': symbol,
                        'timestamp': ts,
                        'price': price,
                        'type': label,
                    })
                    last_low = price

        # Order by timestamp so downstream consumers see chronological order
        for key in swing_points:
            swing_points[key].sort(key=lambda p: p['timestamp'])

        return swing_points

    def detect_bos_choch(self, df: pd.DataFrame) -> Dict[str, List[Dict]]:
        """
        Detect Break of Structure (BOS) and Change of Character (CHoCH).

        BOS: price closes beyond previous swing point while continuing the trend.
        CHoCH: price closes beyond the previous swing point against the prior trend.

        Returns:
            Dict with 'bos' and 'choch' lists
        """
        bos: List[Dict[str, Any]] = []
        choch: List[Dict[str, Any]] = []

        for symbol, group in df.groupby(level='symbol'):
            g = self._mark_swings(group)

            last_high: float | None = None
            last_low: float | None = None
            # Track the trend the market is currently in: 'up', 'down', or None
            trend: str | None = None

            for i in range(len(g)):
                row = g.iloc[i]
                ts = _ts_label(row.name)

                # A break is validated against the most recent confirmed swing level
                if last_high is not None and row['close'] > last_high:
                    if trend == 'down':
                        choch.append({
                            'symbol': symbol, 'timestamp': ts,
                            'type': 'CHoCH_UP', 'level': float(last_high),
                        })
                        trend = 'up'
                    else:
                        bos.append({
                            'symbol': symbol, 'timestamp': ts,
                            'type': 'BOS_UP', 'level': float(last_high),
                            'direction': 'up',
                        })
                        trend = 'up'
                    last_high = None

                if last_low is not None and row['close'] < last_low:
                    if trend == 'up':
                        choch.append({
                            'symbol': symbol, 'timestamp': ts,
                            'type': 'CHoCH_DOWN', 'level': float(last_low),
                        })
                        trend = 'down'
                    else:
                        bos.append({
                            'symbol': symbol, 'timestamp': ts,
                            'type': 'BOS_DOWN', 'level': float(last_low),
                            'direction': 'down',
                        })
                        trend = 'down'
                    last_low = None

                # Record new confirmed swings after break checks so they can be
                # broken by a later bar, not the bar that created them.
                if pd.notna(row['swing_high']):
                    last_high = float(row['swing_high'])
                if pd.notna(row['swing_low']):
                    last_low = float(row['swing_low'])

        return {'bos': bos, 'choch': choch}

    def detect_impulse_pullback(self, df: pd.DataFrame) -> List[Dict]:
        """
        Detect impulse + pullback rhythm pattern.
        Impulse: strong move in one direction (body >= 1% of price).
        Pullback: next bar moves back against the impulse.

        Returns:
            List of impulse-pullback events
        """
        events: List[Dict[str, Any]] = []

        for symbol, group in df.groupby(level='symbol'):
            g = group.copy()
            closes = g['close'].to_numpy()
            opens = g['open'].to_numpy()

            for i in range(2, len(g) - 1):
                body = closes[i] - opens[i]
                ts = _ts_label(g.index[i])

                impulse_up = (
                    body > 0
                    and body > 0.01 * opens[i]
                    and closes[i] > closes[i - 2] * 1.01
                )
                pullback_down = closes[i + 1] < closes[i] * 0.99

                if impulse_up and pullback_down:
                    events.append({
                        'symbol': symbol, 'timestamp': ts,
                        'impulse': 'up', 'pullback': 'down',
                    })

                impulse_down = (
                    body < 0
                    and -body > 0.01 * opens[i]
                    and closes[i] < closes[i - 2] * 0.99
                )
                pullback_up = closes[i + 1] > closes[i] * 1.01

                if impulse_down and pullback_up:
                    events.append({
                        'symbol': symbol, 'timestamp': ts,
                        'impulse': 'down', 'pullback': 'up',
                    })

        return events