"""Drift Detection — detects when a paper-trading rule has gone stale.

Tracks per-rule PnL from actual trade outcomes and flags a rule as "gone wrong"
when its recent trades underperform significantly compared to its historical
baseline. Rules are candidates for targeted correction only when drift is
confirmed — never the whole strategy based on one bad rule.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class RuleDrift:
    rule_text: str
    rule_id: int
    recent_trades: int
    recent_win_rate: float
    recent_avg_pnl: float
    baseline_win_rate: float
    baseline_avg_pnl: float
    drift_score: float  # positive = rule performing worse than baseline
    is_drifting: bool
    recommendation: str


class DriftDetector:
    """Compares each rule's recent paper-trading outcomes to its own baseline.

    A rule "goes wrong" when its recent win rate and average PnL fall below
    historical norms by a meaningful margin. Only then is it offered to the
    rule-correction loop as a single-rule fix candidate.
    """

    def __init__(self, memory, db_path: str = "data/drift.sqlite"):
        self.memory = memory
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS rule_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id INTEGER,
                    rule_text TEXT NOT NULL,
                    trade_id TEXT NOT NULL,
                    pnl REAL NOT NULL,
                    win INTEGER NOT NULL,
                    trade_date TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_rule_outcomes_rule_date
                    ON rule_outcomes(rule_id, trade_date);
            """)

    def record_outcome(self, rule_id: int, rule_text: str, trade_id: str,
                       pnl: float, trade_date: datetime) -> None:
        """Record one trade outcome against a rule for drift tracking."""
        win = int(pnl > 0)
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO rule_outcomes
                   (rule_id, rule_text, trade_id, pnl, win, trade_date)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (rule_id, rule_text, trade_id, pnl, win,
                 trade_date.isoformat()),
            )

    def _baseline(self, rule_id: int) -> tuple[float, float]:
        """Return (win_rate, avg_pnl) across ALL history for this rule."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT win, pnl FROM rule_outcomes WHERE rule_id = ?",
                (rule_id,),
            ).fetchall()
        if not rows:
            return 0.0, 0.0
        wins = [r for r in rows if r["win"] == 1]
        pnls = [r["pnl"] for r in rows]
        return (len(wins) / len(rows), sum(pnls) / len(pnls))

    def _recent(self, rule_id: int, window_days: int = 14) -> tuple[int, float, float]:
        """Return (trade_count, win_rate, avg_pnl) for the last window_days."""
        cutoff = (datetime.now() - timedelta(days=window_days)).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT win, pnl FROM rule_outcomes WHERE rule_id = ? AND trade_date >= ?",
                (rule_id, cutoff),
            ).fetchall()
        if not rows:
            return 0, 0.0, 0.0
        wins = [r for r in rows if r["win"] == 1]
        pnls = [r["pnl"] for r in rows]
        return len(rows), len(wins) / len(rows), sum(pnls) / len(pnls)

    def evaluate_rule(self, rule_id: int, rule_text: str,
                      window_days: int = 14,
                      drift_threshold: float = 0.20) -> Optional[RuleDrift]:
        """Evaluate whether a single rule is drifting.

        drift_score = baseline_win_rate - recent_win_rate + (baseline_avg_pnl - recent_avg_pnl scaled)
        Positive drift_score means the rule is performing worse than baseline.
        Returns None if there isn't enough data to evaluate.
        """
        b_win, b_avg = self._baseline(rule_id)
        n_recent, r_win, r_avg = self._recent(rule_id, window_days)

        if n_recent < 3:
            return None  # not enough data to call drift

        # Drift: how much worse recent performance is vs baseline
        win_drop = max(0.0, b_win - r_win)
        pnl_drop = max(0.0, b_avg - r_avg)
        # Normalize pnl_drop to a comparable magnitude for scoring
        scale = abs(b_avg) if b_avg != 0 else 1.0
        drift_score = win_drop + (pnl_drop / scale if scale else 0.0)
        is_drifting = drift_score >= drift_threshold

        if is_drifting:
            recommendation = (
                f"Rule '{rule_text}' recent win_rate={r_win:.2%} vs baseline={b_win:.2%}, "
                f"recent avg_pnl=${r_avg:.2f} vs baseline=${b_avg:.2f}. "
                "Candidate for targeted single-rule correction."
            )
        else:
            recommendation = (
                f"Rule '{rule_text}' performing within norms "
                f"(win_rate={r_win:.2%}, avg_pnl=${r_avg:.2f}). No correction needed."
            )

        return RuleDrift(
            rule_text=rule_text,
            rule_id=rule_id,
            recent_trades=n_recent,
            recent_win_rate=r_win,
            recent_avg_pnl=r_avg,
            baseline_win_rate=b_win,
            baseline_avg_pnl=b_avg,
            drift_score=drift_score,
            is_drifting=is_drifting,
            recommendation=recommendation,
        )

    def evaluate_all_rules(self, window_days: int = 14,
                           drift_threshold: float = 0.20) -> List[RuleDrift]:
        """Evaluate every stored rule and return those that are drifting."""
        rules = self.memory.get_all_rules()
        drifting: List[RuleDrift] = []
        for rule in rules:
            result = self.evaluate_rule(
                rule["id"], rule["rule_text"],
                window_days=window_days,
                drift_threshold=drift_threshold,
            )
            if result and result.is_drifting:
                drifting.append(result)
        drifting.sort(key=lambda d: d.drift_score, reverse=True)
        return drifting
