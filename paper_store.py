"""Operational store for one dedicated paper account; no legacy trades are imported."""
import hashlib, json, sqlite3
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')

def stamp(s):
    dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('UTC offset required')
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds')

def amt(x, positive=False):
    v = D(str(x))
    if not v.is_finite() or v < 0 or (positive and (not v)):
        raise ValueError('invalid amount')
    return v

def key(*parts):
    return 'ev-' + hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:40]

@contextmanager
def db(path, write=False):
    path = Path(path).resolve()
    uri = path.as_uri() + ('?mode=rw' if write else '?mode=ro')
    with closing(sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)) as c:
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA trusted_schema=OFF')
        if not write:
            c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        with c:
            yield c
SCHEMA = "\nCREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT NOT NULL);\nCREATE TABLE bars(symbol TEXT,t TEXT,o REAL,h REAL,l REAL,c REAL,v REAL,PRIMARY KEY(symbol,t));\nCREATE TABLE decisions(symbol TEXT,t TEXT,strategy TEXT,target INTEGER,atr REAL,regime TEXT,PRIMARY KEY(symbol,t));\nCREATE TABLE watermarks(symbol TEXT PRIMARY KEY,t TEXT NOT NULL);\nCREATE TABLE strategies(id TEXT PRIMARY KEY,name TEXT,source TEXT,rule_ids TEXT,parent_id TEXT,generation INTEGER,status TEXT,regime TEXT,born TEXT);\nCREATE UNIQUE INDEX one_champ_per_regime ON strategies(status,regime) WHERE status='champion' AND regime IS NOT NULL;\nCREATE UNIQUE INDEX one_champ_global ON strategies(status) WHERE status='champion' AND regime IS NULL;\nCREATE UNIQUE INDEX one_child_per_regime ON strategies(status,regime) WHERE status='challenger' AND regime IS NOT NULL;\nCREATE TABLE trials(strategy TEXT,cut TEXT,phase TEXT,metrics TEXT,PRIMARY KEY(strategy,cut,phase));\nCREATE TABLE rules(id TEXT PRIMARY KEY,text TEXT,feature TEXT,state TEXT,created_at TEXT,parent TEXT);\nCREATE TABLE lessons(id TEXT PRIMARY KEY,lot TEXT,rule_id TEXT,lesson TEXT,created_at TEXT);\nCREATE TABLE reflections(id TEXT PRIMARY KEY,subject TEXT,message TEXT,at TEXT);\nCREATE TABLE regimes(t TEXT PRIMARY KEY,label TEXT,indicators TEXT);\nCREATE TABLE intents(id TEXT PRIMARY KEY,lot TEXT,symbol TEXT,strategy TEXT,side TEXT,qty TEXT,stop_gap TEXT,state TEXT,broker TEXT UNIQUE,created TEXT);\nCREATE TABLE fills(id TEXT PRIMARY KEY,intent TEXT NOT NULL REFERENCES intents(id),qty TEXT,price TEXT,at TEXT);\nCREATE TABLE outcomes(id TEXT PRIMARY KEY,lot TEXT,strategy TEXT,symbol TEXT,qty TEXT,entry REAL,exit REAL,gross REAL,at TEXT);\nCREATE TABLE lots(id TEXT PRIMARY KEY,symbol TEXT,strategy TEXT,qty TEXT,basis TEXT,opened TEXT,gap TEXT);\n"

def init(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10, isolation_level=None)) as c:
        c.execute('BEGIN IMMEDIATE')
        with c:
            tables = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
            if tables:
                if 'meta' not in tables or c.execute("select v from meta where k='schema'").fetchone() != ('hourly-paper-1',):
                    raise ValueError('use a separate new operational database')
                cols = {r[1] for r in c.execute("PRAGMA table_info(strategies)")}
                if 'regime' not in cols:
                    c.execute("ALTER TABLE strategies ADD COLUMN regime TEXT")
                    c.execute("DROP INDEX IF EXISTS one_champ")
                    c.execute("DROP INDEX IF EXISTS one_child")
                    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_champ_per_regime ON strategies(status,regime) WHERE status='champion' AND regime IS NOT NULL")
                    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_champ_global ON strategies(status) WHERE status='champion' AND regime IS NULL")
                    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_child_per_regime ON strategies(status,regime) WHERE status='challenger' AND regime IS NOT NULL")
            else:
                for sql in SCHEMA.split(';'):
                    if sql.strip():
                        c.execute(sql)
                c.execute("insert into meta values ('schema','hourly-paper-1')")
                c.execute("insert into meta values ('since',?)", (now(),))
    return path

def put(c, k, v):
    c.execute('insert into meta values(?,?) on conflict(k) do update set v=excluded.v', (k, str(v)))

def get(c, k, fallback=None):
    row = c.execute('select v from meta where k=?', (k,)).fetchone()
    return row[0] if row else fallback

def rebuild(c):
    """Replay by broker execution time, not delivery order. Book gross only, never invent a fee."""
    c.execute('delete from outcomes')
    c.execute('delete from lots')
    lots = {}
    for r in c.execute("select f.*,i.side,i.lot,i.symbol,i.strategy,i.stop_gap from fills f join intents i on i.id=f.intent order by at,case side when 'buy' then 0 else 1 end,f.id"):
        q, p = (amt(r['qty'], True), amt(r['price'], True))
        lot = r['lot']
        held, basis, at = lots.get(lot, (D(0), D(0), r['at']))
        if r['side'] == 'buy':
            held += q
            basis += p * q
        else:
            if q > held:
                raise ValueError('exit has no matched entry')
            cost = basis if q == held else q * basis / held
            c.execute('insert into outcomes values(?,?,?,?,?,?,?,?,?)', (r['id'], lot, r['strategy'], r['symbol'], str(q), float(cost / q), float(p), float(p * q - cost), r['at']))
            held -= q
            basis -= cost
        lots[lot] = (held, basis, at)
        c.execute('insert or replace into lots values(?,?,?,?,?,?,?)', (lot, r['symbol'], r['strategy'], str(held), str(basis), at, r['stop_gap']))
