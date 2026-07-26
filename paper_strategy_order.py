import fcntl
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime as _datetime
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

import notification_health
import order_intent_ledger
from account_risk import check_account_exposure_limits, check_daily_loss_limit
from broker import AlpacaBroker, BrokerResponse
from kill_switch import is_trading_halted
from kill_switch_state import is_entry_allowed, is_liquidation_allowed
from market_hours import eastern_now, get_us_market_session
from order_safety import check_daily_trade_count, run_order_safety_check
from slack_utils import send_slack_alert


BASE_DIR = Path(__file__).resolve().parent
ORDER_CANDIDATES_FILE = BASE_DIR / "order_candidates.csv"
CANDIDATES_FILE = BASE_DIR / "candidates.csv"
ORDER_HISTORY_FILE = BASE_DIR / "order_history.csv"
ORDER_HISTORY_LOCK_FILE = BASE_DIR / "order_history.lock"
ORDER_HISTORY_LOCK_TIMEOUT_SECONDS = 5.0
REQUIRED_HISTORY_COLUMNS = ["symbol", "order_date", "mode", "dry_run", "status"]

# order_history.csv's schema is frozen (existing consumers, e.g. the
# dashboard, read it) so broker reconciliation state lives in this separate
# companion file instead of adding columns. Rows are correlated to
# order_history.csv by (symbol, order_date), which is already a unique key
# there (duplicate-order prevention guarantees at most one row per pair).
ORDER_RECONCILIATION_FILE = BASE_DIR / "order_reconciliation.csv"
ORDER_RECONCILIATION_LOCK_FILE = BASE_DIR / "order_reconciliation.lock"
RECONCILIATION_COLUMNS = [
    "client_order_id",
    "symbol",
    "order_date",
    "requested_qty",
    "filled_qty",
    "remaining_qty",
    "average_fill_price",
    "broker_status",
    "local_status",
    "last_reconciled_at",
]

# Local status vocabulary for reconciliation rows. PENDING_SUBMISSION/
# SUBMITTED/PARTIALLY_FILLED/UNKNOWN are non-terminal (still worth
# re-checking on a future run); the rest are terminal and sticky.
#
# SUBMISSION_FAILED is set directly by main()'s RequestException handler
# (never by reconcile_pending_orders itself): it means the ledger already
# determined this specific client_order_id never reached the broker, so the
# row must never be re-checked and flipped to MANUAL_REVIEW on a later run --
# that would erase the one signal (is_duplicate_order's SUBMISSION_FAILED
# exemption, see below) that lets a retry for the same (symbol, order_date)
# proceed.
RECONCILIATION_NON_TERMINAL_STATUSES = {"PENDING_SUBMISSION", "SUBMITTED", "PARTIALLY_FILLED", "UNKNOWN"}
RECONCILIATION_TERMINAL_STATUSES = {"FILLED", "REJECTED", "CANCELLED", "EXPIRED", "MANUAL_REVIEW", "SUBMISSION_FAILED"}

# Monotonic progression rank used by merge_reconciliation_state(). Terminal
# statuses (including FILLED) are handled separately: once existing status
# is terminal, no incoming value may change it.
_STATUS_PROGRESS_RANK = {
    "PENDING_SUBMISSION": 0,
    # UNKNOWN ranks alongside SUBMITTED: it means the broker responded with
    # a status string we don't recognize, which is more informative than
    # never having heard back (PENDING_SUBMISSION) but must never regress a
    # more advanced state (PARTIALLY_FILLED/FILLED) back down.
    "UNKNOWN": 1,
    "SUBMITTED": 1,
    "PARTIALLY_FILLED": 2,
    "FILLED": 3,
}

_BROKER_STATUS_TO_LOCAL_STATUS = {
    "new": "SUBMITTED",
    "accepted": "SUBMITTED",
    "pending_new": "SUBMITTED",
    "partially_filled": "PARTIALLY_FILLED",
    "filled": "FILLED",
    "rejected": "REJECTED",
    "canceled": "CANCELLED",
    "cancelled": "CANCELLED",
    "expired": "EXPIRED",
}

# CODEX-007: order_date must be exactly YYYY-MM-DD, America/New_York
# calendar date. No datetime forms, no timezone suffixes, no whitespace, no
# missing zero-padding — a parseable-but-non-canonical value (e.g.
# "2026-07-20 10:30:00") would otherwise round-trip through pandas fine
# while count_orders_for_date()'s exact string comparison against "today"
# silently treats it as a different day, undercounting the daily limit.
_ORDER_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_order_date_str(value):
    """Validate that `value` is a canonical YYYY-MM-DD order date string.

    Returns the validated string on success. Raises ValueError otherwise —
    including for non-string input (None, NaN, numbers), leading/trailing
    whitespace (never auto-stripped), any datetime/timezone suffix, missing
    zero-padding, and dates that don't exist on the calendar (e.g. Feb 30).
    """
    if not isinstance(value, str):
        raise ValueError(f"order_date must be a string, got {type(value).__name__}: {value!r}")
    if value != value.strip():
        raise ValueError(f"order_date has leading/trailing whitespace: {value!r}")
    if not _ORDER_DATE_PATTERN.match(value):
        raise ValueError(f"order_date is not in YYYY-MM-DD form: {value!r}")
    try:
        parsed = _datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"order_date is not a real calendar date: {value!r} ({exc})")
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError(f"order_date does not round-trip to itself: {value!r}")
    return value


def diagnose_order_history_dates():
    """Read order_history.csv without validation and report which order_date
    values (if any) are non-canonical, for operator use.

    Never used to auto-migrate data: CODEX-007 requires that a corrupted or
    non-canonical history be surfaced for explicit human review rather than
    silently rewritten. Returns a list of {row, raw_value, error} dicts;
    empty if the file is missing, unreadable as CSV, or every value is
    already canonical.
    """
    if not ORDER_HISTORY_FILE.exists():
        return []
    try:
        df = pd.read_csv(ORDER_HISTORY_FILE)
    except Exception as exc:
        return [{"row": None, "raw_value": None, "error": f"file unreadable as CSV: {exc}"}]
    if "order_date" not in df.columns:
        return [{"row": None, "raw_value": None, "error": "order_date column missing"}]
    problems = []
    for row_index, raw_value in df["order_date"].items():
        try:
            validate_order_date_str(raw_value)
        except ValueError as exc:
            problems.append({"row": int(row_index), "raw_value": raw_value, "error": str(exc)})
    return problems


