"""Configuration and settings loader for the self-evolving trader.

Loads environment variables from a .env file and provides typed, validated
configuration for the whole system. All values default to safe micro-capital
settings suitable for a ~$10 paper-trading account.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


def _getenv(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _getenv_float(key: str, default: float) -> float:
    try:
        return float(_getenv(key, str(default)))
    except ValueError:
        return default


def _getenv_int(key: str, default: int) -> int:
    try:
        return int(_getenv(key, str(default)))
    except ValueError:
        return default


def _getenv_list(key: str, default: List[str]) -> List[str]:
    raw = _getenv(key, ",".join(default))
    return [s.strip() for s in raw.split(",") if s.strip()]


@dataclass(frozen=True)
class AlpacaConfig:
    api_key: str
    api_secret: str
    base_url: str

    @property
    def is_paper(self) -> bool:
        return "paper" in self.base_url


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    base_url: str
    api_key: str
    model: str
    fallback_provider: str = ""
    fallback_base_url: str = ""
    fallback_api_key: str = ""
    fallback_model: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and self.api_key != "your_llm_api_key_here"


@dataclass(frozen=True)
class RiskConfig:
    max_capital_usd: float
    max_position_usd: float
    max_daily_loss_usd: float
    max_order_size_usd: float = 1.0
    min_order_size_usd: float = 0.01
    commission_per_order_usd: float = 0.0
    spread_bps: float = 5.0
    slippage_bps: float = 5.0


@dataclass(frozen=True)
class UniverseConfig:
    symbols: List[str]
    equities: List[str] = field(default_factory=list)
    cryptos: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        equities = [s for s in self.symbols if "/" not in s]
        cryptos = [s for s in self.symbols if "/" in s]
        object.__setattr__(self, "equities", equities)
        object.__setattr__(self, "cryptos", cryptos)


@dataclass(frozen=True)
class RuntimeConfig:
    data_dir: str
    db_path: str
    log_level: str


@dataclass(frozen=True)
class Settings:
    alpaca: AlpacaConfig
    llm: LLMConfig
    risk: RiskConfig
    universe: UniverseConfig
    runtime: RuntimeConfig

    @classmethod
    def load(cls, env_path: str | None = None) -> "Settings":
        if env_path:
            try:
                from dotenv import load_dotenv  # type: ignore

                load_dotenv(env_path)
            except ImportError:
                pass  # dotenv optional; rely on existing environment

        return cls(
            alpaca=AlpacaConfig(
                api_key=_getenv("ALPACA_API_KEY", ""),
                api_secret=_getenv("ALPACA_API_SECRET", ""),
                base_url=_getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
            ),
            llm=LLMConfig(
                provider=_getenv("LLM_PROVIDER", "openai"),
                base_url=_getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
                api_key=_getenv("LLM_API_KEY", ""),
                model=_getenv("LLM_MODEL", "gpt-4o-mini"),
                fallback_provider=_getenv("LLM_FALLBACK_PROVIDER", ""),
                fallback_base_url=_getenv("LLM_FALLBACK_BASE_URL", ""),
                fallback_api_key=_getenv("LLM_FALLBACK_API_KEY", ""),
                fallback_model=_getenv("LLM_FALLBACK_MODEL", ""),
            ),
            risk=RiskConfig(
                max_capital_usd=_getenv_float("MAX_CAPITAL_USD", 10.0),
                max_position_usd=_getenv_float("MAX_POSITION_USD", 1.0),
                max_daily_loss_usd=_getenv_float("MAX_DAILY_LOSS_USD", 0.50),
                max_order_size_usd=_getenv_float("MAX_ORDER_SIZE_USD", 1.0),
                min_order_size_usd=_getenv_float("MIN_ORDER_SIZE_USD", 0.01),
                commission_per_order_usd=_getenv_float("COMMISSION_PER_ORDER_USD", 0.0),
                spread_bps=_getenv_float("SPREAD_BPS", 5.0),
                slippage_bps=_getenv_float("SLIPPAGE_BPS", 5.0),
            ),
            universe=UniverseConfig(
                symbols=_getenv_list(
                    "TRADE_UNIVERSE",
                    ["AAPL", "MSFT", "NVDA", "TSLA", "BTC/USD", "ETH/USD"],
                )
            ),
            runtime=RuntimeConfig(
                data_dir=_getenv("DATA_DIR", "data"),
                db_path=_getenv("DB_PATH", "data/memory.sqlite"),
                log_level=_getenv("LOG_LEVEL", "INFO"),
            ),
        )
