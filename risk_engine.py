from decimal import Decimal, InvalidOperation

from config import Settings

class RiskEngine:
    def __init__(self, settings: Settings):
        self.risk_config = settings.risk

    def validate_trade(self, amount_usd: float) -> bool:
        """Apply equity entry limits. A caller must independently verify broker eligibility."""
        try:
            value = Decimal(str(amount_usd))
            risk = self.risk_config
            floor = max(Decimal(1), Decimal(str(risk.min_order_size_usd)))
            ceiling = min(Decimal(str(v)) for v in (
                1, risk.max_order_size_usd, risk.max_position_usd, risk.max_capital_usd))
            return value.is_finite() and floor <= value <= ceiling
        except (InvalidOperation, ValueError):
            return False
