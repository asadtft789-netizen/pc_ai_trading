from config import Settings

class RiskEngine:
    def __init__(self, settings: Settings):
        self.risk_config = settings.risk

    def validate_trade(self, amount_usd: float) -> bool:
        if amount_usd > self.risk_config.max_order_size_usd:
            return False
        if amount_usd < self.risk_config.min_order_size_usd:
            return False
        return True
