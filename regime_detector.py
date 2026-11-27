"""Frozen RegimeSpec v1. All inputs are completed intervals in one market."""
from datetime import timedelta

import numpy as np
import pandas as pd

ANCHORS = {"equity": "SPY", "crypto": "BTC/USD"}


def _anchor_history(frame):
    if frame.index.has_duplicates:
        raise ValueError("duplicate regime anchor interval")
    close, high, low = (pd.to_numeric(frame[k], errors="coerce") for k in ("close","high","low"))
    valid = np.isfinite(close) & np.isfinite(high) & np.isfinite(low) & (low > 0) & (low <= close) & (close <= high)
    # Reset indicators at invalid records. An old EMA cannot certify a new history.
    result = pd.DataFrame(index=frame.index, columns=["regime","ema20","ema50","slope5","natr","p90"])
    result["regime"] = "unknown"
    for _, group in close[valid].groupby((~valid).cumsum()[valid]):
        idx = group.index
        if len(idx) < 301:
            continue
        previous = group.shift()
        tr = pd.concat([high[idx] - low[idx], (high[idx]-previous).abs(), (low[idx]-previous).abs()], axis=1).max(axis=1)
        # Wilder ATR starts with the first fourteen true ranges.
        first_atr = tr.iloc[:14].mean()
        tr = tr.iloc[13:].copy()
        tr.iloc[0] = first_atr
        natr = tr.ewm(alpha=1/14, adjust=False).mean().reindex(idx) / group
        p90 = natr.shift().rolling(252, min_periods=252).quantile(.9)
        fast = group.ewm(span=20, adjust=False, min_periods=20).mean()
        slow = group.ewm(span=50, adjust=False, min_periods=50).mean()
        slope = fast.diff(5)
        raw = np.select([natr > p90, (fast > slow) & (slope > 0), (fast < slow) & (slope < 0)],
                        ["vol_expansion","trend_up","trend_down"], default="range_quiet")
        raw = pd.Series(raw, index=idx)
        raw.iloc[:300] = None
        confirmed = raw.where(raw.eq(raw.shift())).ffill().fillna("unknown")
        result.loc[idx, "regime"] = confirmed
        for field, values in zip(("ema20","ema50","slope5","natr","p90"), (fast,slow,slope,natr,p90)):
            result.loc[idx, field] = values
    return result


def point_in_time_regimes(frame, *, market="equity"):
    if market not in ANCHORS:
        raise ValueError("unknown regime market")
    anchor = ANCHORS[market]
    symbols = frame.index.get_level_values("symbol")
    market_rows = symbols.str.contains("/", regex=False) == (market == "crypto")
    labels = pd.Series("unknown", index=frame.index, dtype=object)
    info = {"anchor": anchor, "spec": "regime-v1"}
    latest = "unknown"
    if anchor in symbols:
        bars = frame.xs(anchor, level="symbol").sort_index()
        if bars.index.has_duplicates or bars.index.tz is None:
            raise ValueError("duplicate or timezone-naive anchor interval")
        if "interval_kind" in bars:
            bars = bars.loc[bars.interval_kind.eq("XNYS_1m" if market == "equity" else "UTC_1h")]
        if bars.empty:
            return labels, {"label": latest, "indicators": info}
        if market == "equity":
            from market_time import equity_sessions
            dates = bars.index.tz_convert("America/New_York")
            sessions = equity_sessions(dates.min().date(), dates.max().date() + timedelta(days=1))
            grid = pd.to_datetime([cut for day in sessions for cut in day.cuts], unit="s", utc=True)
        else:
            grid = pd.date_range(bars.index.min().ceil("h"), bars.index.max().floor("h"), freq="h")
        # Reindex before indicator calculation so a missing expected interval
        # breaks the warm-up and the two-observation confirmation.
        history = _anchor_history(bars.reindex(grid))
        times = frame.index.get_level_values("timestamp")[market_rows]
        labels.loc[market_rows] = history["regime"].reindex(times).fillna("unknown").to_numpy()
        last = times.max()
        if last in history.index:
            row = history.loc[last]
            latest = row["regime"]
            if latest != "unknown":
                info.update({k: float(row[k]) for k in ("ema20","ema50","slope5","natr","p90")})
    return labels, {"label": latest, "indicators": info}


def classify(df):
    return point_in_time_regimes(df)[1]
