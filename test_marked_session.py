import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import patch

from learning_loop import digest
from marked_session import SessionTape, replay_marked_session
from portfolio_events import Decision, MarketFrame, Quote


def build_tape(opened, spec, trade):
    opening = datetime.fromtimestamp(opened, timezone.utc)
    close = opening + timedelta(hours=3, minutes=30)
    cut = opening + timedelta(hours=1)
    first_quote = cut + timedelta(seconds=2, microseconds=500)
    extra = [(first_quote, 100), (first_quote + timedelta(seconds=1), 100),
             (first_quote + timedelta(seconds=2), 98)] if trade else []
    scheduled = [(opening + timedelta(hours=i), 100) for i in range(4)] + [(close, 100)]
    frames = tuple(MarketFrame(at, opening, close,
                               quotes=(Quote("AAPL", at, D(price), D(price)),))
                   for at, price in sorted(scheduled + extra))
    signals = {cut: (Decision("AAPL", digest(spec), opening, cut, cut, cut, 1, D(1)),)} if trade else {}
    return SessionTape(frames, {digest(spec): signals}, ((opened, "trend_up"),), D(100), D("101"))


class MarkedSessionTests(unittest.TestCase):
    def setUp(self):
        if importlib.util.find_spec("campaign_registry") is None:
            registry = SimpleNamespace(replay_inputs=lambda m, h: {"fixture_binding": h})
            override = patch.dict(sys.modules, {"campaign_registry": registry})
            override.start()
            self.addCleanup(override.stop)
        self.first = 1795617000
        self.second = self.first + 86400
        self.spec = {"source": "fixture"}
        self.m = dict(scope="equity", common={"universe": ["AAPL"], "base_fill_bps": 1},
                      protocol={"capital": 10}, arms={"candidate": self.spec},
                      calendar=[["d1", self.first, self.first + 12600],
                                ["d2", self.second, self.second + 12600]],
                      scenarios={"candidate": {"delay": {"eligible_quotes": 1},
                                               "cost": {"fill_bps": 4},
                                               "gap": {"adverse_atr": 2}}},
                      dataset="fixture", routing={})
        self.tapes = {"d1": build_tape(self.first, self.spec, True),
                      "d2": build_tape(self.second, self.spec, False)}

    def test_common_grid_cost_scenarios_continuity_and_zero_trade_session(self):
        from campaign_registry import replay_inputs
        p = replay_marked_session(self.m, "d1", self.tapes)
        main = p["portfolios"]["candidate"]
        stress = p["scenarios"]["candidate"]
        self.assertEqual(p["replay_inputs"], replay_inputs(self.m, p["event_digest"]))
        self.assertEqual(p["regime_seconds"], {"trend_up": 12600})
        self.assertAlmostEqual(main["marks"][-1][1], 9.979802)
        self.assertEqual(len(main["episodes"]), 1)
        self.assertEqual(len(main["episodes"][0][-1]), 64)
        self.assertEqual(main["by_symbol"], {"AAPL": main["by_regime"]["trend_up"]})
        self.assertGreater(stress["delay"]["episodes"][0][0], main["episodes"][0][0])
        self.assertLess(stress["cost"]["marks"][-1][1], main["marks"][-1][1])
        self.assertLess(stress["gap"]["marks"][-1][1], main["marks"][-1][1])
        for value in stress.values():
            self.assertEqual([t for t, _ in main["marks"]], [t for t, _ in value["marks"]])
        zero = replay_marked_session(self.m, "d2", self.tapes)
        row = zero["portfolios"]["candidate"]
        self.assertEqual(row["episodes"], [])
        self.assertEqual(row["turnover"], 0)
        self.assertTrue(all(v == main["marks"][-1][1] for _, v in row["marks"]))
        self.assertNotEqual(p["event_digest"], zero["event_digest"])
        self.assertEqual(p, replay_marked_session(self.m, "d1", self.tapes))

    def test_missing_stale_and_changed_source(self):
        tape = self.tapes["d1"]
        frame = tape.frames[0]
        quote, = frame.quotes
        stale = replace(frame, quotes=(replace(quote, at=quote.at - timedelta(seconds=11)),))
        with self.assertRaisesRegex(ValueError, "missing or stale"):
            replay_marked_session(self.m, "d1", {"d1": replace(tape, frames=(stale, *tape.frames[1:]))})
        with self.assertRaisesRegex(ValueError, "missing frozen strategy"):
            replay_marked_session(self.m, "d1", {"d1": replace(tape, decision_streams={})})
        with self.assertRaisesRegex(ValueError, "missing registered"):
            replay_marked_session(self.m, "d2", {"d2": self.tapes["d2"]})
        a = replay_marked_session(self.m, "d1", self.tapes)
        changed = replace(tape, benchmark_close=D(102))
        b = replay_marked_session(self.m, "d1", {"d1": changed})
        self.assertNotEqual(a["event_digest"], b["event_digest"])


    @unittest.skipUnless(importlib.util.find_spec("campaign_registry"), "requires PR #1 registry")
    def test_actual_registry_accepts_and_deduplicates_panel(self):
        from campaign_registry import record_session
        from experiment_store import append, install, needed
        from paper_store import db, init

        manifest = {**self.m, "candidates": {"candidate": {}},
                    "protocol": {**self.m["protocol"], "close_grace_seconds": 120}}
        ident = digest(manifest)
        payload = replay_marked_session(manifest, "d1", self.tapes)
        with tempfile.TemporaryDirectory() as work:
            path = init(Path(work) / "paper.sqlite")
            with db(path, True) as conn:
                install(conn)
                append(conn, "campaign", ident, manifest)
            for offset in (1, 121):
                record_session(path, ident, "d1", payload, clock=lambda: self.first + 12600 + offset)
            with db(path) as conn:
                self.assertEqual(needed(conn, "shadow_session", ident + "/d1")["panel"], payload)
