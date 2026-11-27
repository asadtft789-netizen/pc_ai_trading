"""Shared fail-closed paper entry and learning status checks."""
import json
import sqlite3
from pathlib import Path
from contextlib import closing
from paper_store import get


def learning_halted(path):
    sidecar = Path(path).resolve().parent / 'learning_loop.sqlite'
    if not sidecar.exists():
        return False
    try:
        with closing(sqlite3.connect(sidecar.as_uri() + '?mode=ro', uri=True, timeout=2)) as c:
            row = c.execute("select v from ll_meta where k='halted'").fetchone()
            return bool(json.loads(row[0])) if row else False
    except (sqlite3.Error, ValueError, OSError):
        return True


def entry_allowed(c, symbol, decision, strategy):
    if get(c, 'integrity_entries_paused', 'YES') != 'NO':
        return False
    d = c.execute('select d.* from decisions d join watermarks w on w.symbol=d.symbol and w.t=d.t where d.symbol=? and d.t=?', (symbol, decision)).fetchone()
    if not d or d['target'] != 1 or d['strategy'] != strategy:
        return False
    champ = c.execute("select regime from strategies where id=? and status='champion'", (strategy,)).fetchone()
    if champ is None or champ['regime'] not in (None, d['regime']):
        return False
    gate = 'entry_gate_' + d['regime'] if d['regime'] else 'entry_gate'
    return get(c, gate) == strategy