class OrderHistoryUnavailable(Exception):
    """Order history could not be safely read; new orders are blocked (fail-closed)."""


class DuplicateOrderError(Exception):
    """A reservation for this symbol/date already exists."""


def calculate_rsi(df, period=14):
    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def submit_order(symbol, qty=1, broker=None, client_order_id=None, *, side, live_entry_context=None):
    """Submit one order, gated by two independent kill switches, both
    re-checked fresh on every call (never cached):

    1. kill_switch.is_trading_halted() -- the original binary halt file.
    2. kill_switch_state's multi-level state machine -- is_entry_allowed()
       for side="buy", is_liquidation_allowed() for side="sell". A corrupted
       state file fails closed via kill_switch_state's own
       _fail_closed_snapshot, so is_entry_allowed()/is_liquidation_allowed()
       both return False in that case without any special-casing here.

    Both gates must pass for the broker to be called; either one blocking
    returns a 423 response without ever reaching `broker`.

    CODEX-026/CODEX-029/CODEX-031: a THIRD gate applies only when
    side="buy" AND the broker is configured for live trading
    (broker.config.is_live_mode) -- Paper trading is entirely unaffected.
    live_entry_context must be a live_readiness.order_gateway.
    LiveEntryContext describing the pilot's allow-list/FX-rate/price
    state; missing it, or it failing any check -- including CODEX-029's
    exact-match requirement that live_entry_context.symbol equal this
    call's own `symbol`, or CODEX-031's authoritative (not
    caller-trusted) 30,000 KRW budget/daily-entry/position-count checks
    -- blocks the order with a 423 before the broker is ever called and
    re-sizes `qty` to the gateway's fail-closed-computed quantity when
    all checks pass.

    AlpacaBroker.submit_order() itself runs this exact gate too (see
    broker/alpaca_client.py) so a caller that bypasses this wrapper
    entirely and calls broker.submit_order() directly is still covered.
    Because the gate now durably reserves budget as a side effect
    (CODEX-031), this wrapper deliberately SKIPS its own copy of the
    gate when `broker` is an AlpacaBroker instance -- letting the broker
    run it exactly once avoids reserving the same notional twice against
    the pilot budget for a single order. For any other broker (test
    doubles that don't implement the gate themselves), this wrapper
    remains the only protection and is responsible for committing/
    releasing the reservation itself once the broker call resolves.
    """
    if is_trading_halted():
        print(f"Kill switch engaged: {symbol} order not submitted.")
        return BrokerResponse(
            status_code=423,
            text="Kill switch engaged: trading halted, order not submitted.",
            data={"halted": True},
            dry_run=False,
        )

    if side not in {"buy", "sell"}:
        raise ValueError("side must be exactly 'buy' or 'sell'")

    state_allows = is_liquidation_allowed() if side == "sell" else is_entry_allowed()
    if not state_allows:
        print(f"Kill switch state blocked {side} order for {symbol}.")
        return BrokerResponse(
            status_code=423,
            text=f"Kill switch state engaged: {side} orders not permitted, order not submitted.",
            data={"halted": True, "side": side},
            dry_run=False,
        )

    broker = broker or AlpacaBroker()

    # getattr(broker, "config", None) first, not getattr(broker.config, ...):
    # test doubles (FakeBroker in many existing tests) commonly have no
    # .config attribute at all, and `broker.config` would raise
    # AttributeError before getattr's default ever applies.
    broker_config = getattr(broker, "config", None)
    is_live_entry = side == "buy" and getattr(broker_config, "is_live_mode", False)

    # CODEX-031: validate_and_size_live_entry() durably reserves budget as
    # a side effect. If `broker` is a real AlpacaBroker, IT runs this gate
    # itself (broker/alpaca_client.py) -- running it again here would
    # reserve the same notional twice for one order. `approval` stays None
    # in that case; this wrapper only owns the reservation lifecycle
    # (commit/release below) when it's the one that created it.
    approval = None
    if is_live_entry and not isinstance(broker, AlpacaBroker):
        from live_readiness.order_gateway import LiveOrderBlockedError, validate_and_size_live_entry
        if live_entry_context is None:
            print(f"CODEX-026: live entry for {symbol} blocked -- no LiveEntryContext supplied.")
            return BrokerResponse(
                status_code=423,
                text="Live entry blocked: no LiveEntryContext supplied, order not submitted.",
                data={"blocked_reason": "MISSING_LIVE_ENTRY_CONTEXT"},
                dry_run=False,
            )
        try:
            # CODEX-034: reuse the caller's own client_order_id (if any)
            # for the reservation, exactly like broker/alpaca_client.py
            # does -- see that module's identical comment.
            approval = validate_and_size_live_entry(live_entry_context, symbol, client_order_id)
        except LiveOrderBlockedError as exc:
            print(f"CODEX-026: live entry for {symbol} blocked -- {exc}")
            return BrokerResponse(
                status_code=423,
                text=f"Live entry blocked: {exc}",
                data={"blocked_reason": str(exc)},
                dry_run=False,
            )
        qty = approval.quantity
        client_order_id = approval.client_order_id  # always the id actually reserved

    submit_kwargs = dict(qty=qty, side=side, client_order_id=client_order_id)
    if is_live_entry and isinstance(broker, AlpacaBroker):
        # Only passed for the exact scenario AlpacaBroker.submit_order()'s
        # own copy of this gate understands (CODEX-026/029/031) -- every
        # other broker double across the test suite has a submit_order()
        # with no live_entry_context parameter at all, so this must never
        # be passed outside the one case that needs it.
        submit_kwargs["live_entry_context"] = live_entry_context

    try:
        response = broker.submit_order(symbol, **submit_kwargs)
    except Exception as exc:
        # CODEX-034: same ambiguous-vs-definitive classification as
        # broker/alpaca_client.py -- see that module's
        # _is_ambiguous_broker_failure() for the full rationale.
        if approval is not None:
            if _is_ambiguous_wrapper_broker_failure(exc):
                _mark_wrapper_owned_submission_unknown(approval.reservation_id)
            else:
                _release_wrapper_owned_reservation(approval.reservation_id)
        raise

    if approval is not None:
        if response.status_code in (200, 201):
            _commit_wrapper_owned_reservation(approval.reservation_id)
            if isinstance(response.data, dict):
                # See broker/alpaca_client.py's identical injection for why:
                # lets enter_position() link this reservation to the
                # position_id it's about to create.
                response.data = {**response.data, "live_entry_reservation_id": approval.reservation_id}
        else:
            _release_wrapper_owned_reservation(approval.reservation_id)

    print(f"{symbol} order result: {response.status_code} {response.text[:500]}")
    return response


