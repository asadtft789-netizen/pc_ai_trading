"""Operational store for one dedicated paper account; no legacy trades are imported."""
import hashlib, json, sqlite3
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4
from sqlite_snapshot import copy_verified

SCHEMA_VERSION = 2

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

@contextmanager
def retain_wal_files(path):
    """Keep WAL and SHM available to read-only processes between short transactions."""
    uri = Path(path).resolve().as_uri() + "?mode=rw"
    with closing(sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)) as connection:
        connection.execute("SELECT name FROM sqlite_schema LIMIT 1").fetchone()
        yield


SCHEMA = "\nCREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT NOT NULL);\nCREATE TABLE bars(symbol TEXT,t TEXT,o REAL,h REAL,l REAL,c REAL,v REAL,PRIMARY KEY(symbol,t));\nCREATE TABLE decisions(symbol TEXT,t TEXT,strategy TEXT,target INTEGER,atr REAL,regime TEXT,PRIMARY KEY(symbol,t));\nCREATE TABLE watermarks(symbol TEXT PRIMARY KEY,t TEXT NOT NULL);\nCREATE TABLE strategies(id TEXT PRIMARY KEY,name TEXT,source TEXT,rule_ids TEXT,parent_id TEXT,generation INTEGER,status TEXT,regime TEXT,born TEXT);\nCREATE UNIQUE INDEX one_champ_per_regime ON strategies(status,regime) WHERE status='champion' AND regime IS NOT NULL;\nCREATE UNIQUE INDEX one_champ_global ON strategies(status) WHERE status='champion' AND regime IS NULL;\nCREATE UNIQUE INDEX one_child_per_regime ON strategies(status,regime) WHERE status='challenger' AND regime IS NOT NULL;\nCREATE TABLE trials(strategy TEXT,cut TEXT,phase TEXT,metrics TEXT,PRIMARY KEY(strategy,cut,phase));\nCREATE TABLE rules(id TEXT PRIMARY KEY,text TEXT,feature TEXT,state TEXT,created_at TEXT,parent TEXT);\nCREATE TABLE lessons(id TEXT PRIMARY KEY,lot TEXT,rule_id TEXT,lesson TEXT,created_at TEXT);\nCREATE TABLE reflections(id TEXT PRIMARY KEY,subject TEXT,message TEXT,at TEXT);\nCREATE TABLE regimes(t TEXT PRIMARY KEY,label TEXT,indicators TEXT);\nCREATE TABLE intents(id TEXT PRIMARY KEY,lot TEXT,symbol TEXT,strategy TEXT,side TEXT,qty TEXT,stop_gap TEXT,state TEXT,broker TEXT UNIQUE,created TEXT);\nCREATE TABLE fills(id TEXT PRIMARY KEY,intent TEXT NOT NULL REFERENCES intents(id),qty TEXT,price TEXT,at TEXT);\nCREATE TABLE outcomes(id TEXT PRIMARY KEY,lot TEXT,strategy TEXT,symbol TEXT,qty TEXT,entry REAL,exit REAL,gross REAL,at TEXT);\nCREATE TABLE lots(id TEXT PRIMARY KEY,symbol TEXT,strategy TEXT,qty TEXT,basis TEXT,opened TEXT,gap TEXT);\nCREATE TABLE unsolved_failures(id TEXT PRIMARY KEY,lot TEXT,symbol TEXT,regime TEXT,strategy TEXT,pnl REAL,cause TEXT,attempts INTEGER,status TEXT,created_at TEXT,report TEXT);\n"

