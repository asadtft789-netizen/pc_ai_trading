"""Black-box replay against the funded, fenced supervisor and its SQLite projections."""
import json
import os
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from backtester import Backtester
from config import Settings
from dashboard.paper_view import read_view
from executor import StrategyExecutor
from hourly_features import ALL_READY, FEATURE_VERSION
from learning_loop import Ledger
from learning_runtime import bind_store
from market_observations import record_observation
from paper_order_policy import EntryEvidence
from paper_store import db, init, key, put
from portfolio_events import Decision, ExecutionPolicy, MarketFrame, Quote
from strategy_worker import FEATURES
from test_hourly import Fake


SECOND = timedelta(seconds=1)
HOUR = timedelta(hours=1)
OPEN = datetime(2026, 11, 27, 14, 30, tzinfo=timezone.utc)
CLOSE = OPEN + 3 * HOUR + timedelta(minutes=30)  # XNYS Thanksgiving Friday.
OWNER = "frozen-entry-policy"
FEE = D(".002")


def decision(symbol, end, target=1):
    return Decision(symbol, OWNER, end - HOUR, end, end, end, target, D("1"))


def quote(symbol, at, bid, ask):
    return Quote(symbol, at, D(bid), D(ask))


class TapeBroker(Fake):
    """Only the test supplies quotes, evidence, and eventual execution reports."""

    def advance(self, frame):
        self.frame = frame
        self.quotes = {q.symbol: q for q in frame.quotes}
        self.decisions.update({(d.symbol, d.end.isoformat()): d for d in frame.decisions})

    def __init__(self):
        super().__init__()
        self.decisions = {}

    def quote(self, symbol):
        q = self.quotes[symbol]
        return q.bid, q.ask, q.at.isoformat()

    def session_open(self, symbol):
        return self.frame.session_open.isoformat()

    def session_close(self, symbol):
        return self.frame.session_close.isoformat()

    def entry_evidence(self, symbol, decision):
        d = self.decisions[symbol, decision]
        return EntryEvidence(
            "acct", symbol, d.start.isoformat(), d.end.isoformat(),
            self.session_open(symbol), self.session_close(symbol),
            d.published.isoformat(), d.decided.isoformat(), D(0), D(1),
            True, True, self.quotes[symbol].entry_ok, d.ready,
        )


def prepare_store(path):
    init(path)
    with closing(Ledger(path.with_name("learning_loop.sqlite"))) as ledger:
        bind_store(path, ledger)
        with ledger.tx():
            ledger._set("halted", False)
    with db(path, True) as c:
        put(c, "integrity_entries_paused", "NO")
        put(c, "entry_gate_trend_up", OWNER)
        c.execute("""INSERT INTO strategies(id,status,regime,born)
                      VALUES(?,'champion','trend_up',?)""", (OWNER, OPEN.isoformat()))


def publish_decisions(path, decisions):
    with db(path, True) as c:
        for d in decisions:
            end = d.end.isoformat()
            record_observation(c, d.symbol, end, dict(
                open=100, high=102, low=98, close=100, volume=1000, feed="fixture",
                interval_kind="XNYS_1m", interval_start=d.start, interval_end=d.end,
                received_at=d.published.isoformat(),
            ))
            c.execute("""INSERT INTO decisions(symbol,t,strategy,target,atr,regime)
                         VALUES(?,?,?,?,?,'trend_up')""",
                      (d.symbol, end, d.strategy, d.target, str(d.atr)))
            c.execute("""INSERT INTO watermarks(symbol,t) VALUES(?,?)
                         ON CONFLICT(symbol) DO UPDATE SET t=excluded.t""", (d.symbol, end))
            c.execute("""INSERT INTO decision_snapshots(
                         symbol,t,strategy,lot,regime,feature_version,readiness_mask,
                         history_ready,flags,rsi,received_at,content_hash)
                         SELECT symbol,t,?,?,'trend_up',?,?,1,?,50,received_at,content_hash
                         FROM usable_bars WHERE symbol=? AND t=?""",
                      (d.strategy, key("lot", d.symbol, end), FEATURE_VERSION, ALL_READY,
                       json.dumps(dict.fromkeys(FEATURES, True)), d.symbol, end))