def _is_ambiguous_wrapper_broker_failure(exc):
    """CODEX-034: identical rationale to broker/alpaca_client.py's
    _is_ambiguous_broker_failure() -- kept as a separate copy here (not
    imported) since this module deliberately doesn't depend on `requests`
    directly, and test doubles on this (non-AlpacaBroker) path may raise
    plain exceptions to simulate ambiguous failures."""
    import requests
    if isinstance(exc, requests.exceptions.HTTPError):
        return exc.response is None
    if isinstance(exc, requests.exceptions.RequestException):
        return True
    return False


def _mark_wrapper_owned_submission_unknown(reservation_id):
    """Best-effort CODEX-034 reservation submission-unknown transition
    for the non-AlpacaBroker (test-double-only) path -- see
    submit_order()'s docstring."""
    from live_readiness import entry_reservation_ledger as live_ledger
    from state_store import db as state_db
    try:
        conn = state_db.open_db()
        try:
            live_ledger.mark_submission_unknown(conn, reservation_id)
        finally:
            conn.close()
    except Exception:
        pass


def _commit_wrapper_owned_reservation(reservation_id):
    """Best-effort CODEX-031 reservation commit for the non-AlpacaBroker
    (test-double-only) path -- see submit_order()'s docstring. A failure
    here must not fail the already-successful order submission."""
    from live_readiness import entry_reservation_ledger as live_ledger
    from state_store import db as state_db
    try:
        conn = state_db.open_db()
        try:
            live_ledger.mark_committed(conn, reservation_id)
        finally:
            conn.close()
    except Exception:
        pass


def _release_wrapper_owned_reservation(reservation_id):
    """Best-effort CODEX-031 reservation release for the non-AlpacaBroker
    (test-double-only) path -- see submit_order()'s docstring. A failure
    here must not mask the original error/response."""
    from live_readiness import entry_reservation_ledger as live_ledger
    from state_store import db as state_db
    try:
        conn = state_db.open_db()
        try:
            live_ledger.mark_released(conn, reservation_id)
        finally:
            conn.close()
    except Exception:
        pass


def analyze_stock(symbol):
    df = yf.Ticker(symbol).history(period="1y")
    if df.empty or len(df) < 220:
        return None

    df["MA200"] = df["Close"].rolling(window=200).mean()
    df["RSI"] = calculate_rsi(df)
    df["AVG_VOLUME_20"] = df["Volume"].rolling(window=20).mean()

    price = float(df["Close"].iloc[-1])
    ma200 = float(df["MA200"].iloc[-1])
    rsi = float(df["RSI"].iloc[-1])
    avg_volume = float(df["AVG_VOLUME_20"].iloc[-1])
    if avg_volume <= 0:
        return None
    volume_ratio = float(df["Volume"].iloc[-1]) / avg_volume

    score = 0
    if price > ma200:
        score += 40
    if 40 <= rsi <= 65:
        score += 30
    if volume_ratio >= 1.2:
        score += 30

    return {
        "symbol": symbol,
        "price": price,
        "ma200": ma200,
        "rsi": rsi,
        "volume_ratio": volume_ratio,
        "score": score,
    }


def _load_symbols_from_csv(path):
    try:
        df = pd.read_csv(path)
        if df.empty or "symbol" not in df.columns:
            return []
        return df["symbol"].dropna().astype(str).unique().tolist()
    except Exception as exc:
        print(f"Failed to read {path.name}: {exc}")
        return []


def load_watchlist():
    order_symbols = _load_symbols_from_csv(ORDER_CANDIDATES_FILE)
    if order_symbols:
        print(f"Loaded {len(order_symbols)} symbols from order_candidates.csv")
        return order_symbols
    fallback = _load_symbols_from_csv(CANDIDATES_FILE)
    print(f"Loaded {len(fallback)} symbols from candidates.csv fallback")
    return fallback


def load_order_history():
    """Read order_history.csv, fail-closed on anything but a valid file.

    A MISSING file (never initialized) or a CORRUPTED file (parse failure,
    missing required columns, or an unparseable order_date column) both
    raise OrderHistoryUnavailable instead of silently returning an empty
    DataFrame. A run that cannot prove today's order count is zero must not
    treat it as zero. Only a file that was explicitly created via
    initialize_order_history() (or already has valid rows) counts as a
    legitimate empty history.
    """
    if not ORDER_HISTORY_FILE.exists():
        raise OrderHistoryUnavailable(
            f"MISSING_HISTORY: {ORDER_HISTORY_FILE} does not exist. "
            "Run initialize_order_history() once during initial setup if this is a fresh deployment."
        )
    try:
        df = pd.read_csv(ORDER_HISTORY_FILE)
    except Exception as exc:
        raise OrderHistoryUnavailable(
            f"CORRUPTED_HISTORY: failed to parse {ORDER_HISTORY_FILE}: {exc}"
        )
    missing_columns = [c for c in REQUIRED_HISTORY_COLUMNS if c not in df.columns]
    if missing_columns:
        raise OrderHistoryUnavailable(
            f"CORRUPTED_HISTORY: {ORDER_HISTORY_FILE} is missing required columns {missing_columns}"
        )
    if not df.empty:
        # CODEX-007: a loose pd.to_datetime(errors="raise") check accepts
        # parseable-but-non-canonical values (e.g. "2026-07-20 10:30:00",
        # "2026-7-20", " 2026-07-20") that then silently fail an exact
        # string comparison against today's canonical date, undercounting
        # the daily order limit. Every row must be exactly YYYY-MM-DD; a
        # single non-canonical value marks the whole history CORRUPTED
        # rather than being coerced or skipped, per fail-closed policy.
        for row_index, raw_value in df["order_date"].items():
            try:
                validate_order_date_str(raw_value)
            except ValueError as exc:
                raise OrderHistoryUnavailable(
                    f"CORRUPTED_HISTORY: order_date at row {row_index} in {ORDER_HISTORY_FILE} "
                    f"is not canonical: {exc}. Run diagnose_order_history_dates() for a full report; "
                    "non-canonical values are never auto-migrated."
                )
    return df


