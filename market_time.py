"""Regular XNYS sessions and completed, session-anchored OHLCV intervals."""
from datetime import date, timedelta
from functools import lru_cache

import exchange_calendars as xcals
import numpy as np
import pandas as pd

from learning_loop import Session


@lru_cache(maxsize=64)
def equity_sessions(start: date, end_exclusive: date) -> tuple[Session, ...]:
    if start >= end_exclusive:
        return ()
    calendar = xcals.get_calendar("XNYS")
    dates = calendar.sessions_in_range(start, end_exclusive - timedelta(days=1))
    sessions = []
    for day in dates:
        opening = int(calendar.session_open(day).timestamp())
        close = int(calendar.session_close(day).timestamp())
        cuts = (*range(opening + 3600, close, 3600), close)
        sessions.append(Session(day.date().isoformat(), opening, cuts))
    return tuple(sessions)


def aggregate_equity_minutes(frame, as_of):
    """Aggregate unique one-minute starts, requiring every minute of each interval.

    A missing minute leaves a gap. Provider-native hourly bars cannot establish
    minute completeness. Rows outside regular sessions do not enter the output.
    """
    columns = ["open", "high", "low", "close", "volume"]
    extras = ["interval_start", "interval_end", "interval_kind"]
    empty_index = pd.MultiIndex.from_arrays(
        [[], pd.DatetimeIndex([], tz="UTC")], names=["symbol", "timestamp"]
    )
    empty = pd.DataFrame(columns=columns + extras, index=empty_index)
    as_of = pd.Timestamp(as_of)
    if as_of.tz is None:
        raise ValueError("as_of requires an explicit offset")
    if frame.empty:
        return empty
    minutes = frame.loc[:, columns].reset_index()
    ts = pd.DatetimeIndex(minutes["timestamp"])
    if ts.tz is None or ts.hasnans or (ts != ts.floor("min")).any():
        raise ValueError("expected offset-aware minute starts")
    minutes["timestamp"] = ts.tz_convert("UTC")
    if minutes.duplicated(["symbol", "timestamp"]).any():
        raise ValueError("duplicate provider minute")
    minutes = minutes.loc[minutes.timestamp + pd.Timedelta(minutes=1) <= as_of].copy()
    if minutes.empty:
        return empty
    dates = minutes.timestamp.dt.tz_convert("America/New_York")
    sessions = equity_sessions(dates.min().date(), dates.max().date() + timedelta(days=1))
    starts, ends = [], []
    for session in sessions:
        starts.extend((session.opened, *session.cuts[:-1]))
        ends.extend(session.cuts)
    if not starts:
        return empty
    minute_seconds = minutes.timestamp.array.as_unit("s").asi8
    interval = np.searchsorted(starts, minute_seconds, side="right") - 1
    first = np.array(starts)[interval]
    last = np.array(ends)[interval]
    eligible = (interval >= 0) & (minute_seconds < last) & (last <= as_of.timestamp())
    minutes["interval_start"] = pd.to_datetime(first, unit="s", utc=True)
    minutes["interval_end"] = pd.to_datetime(last, unit="s", utc=True)
    minutes = minutes.loc[eligible].sort_values(["symbol", "timestamp"])
    if minutes.empty:
        return empty
    if any(pd.api.types.is_bool_dtype(minutes[col]) for col in columns):
        raise ValueError("boolean OHLCV in minute feed")
    minutes[columns] = minutes[columns].apply(pd.to_numeric, errors="raise")
    if (not np.isfinite(minutes[columns]).all().all()
            or (minutes.low <= 0).any() or (minutes.volume < 0).any()
            or (minutes.low > minutes[["open", "close"]].min(axis=1)).any()
            or (minutes.high < minutes[["open", "close"]].max(axis=1)).any()):
        raise ValueError("invalid minute OHLCV")
    grouped = minutes.groupby(["symbol", "interval_end"], sort=True)
    result = grouped.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                         close=("close", "last"), volume=("volume", "sum"),
                         interval_start=("interval_start", "first"), count=("timestamp", "size"))
    result["interval_end"] = result.index.get_level_values("interval_end")
    expected = (result.interval_end - result.interval_start) / pd.Timedelta(minutes=1)
    result = result.loc[result["count"] == expected].copy()
    result["interval_kind"] = "XNYS_1m"
    result.index = result.index.set_names(["symbol", "timestamp"])
    return result[columns + extras]
