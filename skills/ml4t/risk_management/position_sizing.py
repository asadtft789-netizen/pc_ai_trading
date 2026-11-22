"""Position Sizing — risk per trade derived from actual paper-trading outcomes.

Computes position size (notional dollars) per symbol using a Kelly-fraction
guard capped by a max-risk-per-trade percentage of equity. The risk budget is
tuned from the strategy's own recent realized win rate / avg PnL, so sizing
shrinks when the strategy is in a losing streak and grows when it's winning —
all driven by real paper-trading results, not fixed assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SizingDecision:
    notional_usd: float
    qty: float
    risk_usd: float
    kelly_fraction: float
    win_rate: float
    avg_win: float
    avg_loss: float
    reason: str


class PositionSizer:
    """Fractional-Kelly position sizing bounded by max risk per trade."""

    def __init__(self, max_risk_per_trade: float = 0.02,
                 kelly_cap: float = 0.25,
                 min_notional: float = 1.0):
        """max_risk_per_trade: fraction of equity to risk on a single trade (default 2%).
        kelly_cap: maximum fraction of the Kelly suggestion to actually use (default 25%).
        min_notional: smallest order notional in USD (default $1)."""
        self.max_risk_per_trade = max_risk_per_trade
        self.kelly_cap = kelly_cap
        self.min_notional = min_notional

    def size(self, equity_usd: float, win_rate: float, avg_win_usd: float,
             avg_loss_usd: float, price: float) -> SizingDecision:
        """Return a position-sizing decision for one trade.

        All inputs are derived from realized paper-trading outcomes — win_rate,
        avg_win_usd, avg_loss_usd come from the strategy's own trade history.
        """
        # Guard: no usable history → trade minimum
        if avg_loss_usd <= 0 or win_rate <= 0 or win_rate >= 1.0:
            notional = max(self.min_notional,
                           equity_usd * self.max_risk_per_trade)
            qty = notional / price if price > 0 else 0.0
            return SizingDecision(
                notional_usd=notional, qty=qty,
                risk_usd=notional * self.max_risk_per_trade,
                kelly_fraction=0.0, win_rate=win_rate,
                avg_win=avg_win_usd, avg_loss=avg_loss_usd,
                reason="No usable history; sized at min notional / max risk cap.",
            )

        # Kelly fraction = (win_rate * avg_win - loss_rate * avg_loss) / avg_win
        loss_rate = 1.0 - win_rate
        edge = win_rate * avg_win_usd - loss_rate * avg_loss_usd
        kelly = edge / avg_win_usd if avg_win_usd > 0 else 0.0
        kelly = max(0.0, kelly)  # never negative (no edge → no size)

        # Cap Kelly to a safe fraction
        used_kelly = min(kelly, self.kelly_cap)

        # Risk = used_kelly * equity (fraction of equity risked)
        risk_usd = used_kelly * equity_usd
        # Also enforce the hard max-risk-per-trade cap
        risk_usd = min(risk_usd, equity_usd * self.max_risk_per_trade)

        # Notional from risk / expected loss per share (avg_loss as fraction of price)
        # Approximate: risk_usd = qty * avg_loss_per_share; avg_loss_per_share ≈ avg_loss_usd
        # So qty = risk_usd / avg_loss_usd; notional = qty * price
        if avg_loss_usd > 0:
            qty = risk_usd / avg_loss_usd
        else:
            qty = 0.0
        notional = qty * price

        # Floor at min notional
        if notional < self.min_notional:
            notional = self.min_notional
            qty = notional / price if price > 0 else 0.0
            risk_usd = notional * self.max_risk_per_trade

        return SizingDecision(
            notional_usd=round(notional, 2),
            qty=round(qty, 6),
            risk_usd=round(risk_usd, 2),
            kelly_fraction=round(used_kelly, 4),
            win_rate=round(win_rate, 4),
            avg_win=round(avg_win_usd, 2),
            avg_loss=round(avg_loss_usd, 2),
            reason=f"Kelly={kelly:.3f} capped={used_kelly:.3f}; risk=${risk_usd:.2f} on ${equity_usd:.2f} equity.",
        )
