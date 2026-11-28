"""Historical evaluation at recorded decision and quote availability times."""
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pandas as pd

from backtester import Backtester
from config import Settings
from hourly_features import ALL_READY
from portfolio_events import MarketFrame, Quote


class HistoricalTapeTests(unittest.TestCase):
    def setUp(self):
        self.open = datetime(2026, 11, 24, 14, 30, tzinfo=timezone.utc)
        self.close = self.open + timedelta(hours=6, minutes=30)
        self.cut = self.open + timedelta(hours=1)
        self.decided = self.cut + timedelta(seconds=6)
        settings = Settings.load()
        self.bt = Backtester(replace(settings, risk=replace(
            settings.risk, commission_per_order_usd=.003)))
        self.rows = pd.DataFrame([
            dict(symbol=symbol, timestamp=self.cut, signal=1, atr=1,
                 history_ready=True, readiness_mask=ALL_READY, interval_kind='XNYS_1m',
                 interval_start=self.open, interval_end=self.cut,
                 received_at=self.cut + timedelta(seconds=1), decided_at=self.decided)
            for symbol in ('MSFT', 'AAPL')
        ]).set_index(['symbol', 'timestamp'])

    def frame(self, seconds, bid, ask, *, quote_seconds=None):
        instant = self.cut + timedelta(seconds=seconds)
        qtime = instant if quote_seconds is None else self.cut + timedelta(seconds=quote_seconds)
        quotes = tuple(Quote(symbol, qtime, D(bid), D(ask)) for symbol in ('MSFT', 'AAPL'))
        return MarketFrame(instant, self.open, self.close, quotes=quotes)

    def test_observed_run_joins_asof_and_uses_shared_policy(self):
        frames = [
            self.frame(1, '99', '99'),                      # before strategy decision
            self.frame(6, '99', '99', quote_seconds=5),    # cached predecision quote
            self.frame(7, '98', '100'),                    # entry spread too wide
            self.frame(20, '99.99', '100', quote_seconds=8),  # stale
            self.frame(21, '99.99', '100'),                # first eligible quote
            self.frame(22, '97.5', '102'),                 # gap stop, wide spread allowed
        ]
        result = self.bt.run('candidate', self.rows, quote_tape=iter(frames))
        self.assertEqual(result['execution_model'], 'observed_quote_simulation_v3')
        self.assertEqual(result['total_trades'], 1)
        trade, = result['trades']
        self.assertEqual(trade['symbol'], 'AAPL')
        self.assertEqual(trade['entry_time'], frames[4].at.isoformat())
        self.assertEqual((trade['entry_price'], trade['exit_price']), (100, 97.5))
        self.assertEqual(D(result['gross_realized']), D('-.025'))
        self.assertEqual(D(result['estimated_fees']), D('.006'))
        self.assertEqual(D(result['marked_equity']), D('9.969'))

    def test_gap_in_held_bid_leaves_endpoint_unresolved(self):
        frames = [self.frame(7, '100', '100'),
                  MarketFrame(self.close, self.open, self.close)]
        result = self.bt.run('candidate', self.rows, quote_tape=frames)
        self.assertEqual(result['_reason'], 'unresolved_exposure')
        self.assertIsNone(result['marked_equity'])
        self.assertIsNone(result['pnl'])
        self.assertEqual(result['total_trades'], 0)

    def test_session_data_never_falls_back_to_ohlc_proxy(self):
        result = self.bt.run('candidate', self.rows)
        self.assertEqual(result['_reason'], 'missing_quote_tape')
        self.assertEqual(result['total_trades'], 0)
        rows = self.rows.drop(columns='decided_at')
        with self.assertRaisesRegex(ValueError, 'missing_decision_evidence'):
            self.bt.run('candidate', rows, quote_tape=[self.frame(7, '100', '100')])

    def test_warmup_and_late_publication_abstain(self):
        for rows in (
            self.rows.assign(history_ready=False),
            self.rows.assign(readiness_mask=ALL_READY - 1),
            self.rows.assign(received_at=self.cut + timedelta(seconds=121),
                             decided_at=self.cut + timedelta(seconds=123)),
        ):
            with self.subTest(rows=rows):
                result = self.bt.run('candidate', rows, quote_tape=[self.frame(124, '100', '100')])
                self.assertEqual(result['_reason'], 'zero_trades')
                self.assertEqual(D(result['marked_equity']), D('10'))

    def scenario(self, mids, signals):
        rows = []
        for i, ((first, last), target) in enumerate(zip(mids, signals, strict=True)):
            start = self.open + timedelta(hours=i)
            cut = min(start + timedelta(hours=1), self.close)
            rows.append(dict(symbol='AAPL', timestamp=cut, signal=target, atr=1.,
                             history_ready=True, readiness_mask=ALL_READY, open=first, close=last,
                             interval_kind='XNYS_1m', interval_start=start, interval_end=cut))
        return pd.DataFrame(rows).set_index(['symbol', 'timestamp'])

    def test_session_ohlc_models_an_explicit_later_open_and_takes_gap_bid(self):
        self.bt.r = replace(self.bt.r, spread_bps=0, slippage_bps=0)
        bars = self.scenario([(103, 107), (100, 95)], [1, 1])
        result = self.bt.run('candidate', bars)
        self.assertEqual(result['execution_model'], 'exploratory_session_ohlc_quote_proxy_v3')
        self.assertIn('availability', result['execution_assumptions'])
        trade, = result['trades']
        self.assertEqual(trade['entry_time'], (self.cut + timedelta(seconds=1)).isoformat())
        self.assertEqual((trade['entry_price'], trade['exit_price'], trade['exit_reason']), (100, 95, 'stop'))
        self.assertEqual(D(result['marked_equity']), D('9.944'))

    def test_proxy_close_boundary_and_missing_held_bar(self):
        self.bt.r = replace(self.bt.r, spread_bps=0, slippage_bps=0)
        bars = self.scenario([(100, 100)] * 7, [1] * 7)
        result = self.bt.run('candidate', bars)
        trade, = result['trades']
        self.assertEqual(trade['exit_time'], self.close.isoformat())
        self.assertEqual(trade['exit_reason'], 'session_close')
        incomplete = bars.drop(bars.index[3])
        self.assertEqual(self.bt.run('candidate', incomplete)['_reason'], 'missing_held_observation')

    def test_proxy_abstains_on_non_executable_spread_or_rounding(self):
        bars = self.scenario([(100, 100)] * 3, [1, 1, 0])
        for spread in (21, 5):
            self.bt.r = replace(self.bt.r, spread_bps=spread, slippage_bps=0)
            result = self.bt.run('candidate', bars)
            self.assertEqual(result['_reason'], 'zero_trades')
            self.assertEqual(D(result['marked_equity']), D(10))
