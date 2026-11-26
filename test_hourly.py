import json, os, sqlite3, tempfile, unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import patch, Mock
import numpy as np
import pandas as pd
from config import Settings
from paper_store import init, db, put, now, stamp
from executor import StrategyExecutor, AlpacaPaper, fresh
from backtester import Backtester
from strategy_worker import SEED, compile_strategy
from hourly_features import enrich, signals
from research import review
from llm_mutator import LLMMutator
from dashboard.paper_view import read_view
from rule_review import import_rules, learn_from_closes

def bars(n=400):
    t = pd.date_range('2024-01-01', periods=n, freq='h', tz='UTC')
    v = 100 + np.sin(np.arange(n) * 0.85) * 4
    idx = pd.MultiIndex.from_product([['TEST'], t], names=['symbol', 'timestamp'])
    return pd.DataFrame(dict(open=v, high=v + 2, low=v - 2, close=v + 0.3, volume=100), index=idx)

class Fake:

    def __init__(self):
        self.hold = {}
        self.orders = {}
        self.activity = []
        self.submits = []
        self.fail = False
        self.bid = D('99.9')
        self.ask = D('100')

    def snapshot(self):
        if self.fail:
            raise TimeoutError()
        return ('acct', D(100), D(100), dict(self.hold), [k for k, o in self.orders.items() if o.status == 'new'], False)

    def fills(self, since):
        return list(self.activity)

    def lookup(self, cid):
        return self.orders[cid]

    def quote(self, symbol):
        return (self.bid, self.ask, now())

    def submit(self, cid, symbol, side, qty):
        self.submits.append(cid)
        self.orders[cid] = N(id='order' + str(len(self.submits)), client_order_id=cid, symbol=symbol, side=side, filled_qty='0', status='new', qty=qty)
        return self.orders[cid]

    def fill(self, cid, qty, price, at):
        o = self.orders[cid]
        q = D(qty)
        o.filled_qty = str(D(o.filled_qty) + q)
        o.status = 'filled' if D(o.filled_qty) == o.qty else 'partially_filled'
        self.activity.append(dict(id='fill' + str(len(self.activity)), order_id=o.id, symbol=o.symbol, side=o.side, price=str(price), qty=qty, transaction_time=at))
        self.hold[o.symbol] = self.hold.get(o.symbol, D(0)) + q * (1 if o.side == 'buy' else -1)
        if not self.hold[o.symbol]:
            del self.hold[o.symbol]

