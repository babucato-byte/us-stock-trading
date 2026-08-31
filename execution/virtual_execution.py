"""Where a PAPER strategy's orders go instead of to a broker.

Why this exists
---------------
Before this, a non-live strategy was DISCOVERY_ONLY: its signals were
recorded and that was all. That is enough to ask "did it fire?" and not
enough to ask "would it have made money?", because a signal is not a
trade. Without an entry price, a holding period, an exit and a realised
number, a scanner cannot be evaluated -- and promoting one to LIVE on
signal counts alone means finding out with real money.

So a PAPER strategy runs the same lifecycle a LIVE one does: intent,
fill, open position, monitoring, exit intent, close, realised PnL. The
only difference is which engine the intent reaches. That is the point:
promotion should be a mode change, not a rewrite.

The fill model, stated rather than assumed
------------------------------------------
Deliberately simple and conservative, and written down because an
unstated fill model is how paper results quietly become optimistic:

  ENTRY   fills at the decision price the strategy supplies. No
          slippage, no spread, no queue position, no partial fills.
  EXIT    fills at the price supplied at exit time.
  PRICE   a missing or non-positive price is REFUSED, not defaulted.
          A fabricated fill is worse than a missing one -- it produces a
          number that looks like evidence.
  QUANTITY whole shares, at least one, matching the live rule. A
          fractional or zero quantity is refused rather than rounded,
          because rounding would silently change what the strategy said.
  SESSION recorded, never used to reject. Which sessions a strategy may
          trade is the strategy's question and it has already been asked
          by the time an intent arrives here.

This is for strategy validation, not broker simulation. It will read
better than reality -- that is a known property of the model, not a
result. Anything measured here is an upper bound.

What it is not
--------------
It never authenticates, never reads an account, never contacts a broker,
and cannot cause a real order. A failure here is confined to the
strategy that caused it.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STATUS_OPEN = "OPEN"
STATUS_CLOSED = "CLOSED"

#: Refusals. Each names what was wrong, because a paper engine that
#: silently declines teaches nothing.
REFUSED_NO_PRICE = "VIRTUAL_REFUSED_NO_PRICE"
REFUSED_QUANTITY = "VIRTUAL_REFUSED_QUANTITY"
REFUSED_NO_SYMBOL = "VIRTUAL_REFUSED_NO_SYMBOL"
REFUSED_ALREADY_OPEN = "VIRTUAL_REFUSED_ALREADY_OPEN"
REFUSED_NOT_OPEN = "VIRTUAL_REFUSED_NOT_OPEN"

#: The fill model in force, recorded on every row so a later reader can
#: tell which assumptions produced a number.
FILL_MODEL = "DECISION_PRICE_V1"


def log_path(trading_day, *, env=None):
    """No production default -- see `slippage_log.log_path` for why."""
    env = env if env is not None else os.environ
    root = env.get("VIRTUAL_EXECUTION_DIR") or env.get("SCANNER_DATA_ROOT")
    if not root:
        return None
    return Path(root) / "virtual_execution" / f"{trading_day}.jsonl"


def _price(value) -> Optional[float]:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _quantity(value) -> Optional[int]:
    """Whole shares, at least one -- the live rule, unchanged.

    Refused rather than rounded: rounding would silently change what the
    strategy asked for, and a paper record of a trade nobody intended is
    worse than no record.
    """
    try:
        if float(value) != int(float(value)):
            return None
        quantity = int(float(value))
    except (TypeError, ValueError):
        return None
    return quantity if quantity >= 1 else None


class VirtualExecutionEngine:
    """One engine, used by every PAPER strategy.

    Deliberately not subclassed per scanner. Six slightly different
    virtual fills would make six scanners' results incomparable, which
    defeats the reason for keeping them.
    """

    def __init__(self, *, store=None, env=None):
        #: symbol -> open virtual position, per strategy.
        self._open: Dict[str, Dict[str, Any]] = dict(store or {})
        self._env = env

    # -- entry ----------------------------------------------------------

    def submit_buy(self, *, strategy_id, scanner, symbol, quantity,
                   decision_price, session=None, trading_day=None,
                   signal_id=None, now=None) -> Dict[str, Any]:
        """A BUY intent becomes a virtual fill and an open position."""
        moment = now or datetime.now(timezone.utc)
        name = str(symbol or "").upper()
        if not name:
            return {"accepted": False, "reason": REFUSED_NO_SYMBOL}
        price = _price(decision_price)
        if price is None:
            return {"accepted": False, "reason": REFUSED_NO_PRICE,
                    "symbol": name}
        shares = _quantity(quantity)
        if shares is None:
            return {"accepted": False, "reason": REFUSED_QUANTITY,
                    "symbol": name, "requested_quantity": quantity}
        key = (strategy_id, name)
        if key in self._open:
            # One open virtual position per (strategy, symbol), mirroring
            # the live per-symbol rule. Without it a scanner that fires
            # every tick would accumulate a position per tick and report
            # a return no real account could have had.
            return {"accepted": False, "reason": REFUSED_ALREADY_OPEN,
                    "symbol": name}

        position = {
            "virtual_position_id": f"vpos_{uuid.uuid4().hex[:16]}",
            "strategy_id": strategy_id,
            "scanner": scanner,
            "symbol": name,
            "side": "buy",
            "quantity": shares,
            "entry_at": moment.isoformat(),
            "entry_session": session,
            "trading_day": trading_day,
            "entry_decision_price": price,
            "entry_fill_price": price,
            "signal_id": signal_id,
            "status": STATUS_OPEN,
            "fill_model": FILL_MODEL,
        }
        self._open[key] = position
        return {"accepted": True, "position": dict(position)}

    # -- exit -----------------------------------------------------------

    def submit_sell(self, *, strategy_id, symbol, decision_price,
                    exit_reason, session=None, now=None) -> Dict[str, Any]:
        """A SELL intent closes the virtual position and realises PnL."""
        moment = now or datetime.now(timezone.utc)
        name = str(symbol or "").upper()
        key = (strategy_id, name)
        position = self._open.get(key)
        if position is None:
            return {"accepted": False, "reason": REFUSED_NOT_OPEN,
                    "symbol": name}
        price = _price(decision_price)
        if price is None:
            # The position stays OPEN. Closing it at an unknown price
            # would invent the one number the whole record exists for.
            return {"accepted": False, "reason": REFUSED_NO_PRICE,
                    "symbol": name}

        entry = position["entry_fill_price"]
        shares = position["quantity"]
        closed = dict(position)
        closed.update({
            "status": STATUS_CLOSED,
            "exit_at": moment.isoformat(),
            "exit_session": session,
            "exit_reason": exit_reason,
            "exit_decision_price": price,
            "exit_fill_price": price,
            "realized_pnl": round((price - entry) * shares, 6),
            "realized_pnl_pct": round((price / entry - 1.0) * 100.0, 6),
        })
        del self._open[key]
        return {"accepted": True, "position": closed}

    # -- state ----------------------------------------------------------

    def open_positions(self, strategy_id=None) -> List[Dict[str, Any]]:
        return [dict(p) for (sid, _sym), p in self._open.items()
                if strategy_id is None or sid == strategy_id]

    def is_open(self, strategy_id, symbol) -> bool:
        return (strategy_id, str(symbol or "").upper()) in self._open


def record(row, *, trading_day, env=None) -> bool:
    """Append one virtual lifecycle row. Never raises."""
    try:
        path = log_path(trading_day, env=env)
        if path is None:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, default=str) + "\n")
        return True
    except Exception:  # noqa: BLE001 - a paper record must never cost
        # anything; it is not on any order path.
        logger.warning("could not record a virtual execution row",
                       exc_info=True)
        return False


def read(trading_day, *, env=None) -> List[Dict[str, Any]]:
    path = log_path(trading_day, env=env)
    rows = []
    try:
        if path is None or not path.exists():
            return rows
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    logger.warning("skipping an unreadable virtual row")
    except Exception:  # noqa: BLE001
        logger.warning("could not read virtual execution for %s", trading_day,
                       exc_info=True)
    return rows


def performance(trading_day, *, strategy_id=None, env=None) -> Dict[str, Any]:
    """Realised results for closed virtual trades.

    Open positions are counted, never valued. Marking them to market
    would mix realised and unrealised into one number, which is the
    figure people then quote.
    """
    closed, opened = [], 0
    for row in read(trading_day, env=env):
        if strategy_id and row.get("strategy_id") != strategy_id:
            continue
        if row.get("status") == STATUS_CLOSED:
            closed.append(row)
        elif row.get("status") == STATUS_OPEN:
            opened += 1
    values = sorted(r["realized_pnl"] for r in closed
                    if isinstance(r.get("realized_pnl"), (int, float)))
    wins = [v for v in values if v > 0]
    return {
        "strategy_id": strategy_id,
        "closed_trades": len(closed),
        "still_open": opened,
        "realized_pnl": round(sum(values), 6) if values else None,
        "median_pnl": values[len(values) // 2] if values else None,
        "win_rate": round(len(wins) / len(values), 4) if values else None,
        "fill_model": FILL_MODEL,
    }