def early_close_tape():
    end = OPEN + HOUR
    next_end = end + HOUR
    entry_at = end + 15 * SECOND
    frames = []

    def poll(at, quotes, decisions=()):
        frames.append(MarketFrame(at, OPEN, CLOSE, tuple(decisions), tuple(quotes)))

    # MSFT arrives first in both the SQLite insert order and the quote cohort.
    symbols = ("MSFT", "AAPL")
    poll(end, [quote(s, end, "100", "100") for s in symbols],
         [decision(s, end) for s in symbols])
    poll(end + 14 * SECOND, [
        quote("MSFT", end + SECOND, "100", "100"),       # 13 seconds stale
        quote("AAPL", end + 14 * SECOND, "99", "100"),    # 100+ bps wide
    ])
    poll(entry_at, [quote(s, entry_at, "99.90", "100") for s in symbols])
    poll(entry_at + SECOND, [quote("AAPL", entry_at + SECOND, "101", "101.10")])
    # The owner goes flat at next_end. A quote at the same cut cannot fill that exit.
    poll(next_end, [quote("AAPL", next_end, "101.5", "101.6")],
         [decision("AAPL", next_end, 0), decision("MSFT", next_end)])
    poll(next_end + SECOND, [quote("AAPL", next_end + SECOND, "101.5", "105")])
    poll(next_end + 2 * SECOND, [quote("MSFT", next_end + 2 * SECOND, "49.99", "50")])
    # At an early close, the five-minute liquidation window starts at 17:55 UTC.
    poll(CLOSE - timedelta(minutes=5, seconds=1),
         [quote("MSFT", CLOSE - timedelta(minutes=5, seconds=1), "50.5", "50.51")])
    poll(CLOSE - timedelta(minutes=5),
         [quote("MSFT", CLOSE - timedelta(minutes=5), "50.5", "52")])
    return frames


