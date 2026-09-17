"""The account's total USD equity, read once a day and then left alone.

What this is for
----------------
Discovery ranks thousands of names, and some of them cost more for ONE
share than the whole account is worth. Those can never become an S6
entry, and every provider call spent on them is spent for nothing. This
captures the account's equity once, during the daily universe refresh,
so the universe builder can drop that class of symbol offline.

It is a COST filter, exactly like `discovery/eligible_universe.py`, and
it inherits that module's asymmetry: wrongly including an unaffordable
name costs one provider call and is caught by every gate downstream,
while wrongly excluding an affordable one removes it from discovery
entirely. So this excludes ONLY when the comparison is unambiguous, and
an equity figure it could not obtain excludes nothing at all.

Nothing here is an order gate. `get_orderable_usd()` (TTTS3007R) remains
the sole authority on whether a specific order can be paid for, and it
is re-read per symbol and price immediately before every submission.
This value is a day-old upper bound used to decide what to LOOK at.

Why the equity is composed rather than read from one field
----------------------------------------------------------
Because KIS does not publish one. A live read-only probe on 2026-08-16
(recorded in `kis_broker.VERIFICATION_MATRIX` as
`account_equity_not_in_tot_asst_amt`) tested every candidate against
`get_orderable_usd()`, which is known to answer in USD:

    output2[USD].frcr_dncl_amt_2      EXACT MATCH      <- USD cash
    output2[USD].frcr_drwg_psbl_amt_1 EXACT MATCH
    output2[USD].frcr_evlu_amt2       x1,414.88        (FX scale -> KRW)
    output3.frcr_use_psbl_amt         x1,414.88        (FX scale -> KRW)
    output3.tot_dncl_amt              x4,429.30        (KRW, all currencies)
    output3.tot_asst_amt              x5,844.18        (KRW, all currencies)

`tot_asst_amt` is the only field whose NAME says "total assets", which is
precisely why the probe disproved it rather than ignoring it: it is a
won-denominated total across every currency the account holds, and
output3 returned identical values whether the request asked for the KRW
or the foreign-currency division. Using it would put KRW cash and an
implicit FX rate into this comparison.

So equity is composed from the two figures that ARE live-verified USD:

    equity = USD cash                      (CTRP6504R output2[USD].frcr_dncl_amt_2)
           + SUM over positions of
               quantity * average_fill_price + unrealized_pnl
                                           (TTTS3012R output1, TR_CRCY_CD=USD)

The position term is cost basis plus unrealized P&L, i.e. current market
value, built from the same fields `get_positions()` already parses for
production use rather than from a raw field this module reaches for on
its own.
"""

import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

FILENAME = "DAILY_ACCOUNT_EQUITY.json"

#: Owner-only, as every other file in shared/state is. Set on the
#: temporary file BEFORE the rename, so the snapshot is never briefly
#: world-readable at its real name.
SNAPSHOT_MODE = 0o600
SNAPSHOT_ENV = "DAILY_ACCOUNT_EQUITY_FILE"

SCHEMA_VERSION = 1

#: Why a capture produced no value. Each is a reason, never a number.
UNAVAILABLE_NO_BROKER = "NO_BROKER"
UNAVAILABLE_CASH_READ_FAILED = "CASH_READ_FAILED"
UNAVAILABLE_POSITION_READ_FAILED = "POSITION_READ_FAILED"
UNAVAILABLE_NOT_FINITE = "EQUITY_NOT_FINITE"

#: The wire values this figure is composed from, carried on the snapshot
#: so a reader a month later can tell WHICH fields produced it.
CASH_SOURCE = "CTRP6504R:output2[crcy_cd=USD].frcr_dncl_amt_2"
POSITION_SOURCE = "TTTS3012R:output1(ovrs_cblc_qty,pchs_avg_pric,evlu_pfls_amt)"


def snapshot_path(explicit=None, env=None) -> Optional[Path]:
    """Where the snapshot lives, or None when no state directory is known.

    Resolved the way `brokers/route_evidence.evidence_path` resolves its
    store, rather than with a path system of this module's own: an
    explicit argument, then this module's own env override, then the
    directory the state DATABASE already lives in.

    There is deliberately NO repository-relative default. The first
    version had one -- `BASE_DIR / FILENAME` -- following the in-repo
    convention of `reconciliation_state` and its siblings, and on the
    Oracle host that resolves INSIDE the release directory:

        /home/ubuntu/releases/us-stock-trading/<sha>/DAILY_ACCOUNT_EQUITY.json

    which does not survive a release switch (so the cap silently reverted
    to "none" after every deploy) and left an untracked file in the
    release worktree that the deploy integrity check flags. Those
    siblings get away with it only because production redirects every one
    of them by env var; a module that is NOT yet in that env file lands
    in the release tree.

    None is the honest answer for a process with no state configuration
    -- a test, a laptop shell. It means no snapshot is read and no cap is
    applied, which is the same safe direction every other uncertainty in
    this module takes.
    """
    if explicit is not None:
        return Path(explicit)
    source = env if env is not None else os.environ
    override = source.get(SNAPSHOT_ENV)
    if override and override.strip():
        return Path(override)
    # The state database is the canonical marker for "the persistent
    # shared state directory", and is what route_evidence keys off too.
    state_db = source.get("STATE_STORE_DB_FILE") or source.get("TRADING_STATE_DB")
    if state_db and state_db.strip():
        return Path(state_db).parent / FILENAME
    return None


