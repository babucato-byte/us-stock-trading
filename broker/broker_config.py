import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

import risk_config

load_dotenv()

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL = "https://api.alpaca.markets"


def env_bool(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class BrokerConfig:
    trading_mode: str = os.getenv("TRADING_MODE", risk_config.TRADING_MODE).strip().lower()
    enable_real_trading: bool = env_bool("ENABLE_REAL_TRADING", risk_config.ENABLE_REAL_TRADING)
    live_dry_run: bool = env_bool("LIVE_DRY_RUN", risk_config.LIVE_DRY_RUN)
    paper_base_url: str = os.getenv("ALPACA_PAPER_BASE_URL", PAPER_BASE_URL)
    live_base_url: str = os.getenv("ALPACA_LIVE_BASE_URL", LIVE_BASE_URL)
    api_key: Optional[str] = os.getenv("ALPACA_API_KEY")
    secret_key: Optional[str] = os.getenv("ALPACA_SECRET_KEY")

    @property
    def is_live_mode(self):
        return self.trading_mode == "live"

    @property
    def is_paper_mode(self):
        return not self.is_live_mode

    @property
    def base_url(self):
        return self.live_base_url if self.is_live_mode else self.paper_base_url

    @property
    def can_submit_live_order(self):
        return self.is_live_mode and self.enable_real_trading and not self.live_dry_run

    @property
    def status_label(self):
        if self.is_live_mode and self.live_dry_run:
            return "LIVE_DRY_RUN"
        if self.is_live_mode and not self.enable_real_trading:
            return "LIVE_DISABLED"
        if self.can_submit_live_order:
            return "LIVE_ENABLED"
        return "PAPER"

    def validate_for_request(self):
        if not self.api_key or not self.secret_key:
            raise RuntimeError("Alpaca API credentials are missing. Set them in .env or environment variables.")

    def validate_order_allowed(self):
        if self.is_paper_mode:
            return True
        if self.can_submit_live_order:
            raise RuntimeError("Real live trading is disabled in this pre-live PR. Use live dry-run only.")
        raise RuntimeError(
            "Live order blocked. Live orders require TRADING_MODE=live, "
            "ENABLE_REAL_TRADING=True, and LIVE_DRY_RUN=False."
        )
