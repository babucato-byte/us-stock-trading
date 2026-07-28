"""KISConfig -- environment-driven configuration for brokers/kis_broker.py,
mirroring broker/broker_config.py's pattern (frozen dataclass,
default_factory reads os.environ at construction time, an explicit
`from_env()` for callers who want to re-read the environment right
before a safety-sensitive decision).

Real vs mock (모의투자) base URLs and the two account fields (CANO/
ACNT_PRDT_CD) come straight from the official KIS Open API examples
(github.com/koreainvestment/open-trading-api, examples_user/kis_auth.py)
-- verified against that source, not guessed. `KIS_LIVE_ORDER_ENABLED`
mirrors this codebase's existing fail-closed-by-default pattern
(risk_config.ENABLE_REAL_TRADING / broker.broker_config.ALPACA_ORDER_
ENABLED): unset or any value other than an explicit true means orders
are blocked.
"""

import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

from broker.broker_config import env_bool

load_dotenv()

LIVE_BASE_URL = "https://openapi.koreainvestment.com:9443"
MOCK_BASE_URL = "https://openapivts.koreainvestment.com:29443"


def _default_kis_env():
    return os.getenv("KIS_ENV", "paper").strip().lower()


def _default_app_key():
    return os.getenv("KIS_APP_KEY")


def _default_app_secret():
    return os.getenv("KIS_APP_SECRET")


def _default_account_no():
    return os.getenv("KIS_ACCOUNT_NO")


def _default_account_product_cd():
    return os.getenv("KIS_ACCOUNT_PRODUCT_CD", "01")


def _default_account_read_enabled():
    return env_bool(os.environ, "KIS_ACCOUNT_READ_ENABLED", False)


def _default_live_order_enabled():
    return env_bool(os.environ, "KIS_LIVE_ORDER_ENABLED", False)


class KISConfigError(Exception):
    """Raised when a KISConfig cannot be safely constructed or used for
    a network call. Callers must treat this as a hard block."""


@dataclass(frozen=True)
class KISConfig:
    kis_env: str = field(default_factory=_default_kis_env)
    app_key: Optional[str] = field(default_factory=_default_app_key)
    app_secret: Optional[str] = field(default_factory=_default_app_secret)
    account_no: Optional[str] = field(default_factory=_default_account_no)
    account_product_cd: str = field(default_factory=_default_account_product_cd)
    account_read_enabled: bool = field(default_factory=_default_account_read_enabled)
    live_order_enabled: bool = field(default_factory=_default_live_order_enabled)

    @classmethod
    def from_env(cls, env=None):
        load_dotenv()
        mapping = env if env is not None else os.environ
        return cls(
            kis_env=mapping.get("KIS_ENV", "paper").strip().lower(),
            app_key=mapping.get("KIS_APP_KEY"),
            app_secret=mapping.get("KIS_APP_SECRET"),
            account_no=mapping.get("KIS_ACCOUNT_NO"),
            account_product_cd=mapping.get("KIS_ACCOUNT_PRODUCT_CD", "01"),
            account_read_enabled=env_bool(mapping, "KIS_ACCOUNT_READ_ENABLED", False),
            live_order_enabled=env_bool(mapping, "KIS_LIVE_ORDER_ENABLED", False),
        )

    @property
    def is_live(self):
        return self.kis_env == "live"

    @property
    def is_paper(self):
        return self.kis_env == "paper"

    @property
    def base_url(self):
        return LIVE_BASE_URL if self.is_live else MOCK_BASE_URL

    def validate_credentials(self):
        if not self.app_key or not self.app_secret:
            raise KISConfigError("KIS_APP_KEY/KIS_APP_SECRET are missing. Set them outside git (Oracle secrets path).")
        if not self.account_no:
            raise KISConfigError("KIS_ACCOUNT_NO is missing.")

    def validate_read_allowed(self):
        self.validate_credentials()
        if not self.account_read_enabled:
            raise KISConfigError("KIS account read is disabled (KIS_ACCOUNT_READ_ENABLED is not true).")
        return True

    def validate_live_order_allowed(self):
        """Fail-closed order gate, independent of read access -- an
        operator can enable account/price reads (Shadow Mode) without
        also enabling real order submission."""
        self.validate_credentials()
        if self.kis_env not in ("paper", "live"):
            raise KISConfigError(f"KIS_ENV must be 'paper' or 'live', got {self.kis_env!r}")
        if not self.live_order_enabled:
            raise KISConfigError(
                "KIS live order submission is disabled (KIS_LIVE_ORDER_ENABLED is not true). "
                "This is the pre-activation default -- see spec §21/§28."
            )
        return True