def capture(broker, *, trading_date, now=None) -> Dict[str, Any]:
    """Read the account once and return the snapshot to persist.

    Never raises for a read failure: a snapshot that says
    `equity_usd = None` with a reason is the fail-closed answer, and the
    universe builder treats it as "no cap" rather than "cap of zero".
    Returning 0.0 here would exclude the entire universe.
    """
    moment = now or datetime.now(timezone.utc)
    base = {
        "schema_version": SCHEMA_VERSION,
        "trading_date": str(trading_date),
        "captured_at": moment.isoformat(),
        "equity_usd": None,
        "cash_usd": None,
        "positions_usd": None,
        "position_count": None,
        "cash_source": CASH_SOURCE,
        "position_source": POSITION_SOURCE,
        "unavailable_reason": None,
    }
    if broker is None:
        base["unavailable_reason"] = UNAVAILABLE_NO_BROKER
        return base

    try:
        cash = float(broker.get_account_cash_usd())
    except Exception as exc:  # noqa: BLE001 - a reason, never a guess
        logger.warning("daily equity: USD cash read failed (%s); the universe "
                       "keeps its current behaviour", type(exc).__name__)
        base["unavailable_reason"] = UNAVAILABLE_CASH_READ_FAILED
        return base

    try:
        positions = broker.get_positions() or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily equity: position read failed (%s); the universe "
                       "keeps its current behaviour", type(exc).__name__)
        base["unavailable_reason"] = UNAVAILABLE_POSITION_READ_FAILED
        return base

    held = 0.0
    counted = 0
    for position in positions:
        value = _position_value_usd(position)
        if value is None:
            # One unreadable row must not silently shrink the total, which
            # would make the cap tighter than the account really is and
            # exclude names it can afford.
            logger.warning("daily equity: position row for %r has no usable "
                           "USD value; reporting the equity unavailable",
                           getattr(position, "symbol", "?"))
            base["unavailable_reason"] = UNAVAILABLE_POSITION_READ_FAILED
            return base
        held += value
        counted += 1

    equity = cash + held
    if not math.isfinite(equity) or equity < 0:
        base["unavailable_reason"] = UNAVAILABLE_NOT_FINITE
        return base

    base.update({"equity_usd": equity, "cash_usd": cash,
                 "positions_usd": held, "position_count": counted})
    return base


def _position_value_usd(position) -> Optional[float]:
    """Current USD market value of one holding, or None.

    quantity * average_fill_price is the cost basis and `unrealized_pnl`
    carries it to market. Both come from the USD-denominated balance read
    that `get_positions()` already parses, so this adds no new wire field
    and inherits that parser's validation.
    """
    try:
        quantity = float(getattr(position, "quantity", None))
        average = float(getattr(position, "average_fill_price", None))
    except (TypeError, ValueError):
        return None
    unrealized = getattr(position, "unrealized_pnl", 0.0)
    try:
        unrealized = float(unrealized if unrealized is not None else 0.0)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (quantity, average, unrealized)):
        return None
    if quantity < 0 or average < 0:
        return None
    return quantity * average + unrealized


def write(snapshot, path=None) -> Optional[Path]:
    """Persist atomically; the reader never sees a half-written file.

    Returns None without writing when no state directory is known, for
    the same reason `snapshot_path` returns None: a process with no state
    configuration must not drop this file into whatever directory it
    happens to be standing in.
    """
    target = snapshot_path(path)
    if target is None:
        logger.warning("no state directory configured (%s / STATE_STORE_DB_FILE "
                       "unset); the daily equity snapshot was not written",
                       SNAPSHOT_ENV)
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(snapshot, indent=2, sort_keys=True),
                    encoding="utf-8")
    # 0600 before the rename, matching RECONCILIATION.json, KILL_SWITCH.json
    # and every other file in shared/state. The directory is already 0700,
    # so this is defence in depth rather than the only barrier -- but this
    # file carries the account's cash and equity totals, and it should not
    # be the one readable thing in there.
    os.chmod(temp, SNAPSHOT_MODE)
    os.replace(temp, target)
    return target


def read(path=None) -> Optional[Dict[str, Any]]:
    target = snapshot_path(path)
    if target is None:
        return None
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 - unreadable is "no snapshot"
        logger.warning("daily equity snapshot unreadable; the universe keeps "
                       "its current behaviour", exc_info=True)
        return None
    return document if isinstance(document, dict) else None


def equity_for(trading_date, *, path=None) -> Optional[float]:
    """The cap for `trading_date`, or None for "no cap".

    None is returned for every uncertainty -- no snapshot, a snapshot
    from another day, a snapshot that recorded a reason instead of a
    number. The caller must treat None as "do not exclude anything", so
    that a failed read can never empty the universe.

    A snapshot from a DIFFERENT trading date is refused rather than
    reused: the point of capturing daily is that the figure belongs to
    its day, and silently applying yesterday's is the one way this could
    exclude a name the account can now afford.
    """
    document = read(path)
    if not document:
        return None
    if str(document.get("trading_date") or "") != str(trading_date):
        return None
    value = document.get("equity_usd")
    if value is None:
        return None
    try:
        equity = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(equity) or equity <= 0:
        # A zero or negative equity is not a cap that excludes everything;
        # it is a figure this module will not act on.
        return None
    return equity


def excludes(price, equity) -> bool:
    """Whether one share at `price` is beyond `equity`.

    Strictly greater-than: an account holding exactly the share price can
    buy the share, and the real orderable-cash check decides whether it
    actually may.
    """
    if equity is None or price is None:
        return False
    try:
        share = float(price)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(share) or share <= 0:
        return False
    return share > float(equity)
