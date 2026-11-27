"""Registered paired comparison statistic. Results cannot activate a strategy."""
from datetime import date
from math import isfinite

import numpy as np

RESAMPLES = 100_000
BLOCK_LENGTHS = (3, 5, 10)
CAMPAIGN_ALPHA = 0.05 / 12


def paired_result(calendar, candidate, control, turnover, capital, episodes, differing_episodes, seed):
    """Each mapping must have one value for every registered date, including cash days."""
    days = tuple(calendar)
    if len(days) != 30 or tuple(sorted(set(days))) != days or any(type(d) is not date for d in days):
        raise ValueError("expected a registered chronological 30-session calendar")
    if not isfinite(capital) or capital <= 0 or type(seed) is not int or seed < 0:
        raise ValueError("invalid campaign capital or seed")
    if any(set(series) != set(days) for series in (candidate, control, turnover)):
        return {"status": "inconclusive", "reason": "incomplete_calendar"}
    if episodes < 20 or differing_episodes < 10:
        return {"status": "inconclusive", "reason": "sparse_comparison"}
    pnl, benchmark, traded = (np.array([series[d] for d in days], dtype=float)
                               for series in (candidate, control, turnover))
    if not np.isfinite((pnl, benchmark, traded)).all() or (traded < 0).any():
        return {"status": "invalid", "reason": "invalid_daily_value"}
    delta = (pnl - benchmark - .0002 * traded) / capital
    observed = float(delta.mean())
    centered = delta - observed
    if not np.isfinite(centered).all() or np.ptp(delta) <= 1e-14:
        return {"status": "inconclusive", "reason": "degenerate_series"}
    block_p = {}
    rng = np.random.default_rng(seed)
    for length in BLOCK_LENGTHS:
        exceeds = 0
        for offset in range(0, RESAMPLES, 512):
            n = min(512, RESAMPLES - offset)
            starts = rng.integers(0, len(days), size=(n, (len(days) + length - 1) // length))
            indices = (starts[:, :, None] + np.arange(length)) % len(days)
            means = centered[indices.reshape(n, -1)[:, :len(days)]].mean(axis=1)
            exceeds += int(np.count_nonzero(means >= observed))
        block_p[length] = (1 + exceeds) / (RESAMPLES + 1)
    return {"status": "measured", "seed": seed, "resamples": RESAMPLES, "mean_increment": observed,
            "block_p": block_p, "p": max(block_p.values())}


def holm(registered, results):
    """Allocate multiplicity to the whole family, even for missing comparisons."""
    family = tuple(registered)
    if not 1 <= len(family) <= 12 or len(set(family)) != len(family) or set(results) - set(family):
        raise ValueError("invalid registered comparison family")
    ordered = []
    for name in family:
        result = results.get(name, {})
        p = result["p"] if result.get("status") == "measured" else 1.0
        if not isfinite(p) or not 0 <= p <= 1:
            raise ValueError("invalid comparison p-value")
        ordered.append((p,name))
    maximum = 0
    adjusted = {}
    for rank, (p,name) in enumerate(sorted(ordered)):
        maximum = max(maximum, min(1.0, (len(family)-rank) * p))
        adjusted[name] = maximum
    return adjusted
