import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dashboard.paper_view import read_view
from dashboard.strategy_status import strategy_status
from operational import write_status
from paper_store import db, init, now


class StrategyStatusTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.path = Path(work.name) / "status.json"
        self.revision = "a" * 40
        self.candidate = "b" * 64
        self.protocol = "c" * 64
        self.certificate = "d" * 64
        self.report = dict(format=1, revision=self.revision, as_of=now(), generation=3,
                           routes={"global": self.candidate, "range_quiet": None},
                           validated=[dict(candidate=self.candidate, certificate=self.certificate,
                                           protocol=self.protocol)])

    def test_f16_hold_does_not_promote_or_hide_observed_routing(self):
        write_status(self.path, self.report)
        with patch("dashboard.strategy_status.release_policy", return_value=("F16_hold", frozenset())):
            status = strategy_status(self.path, self.revision)
        self.assertEqual(status["evidence"], "F16_hold")
        self.assertEqual(status["validated_strategies"], [])
        self.assertEqual(status["routing"]["state"], "observed")
        self.assertEqual(status["routing"]["routes"], self.report["routes"])

    def test_released_validation_is_distinct_from_an_active_route(self):
        self.report["routes"] = {"global": None}
        write_status(self.path, self.report)
        with patch("dashboard.strategy_status.release_policy", return_value=("review", {self.protocol})):
            status = strategy_status(self.path, self.revision)
        self.assertEqual(status["validated_strategies"],
                         [{"candidate": self.candidate, "certificate": self.certificate}])
        self.assertEqual(status["routing"]["routes"], {"global": None})

    def test_absent_stale_mismatched_and_writable_projections_remain_unknown(self):
        self.assertEqual(strategy_status(self.path, self.revision)["routing"]["state"], "unknown")
        write_status(self.path, {**self.report, "as_of": "2000-01-01T00:00:00+00:00"})
        self.assertEqual(strategy_status(self.path, self.revision)["routing"]["state"], "unknown")
        write_status(self.path, self.report)
        self.assertEqual(strategy_status(self.path, "e" * 40)["routing"]["state"], "unknown")
        self.path.chmod(0o662)
        self.assertEqual(strategy_status(self.path, self.revision)["routing"]["state"], "unknown")

    def test_legacy_champion_is_exploratory_and_cannot_fake_a_route(self):
        path = init(self.path.parent / "paper.sqlite")
        with db(path, True) as connection:
            connection.execute("INSERT INTO strategies(id,name,generation,status) VALUES(?,?,?,?)",
                               ("legacy", "Legacy champion", 1, "champion"))
        with patch.dict(os.environ, {"PHOENIX_ROUTING_STATUS_FILE": str(self.path),
                                     "PHOENIX_REVISION": self.revision}):
            report = read_view("health", path=path)
            strategies = read_view("strategies", path=path)
        self.assertEqual(report["routing"]["state"], "unknown")
        self.assertEqual(report["validated_strategies"], [])
        self.assertEqual(report["entries"], "unreleased")
        self.assertEqual(strategies[0]["evidence_class"], "exploratory")