def initialize_order_history():
    """Explicit one-time setup: create a valid, empty order history file.

    Only this function (or an already-valid file) may produce the
    EMPTY_VALID_HISTORY state load_order_history() accepts. A file that
    goes missing after this point is treated as an operational anomaly,
    not a fresh install.
    """
    df = pd.DataFrame(columns=REQUIRED_HISTORY_COLUMNS)
    if not save_order_history(df):
        raise OrderHistoryUnavailable(f"Failed to initialize {ORDER_HISTORY_FILE}")
    return df


def _atomic_write_csv(path, dataframe):
    """Write a DataFrame to `path` atomically: temp file + fsync + os.replace().

    A crash or exception mid-write leaves the previous file untouched
    (os.replace is atomic on the same filesystem on macOS/Ubuntu), instead
    of a partially-written CSV from an in-place to_csv().
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", newline="") as tmp_file:
                dataframe.to_csv(tmp_file, index=False)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.replace(tmp_name, path)
        except Exception:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
            raise
        return True
    except Exception as exc:
        print(f"Failed to save {path}: {exc}")
        return False


def save_order_history(order_history):
    return _atomic_write_csv(ORDER_HISTORY_FILE, order_history)


class ReconciliationUnavailable(Exception):
    """order_reconciliation.csv exists but could not be safely read.

    Unlike a missing file (a legitimate empty state before any order has
    ever been reserved), a file that fails to parse or is missing required
    columns must not be silently replaced with an empty DataFrame — that
    would erase whatever fill/status history it held. Writes are refused
    until a human resolves it; the file itself is left untouched.
    """


def load_reconciliation():
    """Read order_reconciliation.csv.

    A missing file returns an empty DataFrame (no order has been reserved
    yet, or none survived — order_history.csv remains the actual
    duplicate/daily-limit safety gate regardless). A file that exists but
    fails to parse or is missing required columns raises
    ReconciliationUnavailable instead of degrading to empty, so corrupted
    tracking data is never silently discarded (CODEX-008).
    """
    if not ORDER_RECONCILIATION_FILE.exists():
        df = pd.DataFrame(columns=RECONCILIATION_COLUMNS)
    else:
        try:
            df = pd.read_csv(ORDER_RECONCILIATION_FILE)
        except Exception as exc:
            raise ReconciliationUnavailable(
                f"CORRUPTED: failed to parse {ORDER_RECONCILIATION_FILE}: {exc}"
            )
        missing_columns = [c for c in RECONCILIATION_COLUMNS if c not in df.columns]
        if missing_columns:
            raise ReconciliationUnavailable(
                f"CORRUPTED: {ORDER_RECONCILIATION_FILE} is missing required columns {missing_columns}"
            )
    # object dtype throughout: columns mix numbers, ISO timestamp strings and
    # None across their lifetime, so a narrower inferred dtype (e.g. an
    # all-empty column defaulting to float64) would warn/fail on later
    # string assignments.
    return df.astype({col: "object" for col in RECONCILIATION_COLUMNS})


def save_reconciliation(reconciliation):
    return _atomic_write_csv(ORDER_RECONCILIATION_FILE, reconciliation)


def _safe_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _status_should_apply(existing_status, incoming_status):
    """CODEX-008 monotonic transition rule, used by merge_reconciliation_state.

    - Once a row reaches a terminal status (including FILLED), no incoming
      status changes it — no regression, no lateral flip to a different
      terminal status, and specifically UNKNOWN can never overwrite FILLED
      (it ranks below FILLED and existing-terminal is checked first anyway).
    - Otherwise, progression only moves forward or holds
      (PENDING_SUBMISSION -> {SUBMITTED, UNKNOWN} -> PARTIALLY_FILLED ->
      FILLED). UNKNOWN ranks alongside SUBMITTED: more informative than
      never having heard back, but must not regress a later PARTIALLY_FILLED.
    - A terminal status arriving from a non-terminal existing status is
      always accepted (the broker gave a definitive answer).
    """
    if incoming_status is None:
        return False
    if existing_status in RECONCILIATION_TERMINAL_STATUSES:
        return False
    existing_rank = _STATUS_PROGRESS_RANK.get(existing_status, -1)
    incoming_rank = _STATUS_PROGRESS_RANK.get(incoming_status)
    if incoming_rank is not None:
        return incoming_rank >= existing_rank
    return True  # incoming is itself a terminal status; existing is not


def merge_reconciliation_state(existing, incoming):
    """Combine an existing reconciliation row (dict) with a new observation
    (dict), enforcing CODEX-008's monotonic guarantees:

    - local_status only ever moves forward per _status_should_apply().
    - filled_qty never decreases (max of existing/incoming).
    - average_fill_price is never cleared once set; only overwritten by a
      new non-null value.
    - remaining_qty is recomputed from requested_qty - merged filled_qty.
    - broker_status is recorded whenever the local_status is applied.

    `existing` and `incoming` are plain dicts (row-shaped); returns a new
    merged dict. Pure function — no I/O, easy to unit test directly.
    """
    merged = dict(existing)

    incoming_status = incoming.get("local_status")
    if _status_should_apply(existing.get("local_status"), incoming_status):
        merged["local_status"] = incoming_status
        if incoming.get("broker_status") is not None:
            merged["broker_status"] = incoming["broker_status"]

    existing_filled = _safe_float(existing.get("filled_qty")) or 0.0
    incoming_filled = _safe_float(incoming.get("filled_qty")) or 0.0
    merged_filled = max(existing_filled, incoming_filled)
    merged["filled_qty"] = merged_filled

    requested = _safe_float(existing.get("requested_qty"))
    if requested is not None:
        merged["remaining_qty"] = max(requested - merged_filled, 0.0)

    incoming_price = incoming.get("average_fill_price")
    if incoming_price not in (None, ""):
        merged["average_fill_price"] = incoming_price

    merged["last_reconciled_at"] = incoming.get("last_reconciled_at") or existing.get("last_reconciled_at")
    return merged


def _update_reconciliation_row(client_order_id, symbol, order_date, incoming,
                                lock_timeout=ORDER_HISTORY_LOCK_TIMEOUT_SECONDS):
    """Lock -> reread -> merge (monotonic) -> write -> unlock for one row.

    Creates the row if this is the first observation for client_order_id.
    Returns False (never raises) on a lock timeout, a corrupted file, or a
    write failure — the caller decides whether that should block the
    broader operation (try_reserve_order does; reconcile_pending_orders
    logs and moves to the next row).
    """
    try:
        with _reconciliation_lock(timeout=lock_timeout):
            try:
                df = load_reconciliation()
            except ReconciliationUnavailable as exc:
                print(f"Reconciliation update for {client_order_id} refused: {exc}")
                return False
            mask = df["client_order_id"].astype(str) == str(client_order_id)
            if mask.any():
                idx = df[mask].index[0]
                merged = merge_reconciliation_state(df.loc[idx].to_dict(), incoming)
                for key, value in merged.items():
                    df.at[idx, key] = value
            else:
                new_row = {
                    "client_order_id": client_order_id,
                    "symbol": symbol,
                    "order_date": order_date,
                    "requested_qty": incoming.get("requested_qty"),
                    "filled_qty": _safe_float(incoming.get("filled_qty")) or 0.0,
                    "remaining_qty": incoming.get("remaining_qty"),
                    "average_fill_price": incoming.get("average_fill_price"),
                    "broker_status": incoming.get("broker_status"),
                    "local_status": incoming.get("local_status", "PENDING_SUBMISSION"),
                    "last_reconciled_at": incoming.get("last_reconciled_at"),
                }
                new_row_df = pd.DataFrame([new_row]).astype({col: "object" for col in RECONCILIATION_COLUMNS})
                df = pd.concat([df, new_row_df], ignore_index=True)
            return save_reconciliation(df)
    except (RuntimeError, OSError) as exc:
        # RuntimeError: lock acquisition timeout from _reconciliation_lock.
        # OSError: e.g. the lock file itself can't be opened/created
        # (permissions, missing directory). Either way this is a failure to
        # update, not a crash — the caller decides how to react.
        print(f"Reconciliation update for {client_order_id} could not acquire lock: {exc}")
        return False


def _record_pending_reconciliation(client_order_id, symbol, order_date, requested_qty,
                                    lock_timeout=ORDER_HISTORY_LOCK_TIMEOUT_SECONDS):
    incoming = {
        "requested_qty": requested_qty,
        "filled_qty": 0,
        "remaining_qty": requested_qty,
        "average_fill_price": None,
        "broker_status": None,
        "local_status": "PENDING_SUBMISSION",
        "last_reconciled_at": None,
    }
    success = _update_reconciliation_row(client_order_id, symbol, order_date, incoming, lock_timeout=lock_timeout)
    if not success:
        # CODEX-008: a silently-ignored failure here previously let
        # try_reserve_order return a client_order_id with no reconciliation
        # row behind it, so a later fill could never be tracked. Now this
        # is a hard failure, same severity as an order_history save failure.
        raise RuntimeError("Reconciliation record failed; order submission blocked")


def _local_status_from_response(response):
    if response.dry_run:
        return "DRY_RUN"
    data = response.data if isinstance(response.data, dict) else {}
    broker_status = data.get("status")
    if broker_status in _BROKER_STATUS_TO_LOCAL_STATUS:
        return _BROKER_STATUS_TO_LOCAL_STATUS[broker_status]
    return "SUBMITTED" if response.status_code in (200, 201) else "REJECTED"


def _update_reconciliation_from_response(client_order_id, symbol, order_date, response):
    """Record whatever fill data the immediate submit response carried.

    This is a best-effort snapshot merged with the same monotonic rules as
    the authoritative reconcile_pending_orders() pass — partially_filled is
    never collapsed into filled, and a stale/duplicate response can never
    move the row backwards.
    """
    data = response.data if isinstance(response.data, dict) else {}
    incoming = {
        "broker_status": data.get("status"),
        "filled_qty": data.get("filled_qty"),
        "average_fill_price": data.get("filled_avg_price"),
        "local_status": _local_status_from_response(response),
        "last_reconciled_at": eastern_now().isoformat(),
    }
    return _update_reconciliation_row(client_order_id, symbol, order_date, incoming)


def reconcile_pending_orders(broker):
    """Resolve non-terminal reconciliation rows against the broker's truth.

    Never resubmits. A row the broker no longer recognizes is marked
    MANUAL_REVIEW instead of being retried automatically. Safe to call
    repeatedly: always updates rows in place by client_order_id rather than
    appending, and every write goes through the same locked,
    monotonic-merge path as the immediate post-submit update, so re-running
    this never regresses a status or duplicates a row (CODEX-008).
    """
    try:
        df = load_reconciliation()
    except ReconciliationUnavailable as exc:
        # Corrupted tracking data blocks further reconciliation writes, but
        # must not block new-candidate evaluation: order_history.csv (not
        # this file) is the actual duplicate/daily-limit safety gate.
        print(f"Reconciliation skipped this run: {exc}")
        return
    if df.empty:
        return
    pending_mask = df["local_status"].isin(RECONCILIATION_NON_TERMINAL_STATUSES)
    if not pending_mask.any():
        return

    for idx in df[pending_mask].index:
        client_order_id = df.at[idx, "client_order_id"]
        symbol = df.at[idx, "symbol"]
        order_date = df.at[idx, "order_date"]
        try:
            broker_order = broker.get_order_by_client_order_id(client_order_id)
        except Exception as exc:
            print(f"Reconciliation lookup failed for {client_order_id} ({symbol}): {exc}")
            continue

        if broker_order is None:
            incoming = {"local_status": "MANUAL_REVIEW", "last_reconciled_at": eastern_now().isoformat()}
        else:
            broker_status = broker_order.get("status")
            incoming = {
                "broker_status": broker_status,
                "filled_qty": broker_order.get("filled_qty"),
                "average_fill_price": broker_order.get("filled_avg_price"),
                "local_status": _BROKER_STATUS_TO_LOCAL_STATUS.get(broker_status, "UNKNOWN"),
                "last_reconciled_at": eastern_now().isoformat(),
            }

        if not _update_reconciliation_row(client_order_id, symbol, order_date, incoming):
            continue  # already logged; leave order_history untouched too

        try:
            updated = load_reconciliation()
        except ReconciliationUnavailable:
            continue
        row_mask = updated["client_order_id"].astype(str) == str(client_order_id)
        if row_mask.any():
            update_order_status(symbol, order_date, updated.loc[row_mask, "local_status"].iloc[0])


@contextmanager
def _file_lock(lock_path, timeout, label):
    """Process-level exclusive lock via fcntl.flock (macOS/Ubuntu; Windows
    compatibility is out of scope for this project). Failure to acquire the
    lock within `timeout` raises RuntimeError rather than proceeding without
    mutual exclusion — the lock file itself is left untouched either way,
    and the finally block guarantees unlock/close even on an exception
    raised by the caller's code inside the `with` block.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "a+")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"Could not acquire {label} lock ({lock_path}) within {timeout}s; order blocked"
                    )
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        finally:
            lock_file.close()


