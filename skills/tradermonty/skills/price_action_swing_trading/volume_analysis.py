"""Volume Analysis module for the self-evolving trader.

Implements volume profile, POC (Point of Control), volume delta,
absorption vs initiation detection as taught in the YouTube course.
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Any


class VolumeAnalyzer:
    """Analyze volume patterns including volume profile, POC, and delta."""

    def __init__(self, profile_levels: int = 50):
        self.profile_levels = profile_levels

    def analyze_volume_profile(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Analyze volume profile to identify:
        - Volume Point of Control (POC): price level with highest volume
        - Volume imbalance (delta)
        - Volume confirmation of price moves

        Args:
            df: DataFrame with multi-index (timestamp, symbol) and OHLCV columns

        Returns:
            Dict with volume profile analysis per symbol
        """
        results = {'poc': {}, 'volume_imbalance': {}, 'confirmation': {}}

        for symbol, group in df.groupby(level='symbol'):
            group = group.copy()
            group['typical_price'] = (group['high'] + group['low'] + group['close']) / 3

            # Volume profile: group volume by price levels
            price_levels = np.linspace(group['low'].min(), group['high'].max(), self.profile_levels)
            volume_by_level = np.zeros_like(price_levels, dtype=float)

            for _, row in group.iterrows():
                price = row['typical_price']
                volume = row['volume']
                idx = np.argmin(np.abs(price_levels - price))
                volume_by_level[idx] += volume

            # POC is price level with maximum volume
            poc_idx = np.argmax(volume_by_level)
            poc_price = price_levels[poc_idx]

            # Volume imbalance (delta): measure buying vs selling pressure
            group['is_up'] = group['close'] >= group['open']
            up_volume = group[group['is_up']]['volume'].sum()
            down_volume = group[~group['is_up']]['volume'].sum()
            total_volume = up_volume + down_volume

            volume_imbalance = (up_volume - down_volume) / total_volume if total_volume > 0 else 0

            # Volume confirmation: high volume on moves in direction of trend
            recent_volume = group.tail(20)['volume'].mean()
            avg_volume = group['volume'].mean()
            volume_confirmation = recent_volume > avg_volume * 1.5  # 50% above average

            results['poc'][symbol] = float(poc_price)
            results['volume_imbalance'][symbol] = float(volume_imbalance)
            results['confirmation'][symbol] = {
                'confirmed': bool(volume_confirmation),
                'recent_avg_volume': float(recent_volume),
                'overall_avg_volume': float(avg_volume),
                'volume_ratio': float(recent_volume / avg_volume) if avg_volume > 0 else 0,
                'up_volume': float(up_volume),
                'down_volume': float(down_volume)
            }

        return results

    def detect_absorption_vs_initiation(self, df: pd.DataFrame) -> Dict[str, str]:
        """
        Detect absorption vs initiation patterns:
        - Absorption: high volume but price not moving much (limit orders absorbing)
        - Initiation: high volume with strong price movement (aggressive market orders)

        Returns:
            Dict mapping symbol -> 'absorption' or 'initiation'
        """
        patterns = {}

        for symbol, group in df.groupby(level='symbol'):
            group = group.copy()

            # Calculate price change vs volume
            group['price_change_pct'] = group['close'].pct_change() * 100
            group['volume_surge'] = group['volume'] > group['volume'].rolling(20, min_periods=1).mean() * 1.5

            # Recent data
            recent = group.tail(5)

            # Absorption: high volume but little price movement
            absorption_candidate = (
                recent['volume_surge'].any() and
                abs(recent['price_change_pct'].mean()) < 0.5  # less than 0.5% move
            )

            # Initiation: high volume with strong price movement
            initiation_candidate = (
                recent['volume_surge'].any() and
                abs(recent['price_change_pct'].mean()) > 1.0  # more than 1% move
            )

            if absorption_candidate:
                patterns[symbol] = 'absorption'
            elif initiation_candidate:
                patterns[symbol] = 'initiation'
            else:
                patterns[symbol] = 'normal'

        return patterns


def detect_volume_confirmation(df: pd.DataFrame) -> bool:
    """Detect if recent price move is confirmed by volume."""
    analyzer = VolumeAnalyzer()
    profile = analyzer.analyze_volume_profile(df)

    for symbol, conf in profile['confirmation'].items():
        if conf['confirmed']:
            return True
    return False