class Hourly(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.p = init(Path(self.d.name) / 'paper.sqlite')
        self.s = Settings.load()
        self.b = Fake()
        self.e = StrategyExecutor(self.p, self.b, self.s)
        self.mut = Mock()
        self.mut.propose.return_value = None

    def start(self):
        self.assertTrue(self.e.reconcile_trades()[1])
        with db(self.p, True) as c:
            put(c, 'entry_gate', 's')
            c.execute('insert into strategies values(?,?,?,?,?,?,?,?,?)', ('s', 'fixture', SEED, '["trend","r"]', None, 1, 'champion', None, now()))
        return self.e._open_position('TEST', '1', 's', '2', now())

    def test_confirmed_partial_across_midnight_and_restart(self):
        o = self.start()
        self.b.fill(o.client_order_id, '1', 100, '2025-01-01T22:00:00Z')
        lots, ok = self.e.reconcile_trades()
        self.assertTrue(ok)
        self.assertTrue(self.e._close_position(lots[0], 'risk'))
        cid = self.b.submits[-1]
        self.b.fill(cid, '.5', 102, '2025-01-01T23:59:59Z')
        self.assertTrue(self.e.reconcile_trades()[1])
        self.b.fill(cid, '.5', 98, '2025-01-02T00:00:00Z')
        self.e = StrategyExecutor(self.p, self.b, self.s)
        self.assertEqual(self.e.reconcile_trades(), ([], True))
        self.assertEqual(self.e.reconcile_trades(), ([], True))
        curve = read_view('equity-curve', path=self.p)
        self.assertEqual([x['equity'] for x in curve], [1, 0])
        hour = read_view('hourly', path=self.p)
        self.assertEqual([x['total_trades'] for x in hour], [1, 0])
        self.assertEqual(len(self.b.submits), 2)

    def test_outage_no_closes_or_orders(self):
        o = self.start()
        self.b.fill(o.client_order_id, '1', 100, now())
        self.e.reconcile_trades()
        self.b.fail = True
        self.assertIn('unknown', self.e.sync_positions())
        self.assertIsNone(read_view('metrics', path=self.p)['total_pnl'])
        self.assertEqual(len(self.b.submits), 1)
        self.assertEqual(read_view('trades', 50, self.p)[0]['status'], 'unknown')

    def test_pending_order_not_a_position(self):
        self.start()
        self.e.reconcile_trades()
        self.assertEqual(read_view('trades', 50, self.p), [])

    def test_ambiguous_submission_not_replayed(self):
        self.b.submit = Mock(side_effect=TimeoutError)
        self.assertIsNone(self.start())
        self.assertFalse(self.e.reconcile_trades()[1])
        self.assertIsNone(self.e._open_position('TEST', 1, 's', 2, 'bar'))
        self.assertEqual(self.b.submit.call_count, 1)

    def test_external_hold_blocks(self):
        self.b.hold['XYZ'] = D(1)
        self.assertFalse(self.e.reconcile_trades()[1])

    def test_conflicting_fill_blocks(self):
        o = self.start()
        self.b.fill(o.client_order_id, '1', 100, now())
        self.e.reconcile_trades()
        self.b.activity[0]['price'] = '101'
        self.assertFalse(self.e.reconcile_trades()[1])
        self.assertEqual(read_view('trades', 1, self.p)[0]['entry_price'], 100)

    def test_terminal_cancel_releases_only_remainder(self):
        o = self.start()
        self.b.fill(o.client_order_id, '.3', 100, now())
        o.status = 'canceled'
        lots, ok = self.e.reconcile_trades()
        self.assertTrue(ok)
        self.assertEqual(D(lots[0]['qty']), D('.3'))
        self.e._close_position(lots[0], 'exit')
        self.assertEqual(self.b.orders[self.b.submits[-1]].qty, D('.3'))

    def test_flat_signals_not_carried(self):
        f = bars(5)
        f['signal'] = [1, 0, 1, 0, 0]
        m = Backtester(self.s).run(None, f)
        self.assertEqual(m['total_trades'], 2)
        self.assertAlmostEqual(m['pnl'], sum((r['pnl_usd'] for r in m['trades'])))
        self.assertEqual(m['trades'][0]['entry_time'], f.index[1][1].isoformat())

    def test_flat_is_not_a_pass(self):
        self.assertEqual(Backtester(self.s).run(None, bars().assign(signal=0))['_reason'], 'zero_trades')

    def test_worker_accepts_real_python(self):
        self.assertEqual(len(signals(SEED, enrich(bars(82)))), 82)

    def test_worker_rejects_out_of_contract(self):
        for s in [SEED + '\nimport sys', SEED.replace('trend and momentum', 'trend.value'), SEED.replace('return 1', 'return 2')]:
            self.assertRaises(ValueError, compile_strategy, s)

    def test_prefix_stability(self):
        f = bars()
        pd.testing.assert_frame_equal(enrich(f).iloc[:100], enrich(f.iloc[:100]))

    def test_review_idempotence_and_no_orders(self):
        f = enrich(bars())
        args = (self.p, self.s, f.iloc[:280], f.iloc[280:], dict(label='sideways_quiet', indicators={}), self.mut)
        self.assertEqual(review(*args), 'hour recorded [sideways_quiet]')
        self.assertEqual(review(*args), 'no new completed bar')
        self.assertEqual(self.mut.propose.call_count, 5)
        self.assertEqual(len(read_view('price', 'TEST', self.p)), 300)
        self.assertEqual(len(self.b.submits), 0)

    def test_missing_database_does_not_get_created(self):
        p = self.p.with_name('missing')
        self.assertRaises(sqlite3.OperationalError, read_view, 'health', None, p)
        self.assertFalse(p.exists())

    def test_no_mixing_legacy_and_paper(self):
        p = self.p.with_name('legacy')
        c = sqlite3.connect(p)
        c.execute('create table rules(id integer,rule_text text)')
        c.execute("insert into rules values(1,'old idea')")
        c.commit()
        c.close()
        self.assertRaises(ValueError, init, p)
        self.assertEqual(import_rules(self.p, p), 1)
        import_rules(self.p, p)
        self.assertEqual(len(read_view('rules', path=self.p)), 1)
        self.assertIn('unattributed', read_view('rules', path=self.p)[0]['rule'])

    def test_health_is_readability_not_broker_status(self):
        self.assertEqual(read_view('health', path=self.p)['snapshot'], 'unknown')

    def test_bad_quote_or_missing_offset(self):
        self.assertFalse(fresh((D(0), D(1), now())))
        self.assertRaises(ValueError, stamp, '2024-01-01')

    def test_live_locked_before_sdk_import(self):
        with patch.dict(os.environ, {'ALPACA_BASE_URL': 'https://api.alpaca.markets'}):
            self.assertRaises(ValueError, AlpacaPaper, Settings.load())

    def test_autonomous_entry_and_supervised_exit(self):
        bar = (datetime.now(timezone.utc) - timedelta(hours=1, minutes=1)).isoformat()
        with db(self.p, True) as c:
            put(c, 'entry_gate', 's')
            c.execute('insert into strategies values(?,?,?,?,?,?,?,?,?)', ('s', 'fixture', SEED, '["trend","r"]', None, 1, 'champion', None, bar))
            c.execute('insert into decisions values(?,?,?,?,?,?)', ('TEST', bar, 's', 1, 1, 'bull_quiet'))
            c.execute('insert into watermarks values(?,?)', ('TEST', bar))
        with patch.dict(os.environ, {'PAPER_ENTRIES_ENABLED': 'YES'}):
            self.assertEqual(self.e.sync_positions(), 'entry submitted')
        o = self.b.orders[self.b.submits[0]]
        self.b.fill(o.client_order_id, str(o.qty), 100, now())
        self.b.bid = D('97')
        self.b.ask = D('97.1')
        self.assertEqual(self.e.sync_positions(), 'exit submitted')
        ex = self.b.orders[self.b.submits[-1]]
        self.assertEqual(ex.qty, o.qty)
        self.b.fill(ex.client_order_id, str(ex.qty), 97, now())
        self.assertTrue(self.e.reconcile_trades()[1])
        self.assertEqual(read_view('metrics', path=self.p)['loss_trades'], 1)
        self.mut.generate_lesson.return_value = 'Test a wider ATR guard.'
        learn_from_closes(self.p, self.mut)
        learn_from_closes(self.p, self.mut)
        self.assertEqual(len(read_view('lessons', 10, self.p)), 1)

    def test_candidate_frozen_then_prospectively_promoted(self):
        # Deterministic market fixture, not evidence that the strategy works on Alpaca.
        f = enrich(bars(1250))
        i = np.arange(len(f))
        f['open'] = np.where(i % 4 == 2, 104.0, 100.0)
        f['close'] = f.open
        f['high'] = f.open + 1
        f['low'] = f.open - 1
        f['support'] = i % 4 == 0
        src = SEED.split(':')[0] + ':\n    return 1'
        self.mut.propose.return_value = dict(name='fixture', source=src, guard='support', lesson='Test the support guard')
        reg = dict(label='sideways_quiet', indicators={})
        born = f.index[599][1].isoformat()
        with patch('research.now', return_value=born):
            review(self.p, self.s, f.iloc[:400], f.iloc[400:600], reg, self.mut)
        self.assertFalse(read_view('strategy', path=self.p))
        review(self.p, self.s, f.iloc[:600], f.iloc[600:700], reg, self.mut)
        self.assertFalse(read_view('strategy', path=self.p))
        review(self.p, self.s, f.iloc[:800], f.iloc[800:1100], reg, self.mut)
        self.assertEqual(read_view('strategy', path=self.p)['name'], 'fixture')
        self.assertIn('supported', read_view('rules', path=self.p)[0]['rule'])
        self.assertFalse(self.b.submits)

    def test_bad_holdout_does_not_promote(self):
        f = enrich(bars(240))
        f['support'] = False
        self.mut.propose.return_value = dict(name='flat', source=SEED, guard='support', lesson='guard')
        review(self.p, self.s, f.iloc[:150], f.iloc[150:], dict(label='unknown', indicators={}), self.mut)
        self.assertEqual(read_view('strategy', path=self.p), {})

    def test_tampered_snapshot_blocks_realization(self):
        o = self.start()
        self.b.fill(o.client_order_id, '1', 100, now())
        self.b.hold.clear()
        self.assertFalse(self.e.reconcile_trades()[1])
        self.assertEqual(read_view('equity-curve', path=self.p), [])

    def test_second_symbol_cannot_overlap(self):
        o = self.start()
        self.b.fill(o.client_order_id, '1', 100, now())
        self.e.reconcile_trades()
        self.assertIsNone(self.e._open_position('XYZ', 1, 's', 1, 'another'))
        self.assertEqual(len(self.b.submits), 1)

    def test_no_secret_in_log_record(self):
        import logging
        from main import SecretRedactingFilter
        with patch.dict(os.environ, {'GOOGLE_API_KEY': 'fake-test-secret'}):
            record = logging.LogRecord('x', 20, 'file', 1, 'error: %s', ('fake-test-secret',), None)
            SecretRedactingFilter().filter(record)
            self.assertNotIn('fake-test-secret', record.getMessage())

    def test_dashboard_reader_cannot_create_table(self):
        with db(self.p) as c:
            self.assertRaises(sqlite3.OperationalError, c.execute, 'create table nope(id)')
    def test_hourly_request_excludes_forming_bar_and_warms_holdout(self):
        import sys,types
        from data_ingestor import DataIngestor
        fake=types.ModuleType('alpaca.data.timeframe');fake.TimeFrame=N(Hour='1Hour')
        ing=DataIngestor.__new__(DataIngestor);ing.settings=self.s
        f=bars(900);latest=pd.Timestamp.now(tz='UTC').floor('h');dates=pd.date_range(end=latest,periods=len(f),freq='h');f.index=pd.MultiIndex.from_product([['TEST'],dates],names=['symbol','timestamp']);ing.fetch_bars=Mock(return_value=f)
        with patch.dict(sys.modules,{'alpaca.data.timeframe':fake}):tr,te=ing.get_train_test_split()
        self.assertEqual(ing.fetch_bars.call_args.args[1],'1Hour');self.assertLess(tr.index[-1][1],te.index[0][1]);self.assertLess(te.index[-1][1],latest);self.assertEqual(te.ema50.iloc[0],enrich(f).loc[te.index[0],'ema50']);self.assertIn(ing.detect_regime(te)['label'],('bull_quiet','bull_volatile','bear_quiet','bear_volatile','sideways_quiet','sideways_volatile'))
    def test_position_snapshot_alone_does_not_confirm_reported_fill(self):
        o=self.start();self.b.fill(o.client_order_id,'1',100,now());self.b.activity=[];self.assertFalse(self.e.reconcile_trades()[1]);self.assertEqual(read_view('trades',10,self.p),[])
    def test_foreign_account_cannot_resume(self):
        self.start();original=self.b.snapshot;self.b.snapshot=lambda:('different',*original()[1:]);self.assertFalse(self.e.reconcile_trades()[1])
    def test_negative_cost_not_a_subsidy(self):
        from dataclasses import replace
        s=replace(self.s,risk=replace(self.s.risk,spread_bps=-50.,slippage_bps=0.,commission_per_order_usd=0.));f=bars(4).assign(open=100.,close=100.,signal=[1,0,0,0]);self.assertEqual(Backtester(s).run(None,f)['pnl'],0.)
if __name__ == '__main__':
    unittest.main()