@contextmanager
def _order_history_lock(timeout=ORDER_HISTORY_LOCK_TIMEOUT_SECONDS):
    """Guards read-check-write of order_history.csv (the duplicate/daily-limit safety gate)."""
    with _file_lock(ORDER_HISTORY_LOCK_FILE, timeout, "order history"):
        yield


@contextmanager
def _reconciliation_lock(timeout=ORDER_HISTORY_LOCK_TIMEOUT_SECONDS):
    """Guards read-check-write of order_reconciliation.csv (CODEX-008)."""
    with _file_lock(ORDER_RECONCILIATION_LOCK_FILE, timeout, "reconciliation"):
        yield


def is_duplicate_order(order_history, symbol, order_date):
    """True if an existing order_history row occupies this (symbol, order_date).

    A row with status SUBMISSION_FAILED is excluded from this match: it means
    order_intent_ledger already recorded (via an explicit abort()) that this
    specific prior attempt never reached the broker, so it is safe to let a
    fresh attempt through to try_reserve_order() -- which replaces that stale
    row rather than appending, keeping at most one row per (symbol,
    order_date). Every other status (including PENDING_SUBMISSION, which
    means the previous run's outcome is still unknown) continues to block.
    """
    if order_history.empty or "symbol" not in order_history.columns or "order_date" not in order_history.columns:
        return False
    mask = (
        (order_history["symbol"].astype(str) == symbol)
        & (order_history["order_date"].astype(str) == order_date)
    )
    if "status" in order_history.columns:
        mask = mask & (order_history["status"].astype(str) != "SUBMISSION_FAILED")
    return bool(mask.any())


