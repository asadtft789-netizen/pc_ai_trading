"""Append-only experiment records in the single operational SQLite ledger."""
import json
from learning_loop import digest, encode


def install(c):
    c.execute("""CREATE TABLE IF NOT EXISTS experiment_records(
        kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL, sha256 TEXT NOT NULL,
        PRIMARY KEY(kind,id))""")
    for operation in ("UPDATE", "DELETE"):
        c.execute(f"""CREATE TRIGGER IF NOT EXISTS experiment_no_{operation}
            BEFORE {operation} ON experiment_records
            BEGIN SELECT RAISE(ABORT,'immutable experiment record'); END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS experiment_no_replace BEFORE INSERT ON experiment_records
        WHEN EXISTS(SELECT 1 FROM experiment_records WHERE kind=NEW.kind AND id=NEW.id)
        BEGIN SELECT RAISE(ABORT,'immutable experiment record'); END""")


def read(c, kind, identity):
    row = c.execute("SELECT body,sha256 FROM experiment_records WHERE kind=? AND id=?",
                    (kind, identity)).fetchone()
    if row is None:
        return None
    body = json.loads(row[0])
    if row[1] != digest([kind, identity, body]):
        raise ValueError("experiment record digest mismatch")
    return body


def append(c, kind, identity, body):
    previous = read(c, kind, identity)
    if previous is not None:
        if encode(previous) != encode(body):
            raise ValueError(f"conflicting {kind} record")
        return
    c.execute("INSERT INTO experiment_records(kind,id,body,sha256) VALUES(?,?,?,?)",
              (kind, identity, encode(body), digest([kind, identity, body])))


def needed(c, kind, identity):
    record = read(c, kind, identity)
    if record is None:
        raise ValueError(f"unregistered {kind}")
    return record
