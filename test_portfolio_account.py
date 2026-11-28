import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from portfolio_account import Account


class PortfolioAccountTests(unittest.TestCase):
    def setUp(self):
        self.a = Account()
        self.t = datetime(2026, 1, 2, 19, tzinfo=timezone.utc)

    def test_reservation_partial_and_cancel_then_mark(self):
        a = self.a
        a.reserve(".01", "100", ".04")
        self.assertEqual((a.available, a.equity), (D("8.96"), D(10)))
        a.apply_fill("buy", ".004", "99", estimated_fee=".016", fill_id="f")
        self.assertIsNone(a.equity)
        self.assertEqual(a.mark(self.t, "98.5"), D("9.982"))
        self.assertEqual((a.reserved_qty, a.reserved, a.available),
                         (D(".006"), D(".624"), D("8.964")))
        self.assertEqual(a.unrealized, D("-.002"))
        a.release()
        self.assertEqual((a.cash, a.available), (D("9.588"), D("9.588")))
        self.assertEqual(a.settle_fee("f", ".02"), None)
        self.assertEqual(a.available, D("9.584"))
        self.assertEqual(a.mark(self.t, "98.5"), D("9.978"))
        self.assertEqual(a.pending_fee_count, 0)

    def test_decimal_basis_gross_realized_and_cost_identity(self):
        a = self.a
        a.apply_fill("buy", ".003", "100.11", ".001", ".4", fill_id="b1")
        a.apply_fill("buy", ".002", "100.16", None, ".003", fill_id="b2")
        self.assertEqual(a.basis, D(".50065"))
        a.apply_fill("sell", ".001", "101.17", ".0021", fill_id="s1")
        a.mark(self.t, "97.95")
        self.assertEqual(a.realized, D(".00104"))
        self.assertEqual(a.unrealized, D("-.00872"))
        self.assertEqual(a.equity, a.capital + a.realized + a.unrealized
                         - a.confirmed_fees - a.estimated_fees)
        a.apply_fill("sell", ".004", "101.17", 0, fill_id="s2")
        self.assertEqual(a.realized, D(".00520"))
        self.assertEqual((a.qty, a.basis, a.unrealized), (0, 0, 0))
        a.settle_fee("b2", 0)
        self.assertEqual(a.cash, D("10.00210"))

    def test_fee_updates_and_duplicate_deliveries_survive_restore(self):
        self.a.apply_fill("buy", ".004", "99.1234567", None, ".0023", fill_id="a")
        restored = Account.restore(json.loads(json.dumps(self.a.snapshot())))
        restored.apply_fill("buy", ".004", "99.1234567", ".0017", fill_id="a")
        settled = restored.snapshot()
        restored.apply_fill("buy", ".004", "99.1234567", None, ".0023", fill_id="a")
        restored.settle_fee("a", ".0017")
        self.assertEqual(restored.snapshot(), settled)
        self.assertEqual(restored.confirmed_fees, D(".0017"))
        self.assertEqual(restored.estimated_fees, 0)
        self.assertEqual(restored.pending_fee_count, 0)
        with self.assertRaisesRegex(ValueError, "changed fill"):
            restored.apply_fill("buy", ".004", "99.12345", fill_id="a")
        with self.assertRaisesRegex(ValueError, "changed confirmed fee"):
            restored.settle_fee("a", ".0018")
        self.assertEqual(restored.snapshot(), settled)

    def test_missing_mark_does_not_book_fictitious_gain_or_loss(self):
        self.a.apply_fill("buy", ".01", "100", 0)
        self.a.mark(self.t, "105")
        self.assertIsNone(self.a.mark(self.t + timedelta(seconds=1), None))
        self.assertIsNone(self.a.unrealized)
        self.assertEqual(self.a.peak, D("10.05"))
        self.assertFalse(self.a.daily_halt)
        self.a.mark(self.t + timedelta(seconds=2), "98")
        self.assertEqual(self.a.unrealized, D("-.02"))
        self.assertEqual(self.a.equity, D("9.98"))

    def test_daily_halt_marks_boundary_and_exact_threshold(self):
        self.a.mark(self.t)
        self.a.apply_fill("buy", ".01", "100", 0)
        self.a.mark(self.t, "50")
        self.assertTrue(self.a.daily_halt)
        self.assertFalse(self.a.drawdown_halt)
        self.a.mark(self.t, "100")
        a = Account.restore(json.loads(json.dumps(self.a.snapshot())))
        self.assertTrue(a.daily_halt)
        next_day = self.t + timedelta(days=1)
        a.mark(next_day, "99")
        self.assertFalse(a.daily_halt)
        self.assertEqual(a.day_start, D(10))
        a.mark(next_day, "49")
        self.assertTrue(a.daily_halt)

    def test_drawdown_latch_and_high_water_mark_survive_days(self):
        a = self.a
        a.apply_fill("buy", ".01", "100", 0)
        a.mark(self.t, "200")
        a.mark(self.t, "90")
        self.assertEqual(a.peak, 11)
        self.assertEqual(a.drawdown, D(".10"))
        self.assertTrue(a.drawdown_halt)
        a = Account.restore(json.loads(json.dumps(a.snapshot())))
        a.apply_fill("sell", ".01", "205", 0)
        a.mark(self.t + timedelta(days=1))
        self.assertEqual(a.drawdown, 0)
        self.assertTrue(a.drawdown_halt)
        self.assertEqual(a.max_drawdown, D(".1"))
        self.assertFalse(a.daily_halt)
        with self.assertRaises(ValueError):
            a.reserve(".01", 100)

    def test_reservation_and_mark_validation_leave_account_unchanged(self):
        a = self.a
        a.reserve(".01", "100", ".01")
        snap = a.snapshot()
        for fn in (lambda: a.reserve(".01", "100"), lambda: a.apply_fill("buy", ".011", "100"),
                   lambda: a.apply_fill("sell", ".001", "90"), lambda: a.apply_fill("buy", ".003", "NaN"),
                   lambda: a.mark(self.t, "Infinity"), lambda: a.mark(self.t.replace(tzinfo=None))):
            with self.assertRaises(ValueError):
                fn()
            self.assertEqual(a.snapshot(), snap)
        a.mark(self.t)
        with self.assertRaises(ValueError):
            a.mark(self.t - timedelta(seconds=1))
        self.assertEqual(a.snapshot(), Account.restore(a.snapshot()).snapshot())

    def test_fractional_fee_shares_cancel_exactly_in_any_settlement_order(self):
        for i in range(19):
            share = D(i + 1) / 19 - D(i) / 19
            self.a.apply_fill("buy", ".00001", "100", estimated_fee=share, fill_id=str(i))
        for i in reversed(range(19)):
            self.a.settle_fee(str(i), "0")
            self.a = Account.restore(self.a.snapshot())
        self.assertEqual(self.a.estimated_fees, 0)
        self.assertEqual(self.a.cash, D("9.981"))

    def test_inconsistent_reservation_or_fee_checkpoint_is_rejected(self):
        self.a.apply_fill("buy", ".01", 100, estimated_fee=".01", fill_id="fill")
        snapshot = self.a.snapshot()
        for invalid in ({"pending_fee_count": 0}, {"estimated_fees": "0"},
                        {"reserved_qty": ".01"}, {"last_known_equity": "1000"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                Account.restore(dict(snapshot, **invalid))

    def test_broker_fill_price_and_fee_can_exceed_reservation(self):
        a = Account(1, ".50")
        a.reserve(".01", "100", 0)
        a.apply_fill("buy", ".01", "105", ".01", fill_id="b")
        self.assertEqual((a.reserved, a.cash, a.basis), (0, D("-.06"), D("1.05")))
        self.assertEqual(a.mark(self.t, "100"), D(".94"))
        self.assertEqual(a.pending_fee_count, 0)


if __name__ == "__main__":
    unittest.main()
