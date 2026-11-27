import json, os, sqlite3, tempfile, unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import patch, Mock
import numpy as np
import pandas as pd
from config import Settings
from paper_store import init, db, put, now, stamp, key
from executor import StrategyExecutor, AlpacaPaper, fresh
from paper_order_policy import EntryEvidence, instant
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
    orders_enabled = True

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

    def entry_evidence(self, symbol, decision):
        end = instant(decision)
        return EntryEvidence(
            'acct', symbol, (end - timedelta(hours=1)).isoformat(), end.isoformat(),
            (end - timedelta(hours=2)).isoformat(), (end + timedelta(hours=3)).isoformat(),
            end.isoformat(), end.isoformat(), D(0), D(1), True, True, True, True)

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

    def seed_decision(self):
        from hourly_features import ALL_READY, FEATURE_VERSION
        from market_observations import record_observation
        from strategy_worker import FEATURES
        bar = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        with sqlite3.connect(self.p.with_name('learning_loop.sqlite')) as journal:
            journal.execute('create table ll_meta(k text primary key, v text not null)')
            journal.execute('create table ll_receipts(seq integer primary key)')
            journal.executemany('insert into ll_meta(k,v) values(?,?)', [
                ('halted', 'false'), ('store_id', '"fixture-store"'), ('owner', '"fixture-owner"')])
        journal.close()
        with db(self.p, True) as c:
            put(c, 'learning_store', 'fixture-store')
            put(c, 'learning_owner', 'fixture-owner')
            put(c, 'integrity_entries_paused', 'NO')
            put(c, 'entry_gate_bull_quiet', 's')
            c.execute("insert into strategies(id,name,source,rule_ids,parent_id,generation,status,regime,born) "
                      "values(?,?,?,?,?,?,?,?,?)",
                      ('s', 'fixture', SEED, '["trend","r"]', None, 1, 'champion', 'bull_quiet', bar))
            c.execute('insert into decisions(symbol,t,strategy,target,atr,regime) values(?,?,?,?,?,?)',
                      ('AAPL', bar, 's', 1, 1, 'bull_quiet'))
            c.execute('insert into watermarks(symbol,t) values(?,?)', ('AAPL', bar))
            end = instant(bar)
            record_observation(c, 'AAPL', bar, dict(open=100, high=101, low=99, close=100,
                volume=1000, interval_kind='XNYS_1m', interval_start=end-timedelta(hours=1),
                interval_end=end, received_at=bar, feed='offline-fixture'))
            c.execute("""insert into decision_snapshots(symbol,t,strategy,lot,regime,feature_version,
                readiness_mask,history_ready,flags,rsi,received_at,content_hash)
                select symbol,t,'s',?,'bull_quiet',?,?,1,?,50,received_at,content_hash
                from usable_bars where symbol='AAPL' and t=?""",
                (key('lot', 'AAPL', bar), FEATURE_VERSION, ALL_READY,
                 json.dumps({name: True for name in FEATURES}), bar))
        return bar

    def start(self):
        self.assertTrue(self.e.reconcile_trades()[1])
        bar = self.seed_decision()
        with patch.dict(os.environ, {'PAPER_ENTRIES_ENABLED': 'YES'}):
            return self.e._open_position('AAPL', '.01', 's', '2', bar)

    def test_confirmed_partial_across_midnight_and_restart(self):
        o = self.start()
        self.b.fill(o.client_order_id, '.01', 100, '2025-01-01T22:00:00Z')
        lots, ok = self.e.reconcile_trades()
        self.assertTrue(ok)
        self.assertTrue(self.e._close_position(lots[0], 'risk'))
        cid = self.b.submits[-1]
        self.b.fill(cid, '.005', 102, '2025-01-01T23:59:59Z')
        self.assertTrue(self.e.reconcile_trades()[1])
        self.b.fill(cid, '.005', 98, '2025-01-02T00:00:00Z')
        self.e = StrategyExecutor(self.p, self.b, self.s)
        self.assertEqual(self.e.reconcile_trades(), ([], True))
        self.assertEqual(self.e.reconcile_trades(), ([], True))
        curve = read_view('equity-curve', path=self.p)
        self.assertEqual([x['equity'] for x in curve], [0.01, 0])
        hour = read_view('hourly', path=self.p)
        self.assertEqual([x['total_trades'] for x in hour if x['h'].startswith('2025-01-')], [1, 0])
        self.assertEqual(len(self.b.submits), 2)

    def test_outage_no_closes_or_orders(self):
        o = self.start()
        self.b.fill(o.client_order_id, '.01', 100, now())
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
        self.b.fill(o.client_order_id, '.01', 100, now())
        self.e.reconcile_trades()
        self.b.activity[0]['price'] = '101'
        self.assertFalse(self.e.reconcile_trades()[1])
        self.assertEqual(read_view('trades', 1, self.p)[0]['entry_price'], 100)

    def test_terminal_cancel_releases_only_remainder(self):
        o = self.start()
        self.b.fill(o.client_order_id, '.003', 100, now())
        o.status = 'canceled'
        lots, ok = self.e.reconcile_trades()
        self.assertTrue(ok)
        self.assertEqual(D(lots[0]['qty']), D('.003'))
        self.e._close_position(lots[0], 'exit')
        self.assertEqual(self.b.orders[self.b.submits[-1]].qty, D('.003'))

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
            self.assertRaises(ValueError, Settings.load)
        from dataclasses import replace
        unsafe = replace(self.s, alpaca=replace(self.s.alpaca, base_url="https://api.alpaca.markets"))
        self.assertRaises(ValueError, AlpacaPaper, unsafe)

    def test_autonomous_entry_and_supervised_exit(self):
        bar = self.seed_decision()
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

    def test_profitable_development_fixture_requires_independent_promotion_evidence(self):
        f = enrich(bars(1250)).rename(index={'TEST': 'AAPL'}, level='symbol')
        i = np.arange(len(f))
        f['open'] = np.where(i % 4 == 2, 104.0, 100.0)
        f['close'], f['high'], f['low'] = f.open, f.open + 1, f.open - 1
        f['support'] = i % 4 == 0
        src = SEED.split(':')[0] + ':\n    return 1'
        self.mut.propose.return_value = dict(name='fixture', source=src, guard='support', lesson='Test support')
        observed = {t.isoformat(): 'trend_up' for t in f.index.get_level_values('timestamp')}
        review(self.p, self.s, f.iloc[:700], f.iloc[700:], dict(label='trend_up', indicators={},
               observed_by_time=observed), self.mut)
        with db(self.p) as c:
            candidate = c.execute("select * from strategies where name='fixture'").fetchone()
            self.assertEqual(candidate['status'], 'development')
            self.assertEqual(candidate['source'], src)
            frozen = json.loads(c.execute('select manifest from development_manifests where id=?',
                                          (candidate['id'],)).fetchone()[0])
            self.assertEqual((frozen['source'], frozen['guard']), (src, 'support'))
            metrics = json.loads(c.execute("select metrics from trials where strategy=?",
                                           (candidate['id'],)).fetchone()[0])
            self.assertGreater(metrics['pnl'], 0)
            self.assertEqual(c.execute("select count(*) from strategies where status='champion'").fetchone()[0], 0)
        self.assertFalse(self.b.submits)

    def test_bad_holdout_does_not_promote(self):
        f = enrich(bars(240))
        f['support'] = False
        self.mut.propose.return_value = dict(name='flat', source=SEED, guard='support', lesson='guard')
        review(self.p, self.s, f.iloc[:150], f.iloc[150:], dict(label='unknown', indicators={}), self.mut)
        self.assertEqual(read_view('strategy', path=self.p), {})

    def test_tampered_snapshot_blocks_realization(self):
        o = self.start()
        self.b.fill(o.client_order_id, '.01', 100, now())
        self.b.hold.clear()
        self.assertFalse(self.e.reconcile_trades()[1])
        self.assertEqual(read_view('equity-curve', path=self.p), [])

    def test_second_symbol_cannot_overlap(self):
        o = self.start()
        self.b.fill(o.client_order_id, '.01', 100, now())
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
    def test_minute_ingestion_excludes_forming_interval_and_isolates_crypto_outage(self):
        from alpaca.data.timeframe import TimeFrame
        from data_ingestor import DataIngestor

        ing = DataIngestor.__new__(DataIngestor)
        ing.settings = self.s
        end = pd.Timestamp('2025-06-04T15:05:00Z')
        times = pd.date_range('2025-06-02T13:30:00Z', end, freq='min')
        idx = pd.MultiIndex.from_product([['AAPL'], times], names=['symbol','timestamp'])
        minutes = pd.DataFrame(dict(open=100, high=102, low=98, close=101, volume=17), index=idx)
        def fetch(symbols, timeframe, start, stop):
            if any('/' in symbol for symbol in symbols):
                raise TimeoutError('offline fixture')
            self.assertEqual(timeframe.value, TimeFrame.Minute.value)
            self.assertIn('SPY', symbols)
            return minutes.loc[(times >= start) & (times <= stop)]
        ing.fetch_bars = Mock(side_effect=fetch)
        tr, te = ing.get_train_test_split(2, 1, as_of=end)
        self.assertLess(tr.index[-1][1], te.index[0][1])
        self.assertEqual(te.index[-1][1], pd.Timestamp('2025-06-04T14:30:00Z'))
        self.assertEqual(te.volume.iloc[-1], 60 * 17)
        self.assertEqual(te.interval_kind.iloc[-1], 'XNYS_1m')
        self.assertTrue(any(d['error'] == 'TimeoutError' for d in ing.diagnostics))

    def test_position_snapshot_alone_does_not_confirm_reported_fill(self):
        o=self.start();self.b.fill(o.client_order_id,'.01',100,now());self.b.activity=[];self.assertFalse(self.e.reconcile_trades()[1]);self.assertEqual(read_view('trades',10,self.p),[])
    def test_foreign_account_cannot_resume(self):
        self.start();original=self.b.snapshot;self.b.snapshot=lambda:('different',*original()[1:]);self.assertFalse(self.e.reconcile_trades()[1])
    def test_negative_cost_not_a_subsidy(self):
        from dataclasses import replace
        s=replace(self.s,risk=replace(self.s.risk,spread_bps=-50.,slippage_bps=0.,commission_per_order_usd=0.));f=bars(4).assign(open=100.,close=100.,signal=[1,0,0,0]);self.assertEqual(Backtester(s).run(None,f)['pnl'],0.)
if __name__ == '__main__':
    unittest.main()
