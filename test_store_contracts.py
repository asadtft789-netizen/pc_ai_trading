import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from paper_store import SCHEMA, SCHEMA_VERSION, db, init, put, rebuild
from sqlite_snapshot import copy_verified


class StoreContracts(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "paper.sqlite"

    def legacy(self):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA.replace("status TEXT,regime TEXT,born TEXT",
                                           "status TEXT,born TEXT,regime TEXT"))
            c.execute("INSERT INTO meta(k,v) VALUES('schema','hourly-paper-1')")
            c.execute("""INSERT INTO strategies(id,status,born,regime)
                         VALUES('bad','challenger','trend_up','2025-01-01T00:00:00+00:00')""")
            c.commit()

    def test_legacy_migration_preserves_original_and_takes_one_verified_snapshot(self):
        self.legacy()
        init(self.path)
        init(self.path)
        with db(self.path) as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(tuple(c.execute("SELECT original_born,original_regime FROM strategy_migration_incidents").fetchone()),
                             ("trend_up","2025-01-01T00:00:00+00:00"))
            self.assertEqual(c.execute("SELECT born FROM strategies WHERE id='bad'").fetchone()[0], "trend_up")
            self.assertEqual(c.execute("SELECT v FROM meta WHERE k='integrity_entries_paused'").fetchone()[0],"YES")
        snapshots = list((self.path.parent / "backups").glob("*.sqlite"))
        self.assertEqual(len(snapshots), 1)
        restored = copy_verified(snapshots[0], self.path.parent / "recovered.sqlite")
        with db(restored) as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT count(*) FROM strategies").fetchone()[0], 1)

    def test_backup_failure_rolls_back_without_advancing_schema(self):
        self.legacy()
        with patch("paper_store.copy_verified", side_effect=OSError("disk full")), self.assertRaises(OSError):
            init(self.path)
        with db(self.path) as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertFalse(c.execute("SELECT name FROM sqlite_master WHERE name='development_batches'").fetchone())

    def test_unknown_version_is_refused(self):
        self.legacy()
        with db(self.path, True) as c:
            c.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(ValueError, "version"):
            init(self.path)
        self.assertFalse((self.path.parent / "backups").exists())

    def test_snapshot_sees_committed_wal_and_never_clobbers_existing_file(self):
        init(self.path)
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("PRAGMA wal_autocheckpoint=0")
            put(c, "committed", "before")
            c.commit()
            put(c, "in_flight", "after")
            snapshot = copy_verified(self.path, self.path.parent / "snapshot.sqlite")
            c.rollback()
        with db(snapshot) as c:
            self.assertEqual(c.execute("SELECT v FROM meta WHERE k='committed'").fetchone()[0],"before")
            self.assertIsNone(c.execute("SELECT v FROM meta WHERE k='in_flight'").fetchone())
        with self.assertRaises(FileExistsError):
            copy_verified(snapshot, self.path)

    def test_partial_exit_cannot_replace_lot_owner_or_change_initial_gap(self):
        init(self.path)
        with db(self.path, True) as c:
            for side, strategy, qty, price, gap, stamp in [
                    ("buy","entry_policy","0.010",100,"2","2026-01-02T15:30:00+00:00"),
                    ("sell","replacement","0.004",110,"99","2026-01-02T16:00:00+00:00")]:
                c.execute("INSERT INTO intents(id,lot,symbol,strategy,side,qty,stop_gap) VALUES(?,'lot','AAPL',?,?,?,?)",
                          (side,strategy,side,qty,gap))
                c.execute("INSERT INTO fills(id,intent,qty,price,at) VALUES(?,?,?,?,?)",
                          (side,side,qty,str(price),stamp))
            rebuild(c)
            self.assertEqual(tuple(c.execute("SELECT strategy,gap,qty FROM lots").fetchone()),("entry_policy","2","0.006"))
            self.assertEqual(c.execute("SELECT strategy FROM outcomes").fetchone()[0],"entry_policy")
