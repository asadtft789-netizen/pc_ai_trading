import json, os, subprocess, sys, tempfile
from pathlib import Path
import numpy as np
import pandas as pd
from strategy_worker import FEATURES

RAW_COLUMNS = ('open', 'high', 'low', 'close', 'volume')
FEATURE_VERSION = 'hourly-boolean-2'
ALL_READY = (1 << len(FEATURES)) - 1
MIN_PRIOR_BARS = 300


def feature_column(name):
    if name not in FEATURES:
        raise ValueError('unknown strategy feature')
    return 'relative_volume_high' if name == 'volume' else name


def strategy_flags(row):
    """Return strategy inputs only when every feature is usable at this close."""
    if row['readiness_mask'] != ALL_READY or not row['history_ready']:
        return None
    return {name: bool(row[feature_column(name)]) for name in FEATURES}


def validate_raw(frame):
    if frame.index.has_duplicates:
        raise ValueError('duplicate raw bar')
    for name in RAW_COLUMNS:
        col = frame[name]
        if (not pd.api.types.is_numeric_dtype(col) or pd.api.types.is_bool_dtype(col)
                or pd.api.types.is_complex_dtype(col) or not np.isfinite(col).all()):
            raise ValueError(f'invalid numeric {name}')
    invalid = ((frame.low <= 0) | (frame.low > frame.open) | (frame.low > frame.close)
               | (frame.high < frame.open) | (frame.high < frame.close) | (frame.volume < 0))
    if invalid.any():
        raise ValueError('invalid OHLCV')


def _contiguous_groups(frame):
    for _, group in frame.sort_index().groupby(level='symbol'):
        if 'interval_kind' not in group:
            yield group
            continue
        kind = group['interval_kind']
        times = pd.DatetimeIndex(group['interval_end'])
        if kind.eq('XNYS_1m').all():
            from market_time import equity_sessions

            local = times.tz_convert('America/New_York')
            sessions = equity_sessions(local[0].date(), (local[-1] + pd.Timedelta(days=1)).date())
            expected = pd.to_datetime([cut for day in sessions for cut in day.cuts], unit='s', utc=True)
            sequence = expected.get_indexer(times)
            if (sequence < 0).any():
                raise ValueError('unscheduled equity interval')
        elif kind.eq('UTC_1h').all():
            sequence = times.as_unit('s').asi8 // 3600
        else:
            raise ValueError('mixed or unknown interval provenance')
        segments = np.r_[False, np.diff(sequence) != 1].cumsum()
        for _, segment in group.groupby(segments):
            yield segment


def enrich(frame):
    validate_raw(frame)
    parts = []
    for g in _contiguous_groups(frame):
        g = g.copy()
        o, h, l, c, v = [g[k].astype(float) for k in ('open', 'high', 'low', 'close', 'volume')]
        prev = c.shift()
        tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
        g['atr'] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        d = c.diff()
        gain = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        loss = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        rsi = 100 * gain / (gain + loss)
        g['rsi'] = rsi
        g['ema20'] = c.ewm(span=20, adjust=False).mean()
        g['ema50'] = c.ewm(span=50, adjust=False).mean()
        up = h.diff()
        down = -l.diff()
        plus = up.where((up > down) & (up > 0), 0.0).ewm(alpha=1 / 14, adjust=False).mean()
        minus = down.where((down > up) & (down > 0), 0.0).ewm(alpha=1 / 14, adjust=False).mean()
        g['adx'] = (100 * (plus - minus).abs() / (plus + minus)).ewm(alpha=1 / 14, adjust=False).mean()
        ma = c.rolling(20).mean()
        sd = c.rolling(20).std()
        lo = l.shift().rolling(30).min()
        hi = h.shift().rolling(30).max()
        z = (c - lo) / (hi - lo)
        body = (c - o).abs()
        g['trend'] = c.ewm(span=20, adjust=False).mean() > c.ewm(span=50, adjust=False).mean()
        g['breakout'] = c > hi
        g['oversold'] = (rsi < 30) | (c < ma - 2 * sd)
        g['hammer'] = (np.minimum(o, c) - l > 2 * body) & (c > o)
        g['pullback'] = g.trend & (l < ma) & (c > ma)
        g['structure'] = h > h.shift().rolling(5).max()
        g['support'] = l < lo + g.atr
        g['resistance'] = h > hi - g.atr
        g['golden'] = z.between(0.382, 0.5)
        volume_reference = v.shift().rolling(20).mean()
        g['relative_volume_high'] = v > volume_reference
        g['quiet'] = g.atr / c < 0.01
        g['momentum'] = c > c.shift(5)
        prior = pd.Series(np.arange(len(g)), index=g.index)
        ready = {
            'trend': prior >= 49, 'breakout': hi.notna(),
            'oversold': rsi.notna() & sd.notna(), 'hammer': prior >= 0,
            'pullback': (prior >= 49) & ma.notna(), 'structure': prior >= 5,
            'support': lo.notna() & g.atr.notna(), 'resistance': hi.notna() & g.atr.notna(),
            'golden': z.notna() & (hi > lo), 'volume': volume_reference.notna(),
            'quiet': g.atr.notna(), 'momentum': prior >= 5,
        }
        g['readiness_mask'] = sum(ready[name].astype(int) * (1 << i) for i, name in enumerate(FEATURES))
        g['history_ready'] = prior >= MIN_PRIOR_BARS
        parts.append(g)
    return pd.concat(parts).sort_index()

def signals(src, frame):
    ready = frame['history_ready'] & frame['readiness_mask'].eq(ALL_READY)
    columns = [feature_column(name) for name in FEATURES]
    rows = frame.loc[ready, columns].astype(bool).values.tolist()
    out = []
    for k in range(0, len(rows) or 1, 12000):
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run([sys.executable, '-I', '-S', str(Path(__file__).with_name('strategy_worker.py').resolve())], cwd=td, env={k: os.environ[k] for k in ('SystemRoot', 'WINDIR') if k in os.environ}, input=json.dumps([src, rows[k:k + 12000]]), text=True, capture_output=True, timeout=8)
        if r.returncode:
            raise ValueError('candidate rejected by strategy worker')
        out.extend(json.loads(r.stdout))
    result = pd.Series(0, index=frame.index, dtype=int)
    if out:
        result.loc[ready] = out
    return result
