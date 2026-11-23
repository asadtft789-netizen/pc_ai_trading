"""Support/Resistance zones and Fibonacci retracement module.

Implements dynamic zone identification based on price action reactions
(wicks, reversal patterns) and Fibonacci golden zone (38.2%-61.8%) detection.
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Any


class SupportResistanceDetector:
    """Detects support and resistance zones based on price action reactions."""

    def __init__(self, wick_threshold: float = 2.0, min_reaction: float = 0.01):
        self.wick_threshold = wick_threshold  # minimum wick-to-body ratio
        self.min_reaction = min_reaction  # minimum price movement to count as reaction

    def detect_zones(self, df: pd.DataFrame) -> Dict[str, List[Dict]]:
        """
        Identify support and resistance zones based on price action reactions.
        Zones are confirmed by wicks and reversal patterns at price extremes.

        Args:
            df: DataFrame with multi-index (timestamp, symbol) and OHLC columns

        Returns:
            Dict with 'support' and 'resistance' lists containing zone dicts
        """
        zones = {'support': [], 'resistance': []}

        for symbol, group in df.groupby(level='symbol'):
            group = group.copy()

            # Calculate body and wick sizes
            group['body'] = abs(group['close'] - group['open'])
            group['upper_wick'] = group['high'] - group['close']
            group['lower_wick'] = group['close'] - group['low']

            # Identify support zones: long lower wick + bullish reversal
            for i in range(1, len(group) - 1):
                current = group.iloc[i]
                prev = group.iloc[i-1]
                next_ = group.iloc[i+1]

                # Support: long lower wick + bullish close
                if (current['lower_wick'] >= self.wick_threshold * current['body'] and
                    current['close'] > current['open']):
                    # Confirm with next bar closing higher
                    if next_['close'] > current['close']:
                        zones['support'].append({
                            'symbol': symbol,
                            'price_low': current['low'],
                            'price_high': current['close'],
                            'timestamp': current.name,
                            'wick_to_body_ratio': current['lower_wick'] / current['body'] if current['body'] > 0 else 0,
                            'confidence': 'high' if current['lower_wick'] >= 3 * current['body'] else 'medium'
                        })

                # Resistance: long upper wick + bearish close
                if (current['upper_wick'] >= self.wick_threshold * current['body'] and
                    current['close'] < current['open']):
                    if next_['close'] < current['close']:
                        zones['resistance'].append({
                            'symbol': symbol,
                            'price_low': current['close'],
                            'price_high': current['high'],
                            'timestamp': current.name,
                            'wick_to_body_ratio': current['upper_wick'] / current['body'] if current['body'] > 0 else 0,
                            'confidence': 'high' if current['upper_wick'] >= 3 * current['body'] else 'medium'
                        })

            # Strong support: lowest point with reversal candle
            if not group.empty:
                lowest_idx = group['low'].idxmin()
                if lowest_idx is not None:
                    row = group.loc[lowest_idx]
                    if (row['close'] > row['open'] and  # bullish
                        row['lower_wick'] >= self.wick_threshold * row['body']):
                        zones['support'].append({
                            'symbol': symbol,
                            'price_low': row['low'],
                            'price_high': row['low'] * 1.01,
                            'timestamp': row.name,
                            'wick_to_body_ratio': row['lower_wick'] / row['body'] if row['body'] > 0 else 0,
                            'confidence': 'high',
                            'source': 'extreme_low'
                        })

                # Strong resistance: highest point with reversal candle
                highest_idx = group['high'].idxmax()
                if highest_idx is not None:
                    row = group.loc[highest_idx]
                    if (row['close'] < row['open'] and  # bearish
                        row['upper_wick'] >= self.wick_threshold * row['body']):
                        zones['resistance'].append({
                            'symbol': symbol,
                            'price_low': row['high'] * 0.99,
                            'price_high': row['high'],
                            'timestamp': row.name,
                            'wick_to_body_ratio': row['upper_wick'] / row['body'] if row['body'] > 0 else 0,
                            'confidence': 'high',
                            'source': 'extreme_high'
                        })

        return zones


class FibonacciRetracement:
    """Calculate Fibonacci retracement levels from significant price moves."""

    FIB_LEVELS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
    GOLDEN_ZONE_LOW = 0.382
    GOLDEN_ZONE_HIGH = 0.618

    def __init__(self, lookback: int = 50):
        self.lookback = lookback

    def detect_retracement_levels(self, df: pd.DataFrame) -> Dict[str, List[Dict]]:
        """
        Detect Fibonacci retracement levels from recent significant price moves.
        Identifies the golden zone (38.2% - 61.8%).

        Args:
            df: DataFrame with multi-index (timestamp, symbol) and OHLC columns

        Returns:
            Dict with fib analysis per symbol including levels and golden zone
        """
        results = {'levels': [], 'golden_zone': []}

        for symbol, group in df.groupby(level='symbol'):
            group = group.copy()

            # Find significant price swings within lookback
            recent = group.tail(self.lookback)

            # Identify recent swing high and low
            high_idx = recent['high'].idxmax()
            low_idx = recent['low'].idxmin()

            if high_idx is None or low_idx is None:
                continue

            high_price = group.loc[high_idx, 'high']
            low_price = group.loc[low_idx, 'low']

            move = high_price - low_price
            if move <= 0:
                continue

            # Calculate Fibonacci levels
            fib_levels = {}
            for fib in self.FIB_LEVELS:
                fib_levels[f'{fib}'] = low_price + fib * move

            # Golden zone (38.2% - 61.8%)
            golden_low = low_price + self.GOLDEN_ZONE_LOW * move
            golden_high = low_price + self.GOLDEN_ZONE_HIGH * move

            # Check if current price is at golden zone
            current_price = group['close'].iloc[-1]

            # Determine if price is retracing towards golden zone
            if high_idx > low_idx:
                # Downtrend from high, retracing up
                direction = 'downtrend_retrace_up'
            else:
                # Uptrend from low, retracing down
                direction = 'uptrend_retrace_down'

            results['levels'].append({
                'symbol': symbol,
                'swing_high': high_price,
                'swing_low': low_price,
                'fib_levels': fib_levels,
                'golden_zone': {'low': golden_low, 'high': golden_high},
                'direction': direction,
                'current_price': current_price,
                'at_golden_zone': golden_low <= current_price <= golden_high
            })

            results['golden_zone'].append({
                'symbol': symbol,
                'low': golden_low,
                'high': golden_high,
                'range_pct': ((golden_high - golden_low) / current_price) * 100,
                'current_at_zone': golden_low <= current_price <= golden_high
            })

        return results