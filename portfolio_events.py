"""Version 3 intraday equity allocation driven only by supplied market facts."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from config import PAPER_EQUITIES
from portfolio_account import Account
from portfolio_policy import STOP_ATR, protection_prices

D = Decimal
ZERO = D(0)
VERSION = "equity-quote-v3"


@dataclass(frozen=True)
class ExecutionPolicy:
    capital: D = D(10)
    order_cap: D = D(1)
    position_cap: D = D(1)
    minimum: D = D(1)
    daily_loss: D = D(".50")
    quantity_step: D = D(".000000001")
    fee_per_order: D = ZERO
    scenario_bps: D = ZERO

    def __post_init__(self):
        for field in self.__dataclass_fields__:
            value = D(str(getattr(self, field)))
            if not value.is_finite() or value < 0 or (field not in ("fee_per_order", "scenario_bps") and not value):
                raise ValueError(f"invalid execution policy {field}")
            object.__setattr__(self, field, value)
        if (self.capital > 10 or self.order_cap > 1 or self.position_cap > 1
                or self.minimum < 1 or self.daily_loss > D(".50")
                or self.quantity_step % D(".000000001")):
            raise ValueError("execution policy exceeds authorized limits")


    def order_fee(self, qty, price):
        return self.fee_per_order + qty * price * self.scenario_bps / 10000


@dataclass(frozen=True)
class Decision:
    symbol: str
    strategy: str
    start: datetime
    end: datetime
    published: datetime
    decided: datetime
    target: int
    atr: D
    ready: bool = True


@dataclass(frozen=True)
class Quote:
    symbol: str
    at: datetime
    bid: D
    ask: D
    entry_ok: bool = True


@dataclass(frozen=True)
class MarketFrame:
    at: datetime
    session_open: datetime
    session_close: datetime
    decisions: tuple = ()
    quotes: tuple = ()

    def __post_init__(self):
        if any(t.utcoffset() is None for t in (self.at, self.session_open, self.session_close)):
            raise ValueError("frame requires timezone-aware session and evaluation times")
        if not timedelta(0) < self.session_close - self.session_open <= timedelta(hours=6.5):
            raise ValueError("invalid equity session boundaries")
        for field in ("quotes", "decisions"):
            records = tuple(getattr(self, field))
            if len({r.symbol for r in records}) != len(records):
                raise ValueError(f"duplicate symbol in frame {field}")
            object.__setattr__(self, field, tuple(sorted(records, key=lambda r: r.symbol)))


def quote_ok(quote, at, entry):
    try:
        if not quote or not quote.symbol or quote.at.utcoffset() is None or at.utcoffset() is None:
            return False
        if not (quote.bid.is_finite() and quote.ask.is_finite() and 0 < quote.bid <= quote.ask):
            return False
        if not -2 <= (at - quote.at).total_seconds() <= 10:
            return False
        return not entry or (
            quote.entry_ok is True
            and (quote.ask - quote.bid) * 20000 <= 20 * (quote.ask + quote.bid)
        )
    except (TypeError, AttributeError, InvalidOperation, OverflowError):
        return False


def decision_ok(decision, frame, quote, entry):
    """Common point-in-time gates. External broker authorization is independent."""
    if not decision or not quote_ok(quote, frame.at, entry):
        return False
    try:
        if (decision.ready is not True or not decision.strategy or decision.symbol != quote.symbol
                or type(decision.target) is not int or decision.target != int(entry)):
            return False
        if not (frame.session_open <= decision.start < decision.end <= frame.at <= frame.session_close
                and frame.session_open <= quote.at <= frame.session_close
                and decision.end < quote.at and decision.decided <= min(quote.at, frame.at)):
            return False
        duration = (decision.end - decision.start).total_seconds()
        offset = (decision.start - frame.session_open).total_seconds()
        if not (offset % 3600 == 0 and (duration == 3600 or decision.end == frame.session_close)
                and 0 < duration <= 3600
                and 0 <= (decision.published - decision.end).total_seconds() <= 120
                and 0 <= (decision.decided - decision.published).total_seconds() <= 30
                and (frame.at - decision.end).total_seconds() <= 300):
            return False
        return not entry or (
            decision.symbol in PAPER_EQUITIES and duration == 3600
            and max(quote.at, frame.at) < frame.session_close - timedelta(minutes=30)
            and decision.atr.is_finite() and decision.atr > 0
        )
    except (TypeError, AttributeError, InvalidOperation, OverflowError):
        return False


def entry_quantity(policy, equity, available, bid, ask, gap):
    """Maximum supported quantity meeting every exposure, cash and minimum check."""
    if not all(v is not None and v.is_finite() and v > 0 for v in (equity, available, bid, ask, gap)):
        return ZERO
    if gap >= bid or bid > ask:
        return ZERO
    cash_for_notional = (available - policy.fee_per_order) / (1 + policy.scenario_bps / 10000)
    budget = min(policy.order_cap, policy.position_cap, cash_for_notional)
    if budget < policy.minimum:
        return ZERO
    size = min(budget / ask, min(equity, policy.capital) * D(".01") / gap)
    qty = (size / policy.quantity_step).to_integral_value(rounding=ROUND_DOWN) * policy.quantity_step
    return qty if policy.minimum <= qty * ask <= budget else ZERO


def exit_reason(bid, basis_price, gap, session_exit, flat, risk_halt):
    stop, target = protection_prices(basis_price, gap)
    if bid <= stop:
        return "stop"
    if bid >= target:
        return "target"
    if session_exit:
        return "session_close"
    if risk_halt:
        return "risk_halt"
    if flat:
        return "signal"
    return None


@dataclass(frozen=True)
class Fill:
    at: datetime
    symbol: str
    strategy: str
    decision_end: datetime
    side: str
    qty: D
    price: D
    reason: str
    fee: D
    gap: D


class QuotePortfolio:
    """Deterministic immediate-fill model. Broker confirmations have separate provenance."""
    def __init__(self, policy=None, *, skip_entry_quotes=0, adverse_stop_atr=ZERO):
        self.policy = policy or ExecutionPolicy()
        self.account = Account(self.policy.capital, self.policy.daily_loss)
        self.skip_entry_quotes = skip_entry_quotes
        self.adverse_stop_atr = D(str(adverse_stop_atr))
        if (type(skip_entry_quotes) is not int or skip_entry_quotes < 0
                or not self.adverse_stop_atr.is_finite() or self.adverse_stop_atr < 0):
            raise ValueError("invalid execution scenario")
        self.fills = []
        self.position = None  # entry fill, including frozen owner, cost basis and gap
        self.decisions = {}
        self.consumed = {}
        self._quotes = {}
        self._entry_quotes = {}
        self._last_frame = None

    def step(self, frame):
        if self._last_frame and frame.at <= self._last_frame.at:
            if frame == self._last_frame:
                return ()
            raise ValueError("out-of-order or conflicting market frame")
        available = tuple(d for d in frame.decisions
                          if all(isinstance(t, datetime) and t.utcoffset() is not None
                                 for t in (d.start, d.end, d.published, d.decided))
                          and d.end <= d.published <= d.decided <= frame.at)
        for decision in available:
            previous = self.decisions.get(decision.symbol)
            if previous and decision.end == previous.end and decision != previous:
                raise ValueError("conflicting completed decision")
        for decision in available:
            previous = self.decisions.get(decision.symbol)
            if not previous or previous.end < decision.end:
                self.decisions[decision.symbol] = decision
        self._last_frame = frame
        quotes = {}
        for quote in frame.quotes:
            if not quote_ok(quote, frame.at, False):
                continue
            if not (frame.session_open <= quote.at <= frame.session_close
                    and frame.session_open <= frame.at <= frame.session_close):
                continue
            previous = self._quotes.get(quote.symbol)
            if previous and (quote.at < previous.at or (quote.at == previous.at
                             and (quote.bid, quote.ask) != (previous.bid, previous.ask))):
                continue
            self._quotes[quote.symbol] = quote
            quotes[quote.symbol] = quote
        entry = self.position
        held_quote = quotes.get(entry.symbol) if entry else None
        valid_bid = quote_ok(held_quote, frame.at, False) and (
            frame.session_open <= held_quote.at <= frame.session_close
            and frame.session_open <= frame.at <= frame.session_close
            and held_quote.at >= entry.at
        )
        self.account.mark(frame.at, held_quote.bid if valid_bid else None)
        halted = self.account.drawdown_halt or self.account.daily_halt
        if entry:
            if not valid_bid:
                return ()
            decision = self.decisions.get(entry.symbol)
            flat = (decision_ok(decision, frame, held_quote, False)
                    and decision.strategy == entry.strategy and decision.end >= entry.at)
            session_exit = (frame.at >= frame.session_close - timedelta(minutes=5)
                            or entry.at < frame.session_open)
            reason = exit_reason(held_quote.bid, entry.price, entry.gap, session_exit, flat, halted)
            if reason:
                price = held_quote.bid
                if reason == "stop" and self.adverse_stop_atr:
                    stop, _ = protection_prices(entry.price, entry.gap)
                    price = max(min(price, D(".000000001")),
                                min(price, stop - self.adverse_stop_atr * entry.gap / STOP_ATR))
                fee = self.policy.order_fee(self.account.qty, price)
                fill = Fill(frame.at, entry.symbol, entry.strategy, entry.decision_end, "sell",
                            self.account.qty, price, reason, fee, entry.gap)
                self.account.apply_fill("sell", fill.qty, fill.price, estimated_fee=fee)
                self.account.mark(frame.at)
                self.position = None
                self.fills.append(fill)
                return (fill,)
        elif not halted:
            for symbol in sorted(quotes):
                quote = quotes[symbol]
                decision = self.decisions.get(symbol)
                if not decision_ok(decision, frame, quote, True):
                    continue
                if self.consumed.get(symbol) == decision.end:
                    continue
                gap = decision.atr * STOP_ATR
                qty = entry_quantity(self.policy, self.account.equity, self.account.available,
                                     quote.bid, quote.ask, gap)
                if qty and self.skip_entry_quotes:
                    cut, previous, skipped = self._entry_quotes.get(symbol, (None, None, 0))
                    if cut != decision.end:
                        previous, skipped = None, 0
                    if previous is None or quote.at > previous:
                        skipped += 1
                        self._entry_quotes[symbol] = (decision.end, quote.at, skipped)
                    if skipped <= self.skip_entry_quotes:
                        continue
                if qty:
                    fee = self.policy.order_fee(qty, quote.ask)
                    fill = Fill(frame.at, symbol, decision.strategy, decision.end, "buy",
                                qty, quote.ask, "entry", fee, gap)
                    self.account.reserve(qty, quote.ask, fee)
                    self.account.apply_fill("buy", qty, quote.ask, estimated_fee=fee)
                    self.account.mark(frame.at, quote.bid)
                    self.position = fill
                    self.consumed[symbol] = decision.end
                    self.fills.append(fill)
                    return (fill,)
        return ()
