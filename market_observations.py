"""Immutable raw hourly observations and a fail-closed projection for consumers."""
import hashlib
import json
import math
import numbers

from paper_store import now


def install_observation_contract(c):
    """Run within the schema migration transaction, before a new writer can ingest."""
    c.execute("""CREATE TABLE IF NOT EXISTS bar_quarantine (
        symbol TEXT NOT NULL, t TEXT NOT NULL, reason TEXT NOT NULL, detected_at TEXT NOT NULL,
        PRIMARY KEY(symbol,t,reason), FOREIGN KEY(symbol,t) REFERENCES bars(symbol,t))""")
    c.execute("""CREATE TABLE IF NOT EXISTS bar_versions (
        symbol TEXT NOT NULL, t TEXT NOT NULL, content_hash TEXT NOT NULL,
        o REAL NOT NULL,h REAL NOT NULL,l REAL NOT NULL,c REAL NOT NULL,v REAL NOT NULL,
        feed TEXT NOT NULL,interval_kind TEXT NOT NULL,interval_start TEXT,interval_end TEXT,
        received_at TEXT NOT NULL, original INTEGER NOT NULL CHECK(original IN (0,1)),
        PRIMARY KEY(symbol,t,content_hash), FOREIGN KEY(symbol,t) REFERENCES bars(symbol,t))""")
    c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_original_observation
                 ON bar_versions(symbol,t) WHERE original=1""")
    c.execute("""CREATE TABLE IF NOT EXISTS decision_snapshots (
        symbol TEXT NOT NULL,t TEXT NOT NULL,strategy TEXT NOT NULL,lot TEXT NOT NULL,
        regime TEXT NOT NULL,feature_version TEXT NOT NULL,readiness_mask INTEGER NOT NULL,
        history_ready INTEGER NOT NULL,flags TEXT,rsi REAL,received_at TEXT NOT NULL,content_hash TEXT NOT NULL,
        PRIMARY KEY(symbol,t,strategy), UNIQUE(lot,strategy))""")
    c.execute("""CREATE TABLE IF NOT EXISTS development_manifests (
        id TEXT PRIMARY KEY, manifest TEXT NOT NULL, registered_at TEXT NOT NULL)""")
    for table in ('bars', 'bar_versions', 'bar_quarantine', 'decision_snapshots', 'development_manifests'):
        for operation in ('UPDATE', 'DELETE'):
            c.execute(f"""CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation}
                BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable market history'); END""")
    c.execute("""CREATE VIEW IF NOT EXISTS usable_bars AS
        SELECT b.symbol,b.t,b.o,b.h,b.l,b.c,b.v,
               COALESCE(p.interval_kind,'legacy_unknown') AS interval_kind,
               p.interval_start,p.interval_end,p.feed,p.received_at,p.content_hash
        FROM bars b LEFT JOIN bar_versions p ON p.symbol=b.symbol AND p.t=b.t AND p.original=1
        WHERE NOT EXISTS (SELECT 1 FROM bar_quarantine q WHERE q.symbol=b.symbol AND q.t=b.t)""")
    if c.execute("SELECT 1 FROM meta WHERE k='bar_contract_version'").fetchone() is None:
        # Genuine zero- and one-unit legacy volumes cannot be distinguished from damaged ones.
        c.execute("""INSERT INTO bar_quarantine(symbol,t,reason,detected_at)
            SELECT symbol,t,'legacy_boolean_volume_ambiguous',? FROM bars WHERE v IN (0,1)""", (now(),))
        c.execute("INSERT INTO meta(k,v) VALUES('bar_contract_version','1')")


def record_observation(c, symbol, timestamp, row):
    """Store the first observation and any later revision without rewriting either."""
    raw = [row[k] for k in ('open', 'high', 'low', 'close', 'volume')]
    if any(isinstance(v, bool) or not isinstance(v, numbers.Real) or not math.isfinite(v) for v in raw):
        raise ValueError('invalid numeric OHLCV')
    o, h, l, cl, v = values = tuple(map(float, raw))
    if not (0 < l <= min(o, cl) <= max(o, cl) <= h and v >= 0):
        raise ValueError('invalid OHLCV')
    feed = str(row.get('feed', 'unspecified'))
    kind = str(row.get('interval_kind', 'unspecified'))
    start = row.get('interval_start')
    end = row.get('interval_end')
    start = start.isoformat() if start is not None else None
    end = end.isoformat() if end is not None else None
    facts = (*values, feed, kind, start, end)
    digest = hashlib.sha256(json.dumps(facts, allow_nan=False).encode()).hexdigest()
    previous = c.execute("SELECT 1 FROM bars WHERE symbol=? AND t=?", (symbol, timestamp)).fetchone()
    if previous is None:
        c.execute("INSERT INTO bars(symbol,t,o,h,l,c,v) VALUES(?,?,?,?,?,?,?)", (symbol, timestamp, *values))
    elif c.execute("SELECT 1 FROM bar_versions WHERE symbol=? AND t=? AND content_hash=?",
                   (symbol, timestamp, digest)).fetchone() is not None:
        return
    else:
        # A new provider revision or ambiguous legacy history cannot replace the facts used at decision time.
        c.execute("""INSERT INTO bar_quarantine(symbol,t,reason,detected_at)
            VALUES(?,?,'source_revision_requires_review',?) ON CONFLICT DO NOTHING""", (symbol, timestamp, now()))
    c.execute("""INSERT INTO bar_versions(symbol,t,content_hash,o,h,l,c,v,feed,interval_kind,
                    interval_start,interval_end,received_at,original) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (symbol, timestamp, digest, *facts, row.get('received_at') or now(), int(previous is None)))
