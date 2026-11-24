"""Disposable integration smoke: no configuration file, order service, or external provider."""
import unittest
from test_hourly import Hourly
if __name__ == '__main__':
    suite = unittest.TestSuite((Hourly(name) for name in ('test_review_idempotence_and_no_orders', 'test_autonomous_entry_and_supervised_exit', 'test_confirmed_partial_across_midnight_and_restart')))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
