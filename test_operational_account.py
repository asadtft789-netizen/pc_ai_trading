"""Exact projections through the versioned SQLite ledger and dashboard reader."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

from config import Settings
from dashboard.paper_view import read_view
from operational_account import (
    allocated_funds, reconcile_account, record_fill_fee, record_reservation,
)
from paper_store import SCHEMA_VERSION, db, init, put, rebuild, stamp

AT = datetime(2026, 6, 1, 14, 31, tzinfo=timezone.utc)


class OperationalAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = init(Path(self.tmp.name) / 'account.sqlite')
        self.risk = Settings.load().risk
        with db(self.path, True) as c:
            put(c, 'snapshot', 'ok')

    def order(self, c, side, qty, price, fee='0', cid=None):
        cid = cid or side
        c.execute("""INSERT INTO intents(id,side,lot,symbol,strategy,qty,stop_gap,state,created)
                     VALUES(?,?,'L','AAPL','frozen',?,'2','acknowledged',?)""",
                  (cid, side, qty, stamp(AT)))
        record_reservation(c, cid, price if side == 'buy' else None, fee)
        return cid

    def fill(self, c, fid, cid, qty, price, at=AT):
        c.execute("INSERT INTO fills(id,intent,qty,price,at) VALUES(?,?,?,?,?)",
                  (fid, cid, qty, price, stamp(at)))
        rebuild(c)

    def test_pending_cash_partial_fill_and_late_fee_confirmation(self):
        with db(self.path, True) as c:
            self.order(c, 'buy', '.01', '100', '.02')
            reconcile_account(c, self.risk, AT, {})
            self.assertEqual(allocated_funds(c, self.risk, AT), (10, D('8.98')))
            self.assertEqual(allocated_funds(c, self.risk, AT, 'buy'), (10, 10))
            self.fill(c, 'partial', 'buy', '.004', '99')
            account = reconcile_account(c, self.risk, AT, {'AAPL': '98'})
            self.assertEqual(account.cash, D('9.596'))
            self.assertEqual(account.estimated_fees, D('.008'))
            self.assertEqual(allocated_funds(c, self.risk, AT)[1], D('8.984'))
            c.execute("UPDATE intents SET state='canceled' WHERE id='buy'")
            record_fill_fee(c, 'partial', '.001')
            record_fill_fee(c, 'partial', '.0010')
            record_fill_fee(c, 'partial', None)
            with self.assertRaisesRegex(ValueError, "conflicting"):
                record_fill_fee(c, 'partial', '.002')
            settled = reconcile_account(c, self.risk, AT + timedelta(seconds=1), {'AAPL': '98'})
            self.assertEqual(settled.equity, D('9.995'))
            self.assertEqual(settled.estimated_fees, 0)
        fields = read_view('metrics', path=self.path)
        self.assertEqual(D(fields['available_cash']), D('9.603'))
        self.assertEqual(D(fields['confirmed_fees']), D('.001'))
        self.assertEqual(fields['unconfirmed_fee_count'], 0)
        self.assertEqual(D(fields['unrealized_gross']), D('-.004'))
        self.assertIsNone(fields['confirmed_net_pnl'])
        with db(self.path, True) as c:
            reconcile_account(c, self.risk, AT + timedelta(seconds=2), {})
        self.assertIsNone(read_view('health', path=self.path)['marked_allocated_equity'])

    def test_missing_legacy_estimate_blocks_funds_until_confirmed(self):
        with db(self.path, True) as c:
            self.order(c, 'buy', '.01', '100')
            self.fill(c, 'b', 'buy', '.01', '100')
            c.execute("DELETE FROM portfolio_order_terms")
            c.execute("UPDATE intents SET state='filled'")
            reconcile_account(c, self.risk, AT, {'AAPL': '100'})
            self.assertEqual(allocated_funds(c, self.risk, AT), (0, 0))
        self.assertIsNone(read_view('health', path=self.path)['marked_allocated_equity'])
        with db(self.path, True) as c:
            record_fill_fee(c, 'b', '0')
            reconcile_account(c, self.risk, AT, {'AAPL': '100'})
            self.assertEqual(allocated_funds(c, self.risk, AT), (10, 9))

    def test_decimal_outcome_and_equal_time_pre_exit_peak_survive_restart(self):
        t = AT + timedelta(seconds=5)
        price = D('100.01234567890123456789')
        with db(self.path, True) as c:
            self.order(c, 'buy', '.01', '100')
            self.fill(c, 'b', 'buy', '.01', '100')
            record_fill_fee(c, 'b', 0)
            c.execute("UPDATE intents SET state='filled'")
            reconcile_account(c, self.risk, AT, {'AAPL': '99.99'})
            peak = reconcile_account(c, self.risk, t, {'AAPL': price}).peak
            self.order(c, 'sell', '.01', None, '.01')
            self.fill(c, 's', 'sell', '.01', str(price), t)
            c.execute("UPDATE intents SET state='filled'")
            self.assertEqual(c.execute("SELECT typeof(gross),gross FROM outcomes").fetchone()[1],
                             str((price - 100) * D('.01')))
            account = reconcile_account(c, self.risk, t, {})
            self.assertEqual(peak, D('9') + price * D('.01'))
            self.assertEqual(peak, account.peak)
        init(self.path)
        with db(self.path, True) as c:
            record_fill_fee(c, 's', '.04')
            account = reconcile_account(c, self.risk, t + timedelta(seconds=1), {})
            self.assertEqual(account.peak, peak)
            self.assertEqual(account.confirmed_fees, D('.04'))
            self.assertEqual(account.realized, (price - 100) * D('.01'))

    def test_day_and_lifetime_loss_latches_even_after_price_recovery(self):
        with db(self.path, True) as c:
            self.order(c, 'buy', '.01', '100', '.02')
            self.fill(c, 'b', 'buy', '.01', '100')
            c.execute("UPDATE intents SET state='filled'")
            # A severe quote gap plus fees breaches both budgets while still held.
            account = reconcile_account(c, self.risk, AT, {'AAPL': '.10'})
            self.assertTrue(account.daily_halt)
            self.assertTrue(account.drawdown_halt)
            recovered = reconcile_account(c, self.risk, AT + timedelta(seconds=30), {'AAPL': '110'})
            self.assertTrue(recovered.daily_halt)
            self.assertGreater(recovered.equity, D('10'))
        init(self.path)
        with db(self.path, True) as c:
            next_day = AT + timedelta(days=1)
            # Force a rebuild at the new receipt, not merely a checkpoint reload.
            record_fill_fee(c, 'b', '.01')
            result = reconcile_account(c, self.risk, next_day, {'AAPL': '110'})
            self.assertFalse(result.daily_halt)
            self.assertTrue(result.drawdown_halt)
            self.assertEqual(allocated_funds(c, self.risk, next_day), (0, 0))

    def test_previous_version_migrates_once_with_original_snapshot(self):
        # Remove v3-only objects to model the exact previous schema.
        with closing(sqlite3.connect(self.path)) as c:
            for table in ('portfolio_bids', 'portfolio_order_terms', 'portfolio_fees', 'portfolio_fill_receipts'):
                c.execute(f'DROP TABLE {table}')
            c.execute('DROP INDEX fills_by_intent')
            c.execute('ALTER TABLE decision_snapshots DROP COLUMN decided_at')
            c.execute('DROP TABLE outcomes')
            c.execute("""CREATE TABLE outcomes(id TEXT PRIMARY KEY,lot TEXT,strategy TEXT,symbol TEXT,
                         qty TEXT,entry REAL,exit REAL,gross REAL,at TEXT)""")
            c.execute("PRAGMA user_version=2")
            c.commit()
        init(self.path)
        init(self.path)
        backups = list((self.path.parent / 'backups').glob('*.sqlite'))
        self.assertEqual(len(backups), 1)
        with db(backups[0]) as previous:
            self.assertEqual(previous.execute('PRAGMA user_version').fetchone()[0], 2)
        with db(self.path) as current:
            self.assertEqual(current.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            columns = {r[1]: r[2] for r in current.execute('PRAGMA table_info(outcomes)')}
            self.assertEqual(columns['gross'], 'TEXT')
