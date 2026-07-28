import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

import risk_config

load_dotenv()

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL = "https://api.alpaca.markets"


def env_bool(env, name, default):
    value = env.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


# Per-instance default_factory callables. Unlike a plain dataclass field
# default (evaluated once, at module import), a default_factory runs on
# every BrokerConfig() call, so a bare BrokerConfig() in a long-running
# process (dashboard/app.py, order_safety.py, ...) always reflects the
# current environment instead of freezing whatever os.environ held at
# import time. Each factory's literal fallback (paper / disabled / dry-run)
# is the safe default used only when the corresponding env var is unset.
def _default_trading_mode():
    return os.getenv("TRADING_MODE", risk_config.TRADING_MODE).strip().lower()


def _default_enable_real_trading():
    return env_bool(os.environ, "ENABLE_REAL_TRADING", risk_config.ENABLE_REAL_TRADING)


def _default_live_dry_run():
    return env_bool(os.environ, "LIVE_DRY_RUN", risk_config.LIVE_DRY_RUN)


def _default_paper_base_url():
    return os.getenv("ALPACA_PAPER_BASE_URL", PAPER_BASE_URL)


def _default_live_base_url():
    return os.getenv("ALPACA_LIVE_BASE_URL", LIVE_BASE_URL)


def _default_api_key():
    return os.getenv("ALPACA_API_KEY")


def _default_secret_key():
    return os.getenv("ALPACA_SECRET_KEY")


def _default_alpaca_order_enabled():
    # KIS migration (see docs/autonomous/DECISION_LOG.md's "Alpaca
    # data-only / KIS live broker" section): Alpaca is being repositioned
    # as a market-data-only provider. Both flags below default to False
    # per the migration spec's own recommended env block (ALPACA_ORDER_
    # ENABLED=false / ALPACA_PAPER_ORDER_ENABLED=false) -- unset means
    # disabled, never enabled. Neither flag is wired into
    # AlpacaBroker.submit_order() yet (a deliberate, documented sequencing
    # decision -- flipping this on today's still-live Alpaca-paper
    # operational path before brokers/kis_broker.py exists to replace it
    # would leave the whole system with no working order path at all);
    # validate_alpaca_order_permitted() below is the call site the KIS
    # cutover step wires in once it's ready to take over.
    return env_bool(os.environ, "ALPACA_ORDER_ENABLED", False)


def _default_alpaca_paper_order_enabled():
    return env_bool(os.environ, "ALPACA_PAPER_ORDER_ENABLED", False)


@dataclass(frozen=True)
class BrokerConfig:
    trading_mode: str = field(default_factory=_default_trading_mode)
    enable_real_trading: bool = field(default_factory=_default_enable_real_trading)
    live_dry_run: bool = field(default_factory=_default_live_dry_run)
    paper_base_url: str = field(default_factory=_default_paper_base_url)
    live_base_url: str = field(default_factory=_default_live_base_url)
    api_key: Optional[str] = field(default_factory=_default_api_key)
    secret_key: Optional[str] = field(default_factory=_default_secret_key)
    alpaca_order_enabled: bool = field(default_factory=_default_alpaca_order_enabled)
    alpaca_paper_order_enabled: bool = field(default_factory=_default_alpaca_paper_order_enabled)

    @classmethod
    def from_env(cls, env=None):
        """Explicit factory: read the given mapping (default os.environ) at
        call time and build a BrokerConfig from it. Prefer this over a bare
        BrokerConfig() whenever the caller wants to be explicit about
        re-reading the environment right before a safety-sensitive
        decision (see validate_order_allowed_now() below)."""
        load_dotenv()
        mapping = env if env is not None else os.environ
        return cls(
            trading_mode=mapping.get("TRADING_MODE", risk_config.TRADING_MODE).strip().lower(),
            enable_real_trading=env_bool(mapping, "ENABLE_REAL_TRADING", risk_config.ENABLE_REAL_TRADING),
            live_dry_run=env_bool(mapping, "LIVE_DRY_RUN", risk_config.LIVE_DRY_RUN),
            paper_base_url=mapping.get("ALPACA_PAPER_BASE_URL", PAPER_BASE_URL),
            live_base_url=mapping.get("ALPACA_LIVE_BASE_URL", LIVE_BASE_URL),
            api_key=mapping.get("ALPACA_API_KEY"),
            secret_key=mapping.get("ALPACA_SECRET_KEY"),
            alpaca_order_enabled=env_bool(mapping, "ALPACA_ORDER_ENABLED", False),
            alpaca_paper_order_enabled=env_bool(mapping, "ALPACA_PAPER_ORDER_ENABLED", False),
        )

    @property
    def is_live_mode(self):
        return self.trading_mode == "live"

    @property
    def is_paper_mode(self):
        return self.trading_mode == "paper"

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
        if self.is_paper_mode and self.base_url.rstrip("/") == PAPER_BASE_URL:
            return True
        if self.is_paper_mode:
            raise RuntimeError(
                "Paper order blocked because ALPACA_PAPER_BASE_URL is not the official Paper endpoint."
            )
        if self.can_submit_live_order:
            raise RuntimeError("Real live trading is disabled in this pre-live PR. Use live dry-run only.")
        raise RuntimeError(
            "Order blocked. TRADING_MODE must be exactly 'paper'; live and unknown modes are disabled."
        )


    def validate_alpaca_order_permitted(self):
        """KIS migration fail-closed gate: raises RuntimeError unless the
        flag matching the CURRENT trading_mode is explicitly True. This is
        independent of (and stricter than) validate_order_allowed() above
        -- that method governs Paper-vs-Live safety within "Alpaca is
        allowed to place orders"; this one governs whether Alpaca is
        allowed to place ANY order at all, live or paper, now that KIS is
        the sole live-order broker. Not yet called from
        AlpacaBroker.submit_order() -- see _default_alpaca_order_enabled()'s
        docstring for why."""
        if self.is_live_mode:
            if not self.alpaca_order_enabled:
                raise RuntimeError(
                    "Alpaca live orders are disabled (ALPACA_ORDER_ENABLED is not set to true). "
                    "Alpaca is data-only in this deployment; KIS is the sole live-order broker."
                )
            return True
        if self.is_paper_mode:
            if not self.alpaca_paper_order_enabled:
                raise RuntimeError(
                    "Alpaca paper orders are disabled (ALPACA_PAPER_ORDER_ENABLED is not set to "
                    "true). Alpaca is data-only in this deployment."
                )
            return True
        raise RuntimeError(
            f"Alpaca order blocked: unrecognized trading_mode {self.trading_mode!r}."
        )


def validate_order_allowed_now(env=None):
    """Re-read the environment right now and validate it allows an order.

    Intended to be called immediately before order submission, in addition
    to (not instead of) validating whatever BrokerConfig instance the
    caller already holds -- this closes the window where the environment
    changed after that instance was constructed.
    """
    return BrokerConfig.from_env(env=env).validate_order_allowed()
