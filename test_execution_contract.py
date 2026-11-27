"""Offline reservation, dispatch, reconciliation and fencing regressions."""
import os
import sqlite3
import subprocess
import sys
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from unittest.mock import patch

import test_hourly as fixtures
from config import Settings
from executor import StrategyExecutor
from paper_gateway import AlpacaPaper, PaperObservationOnly
from paper_order_policy import fresh, instant, writer_lock
from paper_store import db, get, now, put
from phoenix_integrity import learning_halted
from risk_engine import RiskEngine


class ExecutionContract(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.Hourly()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.e, self.b, self.p, self.s = fixture.e, fixture.b, fixture.p, fixture.s
        self.bar = fixture.seed_decision()
        enabled = patch.dict(os.environ, {'PAPER_ENTRIES_ENABLED': 'YES'})
        enabled.start()
        self.addCleanup(enabled.stop)

    def enter(self, qty='.01', gap='2', symbol='AAPL'):
        return self.e._open_position(symbol, qty, 's', gap, self.bar)

    def states(self):
        with db(self.p) as c:
            return [r[0] for r in c.execute('select state from intents order by created,id')]

    def test_production_adapter_has_zero_order_transport(self):
        broker = AlpacaPaper(self.s)
        with patch('paper_gateway.build_opener') as open_http:
            for side in ('buy', 'sell'):
                with self.assertRaises(PaperObservationOnly):
                    broker.submit('cid', 'AAPL', side, D('.01'))
            self.assertIsNone(StrategyExecutor(self.p, broker, self.s)._open_position(
                'AAPL', '.01', 's', '2', self.bar))
            open_http.assert_not_called()

    def test_unrecognized_paper_origins_and_oversized_budget(self):
        for bad in ('http://paper-api.alpaca.markets', 'https://paper-api.alpaca.markets./',
                    'https://paper-api.alpaca.markets//', 'https://api.alpaca.markets',
                    'https://paper-api.alpaca.markets@other.invalid', 'https://paper-api.alpaca.markets/?key=x'):
            with self.subTest(origin=bad), patch.dict(os.environ, {'ALPACA_BASE_URL': bad}):
                with self.assertRaises(ValueError):
                    Settings.load()
        for name, value in (('MAX_CAPITAL_USD','11'), ('MAX_POSITION_USD','2'),
                            ('MAX_ORDER_SIZE_USD','1.01'), ('MAX_DAILY_LOSS_USD','.51')):
            with self.subTest(setting=name), patch.dict(os.environ, {name: value}):
                with self.assertRaises(ValueError):
                    Settings.load()

    def test_direct_reservation_rechecks_qty_policy_and_universe(self):
        for qty in ('NaN', 'Infinity', '0', '-.1', '.005', '.02', '.0099999999'):
            with self.subTest(qty=qty):
                self.assertIsNone(self.enter(qty=qty))
        self.assertIsNone(self.enter(gap='1'))
        self.assertIsNone(self.enter(symbol='BTC/USD'))
        self.assertIsNone(self.enter(symbol='SPY'))
        self.assertEqual(self.states(), [])
        self.assertIsNotNone(self.enter())

    def test_halt_between_reservation_and_dispatch(self):
        evidence = self.b.entry_evidence
        def changed(*args):
            if self.states() == ['reserved_local']:
                with db(self.p, True) as c:
                    put(c, 'integrity_entries_paused', 'YES')
            return evidence(*args)
        self.b.entry_evidence = changed
        self.assertIsNone(self.enter())
        self.assertEqual(self.states(), ['rejected_locally'])
        self.assertFalse(self.b.submits)

    def test_source_correction_between_reservation_and_dispatch(self):
        evidence = self.b.entry_evidence
        def revised(*args):
            if self.states() == ['reserved_local']:
                with db(self.p, True) as c:
                    c.execute("insert into bar_quarantine(symbol,t,reason,detected_at) values(?,?,?,?)",
                              ('AAPL', self.bar, 'source_revision_requires_review', now()))
            return evidence(*args)
        self.b.entry_evidence = revised
        self.assertIsNone(self.enter())
        self.assertEqual(self.states(), ['rejected_locally'])
        self.assertFalse(self.b.submits)

    def test_unknown_reconciles_by_original_id_without_replaying(self):
        submit = self.b.submit
        def timed_out(*args):
            submit(*args)
            raise TimeoutError
        self.b.submit = timed_out
        self.assertIsNone(self.enter())
        self.assertEqual(self.states(), ['submission_unknown'])
        cid = self.b.submits[0]
        self.assertTrue(self.e.reconcile_trades()[1])
        self.assertEqual(self.states(), ['new'])
        self.b.orders[cid].status = 'rejected'
        self.assertTrue(self.e.reconcile_trades()[1])
        self.assertEqual(self.states(), ['rejected'])
        self.assertIsNone(self.enter())
        self.assertEqual(self.b.submits, [cid])

    def test_restart_only_releases_provably_local_reservation(self):
        with db(self.p, True) as c:
            c.execute("""insert into intents(id,lot,symbol,strategy,side,qty,stop_gap,state,created)
                values('before','lot','AAPL','s','buy','.01','2','reserved_local',?)""", (now(),))
        self.assertTrue(self.e.reconcile_trades()[1])
        self.assertEqual(self.states(), ['rejected_locally'])
        with db(self.p, True) as c:
            c.execute("update intents set state='reserved' where id='before'")
        self.assertFalse(self.e.reconcile_trades()[1])
        self.assertEqual(self.states(), ['reserved'])
        self.assertFalse(self.b.submits)

    def test_exit_ignores_entry_pause_and_wide_spread(self):
        order = self.enter()
        self.b.fill(order.client_order_id, '.01', '100', now())
        with db(self.p, True) as c:
            put(c, 'integrity_entries_paused', 'YES')
        self.b.bid, self.b.ask = D(95), D(100)
        with patch.dict(os.environ, {'PAPER_ENTRIES_ENABLED': 'NO'}):
            self.assertEqual(self.e.sync_positions(), 'exit submitted')
        self.assertEqual(self.b.orders[self.b.submits[-1]].qty, D('.01'))

    def test_foreign_lot_or_oversized_reduction_cannot_sell(self):
        order = self.enter()
        self.b.fill(order.client_order_id, '.01', '100', now())
        lots, ok = self.e.reconcile_trades()
        self.assertTrue(ok)
        for changed in ({'symbol': 'MSFT'}, {'strategy': 'new'}, {'qty': '1'}, {'gap': '50'}):
            with self.subTest(changed=changed):
                self.assertFalse(self.e._close_position({**lots[0], **changed}, 'exit'))
        self.assertEqual(len(self.b.submits), 1)

    def test_learning_identity_checkpoint_and_restore(self):
        self.assertFalse(learning_halted(self.p))
        with db(self.p, True) as c:
            put(c, 'learning_consumed_seq', 2)
        self.assertTrue(learning_halted(self.p))
        journal = self.p.with_name('learning_loop.sqlite')
        with sqlite3.connect(journal) as c:
            c.execute('insert into ll_receipts(seq) values(2)')
        c.close()
        self.assertFalse(learning_halted(self.p))
        with db(self.p, True) as c:
            put(c, 'learning_owner', 'other')
        self.assertTrue(learning_halted(self.p))
        journal.unlink()
        self.assertTrue(learning_halted(self.p))
        self.assertIsNone(self.enter())

    def test_missing_capability_or_unusable_timestamp_blocks(self):
        evidence = self.b.entry_evidence('AAPL', self.bar)
        mutations = ({'regular_session': False}, {'fractionable': False}, {'feature_ready': False},
                     {'protection_verified': False}, {'account': 'wrong'}, {'symbol': 'MSFT'},
                     {'clock_offset_seconds': D(3)}, {'minimum_notional': D(2)},
                     {'session_close': (instant(now())+timedelta(minutes=25)).isoformat()},
                     {'session_open': now()}, {'decided_at': (instant(now())+timedelta(days=1)).isoformat()},
                     {'interval_start': (instant(self.bar)-timedelta(minutes=30)).isoformat()})
        for mutation in mutations:
            with self.subTest(fields=mutation):
                self.b.entry_evidence = lambda *args: replace(evidence, **mutation)
                self.assertIsNone(self.enter())
        self.assertEqual(self.states(), [])

    def test_quote_boundaries_and_nonfinite_amounts(self):
        self.assertFalse(fresh((D('NaN'), D(1), now())))
        for seconds in (-11, 3):
            self.assertFalse(fresh((D(1), D(1), (instant(now())+timedelta(seconds=seconds)).isoformat())))
        self.assertTrue(fresh((D(99.9), D(100), now())))
        self.assertFalse(fresh((D(98), D(100), now())))
        self.assertTrue(fresh((D(98), D(100), now()), entry=False))
        risk = RiskEngine(self.s)
        for invalid in ('NaN', 'Infinity', -1, 0.99, 1.01):
            self.assertFalse(risk.validate_trade(invalid))
        self.assertTrue(risk.validate_trade(1))

    def test_future_fill_is_quarantined(self):
        order = self.enter()
        self.b.fill(order.client_order_id, '.01', '100', (instant(now())+timedelta(hours=1)).isoformat())
        self.assertFalse(self.e.reconcile_trades()[1])
        with db(self.p) as c:
            self.assertEqual(c.execute('select count(*) from fills').fetchone()[0], 0)
            self.assertEqual(get(c, 'snapshot'), 'unknown')

    def test_writer_exclusion_across_processes_and_release(self):
        script = ("from paper_order_policy import writer_lock, WriterBusy\nimport sys\n"
                  "try:\n with writer_lock(sys.argv[1]): pass\nexcept WriterBusy: sys.exit(23)")
        with writer_lock(self.p):
            self.assertIsNone(self.enter())
            self.assertEqual(subprocess.run([sys.executable, '-c', script, str(self.p)], timeout=10).returncode, 23)
        self.assertEqual(subprocess.run([sys.executable, '-c', script, str(self.p)], timeout=10).returncode, 0)
