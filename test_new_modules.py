"""Simple test to verify new modules work correctly."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'skills', 'tradermonty', 'skills'))

import pandas as pd
import numpy as np
from price_action_swing_trading.price_action import PriceActionDetector
from price_action_swing_trading.market_structure import MarketStructureDetector
from price_action_swing_trading.support_resistance import SupportResistanceDetector, FibonacciRetracement
from price_action_swing_trading.volume_analysis import VolumeAnalyzer

# Create sample data
data = {
    'timestamp': pd.date_range('2023-01-01', periods=10, freq='D'),
    'symbol': ['AAPL'] * 10,
    'open': [150, 152, 151, 153, 152, 154, 153, 155, 154, 156],
    'high': [152, 155, 153, 156, 154, 157, 155, 158, 156, 159],
    'low': [148, 150, 150, 152, 151, 153, 152, 154, 153, 155],
    'close': [151, 151, 152, 153, 152, 154, 153, 155, 154, 156],
    'volume': [1000000] * 10
}

df = pd.DataFrame(data)
df.set_index(['timestamp', 'symbol'], inplace=True)

print("Testing Price Action Detector...")
price_detector = PriceActionDetector()
patterns = price_detector.detect_candlestick_patterns(df)
print(f"Patterns detected: {len(patterns)}")

print("\nTesting Market Structure...")
market_detector = MarketStructureDetector()
structure = market_detector.detect_bos_choch(df)
print(f"BOS/CHOCH detected: {len(structure['bos'])} BOS, {len(structure['choch'])} CHoCH")

print("\nTesting Support/Resistance...")
sr_detector = SupportResistanceDetector()
zones = sr_detector.detect_zones(df)
print(f"Zones found: {len(zones['support']) + len(zones['resistance'])}")

print("\nTesting Fibonacci...")
fib = FibonacciRetracement()
fib_levels = fib.detect_retracement_levels(df)
print(f"Fibonacci levels: {fib_levels['golden_zone']}")

print("\nTesting Volume Analysis...")
volume_analyzer = VolumeAnalyzer()
volume_data = volume_analyzer.analyze_volume_profile(df)
print(f"Volume POC: {volume_data['poc']}")

print("\nAll modules loaded successfully!")