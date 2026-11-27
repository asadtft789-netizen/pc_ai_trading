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


def point_in_time_regimes(frame):
    """One causal equity anchor; labels never depend on later bars.

    Choose QQQ, SPY or a deterministic available equity. Crypto is not
    used as the anchor for the equity-only paper execution policy.
    """
    symbols = sorted(str(s) for s in frame.index.get_level_values('symbol').unique() if '/' not in str(s))
    if not symbols:
        return pd.Series('unknown', index=frame.index, dtype=object), {'label': 'unknown', 'indicators': {}}
    anchor = next((s for s in ('QQQ', 'SPY') if s in symbols), symbols[0])
    close = frame.xs(anchor, level='symbol').sort_index()['close'].astype(float)
    if close.index.has_duplicates:
        raise ValueError('duplicate regime anchor bar')
    returns = close.pct_change(fill_method=None)
    valid = returns.notna() & np.isfinite(returns) & np.isfinite(close) & (close > 0)
    ret = returns.where(valid)
    vol = ret.rolling(100, min_periods=50).std()
    mom = (ret > 0).where(ret.notna()).rolling(100, min_periods=50).mean()
    labels = pd.Series('unknown', index=close.index, dtype=object)
    ready = vol.notna() & mom.notna()
    labels.loc[ready] = 'range_quiet'
    labels.loc[ready & (mom > .6)] = 'trend_up'
    labels.loc[ready & (mom < .4)] = 'trend_down'
    labels.loc[ready & (vol > .015)] = 'vol_expansion'
    times = pd.DatetimeIndex(frame.index.get_level_values('timestamp'))
    mapped = labels.reindex(times, method='ffill').fillna('unknown')
    latest = labels.iloc[-1] if len(labels) else 'unknown'
    indicators = {'anchor': anchor}
    if latest != 'unknown':
        indicators.update(volatility=float(vol.iloc[-1]), momentum=float(mom.iloc[-1]))
    return pd.Series(mapped.to_numpy(), index=frame.index, dtype=object), {'label': latest, 'indicators': indicators}