def count_orders_for_date(order_history, order_date):
    """Count order attempts that consume the daily trade limit for order_date.

    Counting policy: every persisted row for the date counts, regardless of
    its status column (PENDING_SUBMISSION, SUBMITTED, DRY_RUN, REJECTED,
    SUBMISSION_FAILED all count). This is deliberately conservative — safety
    margin is prioritized over squeezing out the maximum allowed trade count
    — since the current schema has no broker order id to dedupe by identity.
    """
    if order_history.empty or "order_date" not in order_history.columns:
        return 0
    return int((order_history["order_date"].astype(str) == order_date).sum())


def _intent_ledger_paths():
    """Order-intent-ledger file/lock paths, derived from ORDER_HISTORY_FILE's
    current directory so tests that monkeypatch ORDER_HISTORY_FILE to a
    tmp_path automatically get an isolated ledger too, with no separate
    monkeypatch of their own required.
    """
    base_dir = ORDER_HISTORY_FILE.parent
    return base_dir / "order_intent_ledger.csv", base_dir / "order_intent_ledger.lock"


def try_reserve_order(symbol, order_date, mode, dry_run, qty=1, broker=None,
                       lock_timeout=ORDER_HISTORY_LOCK_TIMEOUT_SECONDS):
    """Atomically reserve an order slot: lock -> reread -> check -> write -> unlock.

    Re-reads order history from disk under an exclusive lock (not the
    caller's possibly-stale in-memory copy) so two concurrent runs can't
    both observe the same duplicate/daily-count state and both proceed.
    Raises DuplicateOrderError (soft block, caller may continue to the next
    symbol) or propagates OrderHistoryUnavailable / the daily-trade-count
    Exception / a reservation-save RuntimeError (hard block, caller should
    stop). Returns (updated_history, client_order_id) on success; the
    client_order_id is what ties this reservation to the broker's own order
    record for later reconciliation.

    Before writing anything, this also reserves the same (symbol, order_date)
    in order_intent_ledger.py -- a restart-safe two-phase (reserve -> commit
    on submission / abort on submission failure) record kept independently
    of order_history.csv. `broker`, if given, lets a stale RESERVED row left
    over from a crashed prior run be re-checked against the broker's own
    truth by client_order_id instead of always failing closed.
    """
    with _order_history_lock(timeout=lock_timeout):
        order_history = load_order_history()
        if is_duplicate_order(order_history, symbol, order_date):
            raise DuplicateOrderError(symbol)
        today_trade_count = count_orders_for_date(order_history, order_date)
        check_daily_trade_count(today_trade_count)

        client_order_id = f"scalp-{symbol}-{order_date}-{uuid.uuid4().hex[:10]}"
        ledger_path, ledger_lock_path = _intent_ledger_paths()
        try:
            order_intent_ledger.reserve(
                ledger_path, ledger_lock_path, symbol, order_date,
                client_order_id=client_order_id, broker=broker, lock_timeout=lock_timeout,
            )
        except order_intent_ledger.DuplicateIntentError as exc:
            raise DuplicateOrderError(symbol) from exc

        new_row = pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "order_date": order_date,
                    "mode": mode,
                    "dry_run": dry_run,
                    "status": "PENDING_SUBMISSION",
                }
            ]
        )
        # is_duplicate_order() above only let this reservation through when
        # every existing row for (symbol, order_date) is SUBMISSION_FAILED (a
        # prior attempt the ledger confirmed never reached the broker).
        # Replace that stale row rather than appending, so order_history.csv
        # keeps at most one row per (symbol, order_date) -- the invariant
        # update_order_status() and reconciliation's symbol/order_date
        # correlation both rely on.
        if not order_history.empty:
            stale_mask = (
                (order_history["symbol"].astype(str) == symbol)
                & (order_history["order_date"].astype(str) == order_date)
            )
            order_history = order_history[~stale_mask]
        reserved_history = pd.concat([order_history, new_row], ignore_index=True)
        if not save_order_history(reserved_history):
            order_intent_ledger.abort(ledger_path, ledger_lock_path, client_order_id, lock_timeout=lock_timeout)
            raise RuntimeError("Order history reservation failed; order submission blocked")
        try:
            _record_pending_reconciliation(client_order_id, symbol, order_date, qty)
        except Exception:
            order_intent_ledger.abort(ledger_path, ledger_lock_path, client_order_id, lock_timeout=lock_timeout)
            raise
        return reserved_history, client_order_id


