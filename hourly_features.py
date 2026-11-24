import json, os, subprocess, sys, tempfile
from pathlib import Path
import numpy as np
import pandas as pd
from strategy_worker import FEATURES

def enrich(frame):
    parts = []
    for _, g in frame.sort_index().groupby(level='symbol'):
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
        g['volume'] = v > v.shift().rolling(20).mean()
        g['quiet'] = g.atr / c < 0.01
        g['momentum'] = c > c.shift(5)
        parts.append(g)
    return pd.concat(parts).sort_index()

def signals(src, frame):
    rows = frame[list(FEATURES)].fillna(False).astype(bool).values.tolist()
    out = []
    for k in range(0, len(rows) or 1, 12000):
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run([sys.executable, '-I', '-S', str(Path(__file__).with_name('strategy_worker.py').resolve())], cwd=td, env={k: os.environ[k] for k in ('SystemRoot', 'WINDIR') if k in os.environ}, input=json.dumps([src, rows[k:k + 12000]]), text=True, capture_output=True, timeout=8)
        if r.returncode:
            raise ValueError('candidate rejected by strategy worker')
        out.extend(json.loads(r.stdout))
    return pd.Series(out, index=frame.index, dtype=int)
