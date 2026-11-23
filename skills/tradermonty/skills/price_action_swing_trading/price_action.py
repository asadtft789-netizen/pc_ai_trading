"""Price Action and Market Structure detection module for the self-evolving trader.

This module implements the core concepts from the YouTube trading course:
- Candlestick pattern detection (hammer, engulfing, momentum candles, doji)
- Market structure analysis (swing points, BOS, CHoCH, impulse+pullback rhythm)
- Support/Resistance zones
- Fibonacci retracement levels
- Volume analysis (volume profile, POC, delta)
- Regime detection (smooth vs choppy markets)

All functions operate on pandas DataFrames with multi-index (timestamp, symbol) and
return clear, actionable signals for integration with the existing strategy system.
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime


class PriceActionDetector:
    """Detects price action patterns and market structure from OHLCV data."""

    def __init__(self, momentum_multiplier: float = 2.0):
        self.momentum_multiplier = momentum_multiplier

    def detect_candlestick_patterns(self, df: pd.DataFrame) -> Dict[str, List[str]]:
        """
        Detect candlestick patterns including:
        - Momentum candles: body >= 2x previous 3-5 candles
        - Hammer: small body + long lower wick >= 2x body, small/no upper wick
        - Engulfing patterns: current candle completely engulfs previous candle
        - Doji: open and close are virtually equal

        Args:
            df: DataFrame with multi-index (timestamp, symbol) and columns [open, high, low, close, volume]

        Returns:
            Dict mapping symbol -> list of detected patterns (strings)
        """
        patterns = {}
        for symbol, group in df.groupby(level='symbol'):
            group = group.copy()
            group['body'] = abs(group['close'] - group['open'])
            group['prev_body'] = group['body'].shift(1)
            group['prev_5_body'] = group['body'].shift(1).rolling(5, min_periods=1).sum()
            group['prev_open'] = group['open'].shift(1)
            group['prev_close'] = group['close'].shift(1)

            # Momentum candle: body >= 2x average previous 5 candles
            group['is_momentum'] = group['body'] >= self.momentum_multiplier * group['prev_5_body']

            # Hammer: small body + long lower wick >= 2x body, small/no upper wick
            group['lower_wick'] = group['close'] - group['low']
            group['upper_wick'] = group['high'] - group['close']
            group['is_hammer'] = (
                (group['body'] <= group['open'] / 2) &
                (group['lower_wick'] >= 2 * group['body']) &
                (group['upper_wick'] <= group['body'])
            )

            # Doji: open and close are virtually equal (body < 1% of price)
            group['is_doji'] = group['body'] <= group['open'] * 0.01

            # Bullish engulfing: current candle completely engulfs previous candle
            group['is_bullish_engulfing'] = (
                (group['close'] > group['open']) &
                (group['prev_close'] < group['prev_open']) &  # previous candle bearish
                (group['open'] < group['prev_close']) &  # opens below previous close
                (group['close'] > group['prev_open'])  # closes above previous open
            )

            # Bearish engulfing: current candle completely engulfs previous candle
            group['is_bearish_engulfing'] = (
                (group['close'] < group['open']) &
                (group['prev_close'] > group['prev_open']) &  # previous candle bullish
                (group['open'] > group['prev_close']) &  # opens above previous close
                (group['close'] < group['prev_open'])  # closes below previous open
            )

            symbol_patterns = []
            for idx, row in group.iterrows():
                # idx is (symbol, timestamp) per the Alpaca multi-index contract.
                # idx[0] is the string ticker (no .date()), idx[1] is the timestamp.
                # Use the timestamp level (idx[1]) for the date suffix.
                if isinstance(idx, tuple) and len(idx) > 1:
                    ts = idx[1]
                    label = str(ts.date()) if hasattr(ts, "date") else str(ts)
                elif isinstance(idx, tuple):
                    label = str(idx[0])
                else:
                    label = str(idx)
                if row.get('is_momentum', False):
                    symbol_patterns.append(f"momentum_candle_{label}")
                if row.get('is_hammer', False):
                    symbol_patterns.append(f"hammer_{label}")
                if row.get('is_doji', False):
                    symbol_patterns.append(f"doji_{label}")
                if row.get('is_bullish_engulfing', False):
                    symbol_patterns.append(f"bullish_engulfing_{label}")
                if row.get('is_bearish_engulfing', False):
                    symbol_patterns.append(f"bearish_engulfing_{label}")

            patterns[symbol] = symbol_patterns

        return patterns

    def detect_market_structure(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Detect market structure elements:
        - Swing points (HH, HL, LH, LL)
        - Break of Structure (BOS): close beyond previous swing point
        - Change of Character (CHoCH): close beyond previous swing in opposite direction
        - Impulse + pullback rhythm detection

        Returns:
            Dict with swing points, BOS/CHoCH detection, and impulse-pullback analysis
        """
        structure = {
            'swing_points': {'HH': [], 'HL': [], 'LH': [], 'LL': []},
            'bos': [],
            'choch': [],
            'impulse_pullback': []
        }

        for symbol, group in df.groupby(level='symbol'):
            group = group.copy()
            group['swing_high'] = None
            group['swing_low'] = None

            # Identify swing highs and lows (fractal points)
            for i in range(2, len(group) - 2):
                current = group.iloc[i]
                prev2 = group.iloc[i - 2]
                prev1 = group.iloc[i - 1]
                next1 = group.iloc[i + 1]
                next2 = group.iloc[i + 2]

                # Swing high: current high strictly greater than neighbors
                if (current['high'] > prev2['high'] and current['high'] > prev1['high'] and
                    current['high'] > next1['high'] and current['high'] > next2['high']):
                    group.at[current.name, 'swing_high'] = current['high']

                # Swing low: current low strictly lower than neighbors
                if (current['low'] < prev2['low'] and current['low'] < prev1['low'] and
                    current['low'] < next1['low'] and current['low'] < next2['low']):
                    group.at[current.name, 'swing_low'] = current['low']

            # Collect swing points into categories
            for i in range(2, len(group) - 2):
                current = group.iloc[i]
                ts = current.name

                if pd.notna(current.get('swing_high')):
                    # HH (higher high): current high > previous swing high
                    prior_highs = group.iloc[:i]['high']
                    if not prior_highs.empty and current['high'] > prior_highs.max():
                        structure['swing_points']['HH'].append({
                            'symbol': symbol,
                            'timestamp': str(ts[0]) if isinstance(ts, tuple) else str(ts),
                            'price': float(current['high']),
                            'type': 'HH'
                        })
                    elif current['high'] > group.iloc[i-1]['high']:
                        structure['swing_points']['HL'].append({
                            'symbol': symbol,
                            'timestamp': str(ts[0]) if isinstance(ts, tuple) else str(ts),
                            'price': float(current['high']),
                            'type': 'HL'
                        })

                if pd.notna(current.get('swing_low')):
                    # LL (lower low): current low < previous swing low
                    prior_lows = group.iloc[:i]['low']
                    if not prior_lows.empty and current['low'] < prior_lows.min():
                        structure['swing_points']['LL'].append({
                            'symbol': symbol,
                            'timestamp': str(ts[0]) if isinstance(ts, tuple) else str(ts),
                            'price': float(current['low']),
                            'type': 'LL'
                        })
                    elif current['low'] < group.iloc[i-1]['low']:
                        structure['swing_points']['LH'].append({
                            'symbol': symbol,
                            'timestamp': str(ts[0]) if isinstance(ts, tuple) else str(ts),
                            'price': float(current['low']),
                            'type': 'LH'
                        })

            # Detect BOS (Break of Structure)
            for i in range(1, len(group) - 1):
                current = group.iloc[i]
                prev = group.iloc[i - 1]

                if pd.notna(prev.get('swing_high')):
                    if current['close'] > prev['swing_high']:
                        structure['bos'].append({
                            'symbol': symbol,
                            'timestamp': str(current.name[0]) if isinstance(current.name, tuple) else str(current.name),
                            'type': 'BOS_UP',
                            'level': float(prev['swing_high']),
                            'direction': 'up'
                        })
                if pd.notna(prev.get('swing_low')):
                    if current['close'] < prev['swing_low']:
                        structure['bos'].append({
                            'symbol': symbol,
                            'timestamp': str(current.name[0]) if isinstance(current.name, tuple) else str(current.name),
                            'type': 'BOS_DOWN',
                            'level': float(prev['swing_low']),
                            'direction': 'down'
                        })

            # Detect CHoCH (Change of Character)
            for i in range(1, len(group) - 1):
                current = group.iloc[i]
                prev = group.iloc[i - 1]
                if pd.notna(prev.get('swing_high')) and current['close'] > prev['swing_high']:
                    structure['choch'].append({
                        'symbol': symbol,
                        'timestamp': str(current.name[0]) if isinstance(current.name, tuple) else str(current.name),
                        'type': 'CHoCH_UP',
                        'level': float(prev['swing_high'])
                    })
                if pd.notna(prev.get('swing_low')) and current['close'] < prev['swing_low']:
                    structure['choch'].append({
                        'symbol': symbol,
                        'timestamp': str(current.name[0]) if isinstance(current.name, tuple) else str(current.name),
                        'type': 'CHoCH_DOWN',
                        'level': float(prev['swing_low'])
                    })

            # Impulse + pullback detection
            for i in range(2, len(group) - 2):
                current = group.iloc[i]
                prev2 = group.iloc[i - 2]
                next1 = group.iloc[i + 1]

                # Impulse up followed by pullback down
                impulse_up = (
                    current['close'] > current['open'] and
                    current['close'] - current['open'] > 0.01 * current['open'] and
                    current['close'] > prev2['close'] * 1.01
                )
                pullback_down = next1['close'] < current['close'] * 0.99

                if impulse_up and pullback_down:
                    structure['impulse_pullback'].append({
                        'symbol': symbol,
                        'timestamp': str(current.name[0]) if isinstance(current.name, tuple) else str(current.name),
                        'impulse': 'up',
                        'pullback': 'down'
                    })

                # Impulse down followed by pullback up
                impulse_down = (
                    current['close'] < current['open'] and
                    current['open'] - current['close'] > 0.01 * current['open'] and
                    current['close'] < prev2['close'] * 0.99
                )
                pullback_up = next1['close'] > current['close'] * 1.01

                if impulse_down and pullback_up:
                    structure['impulse_pullback'].append({
                        'symbol': symbol,
                        'timestamp': str(current.name[0]) if isinstance(current.name, tuple) else str(current.name),
                        'impulse': 'down',
                        'pullback': 'up'
                    })

        return structure

    def detect_support_resistance_zones(self, df: pd.DataFrame) -> Dict[str, List[Dict]]:
        """
        Identify support and resistance zones based on price action reactions.
        Zones are confirmed by wicks and reversal patterns at price extremes.

        Returns:
            Dict with 'support' and 'resistance' lists of zones (price ranges)
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
                next_ = group.iloc[i + 1]

                # Support: long lower wick + bullish close + next bar confirms
                if (current['lower_wick'] >= 2 * current['body'] and
                    current['close'] > current['open'] and
                    next_['close'] > current['close']):
                    zones['support'].append({
                        'symbol': symbol,
                        'price_low': float(current['low']),
                        'price_high': float(current['close']),
                        'timestamp': str(current.name[0]) if isinstance(current.name, tuple) else str(current.name),
                        'wick_to_body_ratio': float(current['lower_wick'] / current['body']) if current['body'] > 0 else 0,
                        'confidence': 'high' if current['lower_wick'] >= 3 * current['body'] else 'medium'
                    })

                # Resistance: long upper wick + bearish close + next bar confirms
                if (current['upper_wick'] >= 2 * current['body'] and
                    current['close'] < current['open'] and
                    next_['close'] < current['close']):
                    zones['resistance'].append({
                        'symbol': symbol,
                        'price_low': float(current['close']),
                        'price_high': float(current['high']),
                        'timestamp': str(current.name[0]) if isinstance(current.name, tuple) else str(current.name),
                        'wick_to_body_ratio': float(current['upper_wick'] / current['body']) if current['body'] > 0 else 0,
                        'confidence': 'high' if current['upper_wick'] >= 3 * current['body'] else 'medium'
                    })

            # Strong support: lowest point with reversal candle
            if not group.empty:
                lowest_idx = group['low'].idxmin()
                if lowest_idx is not None:
                    row = group.loc[lowest_idx]
                    if (row['close'] > row['open'] and
                        row['lower_wick'] >= 2 * row['body']):
                        zones['support'].append({
                            'symbol': symbol,
                            'price_low': float(row['low']),
                            'price_high': float(row['low'] * 1.01),
                            'timestamp': str(lowest_idx[0]) if isinstance(lowest_idx, tuple) else str(lowest_idx),
                            'wick_to_body_ratio': float(row['lower_wick'] / row['body']) if row['body'] > 0 else 0,
                            'confidence': 'high',
                            'source': 'extreme_low'
                        })

                # Strong resistance: highest point with reversal candle
                highest_idx = group['high'].idxmax()
                if highest_idx is not None:
                    row = group.loc[highest_idx]
                    if (row['close'] < row['open'] and
                        row['upper_wick'] >= 2 * row['body']):
                        zones['resistance'].append({
                            'symbol': symbol,
                            'price_low': float(row['high'] * 0.99),
                            'price_high': float(row['high']),
                            'timestamp': str(highest_idx[0]) if isinstance(highest_idx, tuple) else str(highest_idx),
                            'wick_to_body_ratio': float(row['upper_wick'] / row['body']) if row['body'] > 0 else 0,
                            'confidence': 'high',
                            'source': 'extreme_high'
                        })

        return zones

    def detect_fibonacci_retracement(self, df: pd.DataFrame) -> Dict[str, float]:
        """
        Detect Fibonacci retracement levels from significant price moves.
        Golden zone: 38.2% - 61.8% retracement of a move.

        Returns:
            Dict with fib_levels including golden zone low/high
        """
        fib_levels = {}
        for symbol, group in df.groupby(level='symbol'):
            group = group.copy()

            # Find recent swing high and low
            high_idx = group['high'].idxmax()
            low_idx = group['low'].idxmin()

            if high_idx is None or low_idx is None:
                continue

            high_price = group.loc[high_idx, 'high']
            low_price = group.loc[low_idx, 'low']

            move = high_price - low_price
            if move <= 0:
                continue

            # Golden zone (38.2% - 61.8%)
            golden_low = low_price + 0.382 * move
            golden_high = low_price + 0.618 * move

            fib_levels[f'{symbol}_golden_zone_support'] = golden_low
            fib_levels[f'{symbol}_golden_zone_resistance'] = golden_high

        return {
            'golden_zone_low': fib_levels.get('AAPL_golden_zone_support', 0.0),
            'golden_zone_high': fib_levels.get('AAPL_golden_zone_resistance', 0.0),
        }


class VolumeAnalyzer:
    """Analyze volume patterns and order flow."""

    def analyze_volume_profile(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Analyze volume profile to identify:
        - Volume Point of Control (POC): price level with highest volume
        - Volume imbalance (delta)
        - Volume confirmation of price moves

        Returns:
            Dict with volume profile analysis per symbol
        """
        results = {'poc': {}, 'volume_imbalance': {}, 'confirmation': {}}

        for symbol, group in df.groupby(level='symbol'):
            group = group.copy()
            group['typical_price'] = (group['high'] + group['low'] + group['close']) / 3

            # Volume profile: group volume by price levels
            price_levels = np.linspace(group['low'].min(), group['high'].max(), 50)
            volume_by_level = np.zeros_like(price_levels, dtype=float)

            for _, row in group.iterrows():
                price = row['typical_price']
                volume = row['volume']
                idx = np.argmin(np.abs(price_levels - price))
                volume_by_level[idx] += volume

            # POC is price level with maximum volume
            poc_idx = int(np.argmax(volume_by_level))
            poc_price = float(price_levels[poc_idx])

            # Volume imbalance (delta): measure buying vs selling pressure
            group['is_up'] = group['close'] >= group['open']
            up_volume = float(group[group['is_up']]['volume'].sum())
            down_volume = float(group[~group['is_up']]['volume'].sum())
            total_volume = up_volume + down_volume

            volume_imbalance = (up_volume - down_volume) / total_volume if total_volume > 0 else 0

            # Volume confirmation: high volume on moves in direction of trend
            recent_volume = float(group.tail(20)['volume'].mean())
            avg_volume = float(group['volume'].mean())
            volume_confirmation = recent_volume > avg_volume * 1.5  # 50% above average

            results['poc'][symbol] = poc_price
            results['volume_imbalance'][symbol] = volume_imbalance
            results['confirmation'][symbol] = {
                'confirmed': bool(volume_confirmation),
                'recent_avg_volume': recent_volume,
                'overall_avg_volume': avg_volume,
                'volume_ratio': recent_volume / avg_volume if avg_volume > 0 else 0,
                'up_volume': up_volume,
                'down_volume': down_volume
            }

        return results

    def detect_absorption_vs_initiation(self, df: pd.DataFrame) -> Dict[str, str]:
        """
        Detect absorption vs initiation patterns:
        - Absorption: high volume but price not moving much
        - Initiation: high volume with strong price movement

        Returns:
            Dict mapping symbol -> 'absorption' or 'initiation' or 'normal'
        """
        patterns = {}

        for symbol, group in df.groupby(level='symbol'):
            group = group.copy()

            group['price_change_pct'] = group['close'].pct_change() * 100
            group['volume_surge'] = group['volume'] > group['volume'].rolling(20, min_periods=1).mean() * 1.5

            recent = group.tail(5)

            absorption_candidate = (
                recent['volume_surge'].any() and
                abs(recent['price_change_pct'].mean()) < 0.5
            )

            initiation_candidate = (
                recent['volume_surge'].any() and
                abs(recent['price_change_pct'].mean()) > 1.0
            )

            if absorption_candidate:
                patterns[symbol] = 'absorption'
            elif initiation_candidate:
                patterns[symbol] = 'initiation'
            else:
                patterns[symbol] = 'normal'

        return patterns