def update_order_status(symbol, order_date, status, lock_timeout=ORDER_HISTORY_LOCK_TIMEOUT_SECONDS):
    """Update a reserved order's status, rereading fresh under lock first.

    Never overwrites the file with a stale in-memory snapshot: another
    process may have reserved a different symbol in the meantime, and a
    blind overwrite would silently drop that row (lost update).
    """
    with _order_history_lock(timeout=lock_timeout):
        try:
            order_history = load_order_history()
        except OrderHistoryUnavailable as exc:
            print(f"Could not update order status for {symbol}: {exc}")
            return False
        mask = (
            (order_history["symbol"].astype(str) == symbol)
            & (order_history["order_date"].astype(str) == order_date)
        )
        order_history.loc[mask, "status"] = status
        return save_order_history(order_history)


def _safe_send_slack_alert(message):
    # Routed through notification_health so every send outcome is recorded
    # (record_success()/record_failure()) and a persistently broken Slack
    # channel escalates kill_switch_state on its own -- see notification_health.
    # send_with_health_tracking never raises, but this stays defensive so a
    # Slack outage can never propagate out of a caller that has no bearing on
    # order correctness.
    try:
        return notification_health.send_with_health_tracking(send_slack_alert, message)
    except Exception as exc:
        print(f"Slack notification failed: {exc}")
        return False


def _notify_order_blocked(symbol, reason):
    # A Slack outage must never propagate out of here: this is called from
    # inside the per-symbol loop in main(), and an unhandled exception would
    # abort processing of every remaining symbol over a side channel that
    # has no bearing on order correctness.
    _safe_send_slack_alert(f"*Order blocked*\n- Symbol: {symbol}\n- Reason: {reason}")


def _notify_order_filled(symbol, response, local_status):
    data = response.data if isinstance(response.data, dict) else {}
    _safe_send_slack_alert(
        f"*Order filled*\n- Symbol: {symbol}\n- Status: {local_status}\n"
        f"- Filled qty: {data.get('filled_qty')}\n"
        f"- Avg fill price: {data.get('filled_avg_price')}"
    )


def _notify_order_rejected(symbol, response):
    _safe_send_slack_alert(
        f"*Order rejected*\n- Symbol: {symbol}\n"
        f"- Broker response: {response.status_code} {response.text[:200]}"
    )


