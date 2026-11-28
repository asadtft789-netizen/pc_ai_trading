"""Decimal ledger for one allocated, long-only portfolio.

Fill prices and fees are economic facts. Risk policy is enforced when reserving
capital and authorizing orders. A worse actual fill must still be accounted for.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext

ZERO = Decimal(0)
DRAWDOWN_LIMIT = Decimal("0.10")


def _amount(value, *, positive=False):
    try:
        amount = Decimal(str(value))
    except (ValueError, InvalidOperation) as exc:
        raise ValueError("invalid decimal amount") from exc
    if not amount.is_finite() or amount < 0 or (positive and not amount):
        raise ValueError("invalid decimal amount")
    return amount


def _sum_amounts(a, b):
    """Add finite decimal currency amounts without discarding fractional cents.

    Pro-rating a flat fee over partial fills can produce 28-digit shares. A
    running total rounded to 28 digits can leave a phantom fee after settlement.
    """
    with localcontext() as context:
        lowest = min(a.as_tuple().exponent, b.as_tuple().exponent)
        context.prec = max(context.prec, max(a.adjusted(), b.adjusted()) - lowest + 2)
        return a + b


class Account:
    def __init__(self, capital=Decimal(10), daily_loss=Decimal(".50")):
        self.capital = _amount(capital, positive=True)
        self.daily_loss = _amount(daily_loss, positive=True)
        if self.capital > 10 or self.daily_loss > Decimal(".50"):
            raise ValueError("allocated risk limits exceeded")

        self.cash = self.peak = self.day_start = self.capital
        self.qty = self.basis = self.realized = ZERO
        self.confirmed_fees = self.estimated_fees = ZERO
        self.pending_fee_count = 0
        self.reserved_qty = self.reserved_price = self.reserved_fee = ZERO
        self.last_bid = self.last_at = None
        self.last_known_equity = self.capital
        self.max_drawdown = ZERO
        self.daily_halt = self.drawdown_halt = False
        # The string identity comes from the source fill, never the order ID.
        self._fills = {}

    @property
    def reserved(self):
        return self.reserved_qty * self.reserved_price + self.reserved_fee

    @property
    def available(self):
        return self.cash - self.reserved

    @property
    def unrealized(self):
        if not self.qty:
            return ZERO
        return None if self.last_bid is None else self.qty * self.last_bid - self.basis

    @property
    def equity(self):
        if not self.qty:
            return self.cash
        return None if self.last_bid is None else self.cash + self.qty * self.last_bid

    @property
    def drawdown(self):
        equity = self.equity
        return None if equity is None else max(ZERO, (self.peak - equity) / self.peak)

    def reserve(self, qty, price, estimated_fee=ZERO):
        """Hold one new buy's dollar commitment, including the expected fee."""
        qty = _amount(qty, positive=True)
        price = _amount(price, positive=True)
        fee = _amount(estimated_fee)
        if (self.qty or self.reserved_qty or self.daily_halt or self.drawdown_halt
                or qty * price > 1 or qty * price + fee > self.cash):
            raise ValueError("buy reservation unavailable")
        self.reserved_qty, self.reserved_price, self.reserved_fee = qty, price, fee

    def release(self):
        """Apply a definitive cancellation/rejection to the unfilled remainder."""
        self.reserved_qty = self.reserved_price = self.reserved_fee = ZERO

    def apply_fill(self, side, qty, price, confirmed_fee=None, estimated_fee=ZERO, *, fill_id=None):
        """Book a confirmed quantity and price. A missing fee is still pending.

        Named fills can be replayed and later settled by identity. Anonymous fills
        are for ordered simulations that never receive subsequent fee reports.
        """
        qty = _amount(qty, positive=True)
        price = _amount(price, positive=True)
        estimate = _amount(estimated_fee)
        confirmed = None if confirmed_fee is None else _amount(confirmed_fee)
        if fill_id is not None and (not isinstance(fill_id, str) or not fill_id):
            raise ValueError("invalid fill identity")
        if fill_id in self._fills:
            old_side, old_qty, old_price, _, _ = self._fills[fill_id]
            if (old_side, old_qty, old_price) != (side, qty, price):
                raise ValueError("changed fill")
            if confirmed is not None:
                self.settle_fee(fill_id, confirmed)
            return
        value = qty * price
        if side == "buy":
            if self.reserved_qty:
                if qty > self.reserved_qty:
                    raise ValueError("fill exceeds reserved quantity")
                if qty == self.reserved_qty:
                    self.release()
                else:
                    remaining = self.reserved_qty - qty
                    self.reserved_fee *= remaining / self.reserved_qty
                    self.reserved_qty = remaining
            if not self.qty:
                self.last_bid = None
            self.qty += qty
            self.basis += value
            self.cash = _sum_amounts(self.cash, value.copy_negate())
        elif side == "sell":
            if qty > self.qty:
                raise ValueError("sell exceeds held quantity")
            cost = self.basis if qty == self.qty else self.basis * qty / self.qty
            self.realized += value - cost
            self.qty -= qty
            self.basis -= cost
            self.cash = _sum_amounts(self.cash, value)
            if not self.qty:
                self.last_bid = None
        else:
            raise ValueError("unsupported fill side")
        if confirmed is None:
            self.cash = _sum_amounts(self.cash, estimate.copy_negate())
            self.estimated_fees = _sum_amounts(self.estimated_fees, estimate)
            self.pending_fee_count += 1
        else:
            self.cash = _sum_amounts(self.cash, confirmed.copy_negate())
            self.confirmed_fees = _sum_amounts(self.confirmed_fees, confirmed)
        if fill_id is not None:
            self._fills[fill_id] = (side, qty, price, estimate, confirmed)

    def settle_fee(self, fill_id, confirmed_fee):
        """Replace this fill's pending fee allowance with one confirmed charge."""
        fee = _amount(confirmed_fee)
        if fill_id not in self._fills:
            raise ValueError("unknown fee identity")
        side, qty, price, estimate, confirmed = self._fills[fill_id]
        if confirmed is not None:
            if confirmed != fee:
                raise ValueError("changed confirmed fee")
            return
        self.cash = _sum_amounts(_sum_amounts(self.cash, estimate), fee.copy_negate())
        self.estimated_fees = _sum_amounts(self.estimated_fees, estimate.copy_negate())
        self.confirmed_fees = _sum_amounts(self.confirmed_fees, fee)
        self.pending_fee_count -= 1
        self._fills[fill_id] = (side, qty, price, estimate, fee)

    def mark(self, at, bid=None):
        """Value a polling instant. Missing held bids produce unknown equity."""
        if not isinstance(at, datetime) or at.utcoffset() is None:
            raise ValueError("mark requires a timezone-aware timestamp")
        at = at.astimezone(timezone.utc)
        if self.last_at is not None and at < self.last_at:
            raise ValueError("out-of-order mark")
        bid = None if bid is None else _amount(bid, positive=True)
        if self.last_at is None or at.date() != self.last_at.date():
            self.day_start = self.last_known_equity
            self.daily_halt = False
        self.last_at = at
        self.last_bid = bid if self.qty else None
        equity = self.equity
        if equity is not None:
            self.last_known_equity = equity
            self.peak = max(self.peak, equity)
            drawdown = self.drawdown
            self.max_drawdown = max(self.max_drawdown, drawdown)
            self.drawdown_halt |= drawdown >= DRAWDOWN_LIMIT
            self.daily_halt |= self.day_start - equity >= self.daily_loss
        return equity

    def snapshot(self):
        """Lossless JSON-compatible checkpoint. Decimals remain strings."""
        return dict(
            version=1, capital=str(self.capital), daily_loss=str(self.daily_loss),
            cash=str(self.cash), qty=str(self.qty), basis=str(self.basis), realized=str(self.realized),
            confirmed_fees=str(self.confirmed_fees), estimated_fees=str(self.estimated_fees),
            pending_fee_count=self.pending_fee_count,
            reserved_qty=str(self.reserved_qty), reserved_price=str(self.reserved_price),
            reserved_fee=str(self.reserved_fee), peak=str(self.peak), day_start=str(self.day_start),
            last_bid=None if self.last_bid is None else str(self.last_bid),
            last_at=None if self.last_at is None else self.last_at.isoformat(),
            last_known_equity=str(self.last_known_equity), max_drawdown=str(self.max_drawdown),
            daily_halt=self.daily_halt, drawdown_halt=self.drawdown_halt,
            fills={k: [s, str(q), str(p), str(e), None if f is None else str(f)]
                   for k, (s, q, p, e, f) in self._fills.items()},
        )

    @classmethod
    def restore(cls, state):
        """Restore one atomic checkpoint, including pending and settled fee IDs."""
        if type(state["version"]) is not int or state["version"] != 1:
            raise ValueError("unsupported portfolio checkpoint")
        result = cls(state["capital"], state["daily_loss"])
        for name in ("qty", "basis", "confirmed_fees", "estimated_fees", "reserved_qty",
                     "reserved_price", "reserved_fee", "peak", "max_drawdown"):
            setattr(result, name, _amount(state[name]))
        for name in ("cash", "realized", "day_start", "last_known_equity"):
            try:
                value = Decimal(str(state[name]))
            except (ValueError, InvalidOperation) as exc:
                raise ValueError("invalid checkpoint amount") from exc
            if not value.is_finite():
                raise ValueError("nonfinite checkpoint amount")
            setattr(result, name, value)
        result.last_bid = None if state["last_bid"] is None else _amount(state["last_bid"], positive=True)
        result.last_at = None if state["last_at"] is None else datetime.fromisoformat(state["last_at"])
        if result.last_at is not None:
            if result.last_at.utcoffset() is None:
                raise ValueError("invalid checkpoint timestamp")
            result.last_at = result.last_at.astimezone(timezone.utc)
        for name in ("daily_halt", "drawdown_halt"):
            if type(state[name]) is not bool:
                raise ValueError("invalid checkpoint risk state")
            setattr(result, name, state[name])
        if (type(state["pending_fee_count"]) is not int or state["pending_fee_count"] < 0
                or result.peak < result.capital or bool(result.qty) != bool(result.basis)
                or (result.max_drawdown >= DRAWDOWN_LIMIT and not result.drawdown_halt)):
            raise ValueError("invalid portfolio checkpoint")
        result.pending_fee_count = state["pending_fee_count"]
        for key, (side, qty, price, estimate, confirmed) in state["fills"].items():
            if not isinstance(key, str) or not key or side not in ("buy", "sell"):
                raise ValueError("invalid checkpoint fill")
            result._fills[key] = (side, _amount(qty, positive=True), _amount(price, positive=True),
                                  _amount(estimate), None if confirmed is None else _amount(confirmed))
        if (bool(result.reserved_qty) != bool(result.reserved_price)
                or (not result.reserved_qty and result.reserved_fee)
                or result.reserved_qty * result.reserved_price > 1
                or (not result.qty and result.last_bid is not None)
                or result.peak < result.last_known_equity
                or (not result.pending_fee_count and result.estimated_fees)):
            raise ValueError("inconsistent portfolio checkpoint")
        # Anonymous simulated fills and projections backed by normalized SQLite
        # facts need not retain all receipt IDs in this snapshot.
        pending_count = 0
        pending_total = ZERO
        for _, _, _, estimate, confirmed in result._fills.values():
            if confirmed is None:
                pending_count += 1
                pending_total = _sum_amounts(pending_total, estimate)
        if pending_count > result.pending_fee_count or pending_total > result.estimated_fees:
            raise ValueError("checkpoint fee liabilities do not cover receipts")
        return result
