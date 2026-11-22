"""Agent State Memory — structured, replayable memory for autonomous agents.

Provides a typed AgentState object (task, evidence, tool_trace, decisions,
quality_gates) that can be checkpointed to disk and reloaded, plus a
persistent EvidenceStore backed by SQLite so the agent can resume, audit,
and compare its own runs. This is the missing "self-reflection" layer that
turns rule-accumulation into genuine learning.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class AgentState:
    """Typed agent state that can be serialized, replayed, and inspected."""
    task: str
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    tool_trace: List[Dict[str, Any]] = field(default_factory=list)
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    quality_gates: Dict[str, bool] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def checkpoint(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = time.time()
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "AgentState":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**data)


class EvidenceStore:
    """SQLite-backed store for agent evidence (rules, reflections, outcomes)."""

    def __init__(self, db_path: str = "data/agent_state.sqlite"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS agent_states (
                    id TEXT PRIMARY KEY,
                    task TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    state_id TEXT,
                    evidence_type TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (state_id) REFERENCES agent_states(id)
                );
                CREATE TABLE IF NOT EXISTS tool_traces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    state_id TEXT,
                    tool TEXT NOT NULL,
                    status TEXT NOT NULL,
                    observation_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (state_id) REFERENCES agent_states(id)
                );
                CREATE TABLE IF NOT EXISTS quality_gates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    state_id TEXT,
                    gate_name TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    details_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (state_id) REFERENCES agent_states(id)
                );
            """)

    def save_state(self, state: AgentState) -> str:
        with self._conn() as conn:
            state.id = str(uuid.uuid4()) if not getattr(state, "id", None) else state.id
            conn.execute(
                "INSERT OR REPLACE INTO agent_states (id, task, payload_json) VALUES (?, ?, ?)",
                (state.id, state.task, json.dumps(asdict(state))),
            )
        return state.id

    def add_evidence(self, state_id: str, evidence_type: str, content: Dict[str, Any]) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO evidence (state_id, evidence_type, content_json) VALUES (?, ?, ?)",
                (state_id, evidence_type, json.dumps(content)),
            )
            return cur.lastrowid

    def add_tool_trace(self, state_id: str, tool: str, status: str,
                       observation: Optional[Dict[str, Any]] = None) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO tool_traces (state_id, tool, status, observation_json) VALUES (?, ?, ?, ?)",
                (state_id, tool, status, json.dumps(observation or {})),
            )
            return cur.lastrowid

    def add_quality_gate(self, state_id: str, gate_name: str, passed: bool,
                         details: Optional[Dict[str, Any]] = None) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO quality_gates (state_id, gate_name, passed, details_json) VALUES (?, ?, ?, ?)",
                (state_id, gate_name, int(passed), json.dumps(details or {})),
            )
            return cur.lastrowid

    def get_recent_evidence(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM evidence ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_tool_traces(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tool_traces ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def quality_gate_pass_rate(self, gate_name: str, window: int = 10) -> float:
        """Fraction of times a quality gate passed in the last `window` runs."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT passed FROM quality_gates WHERE gate_name = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (gate_name, window),
            ).fetchall()
        if not rows:
            return 0.0
        return sum(r["passed"] for r in rows) / len(rows)
