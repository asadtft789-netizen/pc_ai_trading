"""Regime-consumer checkpoints, committed alongside the operational review."""
import json

from paper_store import get, put


def _prefix(regime_label):
    return "review_checkpoint/" + json.dumps(regime_label, ensure_ascii=True) + "/"


def review_marks(c, regime_label):
    prefix = _prefix(regime_label)
    return {k[len(prefix):]: t for k, t in c.execute(
        "SELECT k, v FROM meta WHERE substr(k, 1, ?) = ?", (len(prefix), prefix)
    )}


def advance_review(c, regime_label, symbol, ts):
    k = _prefix(regime_label) + symbol
    previous = get(c, k)
    if previous is not None and ts < previous:
        raise ValueError("review checkpoint regression")
    put(c, k, ts)
