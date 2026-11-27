import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import patch

import numpy as np
import pandas as pd

from config import RiskConfig
from learning_loop import Ledger
from learning_runtime import LearningRecoveryRequired, observe_new_bars
from market_observations import record_observation
from market_time import aggregate_equity_minutes, equity_sessions
from paper_store import db, get, init
from regime_detector import point_in_time_regimes
from review_progress import advance_review, review_marks


def instant(s):
    return int(pd.Timestamp(s).timestamp())


class MarketTimeTests(unittest.TestCase):
    def test_holiday_early_close_and_dst(self):
        days = equity_sessions(date(2025, 11, 27), date(2025, 11, 29))
        self.assertEqual([d.label for d in days], ["2025-11-28"])
        self.assertEqual(days[0].opened, instant("2025-11-28T14:30Z"))
        self.assertEqual(days[0].cuts[-1], instant("2025-11-28T18:00Z"))
        self.assertEqual(days[0].cuts[-1] - days[0].cuts[-2], 1800)
        sessions = equity_sessions(date(2025, 3, 7), date(2025, 3, 11))
        self.assertEqual(sessions[0].opened, instant("2025-03-07T14:30Z"))
        self.assertEqual(sessions[1].opened, instant("2025-03-10T13:30Z"))

    def minutes(self):
        timestamps = pd.date_range("2025-11-28T14:29Z", "2025-11-28T18:00Z", freq="min")
        index = pd.MultiIndex.from_product([["AAPL"], timestamps], names=["symbol", "timestamp"])
        x = np.arange(len(index), dtype=float) + 1
        return pd.DataFrame(dict(open=x, high=x+1, low=x, close=x+0.25, volume=2), index=index)

    def test_minute_aggregation_session_alignment_and_partial_cut(self):
        minute = self.minutes()
        result = aggregate_equity_minutes(minute, "2025-11-28T18:01Z").xs("AAPL")
        self.assertEqual(result.index[0], pd.Timestamp("2025-11-28T15:30Z"))
        self.assertEqual(list(result.volume), [120, 120, 120, 60])
        self.assertEqual(list(result.open), [2, 62, 122, 182])
        self.assertEqual(result.iloc[-1].close, 211.25)

    def test_missing_duplicate_future_and_boolean_input(self):
        minute = self.minutes()
        missing = aggregate_equity_minutes(minute.drop(minute.index[5]), "2025-11-28T17:29Z")
        self.assertEqual(list(missing.index.get_level_values('timestamp')), [pd.Timestamp("2025-11-28T16:30Z")])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            aggregate_equity_minutes(pd.concat([minute, minute.iloc[:1]]), "2025-11-28T18:01Z")
        with self.assertRaisesRegex(ValueError, "boolean"):
            aggregate_equity_minutes(minute.assign(volume=True), "2025-11-28T18:01Z")
        with self.assertRaises(ValueError):
            aggregate_equity_minutes(minute, "2025-11-28")

    def test_regime_fixed_anchor_confirmation_gap_and_prefix(self):
        days = equity_sessions(date(2025, 1, 2), date(2025, 4, 1))
        stamps = pd.to_datetime([c for d in days for c in d.cuts], unit="s", utc=True)
        idx = pd.MultiIndex.from_product([["SPY"], stamps], names=["symbol", "timestamp"])
        x = 100 + np.arange(len(idx)) * 0.01
        f = pd.DataFrame(dict(close=x, high=x+0.2, low=x-0.2, interval_kind="XNYS_1m"), index=idx)
        labels, summary = point_in_time_regimes(f)
        self.assertTrue(labels.iloc[:301].eq('unknown').all())
        self.assertEqual(labels.iloc[301], 'trend_up')
        self.assertEqual(summary['indicators']['anchor'], 'SPY')
        pd.testing.assert_series_equal(labels.iloc[:305], point_in_time_regimes(f.iloc[:305])[0])
        self.assertTrue(point_in_time_regimes(f.drop(f.index[303]))[0].iloc[303:].eq("unknown").all())
        self.assertTrue(point_in_time_regimes(f.rename(index={'SPY':'QQQ'}))[0].eq("unknown").all())