class PortfolioParity(unittest.TestCase):
    def setUp(self):
        self.settings = Settings.load()
        self.settings = replace(self.settings, risk=replace(
            self.settings.risk, commission_per_order_usd=float(FEE)))
        self.policy = ExecutionPolicy(fee_per_order=FEE)
        self.stack = patch.dict(os.environ, {"PAPER_ENTRIES_ENABLED": "YES"})
        self.stack.start()
        self.addCleanup(self.stack.stop)

    def exercise(self, tape, expected):
        """Compare each prefix against the broker journal and an independent expected path."""
        from shadow_campaign import replay_shadow
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "paper.sqlite"
            prepare_store(path)
            broker = TapeBroker()
            actual = []
            for n, frame in enumerate(tape, 1):
                with self.subTest(poll=n, at=frame.at):
                    broker.advance(frame)
                    publish_decisions(path, frame.decisions)
                    # A fresh supervisor reconstructs state from durable broker facts at every poll.
                    executor = StrategyExecutor(path, broker, self.settings, clock=lambda: frame.at)
                    offset = len(broker.submits)
                    result = executor.sync_positions()
                    self.assertNotIn("unknown", result)
                    for cid in broker.submits[offset:]:
                        order = broker.lookup(cid)
                        q = broker.quotes[order.symbol]
                        price = q.ask if order.side == "buy" else q.bid
                        broker.fill(cid, order.qty, price, frame.at.isoformat())
                    _, reconciled = executor.reconcile_trades()
                    self.assertTrue(reconciled)
                    with db(path) as c:
                        rows = c.execute("""SELECT i.symbol,i.side,f.qty,f.price,f.at
                                          FROM fills f JOIN intents i ON i.id=f.intent
                                          ORDER BY f.at,f.id""")
                        actual = [(s, side, D(q), D(p), datetime.fromisoformat(at))
                                  for s, side, q, p, at in rows]
                    required_fills = [(s, side, D(q), D(p), tape[index].at)
                                      for index, s, side, q, p in expected if index < n]
                    self.assertEqual(actual, required_fills)
                    metrics = read_view("metrics", path=path)
                    for run in (Backtester(self.settings).run_tape, replay_shadow):
                        replay = run(tape[:n], self.policy)
                        self.assertEqual([(f.symbol, f.side, f.qty, f.price, f.at)
                                          for f in replay.fills], actual)
                        for field, attr in (
                            ("allocated_cash", "cash"), ("reserved_cash", "reserved"),
                            ("available_cash", "available"), ("realized_gross", "realized"),
                            ("unrealized_gross", "unrealized"), ("marked_allocated_equity", "equity"),
                            ("confirmed_fees", "confirmed_fees"), ("estimated_fees", "estimated_fees"),
                            ("peak_equity", "peak"), ("drawdown", "drawdown"),
                        ):
                            self.assertEqual(D(metrics[field]), getattr(replay.account, attr), field)
                        for flag in ("daily_halt", "drawdown_halt"):
                            self.assertIs(metrics[flag], getattr(replay.account, flag), flag)
            return actual, metrics

    def test_competing_entries_owner_exit_and_early_close(self):
        tape = early_close_tape()
        expected = [(2, "AAPL", "buy", ".01", "100"), (5, "AAPL", "sell", ".01", "101.5"),
                    (6, "MSFT", "buy", ".02", "50"), (8, "MSFT", "sell", ".02", "50.5")]
        fills, metrics = self.exercise(tape, expected)
        self.assertEqual(D(metrics["realized_gross"]), D(".025"))
        self.assertEqual(D(metrics["estimated_fees"]), D(".008"))
        self.assertEqual(D(metrics["marked_allocated_equity"]), D("10.017"))
        # Replay in a separate operational DB with reversed input enumeration.
        reverse = [replace(f, decisions=f.decisions[::-1], quotes=f.quotes[::-1]) for f in tape]
        self.assertEqual(self.exercise(reverse, expected)[0], fills)

    def test_adverse_gap_and_favorable_gap_use_observed_bid(self):
        for exit_bid in ("92", "107"):
            with self.subTest(exit_bid=exit_bid):
                end = OPEN + HOUR
                tape = [
                    MarketFrame(end, OPEN, CLOSE, (decision("AAPL", end),), ()),
                    MarketFrame(end + SECOND, OPEN, CLOSE, (),
                                (quote("AAPL", end + SECOND, "99.90", "100"),)),
                    MarketFrame(end + 2 * SECOND, OPEN, CLOSE, (),
                                (quote("AAPL", end + 2 * SECOND, exit_bid, "108"),)),
                ]
                expected = [(1, "AAPL", "buy", ".01", "100"),
                            (2, "AAPL", "sell", ".01", exit_bid)]
                self.exercise(tape, expected)

    def test_broker_partial_slippage_cancel_and_late_fee_are_evidence(self):
        end = OPEN + HOUR
        d = decision("AAPL", end)
        at = end + SECOND
        f = MarketFrame(at, OPEN, CLOSE, (d,), (quote("AAPL", at, "99.9", "100"),))
        simulated = Backtester(self.settings).run_tape([f], self.policy)
        self.assertEqual(simulated.account.qty, D(".01"))
        self.assertEqual(simulated.account.equity, D("9.997"))

        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "paper.sqlite"
            prepare_store(path)
            publish_decisions(path, [d])
            broker = TapeBroker()
            broker.advance(f)
            supervisor = StrategyExecutor(path, broker, self.settings, clock=lambda: at)
            self.assertEqual(supervisor.sync_positions(), "entry submitted")
            self.assertTrue(supervisor.reconcile_trades()[1])

            def money(field):
                return D(read_view("metrics", path=path)[field])

            self.assertEqual(money("reserved_cash"), D("1.002"))
            self.assertEqual(money("marked_allocated_equity"), D(10))
            cid, = broker.submits
            broker.fill(cid, ".004", 101, at.isoformat())
            self.assertTrue(supervisor.reconcile_trades()[1])
            self.assertEqual(money("reserved_cash"), D(".6012"))
            self.assertEqual(money("estimated_fees"), D(".0008"))
            self.assertEqual(money("marked_allocated_equity"), D("9.9948"))

            at += SECOND
            broker.orders[cid].status = "canceled"
            broker.activity[0]["fee"] = ".003"
            held, ok = supervisor.reconcile_trades()
            self.assertTrue(ok)
            self.assertEqual(D(held[0]["qty"]), D(".004"))
            self.assertEqual(money("reserved_cash"), D(0))
            self.assertEqual(money("confirmed_fees"), D(".003"))
            self.assertEqual(money("estimated_fees"), D(0))
            self.assertEqual(money("marked_allocated_equity"), D("9.9926"))
            self.assertTrue(supervisor.reconcile_trades()[1])
            self.assertEqual(money("confirmed_fees"), D(".003"))

    def test_gap_latches_drawdown_across_restart_and_new_session(self):
        end = OPEN + HOUR
        at = end + SECOND
        next_open = OPEN + timedelta(days=3)  # Monday after Thanksgiving.
        next_end = next_open + HOUR
        tape = [
            MarketFrame(at, OPEN, CLOSE, (decision("AAPL", end),),
                        (quote("AAPL", at, "99.9", "100"),)),
            MarketFrame(at + SECOND, OPEN, CLOSE, (),
                        (quote("AAPL", at + SECOND, ".1", "1"),)),
            MarketFrame(next_end + SECOND, next_open, next_open + timedelta(hours=6.5),
                        (decision("MSFT", next_end),),
                        (quote("MSFT", next_end + SECOND, "50", "50"),)),
        ]
        expected = [(0, "AAPL", "buy", ".01", "100"), (1, "AAPL", "sell", ".01", ".1")]
        _, metrics = self.exercise(tape, expected)
        self.assertEqual(D(metrics["marked_allocated_equity"]), D("8.997"))
        self.assertIs(metrics["drawdown_halt"], True)
        self.assertIs(metrics["daily_halt"], False)
