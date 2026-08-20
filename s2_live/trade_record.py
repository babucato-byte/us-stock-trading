"""What every S2 position leaves behind, live or shadow.

Why this exists separately from the position store
--------------------------------------------------
The position store holds what the executor needs to ACT: enough state to
make the next decision and no more. This holds what the month-1 review
needs to JUDGE, which is a different and larger set -- and most of it
cannot be reconstructed afterwards.

`peak_volume_multiple` and `price_at_volume_peak` are gone the moment the
volume falls. `effective_stop` recomputed later would use whatever the
config says then, not what was in force at the time, and the point of the
re-evaluation in §7 is precisely that the config is expected to change.
`post_stop_return_30m` is unanswerable unless someone goes back and asks.

So the rule for what belongs here: a field earns its place by being
impossible or misleading to recompute later.

Shadow trades count
-------------------
A candidate that was published and never bought produces the same record
with `live=False`. That is the comparison the review actually needs --
"the ones we took did better than the ones we skipped" is only a finding
if both sides were measured the same way, and measuring only the live
ones would make every review a study of survivors.

Nothing here decides anything
-----------------------------
This module records. It imports no policy, evaluates no exit, and cannot
influence a position: a measurement that feeds back into the thing it
measures stops being a measurement.
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TRADE_DIR_ENV = "S2_TRADE_RECORD_DIR"
DEFAULT_SUBDIR = "s2_trades"
SCHEMA_VERSION = "s2_trade_record_v1"


@dataclass
class S2TradeRecord:
    """One S2 position, live or shadow, from entry to after the exit."""

    symbol: str
    trading_day: str
    session: Optional[str] = None
    strategy_id: str = "S2_VOLUME_ACCUMULATION_V1"
    #: False for a published candidate that was never bought. The
    #: comparison between the two is the point of collecting either.
    live: bool = False

    # --- entry ---
    entry_time: Optional[str] = None
    entry_price: Optional[float] = None
    entry_volume_multiple: Optional[float] = None
    baseline_volume: Optional[float] = None
    signal_price: Optional[float] = None

    # --- stops as they stood AT THE TIME, not as recomputed later ---
    effective_stop: Optional[float] = None
    structural_stop: Optional[float] = None
    hard_stop: Optional[float] = None
    max_loss_pct: Optional[float] = None

    # --- the volume story, which cannot be recovered afterwards ---
    peak_volume_multiple: Optional[float] = None
    price_at_volume_peak: Optional[float] = None
    current_volume_multiple: Optional[float] = None
    volume_decay_ratio: Optional[float] = None
    time_to_volume_peak_minutes: Optional[float] = None

    # --- price path ---
    current_price: Optional[float] = None
    vwap: Optional[float] = None
    mfe_pct: Optional[float] = None
    mae_pct: Optional[float] = None
    time_to_peak_minutes: Optional[float] = None

    # --- exit ---
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    exit_detail: Dict[str, Any] = field(default_factory=dict)
    time_to_stop_minutes: Optional[float] = None

    # --- what happened AFTER, which is why the record is kept ---
    #: Whether exiting was right. A stop that consistently precedes a
    #: recovery is a stop that is too tight, and nothing else in the
    #: system can notice that.
    post_stop_return_30m: Optional[float] = None
    post_stop_return_1h: Optional[float] = None

    provenance: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _finite(value) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number or number in (
        float("inf"), float("-inf")) else number


def _minutes(start, end) -> Optional[float]:
    if start is None or end is None:
        return None
    try:
        delta = (end - start).total_seconds() / 60.0
    except Exception:  # noqa: BLE001
        return None
    return delta if delta >= 0 else None


def excursion_pct(entry_price, extreme_price) -> Optional[float]:
    """Percent move from entry to an extreme.

    Not clamped here. MFE is clamped at zero where it is COMPUTED from a
    running high, because "the favourable excursion" of something that
    never went favourable is zero; but this helper is also used for MAE
    and for post-exit returns, where a sign is the answer rather than an
    artefact. The caller clamps if clamping is what it means.
    """
    entry = _finite(entry_price)
    extreme = _finite(extreme_price)
    if entry is None or extreme is None or entry <= 0:
        return None
    return (extreme - entry) / entry * 100.0


def from_decision(state, decision, *, symbol=None, trading_day, session=None,
                  live=False, entry_time=None, now=None,
                  signal_price=None) -> S2TradeRecord:
    """Build a record from a position and the decision just taken.

    The decision's `detail` already carries the volume picture and the
    stop levels -- `exit_policy` puts them on every decision, HOLD
    included, precisely so this does not have to recompute them from a
    config that may since have changed.
    """
    detail = dict(getattr(decision, "detail", None) or {})
    sells = bool(getattr(decision, "sells", False))
    record = S2TradeRecord(
        symbol=symbol or getattr(state, "symbol", ""),
        trading_day=trading_day,
        session=session,
        live=live,
        entry_time=entry_time.isoformat() if hasattr(entry_time, "isoformat")
        else entry_time,
        entry_price=_finite(getattr(state, "entry_price", None)),
        entry_volume_multiple=detail.get("entry_volume_multiple"),
        baseline_volume=_finite(getattr(state, "baseline_volume", None)),
        signal_price=_finite(signal_price),
        effective_stop=detail.get("effective_stop"),
        structural_stop=detail.get("structural_stop"),
        hard_stop=detail.get("hard_stop"),
        max_loss_pct=detail.get("max_loss_pct"),
        peak_volume_multiple=detail.get("peak_volume_multiple"),
        price_at_volume_peak=detail.get("price_at_volume_peak"),
        current_volume_multiple=detail.get("current_volume_multiple"),
        volume_decay_ratio=detail.get("volume_decay_ratio"),
        current_price=detail.get("current_price"),
        vwap=detail.get("vwap"),
        exit_reason=getattr(decision, "reason", None) if sells else None,
        exit_detail=detail if sells else {},
        exit_price=detail.get("current_price") if sells else None,
        exit_time=(now.isoformat() if sells and hasattr(now, "isoformat")
                   else None),
        time_to_stop_minutes=_minutes(entry_time, now) if sells else None,
        provenance={"schema": SCHEMA_VERSION,
                    "recorded_at": (now or datetime.now(timezone.utc)).isoformat()
                    if hasattr(now or datetime.now(timezone.utc), "isoformat")
                    else None},
    )
    return record


def trade_dir() -> Path:
    configured = os.environ.get(TRADE_DIR_ENV)
    if configured and str(configured).strip():
        return Path(str(configured).strip())
    from scanners.base import result_store

    return result_store.analytics_dir() / DEFAULT_SUBDIR


def trades_path(trading_day: str) -> Path:
    directory = trade_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{trading_day}.jsonl"


def append(record: S2TradeRecord) -> bool:
    """Write one record. Never raises.

    A failed write must not fail a trade: the position is real whether or
    not the study of it was saved. The loss is logged rather than
    swallowed silently, because a study with a hole in it that nobody
    knows about is worse than one that is known to be incomplete.
    """
    try:
        with trades_path(record.trading_day).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.as_dict(), sort_keys=True,
                                default=str) + "\n")
        return True
    except Exception:  # noqa: BLE001
        logger.warning("could not record the S2 trade for %s on %s",
                       record.symbol, record.trading_day, exc_info=True)
        return False


def read(trading_day: str) -> List[Dict[str, Any]]:
    """Every record for a day. Missing file -> []."""
    try:
        path = trades_path(trading_day)
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                logger.warning("skipping unparseable S2 trade row in %s", path)
        return rows
    except Exception:  # noqa: BLE001
        logger.warning("could not read S2 trades for %s", trading_day,
                       exc_info=True)
        return []