class LearningRuntimeTests(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.path = init(Path(td.name) / "paper.sqlite")
        self.side = self.path.with_name("learning_loop.sqlite")
        self.settings = N(universe=N(equities=["AAPL", "SPY"], cryptos=["BTC/USD"]),
                          risk=RiskConfig(10, 1, 0.50))
        self.t = instant("2025-11-28T13:00Z")

    def tick(self):
        return observe_new_bars(self.path, self.settings, clock=lambda: self.t)

    def facts(self, until, since=date(2025, 8, 1)):
        rows = equity_sessions(since, date(2025, 11, 29))
        with db(self.path, True) as c:
            i = 0
            for day in rows:
                for a, b in zip((day.opened, *day.cuts[:-1]), day.cuts):
                    if b > until:
                        return
                    i += 1
                    x = 100.0 + np.sin(i * .1)
                    start, end = [pd.Timestamp(v, unit='s', tz='UTC') for v in (a,b)]
                    record_observation(c, "AAPL", end.isoformat(), dict(open=x, high=x+1, low=x-1,
                        close=x+0.1, volume=950+i, interval_start=start, interval_end=end,
                        interval_kind="XNYS_1m", feed="fixture", received_at=end.isoformat()))

    def test_restart_replays_each_cut_once_independent_of_all_review_marks(self):
        self.assertEqual(self.tick()['lifecycle'], "initialized")
        self.t = instant("2025-11-28T15:30:10Z")
        self.facts(self.t + 3600)
        with db(self.path, True) as c:
            c.execute("INSERT INTO watermarks(symbol,t) VALUES('AAPL','2099-01-01')")
            for scope in ["trend_up", "trend_down", "range_quiet", "vol_expansion"]:
                advance_review(c, scope, "AAPL", "2099-01-01")
        self.assertEqual(self.tick()['consumed'], 1)
        self.assertEqual(self.tick()['consumed'], 0)
        self.t += 3600
        self.assertEqual(self.tick()['consumed'], 1)
        with closing(Ledger(self.side)) as ledger:
            bodies = ledger.db.execute("SELECT body FROM ll_bars ORDER BY cut").fetchall()
            self.assertEqual(len(bodies), 2)
            self.assertIsNone(json.loads(bodies[0][0])["AAPL"]["entry_ok"])
        with db(self.path) as c:
            self.assertEqual(get(c, "learning_consumed_seq"), "2")

    def test_missing_history_is_a_diagnostic_and_can_be_repaired(self):
        self.tick()
        self.t = instant("2025-11-28T15:30:10Z")
        self.assertEqual(self.tick()["missing_batch"], 1)
        self.facts(self.t, date(2025, 11, 28))
        self.assertEqual(self.tick()["features_unavailable"], 1)
        self.facts(self.t - 86400)
        self.assertEqual(self.tick()["consumed"], 1)

    def test_crash_between_sidecar_write_and_receipt_reuses_frozen_batch(self):
        self.tick()
        self.t = instant("2025-11-28T15:30:10Z")
        self.facts(self.t)
        def crash(ledger, cut, batch, **kwargs):
            ledger.bar(cut, batch)
            raise RuntimeError("injected fault")
        with patch('learning_runtime.close_tick', side_effect=crash), self.assertRaises(RuntimeError):
            self.tick()
        self.assertEqual(self.tick()["consumed"], 1)
        self.assertEqual(self.tick()["consumed"], 0)

    def test_rollover_preserves_halt_history_and_immutable_observations(self):
        self.tick()
        self.t = instant("2025-11-28T15:30:10Z")
        self.facts(self.t)
        self.tick()
        with closing(Ledger(self.side)) as ledger:
            ledger.halt("fixture halt")
        self.t = instant("2026-01-05T10:00Z")
        self.assertEqual(self.tick()['lifecycle'], 'rolled_over')
        with closing(Ledger(self.side)) as ledger:
            self.assertTrue(ledger._get("halted"))
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM ll_bars").fetchone()[0], 1)
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM ll_events WHERE kind='campaign_closed'").fetchone()[0], 1)
        self.assertEqual(self.tick()['lifecycle'], 'resumed')

    def test_restore_detects_stale_or_missing_sidecar(self):
        self.tick()
        snapshot = self.path.with_name("backup.sqlite")
        with closing(sqlite3.connect(self.side)) as src, closing(sqlite3.connect(snapshot)) as dst:
            src.backup(dst)
        self.t = instant("2025-11-28T15:30:10Z")
        self.facts(self.t)
        self.tick()
        with closing(sqlite3.connect(snapshot)) as src, closing(sqlite3.connect(self.side)) as dst:
            src.backup(dst)
        with self.assertRaisesRegex(LearningRecoveryRequired, "regressed"):
            self.tick()
        self.side.unlink()
        with self.assertRaisesRegex(LearningRecoveryRequired, "missing"):
            self.tick()
        self.assertFalse(self.side.exists())

    def test_independent_review_progress_and_rollback(self):
        for scope in ["trend_up", "trend_down", "range_quiet", "vol_expansion"]:
            with db(self.path, True) as c:
                self.assertEqual(review_marks(c, scope), {})
                advance_review(c, scope, "AAPL", "2025-11-28T15:30:00+00:00")
        with self.assertRaises(RuntimeError), db(self.path, True) as c:
            advance_review(c, "trend_up", "AAPL", "2025-11-28T16:30:00+00:00")
            raise RuntimeError
        with db(self.path) as c:
            self.assertEqual(review_marks(c, "trend_up"), {"AAPL":"2025-11-28T15:30:00+00:00"})
