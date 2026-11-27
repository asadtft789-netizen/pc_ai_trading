"""Shared fail-closed paper entry and learning status checks."""
import json
import sqlite3
from pathlib import Path
from contextlib import closing
from paper_store import db, get


def learning_halted(path):
    sidecar = Path(path).resolve().parent / 'learning_loop.sqlite'
    try:
        with db(path) as operational:
            owner = get(operational, 'learning_owner')
            store = get(operational, 'learning_store')
            consumed = int(get(operational, 'learning_consumed_seq', '0'))
            progress = json.loads(get(operational, 'learning_progress', '{}'))
        if not owner or not store or consumed < 0 or progress.get('state') == 'recovery_required':
            return True
        with closing(sqlite3.connect(sidecar.as_uri() + '?mode=ro', uri=True, timeout=2)) as c:
            c.execute("pragma query_only=on")
            c.execute("begin")
            meta = {k: json.loads(v) for k, v in c.execute(
                "select k,v from ll_meta where k in ('halted','store_id','owner')")}
            seq = c.execute('select coalesce(max(seq),0) from ll_receipts').fetchone()[0]
            return (meta.get('halted') is not False or meta.get('store_id') != store
                    or meta.get('owner') != owner or seq < consumed)
    except (sqlite3.Error, ValueError, TypeError, AttributeError, OSError):
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
