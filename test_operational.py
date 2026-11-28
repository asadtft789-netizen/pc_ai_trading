"""Backup, operational health, and durable recovery regressions."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backup_sets import backup, keep_names, pair_state, restore
from dashboard.paper_view import read_view
from executor import StrategyExecutor
from operational import heartbeat, status, write_status
from paper_store import db, init, now, put, retain_wal_files
from test_hourly import Fake
from config import Settings


class OperationalTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.database = init(self.root / "operational.sqlite")
        self.learning = self.root / "learning_loop.sqlite"
        self.backups = self.root / "backups"
        self.backups.mkdir()
        with sqlite3.connect(self.learning) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE ll_meta(k TEXT PRIMARY KEY,v TEXT)")
            connection.execute("CREATE TABLE ll_receipts(seq INTEGER PRIMARY KEY)")
            connection.executemany("INSERT INTO ll_meta VALUES(?,?)",
                                   [("owner", json.dumps("owner")), ("store_id", json.dumps("ledger"))])
            connection.execute("INSERT INTO ll_receipts VALUES(1)")
        connection.close()
        with db(self.database, True) as connection:
            put(connection, "learning_owner", "owner")
            put(connection, "learning_store", "ledger")
            put(connection, "learning_consumed_seq", 1)

    def test_wal_keeper_permits_idle_read_only_dashboard(self):
        with retain_wal_files(self.database):
            self.assertTrue(Path(str(self.database) + "-wal").exists())
            self.assertTrue(Path(str(self.database) + "-shm").exists())
            self.root.chmod(0o500)
            try:
                self.assertEqual(read_view("health", path=self.database)["status"], "readable")
            finally:
                self.root.chmod(0o700)

    def test_wal_backup_restores_bound_pair_and_rejects_tampering(self):
        with sqlite3.connect(self.learning) as writer:
            writer.execute("INSERT INTO ll_receipts VALUES(2)")
            writer.commit()
            writer.execute("INSERT INTO ll_receipts VALUES(3)")
            snapshot = backup(self.database, self.backups, "a" * 40)
            writer.rollback()
        writer.close()
        restored = self.root / "restored"
        restore(snapshot, restored)
        self.assertEqual(pair_state(restored / self.database.name, restored / self.learning.name),
                         {"schema": 2, "operational_sequence": 1, "learning_sequence": 2})
        with (snapshot / self.learning.name).open("ab") as handle:
            handle.write(b"tamper")
        rejected = self.root / "rejected"
        with self.assertRaisesRegex(ValueError, "checksum"):
            restore(snapshot, rejected)
        self.assertFalse(rejected.exists())

    def test_backup_rejects_unbound_or_regressed_sidecar(self):
        with db(self.database, True) as connection:
            put(connection, "learning_consumed_seq", 2)
        with self.assertRaisesRegex(ValueError, "mismatched"):
            backup(self.database, self.backups, "a" * 40)
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_retention_spans_seven_days_and_four_iso_weeks(self):
        current = datetime(2026, 11, 28, 20, tzinfo=timezone.utc)
        names = [(current - timedelta(hours=n)).strftime("%Y%m%dT%H%M%S%fZ") for n in range(900)]
        offsite = keep_names(names + ["unknown", "20262591T000000000000Z"], current)
        self.assertTrue({name[:8] for name in names[::24][:7]} <= {name[:8] for name in offsite})
        self.assertEqual(len({datetime.strptime(name[:8], "%Y%m%d").isocalendar()[:2]
                              for name in offsite}), 4)
        self.assertTrue(offsite < keep_names(names, current, hourly=True))

    def test_dashboard_separates_foreign_broker_state_from_local_ledger(self):
        broker = Fake()
        broker.hold = {"AAPL": 1}
        self.assertFalse(StrategyExecutor(self.database, broker, Settings.load()).reconcile_trades()[1])
        report = read_view("health", path=self.database)
        self.assertEqual(report["snapshot"], "unknown")
        self.assertEqual(report["broker_observation"]["positions"], {"AAPL": "1"})
        self.assertNotIn("acct", json.dumps(report))
        self.assertIsNone(report["marked_allocated_equity"])
        self.assertEqual(report["local_open_orders"], [])

    def test_health_uses_backup_age_and_distinguishes_unknown_account(self):
        backup_file = self.root / "status.json"
        write_status(backup_file, {"state": "verified", "verified_at": now(), "offsite": "unconfigured"})
        report = status(self.database, backup_file)
        self.assertIn("broker_reconciliation_unhealthy", report["alerts"])
        self.assertNotIn("local_backup_unhealthy", report["alerts"])
        self.assertEqual(report["database"]["state"], "readable")

    def test_heartbeat_blocks_non_https_and_uses_explicit_failure_route(self):
        for url in ("http://example.org/ping", "https://name:secret@example.org/ping",
                    "https://example.org/ping?token=placeholder"):
            with self.assertRaises(ValueError):
                heartbeat(url, True)
        with patch("operational.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.status = 200
            heartbeat("https://example.org/ping", False)
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://example.org/ping/fail")
            self.assertEqual(request.get_method(), "POST")
