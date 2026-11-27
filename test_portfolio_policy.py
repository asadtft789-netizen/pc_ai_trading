import unittest
from decimal import Decimal as D

import pandas as pd
from backtester import Backtester
from config import Settings
from portfolio_policy import allocated_targets, portfolio_entry_budget, protection_prices


class PortfolioPolicyTests(unittest.TestCase):
    def test_shared_cash_and_stable_tie_break(self):
        dates = pd.date_range("2026-01-02",periods=4,freq="h",tz="UTC")
        idx = pd.MultiIndex.from_product([["MSFT","AAPL"],dates],names=["symbol","timestamp"])
        bars = pd.DataFrame({"signal":[1,1,0,0]*2,"open":[100,100,101,101]*2,
                             "close":[100,100,101,101]*2},index=idx)
        replay = Backtester(Settings.load()).run(None,bars)
        self.assertEqual([t["symbol"] for t in replay["trades"]], ["AAPL"])
        self.assertEqual(replay["trades"][0]["entry_time"], dates[1].isoformat())

    def test_missing_held_mark_aborts_replay(self):
        with self.assertRaisesRegex(ValueError,"held-instrument"):
            allocated_targets(pd.DataFrame({"AAPL":[1,float("nan")],"MSFT":[1,1]}))

    def test_stop_target_and_caps(self):
        self.assertEqual(protection_prices(D("50"),D("0.25")), (D("49.75"),D("50.50")))
        self.assertEqual(portfolio_entry_budget(D(10),D("0.60"),D(1),D(1)), D("0.60"))
        self.assertEqual(portfolio_entry_budget(D(1000),D(1000),D(100),D(100)), D(1))

    def test_frozen_shadow_campaign_preserves_target_semantics(self):
        from dataclasses import replace
        from learning_loop import Bar, Portfolio, fixture
        from example_hourly_run import risk
        decision = Bar(100.,100.,100.,100.,4095,1.,True,1.)
        target = replace(decision,h=103.)
        for revision, expected in ((1,False),(2,True)):
            model = Portfolio(fixture(),replace(risk(),policy_revision=revision),0)
            model.step({"AAPL":decision},True,True,False,True)
            result = model.step({"AAPL":target},False,True,False,True)
            self.assertEqual(("AAPL","target") in result.actions,expected)
            self.assertEqual(result.flat,expected)
