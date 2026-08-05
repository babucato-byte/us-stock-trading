"""T8: the daily account-cash figure the universe filter sizes against.

Split from `universe_filter.py` on purpose: this is the only part of the
T8 path that touches a real account. `refresh_budget()` performs the KIS
read; everything downstream (`universe_builder`, `universe_filter`) reads
the persisted JSON and never opens a socket, which is what lets the whole
build be tested with no broker at all.

Failure policy (T8: "조회 실패 시 직전값 유지"): a failed KIS read leaves
the previously persisted state file byte-for-byte untouched and returns
it with `stale=True`. It never fabricates a figure, never falls back to
zero, and never widens the previous ceiling. If there is no previous
state at all, `refresh_budget()` returns None and the caller must not
build a filtered universe -- there is no safe default for "how much cash
does the account have".

Cash figure: `AccountSnapshot.usd_available_for_new_order`, i.e. KIS's
own orderable amount minus this codebase's durable open-order
reservations -- the same property `domain/account_snapshot.py` documents
as the two-sided floor. `usd_cash` (total deposit) is deliberately NOT
used: it can exceed what is actually orderable.
"""

import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config.paths import get_project_root
from universe_filter import UniverseBudget

DEFAULT_STATE_FILE = get_project_root() / "state" / "universe_budget.json"

SOURCE_KIS = "kis_balance"
SOURCE_CACHED_PREFIX = "cached:"

STATE_VERSION = 1


class UniverseBudgetError(Exception):
    """Raised only when a *persisted* state file is unusable. A live KIS
    read failure is not an error here -- it is the documented fall back to
    the previous value."""


def _state_path(path=None):
    if path is not None:
        return Path(path)
    override = os.environ.get("UNIVERSE_BUDGET_STATE_FILE", "").strip()
    return Path(override) if override else DEFAULT_STATE_FILE


def _is_usable_cash(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


@dataclass(frozen=True)
class BudgetState:
    available_cash_usd: float
    as_of: str
    source: str
    account_id: str = ""
    stale: bool = False

    def to_budget(self) -> UniverseBudget:
        return UniverseBudget(
            available_cash_usd=self.available_cash_usd,
            as_of=self.as_of,
            source=self.source,
        )

    def as_dict(self):
        return {
            "version": STATE_VERSION,
            "available_cash_usd": self.available_cash_usd,
            "as_of": self.as_of,
            "source": self.source,
            "account_id": self.account_id,
        }


def load_budget_state(path=None) -> Optional[BudgetState]:
    """Returns the persisted state, or None if the file does not exist.

    Raises UniverseBudgetError for a file that exists but is corrupt or
    carries an unusable cash figure -- fail closed. Silently treating a
    corrupt state file as "no budget yet" would let a later successful
    write be built on top of an unnoticed data-loss event.
    """
    file_path = _state_path(path)
    if not file_path.exists():
        return None
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        raise UniverseBudgetError(f"universe budget state is unreadable ({file_path}): {exc}") from exc
    if not isinstance(payload, dict):
        raise UniverseBudgetError(f"universe budget state is not an object ({file_path})")
    cash = payload.get("available_cash_usd")
    if not _is_usable_cash(cash):
        raise UniverseBudgetError(
            f"universe budget state has an unusable available_cash_usd {cash!r} ({file_path})"
        )
    as_of = payload.get("as_of")
    if not isinstance(as_of, str) or not as_of.strip():
        raise UniverseBudgetError(f"universe budget state has no usable as_of ({file_path})")
    source = payload.get("source")
    if not isinstance(source, str) or not source.strip():
        raise UniverseBudgetError(f"universe budget state has no usable source ({file_path})")
    return BudgetState(
        available_cash_usd=float(cash),
        as_of=as_of,
        source=source,
        account_id=str(payload.get("account_id") or ""),
    )


def save_budget_state(state: BudgetState, path=None) -> Path:
    """Atomic write (temp + fsync + os.replace), same technique as
    scalping_watchlist/atomic_io.py -- a crash mid-write must never leave
    a half-written cash figure that the next build would trust."""
    file_path = _state_path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(file_path.parent), prefix=f".{file_path.stem}_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state.as_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, file_path)
    except Exception:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise
    return file_path


def state_from_account_snapshot(snapshot, *, source=SOURCE_KIS) -> BudgetState:
    """Converts a `domain.account_snapshot.AccountSnapshot` (what
    `brokers/kis_broker.py::KISBroker.get_account_snapshot()` returns)
    into a persistable budget state. The snapshot type already validates
    non-negative/finite cash and a tz-aware `as_of` in __post_init__."""
    cash = snapshot.usd_available_for_new_order
    if not _is_usable_cash(cash):  # pragma: no cover -- AccountSnapshot forbids it
        raise UniverseBudgetError(f"account snapshot produced an unusable cash figure {cash!r}")
    return BudgetState(
        available_cash_usd=float(cash),
        as_of=snapshot.as_of.astimezone(timezone.utc).isoformat(),
        source=source,
        account_id=getattr(snapshot, "account_id", "") or "",
    )


def refresh_budget(broker, *, path=None, now=None, logger=print):
    """Query KIS, persist on success, keep the previous value on failure.

    Returns (state, error_text). `error_text` is None on a successful
    read. On failure the returned state is the previously persisted one
    with `stale=True` and `source` prefixed `cached:`, or None when there
    is no previous state to keep.

    A failure here is never re-raised: the daily runner must still be able
    to log the outcome and exit deliberately rather than crash.
    """
    try:
        snapshot = broker.get_account_snapshot()
        state = state_from_account_snapshot(snapshot)
    except Exception as exc:  # noqa: BLE001 -- any read failure means "keep previous"
        error = f"{type(exc).__name__}: {exc}"
        logger(f"[UNIVERSE BUDGET] KIS balance read failed, keeping previous value: {error}")
        try:
            previous = load_budget_state(path)
        except UniverseBudgetError as load_exc:
            logger(f"[UNIVERSE BUDGET] no usable previous value: {load_exc}")
            return None, error
        if previous is None:
            logger("[UNIVERSE BUDGET] no previous value persisted; cannot size the universe")
            return None, error
        stale = BudgetState(
            available_cash_usd=previous.available_cash_usd,
            as_of=previous.as_of,
            source=(
                previous.source
                if previous.source.startswith(SOURCE_CACHED_PREFIX)
                else f"{SOURCE_CACHED_PREFIX}{previous.source}"
            ),
            account_id=previous.account_id,
            stale=True,
        )
        return stale, error

    save_budget_state(state, path)
    stamped = now or datetime.now(timezone.utc)
    logger(
        f"[UNIVERSE BUDGET] KIS balance read ok at {stamped.isoformat()}: "
        f"available_cash_usd={state.available_cash_usd}"
    )
    return state, None


def resolve_budget(path=None) -> Optional[UniverseBudget]:
    """What the builder calls: read the persisted figure, no network.

    Returns None when nothing has ever been persisted -- the caller must
    then refuse to write a filtered universe.
    """
    state = load_budget_state(path)
    if state is None:
        return None
    return state.to_budget()
