import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from paper_store import init
from research_admission import MemoryAdmission, reserve_lanes


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = init(Path(temp.name) / "paper.sqlite")

    def test_concurrent_budget(self):
        with ThreadPoolExecutor(4) as pool:
            reserved = list(pool.map(lambda _: reserve_lanes(self.path, 5), range(25)))
        self.assertEqual(sum(reserved), 20)

    def test_low_memory_restart_and_continuity(self):
        gate = MemoryAdmission(self.path)
        self.assertFalse(gate.sample(255, 1))
        gate = MemoryAdmission(self.path)
        for t in [5,65,125,200,260,320,380,440]:
            self.assertFalse(gate.sample(384,t))
        self.assertTrue(gate.sample(384,500))
        self.assertFalse(gate.sample(None,501))
