import unittest
from datetime import date, timedelta

import numpy as np
from campaign_statistics import BLOCK_LENGTHS, CAMPAIGN_ALPHA, RESAMPLES, holm, paired_result


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.days = tuple(date(2025,1,1) + timedelta(days=i) for i in range(30))
        self.cash = dict.fromkeys(self.days,0.)

    def compare(self, values, episodes=20):
        return paired_result(self.days, values, self.cash, self.cash, 10, episodes, 10, 771)

    def test_missing_and_flat_are_inconclusive(self):
        self.assertEqual(self.compare({})["reason"], "incomplete_calendar")
        self.assertEqual(self.compare(self.cash)["reason"], "degenerate_series")
        self.assertEqual(self.compare(self.cash,19)["reason"], "sparse_comparison")
        self.assertEqual(self.compare({**self.cash,self.days[0]:float('nan')})["status"], "invalid")

    def test_null_with_serial_dependence_and_alternative(self):
        values = np.repeat([-0.01,0.01,-0.01,0.01,-0.01,0.01],5)
        result = self.compare(dict(zip(self.days,values)))
        self.assertGreater(result["p"], CAMPAIGN_ALPHA)
        positive = self.compare(dict(zip(self.days,values + 0.10)))
        self.assertEqual(positive["resamples"], RESAMPLES)
        self.assertEqual(tuple(positive["block_p"]), BLOCK_LENGTHS)
        self.assertEqual(positive["p"], 1/(RESAMPLES+1))
        self.assertAlmostEqual(positive["mean_increment"],.01)

    def test_noise_allowance_and_full_family(self):
        pnl = dict(zip(self.days, np.linspace(-.02,.02,30)))
        result = paired_result(self.days,pnl,self.cash,dict.fromkeys(self.days,2),10,20,12,1234)
        self.assertAlmostEqual(result["mean_increment"],-.00004)
        self.assertEqual(result["p"],max(result["block_p"].values()))
        self.assertEqual(holm(["cash","incumbent","ablation"],
            {"cash":{"status":"measured","p":.01},"incumbent":{"status":"measured","p":.012}}),
            {"cash":.03,"incumbent":.03,"ablation":1.})
        with self.assertRaises(ValueError):
            holm(["cash"],{"other":{}})
