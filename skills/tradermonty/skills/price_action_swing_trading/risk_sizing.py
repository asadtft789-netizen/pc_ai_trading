"""Risk-based position sizing module.

Implements proper risk-per-trade sizing as taught in the YouTube course:
- Position size = account_risk_amount / (entry - stop_loss)
- Risk 0.5-1% of account per trade
- Every trade has entry + stop-loss + target + position size
"""

from dataclasses import dataclass
from typing import Dict, Any, Optional


@dataclass
class PositionSizeResult:
    """Result of position sizing calculation."""
    qty: float
    notional_usd: float
    risk_amount_usd: float
    risk_pct: float
    entry_price: float
    stop_loss_price: float
    stop_loss_pct: float
    target_price: float
    reward_to_risk_ratio: float
    sizing_method: str


class RiskSizer:
    """Calculate proper position sizes based on risk per trade."""

    def __init__(self, default_risk_pct: float = 0.01, min_risk_pct: float = 0.005,
                 max_risk_pct: float = 0.02, default_r_to_r: float = 2.0):
        """
        Args:
            default_risk_pct: Default risk per trade (1%)
            min_risk_pct: Minimum risk per trade (0.5%)
            max_risk_pct: Maximum risk per trade (2%)
            default_r_to_r: Default reward-to-risk ratio (2:1)
        """
        self.default_risk_pct = default_risk_pct
        self.min_risk_pct = min_risk_pct
        self.max_risk_pct = max_risk_pct
        self.default_r_to_r = default_r_to_r

    def calculate_position_size(
        self,
        account_equity_usd: float,
        entry_price: float,
        stop_loss_price: float,
        risk_pct: Optional[float] = None,
        side: str = 'buy'
    ) -> PositionSizeResult:
        """
        Calculate position size based on risk per trade.

        Formula: position_size = account_risk_amount / |entry - stop_loss|
        where account_risk_amount = account_equity_usd * risk_pct

        Args:
            account_equity_usd: Total account equity in USD
            entry_price: Entry price for the trade
            stop_loss_price: Stop loss price
            risk_pct: Risk per trade as decimal (defaults to default_risk_pct)
            side: 'buy' for long, 'sell' for short

        Returns:
            PositionSizeResult with qty, notional, risk details
        """
        if risk_pct is None:
            risk_pct = self.default_risk_pct

        # Clamp risk to allowed range
        risk_pct = max(self.min_risk_pct, min(self.max_risk_pct, risk_pct))

        account_risk_amount_usd = account_equity_usd * risk_pct

        # Distance from entry to stop loss
        stop_distance = abs(entry_price - stop_loss_price)
        if stop_distance <= 0:
            raise ValueError("Stop loss distance must be positive")

        # Position size in shares/contracts
        qty = account_risk_amount_usd / stop_distance

        # Notional value of position
        notional_usd = qty * entry_price

        # Stop loss as percentage of entry
        stop_loss_pct = stop_distance / entry_price

        # Calculate target based on reward-to-risk ratio
        if side.lower() == 'buy':
            target_price = entry_price * (1 + self.default_r_to_r * stop_loss_pct)
        else:
            target_price = entry_price * (1 - self.default_r_to_r * stop_loss_pct)

        reward_to_risk_ratio = self.default_r_to_r

        return PositionSizeResult(
            qty=qty,
            notional_usd=notional_usd,
            risk_amount_usd=account_risk_amount_usd,
            risk_pct=risk_pct,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            stop_loss_pct=stop_loss_pct,
            target_price=target_price,
            reward_to_risk_ratio=reward_to_risk_ratio,
            sizing_method='risk_based'
        )


def size_position_for_trade(
    account_equity_usd: float,
    entry_price: float,
    stop_loss_price: float,
    risk_pct: float = 0.01,
    side: str = 'buy'
) -> PositionSizeResult:
    """
    Convenience function for quick position sizing calculation.

    Example:
        >>> result = size_position_for_trade(10000, 100.0, 95.0)
        >>> print(f"Buy {result.qty:.2f} shares, risk ${result.risk_amount_usd:.2f}")
    """
    sizer = RiskSizer()
    return sizer.calculate_position_size(
        account_equity_usd=account_equity_usd,
        entry_price=entry_price,
        stop_loss_price=stop_loss_price,
        risk_pct=risk_pct,
        side=side
    )