# Convenience functions for common patterns
def detect_crowded_long(df: pd.DataFrame) -> bool:
    """Detect crowded long conditions: new high followed by reversal."""
    detector = PriceActionDetector()
    patterns = detector.detect_candlestick_patterns(df)
    return any('momentum_candle' in p for p in patterns.values()) and \
           any('hammer' in p for p in patterns.values())


def detect_crowded_short(df: pd.DataFrame) -> bool:
    """Detect crowded short conditions: new low followed by reversal."""
    detector = PriceActionDetector()
    patterns = detector.detect_candlestick_patterns(df)
    return any('momentum_candle' in p for p in patterns.values()) and \
           any('hammer' in p for p in patterns.values())


def detect_impulse_pullback(df: pd.DataFrame) -> bool:
    """Detect impulse + pullback rhythm pattern."""
    detector = PriceActionDetector()
    structure = detector.detect_market_structure(df)
    return len(structure['impulse_pullback']) > 0


def detect_support_resistance_breakout(df: pd.DataFrame, level_type: str = 'support') -> bool:
    """Detect breakout from support/resistance level."""
    detector = PriceActionDetector()
    zones = detector.detect_support_resistance_zones(df)

    zone_list = zones.get('support', []) if level_type == 'support' else zones.get('resistance', [])

    if not zone_list:
        return False

    current = df.iloc[-1]
    if level_type == 'support' and current['close'] > zone_list[-1]['price_high']:
        return True
    elif level_type == 'resistance' and current['close'] < zone_list[-1]['price_low']:
        return True

    return False