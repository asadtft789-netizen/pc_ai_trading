"""Premium web dashboard for the self-evolving trading AI.

Reads from the same SQLite database the daemon writes to (data/memory.sqlite,
data/drift.sqlite). Fully decoupled — the dashboard never executes trades,
it only displays what the daemon already did.

Run:  python dashboard/app.py
Then open: http://127.0.0.1:8000
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# Ensure the project root (parent of dashboard/) is importable so `config`
# resolves when running `python dashboard/app.py` from any CWD.
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.requests import Request
from jinja2 import Environment, FileSystemLoader, select_autoescape

from config import Settings


# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
MEMORY_DB = os.path.join(DATA_DIR, "memory.sqlite")
DRIFT_DB = os.path.join(DATA_DIR, "drift.sqlite")

settings = Settings.load(env_path=os.path.join(BASE_DIR, ".env"))

app = FastAPI(title="Self-Evolving Trading AI — Dashboard")

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "dashboard", "static")), name="static")

templates_dir = os.path.join(BASE_DIR, "dashboard", "templates")
jinja_env = Environment(
    loader=FileSystemLoader(templates_dir),
    autoescape=select_autoescape(["html", "xml"]),
)


# ---------------------------------------------------------------------------
# SQLite helpers (read-only)
# ---------------------------------------------------------------------------
def _dict_row(row) -> Optional[Dict[str, Any]]:
    return dict(row) if row else None


def _query_memory(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(MEMORY_DB)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _query_memory_one(sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(MEMORY_DB)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(sql, params).fetchone()
        return _dict_row(row)
    finally:
        conn.close()


def _query_drift(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(DRIFT_DB)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse a stored timestamp (ISO string or datetime) into a datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _pnl_color(pnl: float) -> str:
    return "#22c55e" if pnl > 0 else ("#ef4444" if pnl < 0 else "#94a3b8")


def _mode_label() -> str:
    base = settings.alpaca.base_url or ""
    if "paper" in base:
        return "PAPER"
    return "REAL"


def _mode_color() -> str:
    return "#22c55e" if _mode_label() == "PAPER" else "#ef4444"


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    template = jinja_env.get_template("index.html")
    return HTMLResponse(template.render(request=request))


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
@app.get("/api/metrics")
def api_metrics():
    """Aggregate per-hour results: total trades, win/loss, win rate, PnL, etc."""
    rows = _query_memory(
        "SELECT pnl, status, direction_right, created_at FROM trades WHERE pnl IS NOT NULL"
    )
    filled = [r for r in rows if r["status"] == "filled"]
    total = len(filled)
    wins = [r for r in filled if r["pnl"] and r["pnl"] > 0]
    losses = [r for r in filled if r["pnl"] and r["pnl"] < 0]
    win_rate = (len(wins) / total * 100) if total else 0.0
    total_pnl = sum(r["pnl"] or 0 for r in filled)
    avg_pnl = (total_pnl / total) if total else 0.0

    # Daily PnL
    daily_rows = _query_memory(
        "SELECT pnl FROM trades WHERE status = 'filled' AND pnl IS NOT NULL "
        "AND date(created_at) = date('now')"
    )
    daily_pnl = sum(r["pnl"] or 0 for r in daily_rows)

    # Correct vs wrong observations (direction_right)
    correct = [r for r in filled if r.get("direction_right") == 1]
    wrong = [r for r in filled if r.get("direction_right") == 0]

    return JSONResponse({
        "total_trades": total,
        "win_trades": len(wins),
        "loss_trades": len(losses),
        "win_rate": round(win_rate, 2),
        "total_pnl": round(total_pnl, 4),
        "avg_pnl": round(avg_pnl, 4),
        "daily_pnl": round(daily_pnl, 4),
        "correct_observations": len(correct),
        "wrong_observations": len(wrong),
        "mode": _mode_label(),
        "mode_color": _mode_color(),
    })


@app.get("/api/regime")
def api_regime():
    """Current market regime + indicators."""
    row = _query_memory_one(
        "SELECT label, indicators_json, timestamp FROM regimes ORDER BY timestamp DESC LIMIT 1"
    )
    if not row:
        return JSONResponse({"label": "unknown", "indicators": {}, "timestamp": None})
    indicators = {}
    try:
        indicators = json.loads(row["indicators_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        indicators = {}
    return JSONResponse({
        "label": row["label"],
        "indicators": indicators,
        "timestamp": row["timestamp"],
    })


@app.get("/api/strategy")
def api_strategy():
    """Active strategy details."""
    row = _query_memory_one(
        "SELECT id, name, generation, parent_id, status, created_at FROM strategies WHERE status = 'active' ORDER BY created_at DESC LIMIT 1"
    )
    if not row:
        return JSONResponse({"id": None, "name": None, "generation": 0, "parent_id": None})
    return JSONResponse(dict(row))


@app.get("/api/trades")
def api_trades(limit: int = 50):
    """Recent trades with PnL and direction_right (correct/wrong observation)."""
    rows = _query_memory(
        "SELECT id, symbol, side, qty, entry_price, exit_price, pnl, status, "
        "direction_right, regime_id, created_at FROM trades "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    out = []
    for r in rows:
        pnl = r["pnl"] or 0
        out.append({
            "id": r["id"],
            "symbol": r["symbol"],
            "side": r["side"],
            "qty": r["qty"],
            "entry_price": r["entry_price"],
            "exit_price": r["exit_price"],
            "pnl": round(pnl, 4),
            "pnl_color": _pnl_color(pnl),
            "status": r["status"],
            "direction_right": r.get("direction_right"),
            "observation": "CORRECT" if r.get("direction_right") == 1 else ("WRONG" if r.get("direction_right") == 0 else "—"),
            "regime_id": r["regime_id"],
            "created_at": r["created_at"],
        })
    return JSONResponse(out)


@app.get("/api/lessons")
def api_lessons(limit: int = 20):
    """Recent lessons / rules learned by the AI."""
    rows = _query_memory(
        "SELECT rule_text, created_at FROM rules ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    return JSONResponse([{"lesson": r["rule_text"], "created_at": r["created_at"]} for r in rows])


@app.get("/api/rules")
def api_rules():
    """All saved rules."""
    rows = _query_memory("SELECT rule_text, created_at FROM rules ORDER BY created_at DESC")
    return JSONResponse([{"rule": r["rule_text"], "created_at": r["created_at"]} for r in rows])


@app.get("/api/equity-curve")
def api_equity_curve():
    """Equity curve data: cumulative PnL over time for the chart."""
    rows = _query_memory(
        "SELECT pnl, created_at FROM trades WHERE status = 'filled' AND pnl IS NOT NULL "
        "ORDER BY created_at ASC"
    )
    cumulative = 0.0
    points = []
    for r in rows:
        cumulative += r["pnl"] or 0
        points.append({"t": r["created_at"], "equity": round(cumulative, 4), "pnl": round(r["pnl"] or 0, 4)})
    return JSONResponse(points)


@app.get("/api/price-chart/{symbol}")
def api_price_chart(symbol: str):
    """OHLC + signal data for a symbol's price chart.

    Reads recent bars from the train/test split data stored in backtests? No —
    the backtests table stores metrics_json, not bars. Instead we synthesize
    chart points from trade entry/exit prices so the user can at least see
    where trades happened. The daemon's test_df holds the real bars but isn't
    persisted; this endpoint returns trade-based markers for now.
    """
    trades = _query_memory(
        "SELECT entry_price, exit_price, side, direction_right, created_at FROM trades "
        "WHERE symbol = ? AND status = 'filled' ORDER BY created_at ASC",
        (symbol,),
    )
    candles = []
    for t in trades:
        entry = t["entry_price"] or 0
        exit_px = t["exit_price"] or 0
        candles.append({
            "t": t["created_at"],
            "o": round(entry, 4),
            "h": round(max(entry, exit_px), 4),
            "l": round(min(entry, exit_px), 4),
            "c": round(exit_px, 4),
            "side": t["side"],
            "direction_right": t.get("direction_right"),
        })
    return JSONResponse(candles)


@app.get("/api/mode")
def api_mode():
    """Current trading mode (paper vs real) from .env ALPACA_BASE_URL."""
    return JSONResponse({
        "mode": _mode_label(),
        "base_url": settings.alpaca.base_url,
        "is_paper": "paper" in (settings.alpaca.base_url or ""),
    })


@app.get("/api/health")
def api_health():
    """Health check — confirms dashboard can read the SQLite DB."""
    try:
        _query_memory_one("SELECT 1")
        return JSONResponse({"status": "ok"})
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
