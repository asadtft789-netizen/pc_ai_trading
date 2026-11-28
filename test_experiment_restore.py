"""Targeted acceptance for backups of the integrated Agent 4 registry."""
from contextlib import closing
from importlib.util import find_spec
from pathlib import Path
import sqlite3
import unittest

from backup_sets import backup, restore
from paper_store import db


@unittest.skipUnless(find_spec("test_experiment_validation"), "requires the Agent 4 integration tree")
class ExperimentRestore(unittest.TestCase):
    def test_restored_registry_retains_consumption_budget_and_immutable_evidence(self):
        from campaign_evidence import verify_eligibility
        from campaign_registry import register
        from dashboard.strategy_status import release_policy
        from statistical_release import BLOCKER
        from experiment_store import append, needed
        from historical_validation import inspect_holdout, register_dataset
        from learning_loop import Ledger, digest
        from learning_runtime import bind_store
        from test_experiment_validation import History, Tournament

        history = History()
        self.addCleanup(history.doCleanups)
        history.setUp()
        tournament = Tournament()
        self.addCleanup(tournament.doCleanups)
        tournament.setUp()
        tournament.path = history.path
        tournament.kw["dataset"] = history.dataset
        path = history.path
        with closing(Ledger(path.with_name("learning_loop.sqlite"))) as learning:
            bind_store(path, learning)

        def interrupted_fit(*args):
            raise RuntimeError("test interruption after holdout consumption")

        family = {digest(history.candidate): history.candidate}
        inspected = inspect_holdout(path, history.dataset, history.frame, family, interrupted_fit, None)
        self.assertEqual(inspected["status"], "inconclusive")
        campaign, frozen, candidate = tournament.start()
        for seed in range(11):
            register(path, **{**tournament.kw, "seed": seed})
        report = tournament.finish(campaign, frozen)
        certificate = report["certificates"][candidate]
        self.assertEqual(release_policy(), (BLOCKER, frozenset()))
        with db(path) as reader:
            identities = list(reader.execute("SELECT kind,id,sha256 FROM experiment_records ORDER BY kind,id"))
            required = {"dataset", "partition", "consumption", "holdout_family", "holdout_result",
                        "campaign", "budget", "decision", "certificate"}
            self.assertTrue(required <= {row[0] for row in identities})

        root = path.parent / "backups"
        root.mkdir()
        class AbortWrite(Exception):
            pass

        with self.assertRaises(AbortWrite):
            with db(path, True) as writer:
                append(writer, "test_pending", campaign, {"visible": False})
                snapshot = backup(path, root, "a" * 40)
                raise AbortWrite()
        target = path.parent / "restore"
        restore(snapshot, target)
        restored = target / "operational.sqlite"
        with db(restored) as reader:
            rows = list(reader.execute("SELECT kind,id,sha256 FROM experiment_records ORDER BY kind,id"))
            self.assertEqual([tuple(row) for row in rows], [tuple(row) for row in identities])
            for kind, key, _ in rows:
                needed(reader, kind, key)  # Agent 4 recomputes the immutable record digest.
            self.assertEqual(sum(row[0] == "consumption" for row in rows), 30)
            self.assertEqual(sum(row[0] == "budget" for row in rows), 12)

        self.assertEqual(inspect_holdout(restored, history.dataset, history.frame, family, None, None), inspected)
        changed = {**history.candidate, "specification": "new family"}
        with self.assertRaisesRegex(ValueError, "consumed"):
            inspect_holdout(restored, history.dataset, history.frame, {digest(changed): changed}, None, None)
        with self.assertRaisesRegex(ValueError, "consumed"):
            register_dataset(restored, history.frame, scope="equity", features=["momentum"],
                             horizon_sessions=2, provenance={**history.provenance, "vintage": "changed"})
        self.assertEqual(register(restored, **tournament.kw), campaign)
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            register(restored, **{**tournament.kw, "seed": 1234})
        self.assertFalse(verify_eligibility(restored, certificate, candidate=candidate,
                                            routing=frozen["routing"], parent=tournament.proposal["parent"]))
        for command in ("UPDATE experiment_records SET body='{}' WHERE kind='certificate'",
                        "DELETE FROM experiment_records WHERE kind='budget'",
                        """INSERT OR REPLACE INTO experiment_records SELECT kind,id,body,sha256
                        FROM experiment_records WHERE kind='certificate'"""):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable experiment record"):
                with db(restored, True) as writer:
                    writer.execute(command)