def init(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10, isolation_level=None)) as c:
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA trusted_schema=OFF')
        c.execute('BEGIN IMMEDIATE')
        with c:
            tables = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
            version = c.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise ValueError('unsupported operational schema version')
            if tables:
                if 'meta' not in tables or c.execute("select v from meta where k='schema'").fetchone() != ('hourly-paper-1',):
                    raise ValueError('use a separate new operational database')
                if version == 0:
                    # BEGIN IMMEDIATE fences competing migrations. A second read connection
                    # sees the committed state preceding any schema changes.
                    copy_verified(path, path.parent / 'backups' / f'{path.name}.pre-v2-{uuid4().hex}.sqlite')
                cols = {r[1] for r in c.execute("PRAGMA table_info(strategies)")}
                if 'regime' not in cols:
                    c.execute("ALTER TABLE strategies ADD COLUMN regime TEXT")
                    c.execute("DROP INDEX IF EXISTS one_champ")
                    c.execute("DROP INDEX IF EXISTS one_child")
                    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_champ_per_regime ON strategies(status,regime) WHERE status='champion' AND regime IS NOT NULL")
                    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_champ_global ON strategies(status) WHERE status='champion' AND regime IS NULL")
                    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_child_per_regime ON strategies(status,regime) WHERE status='challenger' AND regime IS NOT NULL")
                decision_cols = {r[1] for r in c.execute('PRAGMA table_info(decisions)')}
                if 'regime' not in decision_cols:
                    c.execute('ALTER TABLE decisions ADD COLUMN regime TEXT')
                if 'unsolved_failures' not in tables:
                    c.execute("CREATE TABLE unsolved_failures(id TEXT PRIMARY KEY, lot TEXT, symbol TEXT, regime TEXT, strategy TEXT, pnl REAL, cause TEXT, attempts INTEGER, status TEXT, created_at TEXT, report TEXT)")
            else:
                for sql in SCHEMA.split(';'):
                    if sql.strip():
                        c.execute(sql)
                c.execute("insert into meta(k,v) values ('schema','hourly-paper-1')")
                c.execute("insert into meta(k,v) values ('since',?)", (now(),))
            if version == 0:
                c.execute("""CREATE TABLE development_batches(
                    consumer TEXT NOT NULL, cut TEXT NOT NULL, results TEXT NOT NULL,
                    PRIMARY KEY(consumer,cut))""")
                c.execute("""CREATE TABLE strategy_migration_incidents(
                    strategy TEXT PRIMARY KEY, original_born TEXT, original_regime TEXT,
                    detected_at TEXT NOT NULL, reason TEXT NOT NULL)""")
                # Old positional INSERTs could put the campaign timestamp in regime.
                # Preserve the ambiguous original values for independent reconstruction.
                for sid, born, regime in c.execute('SELECT id,born,regime FROM strategies'):
                    try:
                        stamp(born)
                    except (ValueError, TypeError):
                        c.execute("""INSERT INTO strategy_migration_incidents
                            (strategy,original_born,original_regime,detected_at,reason)
                            VALUES(?,?,?,?,?)""", (sid,born,regime,now(),'invalid_strategy_birth'))
                        put(c, 'integrity_entries_paused', 'YES')
                from market_observations import install_observation_contract
                install_observation_contract(c)
                c.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        if c.execute('PRAGMA journal_mode=WAL').fetchone()[0] != 'wal':
            raise RuntimeError('operational store requires WAL')
    return path

def record_unsolved_failure(c, lot, symbol, regime, strategy, pnl, cause, report):
    """Queue or update an unsolved failure for human review if AI cannot repair it."""
    fid = key('failure', lot, strategy)
    if not lot or lot == 'unsolved':
        raise ValueError('failure requires an actual lot identifier')
    row = c.execute('select attempts from unsolved_failures where id=?', (fid,)).fetchone()
    if row:
        c.execute('update unsolved_failures set attempts=attempts+1, report=? where id=?', (report, fid))
    else:
        c.execute('insert into unsolved_failures(id,lot,symbol,regime,strategy,pnl,cause,attempts,status,created_at,report) values(?,?,?,?,?,?,?,?,?,?,?)',
                  (fid, lot, symbol, regime, strategy, pnl, cause, 1, 'queued_for_human', now(), report))


def put(c, k, v):
    c.execute('insert into meta(k,v) values(?,?) on conflict(k) do update set v=excluded.v', (k, str(v)))

def get(c, k, fallback=None):
    row = c.execute('select v from meta where k=?', (k,)).fetchone()
    return row[0] if row else fallback

def rebuild(c):
    """Replay by broker execution time, not delivery order. Book gross only, never invent a fee."""
    c.execute('delete from outcomes')
    c.execute('delete from lots')
    lots = {}
    owners = {}
    for r in c.execute("select f.*,i.side,i.lot,i.symbol,i.strategy,i.stop_gap from fills f join intents i on i.id=f.intent order by at,case side when 'buy' then 0 else 1 end,f.id"):
        q, p = (amt(r['qty'], True), amt(r['price'], True))
        lot = r['lot']
        held, basis, at = lots.get(lot, (D(0), D(0), r['at']))
        if r['side'] == 'buy':
            owner = (r['symbol'], r['strategy'], r['stop_gap'])
            if owners.setdefault(lot, owner) != owner:
                raise ValueError('entry identity or protection changed within lot')
            held += q
            basis += p * q
        elif r['side'] == 'sell':
            if q > held or owners[lot][0] != r['symbol']:
                raise ValueError('exit has no matched entry')
            cost = basis if q == held else q * basis / held
            c.execute('insert into outcomes(id,lot,strategy,symbol,qty,entry,exit,gross,at) values(?,?,?,?,?,?,?,?,?)', (r['id'], lot, owners[lot][1], r['symbol'], str(q), float(cost / q), float(p), float(p * q - cost), r['at']))
            held -= q
            basis -= cost
        else:
            raise ValueError('invalid order side')
        lots[lot] = (held, basis, at)
        symbol, strategy, gap = owners[lot]
        c.execute('insert or replace into lots(id,symbol,strategy,qty,basis,opened,gap) values(?,?,?,?,?,?,?)', (lot, symbol, strategy, str(held), str(basis), at, gap))
