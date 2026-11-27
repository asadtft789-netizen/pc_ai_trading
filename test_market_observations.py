"""Offline regression coverage for raw observations, feature readiness and attribution."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

from config import Settings
from hourly_features import ALL_READY, enrich, signals, strategy_flags
from market_observations import install_observation_contract, record_observation
from paper_store import db, init
from research import analyze_failure, ingest_observations, review
from review_progress import review_marks
from strategy_worker import FEATURES, SEED


def raw_frame(n=325):
    times = pd.date_range('2025-01-01', periods=n, freq='h', tz='UTC')
    idx = pd.MultiIndex.from_product([['AAPL'], times], names=['symbol', 'timestamp'])
    values = np.arange(n)
    return pd.DataFrame(dict(open=100., high=103., low=97., close=100 + np.sin(values),
                             volume=np.where(values % 2 == 1, 9215, 789)), index=idx)


class Observations(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = init(Path(tmp.name) / 'store.sqlite')
        self.settings = Settings.load()
        self.mutator = Mock()
        self.mutator.propose.return_value = None

    def test_raw_numerics_and_boolean_compatibility_across_round_trip(self):
        raw = raw_frame()
        frame = enrich(raw)
        pd.testing.assert_series_equal(raw.volume, frame.volume)
        pd.testing.assert_frame_equal(enrich(frame), frame)
        src = SEED.split(':')[0] + ':\n    return 1 if volume else 0'
        self.assertEqual(signals(src, frame).iloc[-2:].tolist(), [1, 0])
        self.assertTrue(strategy_flags(frame.iloc[-2])['volume'])
        self.assertFalse(strategy_flags(frame.iloc[-1])['volume'])
        ingest_observations(self.path, frame.iloc[:305], frame.iloc[305:])
        with db(self.path) as c:
            self.assertEqual([r[0] for r in c.execute('SELECT v FROM usable_bars ORDER BY t')],
                             raw.volume.tolist())
            self.assertEqual(c.execute('SELECT count(*) FROM bar_versions').fetchone()[0], len(raw))

    def test_per_feature_readiness_and_warmup_block_negated_rule(self):
        frame = enrich(raw_frame())
        volume_bit = 1 << FEATURES.index('volume')
        self.assertFalse(frame.readiness_mask.iloc[19] & volume_bit)
        self.assertTrue(frame.readiness_mask.iloc[20] & volume_bit)
        self.assertEqual(frame.readiness_mask.iloc[299], ALL_READY)
        self.assertIsNone(strategy_flags(frame.iloc[299]))
        self.assertIsNotNone(strategy_flags(frame.iloc[300]))
        source = SEED.split(':')[0] + ':\n    return 1 if not breakout else 0'
        self.assertFalse(signals(source, frame.iloc[:300]).any())

    def test_boolean_ohlcv_and_invalid_values_rejected_before_storage(self):
        for name, val in [('volume', True), ('close', np.nan), ('open', np.inf), ('volume', -1),
                          ('low', 300), ('high', 1), ('open', '100')]:
            frame = raw_frame(8).assign(**{name: val})
            with self.subTest(name=name, val=val), self.assertRaises(ValueError):
                enrich(frame)
        with db(self.path) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM bars').fetchone()[0], 0)

    def test_a_missing_crypto_interval_restarts_warmup(self):
        frame = raw_frame(625)
        times = frame.index.get_level_values('timestamp')
        frame = frame.assign(interval_kind='UTC_1h', interval_start=times - pd.Timedelta(hours=1), interval_end=times)
        frame = frame.drop(frame.index[310])
        enriched = enrich(frame)
        self.assertTrue(enriched.history_ready.iloc[309])
        self.assertFalse(enriched.history_ready.iloc[609])
        self.assertTrue(enriched.history_ready.iloc[610])

    def test_quarantine_only_ambiguous_pre_migration_volume(self):
        c = sqlite3.connect(':memory:')
        self.addCleanup(c.close)
        c.executescript("CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT NOT NULL);"
                        "CREATE TABLE bars(symbol TEXT,t TEXT,o REAL,h REAL,l REAL,c REAL,v REAL,PRIMARY KEY(symbol,t));")
        c.executemany("INSERT INTO bars VALUES('AAPL',?,10,12,8,11,?)", [('t0',0), ('t1',1), ('t2',61)])
        install_observation_contract(c)
        values = dict(open=10, high=12, low=8, close=11, volume=0)
        record_observation(c, 'AAPL', 't3', values)
        install_observation_contract(c)
        self.assertEqual(c.execute('SELECT t FROM usable_bars ORDER BY t').fetchall(), [('t2',),('t3',)])
        self.assertEqual(c.execute('SELECT t FROM bar_quarantine ORDER BY t').fetchall(), [('t0',),('t1',)])
        self.assertEqual(c.execute("SELECT v FROM bars WHERE t='t1'").fetchone()[0], 1)

    def test_duplicate_and_conflicting_source_revision_preserve_first_facts(self):
        row = dict(open=10,high=12,low=8,close=11,volume=0,feed='fixture',received_at='2025-01-01T11:00:00Z')
        with db(self.path, True) as c:
            record_observation(c, 'AAPL', 't', row)
            record_observation(c, 'AAPL', 't', dict(row, received_at='2025-01-01T12:00:00Z'))
            self.assertEqual(c.execute('SELECT received_at FROM usable_bars').fetchone()[0], row['received_at'])
            record_observation(c, 'AAPL', 't', dict(row, volume=317))
            record_observation(c, 'AAPL', 't', dict(row, volume=317))
            self.assertEqual(c.execute('SELECT count(*) FROM usable_bars').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT v FROM bars').fetchone()[0], 0)
            self.assertEqual([r[0] for r in c.execute('SELECT v FROM bar_versions ORDER BY original DESC')], [0,317])
            for query in ('UPDATE bars SET v=317', 'DELETE FROM bar_versions', 'DELETE FROM bar_quarantine'):
                with self.subTest(query=query), self.assertRaises(sqlite3.IntegrityError):
                    c.execute(query)

    def test_regimes_consume_same_data_independently_and_route_once(self):
        frame = enrich(raw_frame(28))
        train, test = frame.iloc[:21], frame.iloc[21:]
        ingest_observations(self.path, train, test)
        actual = {t.isoformat(): 'trend_down' for t in frame.index.get_level_values('timestamp')}
        for scope in ('trend_up','trend_down'):
            result = review(self.path, self.settings, train, test,
                            dict(label=scope, observed_regime='trend_down', indicators={}, observed_by_time=actual),
                            self.mutator, observations_committed=True)
            self.assertIn('hour recorded', result)
        with db(self.path) as c:
            self.assertEqual(review_marks(c,'trend_up'), review_marks(c,'trend_down'))
            self.assertIn('AAPL', review_marks(c,'trend_up'))
            self.assertEqual(c.execute('SELECT count(*) FROM bars').fetchone()[0], 28)
            self.assertEqual(c.execute('SELECT count(*) FROM development_batches').fetchone()[0], 2)
            self.assertEqual({r[0] for r in c.execute('SELECT regime FROM decisions')}, {'trend_down'})
            self.assertEqual({r[0] for r in c.execute('SELECT target FROM decisions')}, {None})

    def test_failure_diagnosis_preserves_missing_entry_evidence(self):
        loss = dict(symbol='AAPL', strategy='old',lot='lot1',gross=-0.01,exit_at='2025-01-02')
        missing = analyze_failure(loss)
        self.assertEqual(missing['diagnosis'], 'entry_snapshot_unavailable')
        flags = {k: True for k in FEATURES}
        flags['volume'] = False
        analysis = analyze_failure(dict(loss,flags=json.dumps(flags),entry_time='2025-01-01'))
        self.assertIn('low_volume',analysis['associations'])
        self.assertEqual((analysis['lot'],analysis['strategy'],analysis['entry_time']),
                         ('lot1','old','2025-01-01'))
        self.assertEqual(analysis['diagnosis'],'hypotheses_only')

    def test_symbol_with_source_conflict_excluded_from_development(self):
        raw = raw_frame(22)
        frame = enrich(raw)
        review(self.path, self.settings, frame.iloc[:15], frame.iloc[15:],
               dict(label='trend_down', indicators={}), self.mutator)
        raw.iloc[10, raw.columns.get_loc('volume')] = 1234
        revised = enrich(raw)
        result = review(self.path, self.settings, revised.iloc[:15], revised.iloc[15:],
                        dict(label='trend_up', indicators={}), self.mutator)
        self.assertEqual(result, 'all symbols quarantined')
        with db(self.path) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM development_batches').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT count(*) FROM bar_quarantine').fetchone()[0], 1)

if __name__ == '__main__':
    unittest.main()