def main(broker=None):
    if is_trading_halted():
        print("Kill switch engaged: trading halted, no orders will be submitted.")
        return {"halted": True, "submitted": 0}

    broker = broker or AlpacaBroker()
    market_session = get_us_market_session()
    allow_order = market_session == "regular"

    print(f"Broker mode: {broker.config.status_label}")
    print(f"Market session: {market_session}")
    if not allow_order:
        print("Orders are only reviewed during regular market hours.")

    tickers = load_watchlist()
    if not tickers:
        print("No order review candidates.")
        return

    # Resolve any PENDING_SUBMISSION/SUBMITTED/PARTIALLY_FILLED rows left
    # over from a prior run (e.g. the process died between broker submission
    # and the local status update) against the broker's own record before
    # evaluating new candidates. Never resubmits.
    reconcile_pending_orders(broker)

    account = broker.get_account()
    positions = broker.get_positions()
    check_daily_loss_limit(account)
    equity = float(account["equity"])

    open_position_count = len(positions)
    # Trading-day boundary is always America/New_York, never the host's
    # local time — a server running in Asia/Seoul would otherwise cross
    # into "tomorrow" (and reset the daily counters) hours before the US
    # market day actually ends.
    today = eastern_now().strftime("%Y-%m-%d")
    held_symbols = [p["symbol"] for p in positions]
    order_history = load_order_history()
    today_trade_count = count_orders_for_date(order_history, today)

    print(f"Open positions: {open_position_count}")
    print(f"Held symbols: {held_symbols}")

    # Per-symbol outcome aggregation, returned to the caller. A symbol lands
    # in exactly one bucket; nothing here ever gets promoted to "submitted"
    # except the branch below that requires both a broker accept AND a
    # durable order_history write (fail-closed).
    results = {"submitted": [], "failed": [], "blocked": [], "skipped": []}

    for symbol in tickers:
        if symbol in held_symbols:
            _notify_order_blocked(symbol, "Already held")
            results["blocked"].append(symbol)
            continue

        if is_duplicate_order(order_history, symbol, today):
            _notify_order_blocked(symbol, "Duplicate order prevented for today")
            results["blocked"].append(symbol)
            continue

        result = analyze_stock(symbol)
        if not result:
            print(f"{symbol} analysis failed")
            results["skipped"].append(symbol)
            continue

        if result["score"] < 70:
            print(f"{symbol} did not meet order score threshold: {result['score']}")
            results["skipped"].append(symbol)
            continue

        if not allow_order:
            _safe_send_slack_alert(
                f"*Order review only*\n- Symbol: {symbol}\n- Score: {result['score']}\n"
                f"- Market session: {market_session}\n- Status: no order submitted"
            )
            results["skipped"].append(symbol)
            continue

        if not check_account_exposure_limits(positions, account):
            _notify_order_blocked(symbol, "Account-wide open position count or total exposure limit reached")
            results["blocked"].append(symbol)
            continue

        order_qty = 1
        position_value = order_qty * result["price"]
        position_rate = (position_value / equity) if equity > 0 else float("inf")

        run_order_safety_check(
            position_rate=position_rate,
            today_trade_count=today_trade_count,
            open_position_count=open_position_count,
        )

        try:
            order_history, client_order_id = try_reserve_order(
                symbol,
                today,
                broker.config.status_label,
                False,
                qty=order_qty,
                broker=broker,
            )
        except DuplicateOrderError:
            _notify_order_blocked(symbol, "Duplicate order prevented for today")
            results["blocked"].append(symbol)
            continue
        # Any other exception here (OrderHistoryUnavailable, the daily trade
        # count check re-verified fresh under lock, or a reservation-save
        # RuntimeError) propagates and stops the run for the remaining
        # symbols too — same conservative behavior as run_order_safety_check
        # above, now re-checked against the authoritative on-disk state.

        today_trade_count = count_orders_for_date(order_history, today)

        ledger_path, ledger_lock_path = _intent_ledger_paths()
        try:
            response = submit_order(symbol, qty=order_qty, broker=broker, client_order_id=client_order_id, side="buy")
        except requests.exceptions.RequestException as exc:
            update_order_status(symbol, today, "SUBMISSION_FAILED")
            try:
                order_intent_ledger.abort(ledger_path, ledger_lock_path, client_order_id)
            except Exception as ledger_exc:
                print(f"Order intent ledger abort failed for {client_order_id}: {ledger_exc}")
            # Settle this attempt's reconciliation row as terminal too (not
            # just order_history's status): otherwise reconcile_pending_orders()
            # would re-check this never-reached-broker client_order_id on the
            # next run, get no match, and overwrite order_history's
            # SUBMISSION_FAILED status with MANUAL_REVIEW before the retry
            # this abort() just unblocked ever reaches try_reserve_order().
            _update_reconciliation_row(
                client_order_id, symbol, today,
                {"local_status": "SUBMISSION_FAILED", "last_reconciled_at": eastern_now().isoformat()},
            )
            print(f"{symbol} order submission failed: {exc}")
            _safe_send_slack_alert(
                f"*Order failed*\n- Symbol: {symbol}\n- Reason: {exc}"
            )
            results["failed"].append(symbol)
            continue

        # The broker gave a definitive response (accepted or rejected) --
        # either way client_order_id is now known to it, so the reservation
        # is settled and must never be retried under a new one.
        try:
            order_intent_ledger.commit(ledger_path, ledger_lock_path, client_order_id)
        except Exception as ledger_exc:
            print(f"Order intent ledger commit failed for {client_order_id}: {ledger_exc}")

        success = response.status_code in [200, 201]
        status = "DRY RUN" if response.dry_run else ("SUBMITTED" if success else "FAILED")
        # CODEX-008: order_history.csv and order_reconciliation.csv must not
        # record two different immediate outcomes for the same submission
        # (e.g. reconciliation says PARTIALLY_FILLED while history says
        # SUBMITTED). Both now derive the same value from the same response.
        immediate_local_status = _local_status_from_response(response)
        _update_reconciliation_from_response(client_order_id, symbol, today, response)

        if success:
            history_saved = update_order_status(symbol, today, immediate_local_status)
            if not response.dry_run:
                open_position_count += 1
            if immediate_local_status in ("FILLED", "PARTIALLY_FILLED"):
                _notify_order_filled(symbol, response, immediate_local_status)
        else:
            history_saved = update_order_status(symbol, today, "REJECTED")
            _notify_order_rejected(symbol, response)

        # Fail-closed aggregation (CODEX-008 companion): a broker accept is
        # only counted as "submitted" if order_history.csv was durably
        # updated to match. A broker accept whose local persistence failed
        # is recorded as "failed", never silently promoted to a success --
        # order_history.csv is the source of truth other runs re-read
        # (duplicate/daily-limit checks), so a local write failure here
        # must not be reported as an order this run actually recorded.
        if success and history_saved:
            results["submitted"].append(symbol)
        else:
            results["failed"].append(symbol)
            if success and not history_saved:
                print(f"{symbol}: broker accepted the order but order_history persistence failed; not counted as submitted")

        _safe_send_slack_alert(
            f"*Paper Strategy Order*\n- Symbol: {symbol}\n- Qty: {order_qty}\n"
            f"- Score: {result['score']}\n- Price: {result['price']:.2f}\n"
            f"- RSI: {result['rsi']:.2f}\n- Volume ratio: {result['volume_ratio']:.2f}x\n"
            f"- Broker mode: {broker.config.status_label}\n- Status: {status}"
        )

    return results


if __name__ == "__main__":
    main()
