"""Persistent memory layer for the self-evolving trader.

Uses SQLite to store strategy code, backtest results, trade history,
market regimes, and LLM-generated reflections for the post-mortem loop.
"""

import os
import sqlite3
import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from dataclasses import asdict

from config import Settings


class MemoryManager:
    """Manages the persistent SQLite database for the trader."""

    def __init__(self, settings: Settings):
        self.db_path = settings.runtime.db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Creates tables if they do not exist."""
        with self._get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS strategies (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    code TEXT NOT NULL,
                    parent_id TEXT,
                    generation INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'draft',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (parent_id) REFERENCES strategies (id)
                );

                CREATE TABLE IF NOT EXISTS regimes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    label TEXT NOT NULL,
                    indicators_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS backtests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    regime_id INTEGER,
                    metrics_json TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    start_date TIMESTAMP NOT NULL,
                    end_date TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (strategy_id) REFERENCES strategies (id),
                    FOREIGN KEY (regime_id) REFERENCES regimes (id)
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id TEXT PRIMARY KEY, -- Alpaca Order ID
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    qty REAL NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL,
                    exit_price REAL,
                    pnl REAL,
                    status TEXT NOT NULL,
                    regime_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    filled_at TIMESTAMP,
                    FOREIGN KEY (strategy_id) REFERENCES strategies (id),
                    FOREIGN KEY (regime_id) REFERENCES regimes (id)
                );

                CREATE TABLE IF NOT EXISTS reflections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_type TEXT NOT NULL, -- 'trade', 'strategy', 'regime'
                    target_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    action_items TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS strategy_failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    last_failure TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reset_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_text TEXT NOT NULL,
                    source_reflection_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (source_reflection_id) REFERENCES reflections (id)
                );
            """)

            # --- Migrations for databases created before direction_right existed ---
            cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
            if "direction_right" not in cols:
                conn.execute("ALTER TABLE trades ADD COLUMN direction_right INTEGER")

    def save_strategy(self, strategy_id: str, name: str, code: str, parent_id: Optional[str] = None, generation: int = 0):
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO strategies (id, name, code, parent_id, generation) VALUES (?, ?, ?, ?, ?)",
                (strategy_id, name, code, parent_id, generation)
            )

    def update_strategy_status(self, strategy_id: str, status: str):
        with self._get_connection() as conn:
            conn.execute("UPDATE strategies SET status = ? WHERE id = ?", (status, strategy_id))

    def get_active_strategies(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM strategies WHERE status = 'active'").fetchall()
            return [dict(row) for row in rows]

    def save_backtest(self, strategy_id: str, metrics: Dict[str, Any], params: Dict[str, Any],
                      start_date: datetime, end_date: datetime, regime_id: Optional[int] = None):
        with self._get_connection() as conn:
            conn.execute(
                """INSERT INTO backtests
                   (strategy_id, regime_id, metrics_json, params_json, start_date, end_date)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (strategy_id, regime_id, json.dumps(metrics), json.dumps(params),
                 start_date.isoformat(), end_date.isoformat())
            )

    def log_trade(self, order_id: str, strategy_id: str, symbol: str, qty: float, side: str, status: str, regime_id: Optional[int] = None, direction_right: Optional[int] = None):
        with self._get_connection() as conn:
            conn.execute(
                """INSERT INTO trades (id, strategy_id, symbol, qty, side, status, regime_id, direction_right)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, strategy_id, symbol, qty, side, status, regime_id, direction_right)
            )

    def update_trade(self, order_id: str, exit_price: Optional[float] = None, pnl: Optional[float] = None, status: Optional[str] = None, direction_right: Optional[int] = None):
        with self._get_connection() as conn:
            if status:
                conn.execute("UPDATE trades SET status = ? WHERE id = ?", (status, order_id))
            if exit_price is not None:
                conn.execute("UPDATE trades SET exit_price = ? WHERE id = ?", (exit_price, order_id))
            if pnl is not None:
                conn.execute("UPDATE trades SET pnl = ? WHERE id = ?", (pnl, order_id))
            if direction_right is not None:
                conn.execute("UPDATE trades SET direction_right = ? WHERE id = ?", (direction_right, order_id))

    def save_reflection(self, target_type: str, target_id: str, content: str, action_items: Optional[str] = None):
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO reflections (target_type, target_id, content, action_items) VALUES (?, ?, ?, ?)",
                (target_type, target_id, content, action_items)
            )

    def get_latest_regime(self) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM regimes ORDER BY timestamp DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def save_regime(self, label: str, indicators: Dict[str, Any]) -> int:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO regimes (label, indicators_json) VALUES (?, ?)",
                (label, json.dumps(indicators))
            )
            return cursor.lastrowid

    def save_rule(self, rule_text: str, source_reflection_id: Optional[int] = None) -> int:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO rules (rule_text, source_reflection_id) VALUES (?, ?)",
                (rule_text, source_reflection_id)
            )
            return cursor.lastrowid

    def get_all_rules(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM rules ORDER BY created_at DESC").fetchall()
            return [dict(row) for row in rows]

    def close_trade_for_symbol(self, symbol: str, exit_price: float, pnl: float) -> None:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT id FROM trades WHERE symbol = ? AND status = 'open' ORDER BY created_at DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE trades SET exit_price = ?, pnl = ?, status = 'filled' WHERE id = ?",
                    (exit_price, pnl, row['id']),
                )

    def close_trade_by_order_id(self, order_id: str, exit_price: float, pnl: float) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE trades SET exit_price = ?, pnl = ?, status = 'filled' WHERE id = ?",
                (exit_price, pnl, order_id),
            )

    def get_open_trades(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status = 'open' ORDER BY created_at ASC"
            ).fetchall()
            return [dict(row) for row in rows]

    def get_daily_pnl(self) -> float:
        """Sum of realized PnL for trades filled today."""
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(pnl), 0) FROM trades "
                    "WHERE status = 'filled' AND pnl IS NOT NULL "
                    "AND date(created_at) = date('now')"
                ).fetchone()
            return float(row[0]) if row else 0.0
        except Exception:
            return 0.0

    def get_trades_for_strategy(self, strategy_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE strategy_id = ? ORDER BY created_at DESC LIMIT ?",
                (strategy_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_recent_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def log_trade_open(self, order_id: str, strategy_id: str, symbol: str, qty: float, side: str, regime_id: Optional[int] = None) -> None:
        self.log_trade(order_id, strategy_id, symbol, qty, side, "open", regime_id)

    def get_active_strategy(self) -> Optional[Dict[str, Any]]:
        strategies = self.get_active_strategies()
        return strategies[0] if strategies else None

    def get_strategy(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
            return dict(row) if row else None

    def should_mutate(self, strategy_id: str) -> bool:
        """Decide whether a strategy should be mutated based on recent performance."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT content FROM reflections WHERE target_type = 'strategy' AND target_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (strategy_id,),
            ).fetchone()
            if row:
                content = row['content'].lower()
                if any(k in content for k in ("decay", "declining", "underperform", "loss", "poor")):
                    return True

            metrics = self.get_trade_metrics()
            if metrics.get("total_trades", 0) >= 5 and metrics.get("win_rate", 1.0) < 0.4:
                return True

        return False

    def get_trade_metrics(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT pnl FROM trades WHERE status = 'filled' AND pnl IS NOT NULL"
            ).fetchall()
        pnls = [r['pnl'] for r in rows]
        if not pnls:
            return {"total_trades": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0}
        wins = [p for p in pnls if p > 0]
        return {
            "total_trades": len(pnls),
            "win_rate": len(wins) / len(pnls),
            "total_pnl": sum(pnls),
            "avg_pnl": sum(pnls) / len(pnls),
        }

    def update_rule_by_text(self, old_text: str, new_text: str) -> bool:
        """Replace an existing rule's text in-place (targeted fix, not append).
        Returns True if a row was updated, False if no matching rule found."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "UPDATE rules SET rule_text = ? WHERE rule_text = ?",
                (new_text, old_text),
            )
            return cursor.rowcount > 0

    def log_reflection(self, target_id: str, title: str, content: str) -> Optional[int]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO reflections (target_type, target_id, content, action_items) VALUES (?, ?, ?, ?)",
                ("strategy", target_id, f"{title}: {content}", None),
            )
            return cursor.lastrowid

    def increment_strategy_failure(self, strategy_id: str) -> None:
        """Increment failure counter for a strategy. If first failure, initialize to 1."""
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO strategy_failures (strategy_id, failure_count, last_failure, reset_at)
                VALUES (?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(strategy_id) DO UPDATE SET
                    failure_count = failure_count + 1,
                    last_failure = CURRENT_TIMESTAMP
            """, (strategy_id,))

    def get_strategy_failure_count(self, strategy_id: str) -> int:
        """Get the current failure count for a strategy (0 if not found)."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT failure_count FROM strategy_failures WHERE strategy_id = ?",
                (strategy_id,),
            ).fetchone()
            return row[0] if row else 0

    def reset_strategy_failure(self, strategy_id: str) -> None:
        """Reset failure counter for a strategy after successful test pass."""
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE strategy_failures
                SET failure_count = 0, reset_at = CURRENT_TIMESTAMP
                WHERE strategy_id = ?
            """, (strategy_id,))
