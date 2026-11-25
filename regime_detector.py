"""Market regime classification using statistical moments of the recent hourly history."""
import numpy as np
import pandas as pd

def classify(df: pd.DataFrame) -> dict:
    """Classifies the last 100 hours into market regimes."""
    h = df.tail(100)
    c = h.close

    # Feature calculation
    trend = c.ewm(span=50).mean().diff().mean()
    vol = c.pct_change().std()
    mom = (c.pct_change() > 0).mean()

    # Regime labeling
    if vol > 0.015:
        label = 'vol_expansion'
    elif mom > 0.6:
        label = 'trend_up'
    elif mom < 0.4:
        label = 'trend_down'
    else:
        label = 'range_quiet'

    return {
        'label': label,
        'indicators': {'trend': float(trend), 'volatility': float(vol), 'momentum': float(mom)}
    }
