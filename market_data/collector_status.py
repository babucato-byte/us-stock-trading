"""Is the collector alive, and is the market quiet? Two questions.

They were one, and that was the defect. The snapshot persisted only
after a trade was processed, so a genuinely quiet premarket produced an
empty file that looked exactly like a collector that had died — in a
system whose entire premise is that "no data" and "no trades" are
different facts.

So liveness is recorded on a timer and market activity is recorded when
it happens, and nothing infers one from the other:

    heartbeat fresh, no trades   -> CONNECTED_NO_TRADES   (quiet market)
    heartbeat fresh, trades      -> CONNECTED_ACTIVE
    heartbeat old                -> COLLECTOR_STALE       (our problem)
    socket closed                -> DISCONNECTED
    fewer subs than asked        -> SUBSCRIPTION_PARTIAL
    could not start at all       -> FAILED

CONNECTED_NO_TRADES is a normal premarket state for an illiquid name and
must never read as a fault. COLLECTOR_STALE always is one.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

CONNECTED_ACTIVE = "CONNECTED_ACTIVE"
CONNECTED_NO_TRADES = "CONNECTED_NO_TRADES"
COLLECTOR_STALE = "COLLECTOR_STALE"
DISCONNECTED = "DISCONNECTED"
SUBSCRIPTION_PARTIAL = "SUBSCRIPTION_PARTIAL"
FAILED = "FAILED"
UNKNOWN = "UNKNOWN"

CONNECTION_CONNECTED = "CONNECTED"
CONNECTION_DISCONNECTED = "DISCONNECTED"
CONNECTION_FAILED = "FAILED"

#: How old a heartbeat may be before the COLLECTOR is considered stale.
#: This is about OUR process, not about the market -- a quiet market
#: still heartbeats every few seconds.
DEFAULT_HEARTBEAT_STALE_SECONDS = 90.0

#: How often the heartbeat is written. Well under the stale threshold so
#: a single slow loop does not read as death.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15.0


def status_path(*, env=None):
    env = env if env is not None else os.environ
    root = (env.get("REALTIME_BAR_DIR") or env.get("SCANNER_DATA_ROOT")
            or "/home/ubuntu/releases/us-stock-trading/shared/scanner")
    return Path(root) / "realtime_bars" / "collector_status.json"


@dataclass
class CollectorStatus:
    collector_started_at: Optional[datetime] = None
    last_heartbeat_at: Optional[datetime] = None
    connection_state: str = CONNECTION_DISCONNECTED
    subscription_requested: int = 0
    subscription_count: int = 0
    subscribed_symbols: List[str] = field(default_factory=list)
    last_message_at: Optional[datetime] = None
    last_trade_at: Optional[datetime] = None
    trades_observed: int = 0
    market_session: Optional[str] = None
    collector_sha: Optional[str] = None
    data_source: str = "KIS_HDFSCNT0"
    error_count: int = 0
    reconnect_count: int = 0
    last_error: Optional[str] = None

    def heartbeat_age_seconds(self, now=None) -> Optional[float]:
        if self.last_heartbeat_at is None:
            return None
        current = now or datetime.now(timezone.utc)
        return (current - self.last_heartbeat_at).total_seconds()

    def state(self, *, now=None,
              stale_after=DEFAULT_HEARTBEAT_STALE_SECONDS) -> str:
        """One word for "what is the collector doing".

        Deliberately answers about the COLLECTOR. Whether a particular
        symbol's data is fresh enough to trade on is a different
        question, asked of that symbol's bars.
        """
        if self.connection_state == CONNECTION_FAILED:
            return FAILED
        age = self.heartbeat_age_seconds(now)
        if age is None:
            return UNKNOWN
        if age > stale_after:
            # Our process, not the market. A quiet market heartbeats.
            return COLLECTOR_STALE
        if self.connection_state != CONNECTION_CONNECTED:
            return DISCONNECTED
        if (self.subscription_requested
                and self.subscription_count < self.subscription_requested):
            return SUBSCRIPTION_PARTIAL
        if self.trades_observed > 0:
            return CONNECTED_ACTIVE
        # Connected, subscribed, and nothing has traded. Normal for an
        # illiquid name in premarket, and never a fault.
        return CONNECTED_NO_TRADES

    def as_record(self, *, now=None) -> dict:
        return {
            "state": self.state(now=now),
            "collector_started_at": _iso(self.collector_started_at),
            "last_heartbeat_at": _iso(self.last_heartbeat_at),
            "heartbeat_age_seconds": self.heartbeat_age_seconds(now),
            "connection_state": self.connection_state,
            "subscription_requested": self.subscription_requested,
            "subscription_count": self.subscription_count,
            "subscribed_symbols": list(self.subscribed_symbols),
            "last_message_at": _iso(self.last_message_at),
            "last_trade_at": _iso(self.last_trade_at),
            "trades_observed": self.trades_observed,
            "market_session": self.market_session,
            "collector_sha": self.collector_sha,
            "data_source": self.data_source,
            "error_count": self.error_count,
            "reconnect_count": self.reconnect_count,
            "last_error": self.last_error,
        }

    def write(self, path=None, *, env=None, now=None):
        """Persist. Never raises: losing a heartbeat must not stop
        collecting, and the reader treats a missing file as UNKNOWN."""
        target = Path(path) if path else status_path(env=env)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix(".tmp")
            temp.write_text(json.dumps(self.as_record(now=now), indent=2),
                            encoding="utf-8")
            temp.replace(target)
        except Exception:  # noqa: BLE001
            logger.warning("could not write collector status to %s", target,
                           exc_info=True)


def read(path=None, *, env=None) -> Optional[dict]:
    """The last written status, or None. A missing file is not an error
    -- it means no collector has run for this deployment yet."""
    target = Path(path) if path else status_path(env=env)
    try:
        if not target.exists():
            return None
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.warning("could not read collector status at %s", target,
                       exc_info=True)
        return None


def describe(path=None, *, env=None, now=None,
             stale_after=DEFAULT_HEARTBEAT_STALE_SECONDS) -> dict:
    """The status as a reader should see it, with the age RECOMPUTED.

    The stored state was true when written. A collector that died five
    minutes ago left a file saying CONNECTED_NO_TRADES, and believing it
    is exactly the mistake this module exists to prevent -- so the age
    is recomputed against the reader's clock and the state re-derived
    from it.
    """
    record = read(path, env=env)
    if record is None:
        return {"state": UNKNOWN, "reason": "no collector status recorded"}
    current = now or datetime.now(timezone.utc)

    # The state is RE-DERIVED against the reader's clock, not adjusted.
    # Overriding only towards staleness would keep whatever the writer
    # concluded in every other case, and the writer's clock is the one
    # thing a reader checking for a dead process cannot trust.
    revived = CollectorStatus(
        collector_started_at=_parse(record.get("collector_started_at")),
        last_heartbeat_at=_parse(record.get("last_heartbeat_at")),
        connection_state=record.get("connection_state") or CONNECTION_DISCONNECTED,
        subscription_requested=int(record.get("subscription_requested") or 0),
        subscription_count=int(record.get("subscription_count") or 0),
        subscribed_symbols=list(record.get("subscribed_symbols") or ()),
        last_message_at=_parse(record.get("last_message_at")),
        last_trade_at=_parse(record.get("last_trade_at")),
        trades_observed=int(record.get("trades_observed") or 0),
        market_session=record.get("market_session"),
        collector_sha=record.get("collector_sha"),
        data_source=record.get("data_source") or "KIS_HDFSCNT0",
        error_count=int(record.get("error_count") or 0),
        reconnect_count=int(record.get("reconnect_count") or 0),
        last_error=record.get("last_error"),
    )
    out = dict(record)
    out["heartbeat_age_seconds"] = revived.heartbeat_age_seconds(current)
    out["state"] = revived.state(now=current, stale_after=stale_after)
    return out


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else None


def _parse(text):
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
