"""Restart-safe, two-phase order intent ledger.

Tracks (symbol, trade_date, client_order_id) reservations independently of
order_history.csv. The contract:

  1. reserve() must be called BEFORE the order is submitted to the broker,
     and is the only thing standing between "we are about to submit" and
     the actual network call. It is written atomically (lock + temp file +
     os.replace), so a crash right after it returns leaves a durable,
     unambiguous RESERVED row on disk.
  2. commit() is called once the broker has given a definitive answer
     (any HTTP response, accepted or rejected) -- the client_order_id is
     now known to the broker either way, so the reservation is settled.
  3. abort() is called when the submission attempt itself failed to reach a
     definitive answer (e.g. a network/timeout error) -- nothing is known
     to have reached the broker, so the (symbol, trade_date) slot is
     explicitly freed for a fresh reservation on a later run.

A RESERVED row still on disk when reserve() is called again (i.e. the
previous run died between step 1 and steps 2/3) is NOT assumed to be safe to
retry: absence of a commit/abort is not proof the broker never received the
order. Without a broker to re-check against, this fails closed and blocks a
new reservation. With a broker, the stale row's client_order_id is looked up
by get_order_by_client_order_id(); a definitive hit upgrades the row to
COMMITTED and still blocks (the order really was placed). A miss (or a
failed lookup) is only ever treated as "not proven safe" -- it also blocks,
matching this project's existing standing policy of never auto-resubmitting
an ambiguous order (see paper_strategy_order.reconcile_pending_orders'
MANUAL_REVIEW handling for the equivalent decision on the order_history
side). Only an explicit abort() -- called by code that already knows for
certain the broker never got the request -- clears an intent for retry.
"""

import fcntl
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pandas as pd


LEDGER_COLUMNS = ["client_order_id", "symbol", "trade_date", "state", "created_at", "updated_at"]

STATE_RESERVED = "RESERVED"
STATE_COMMITTED = "COMMITTED"
STATE_ABORTED = "ABORTED"

DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0

# Sentinel distinguishing "broker looked it up and found nothing" (None) from
# "the lookup itself blew up" -- both are treated the same way (fail closed,
# not proof of anything), but keeping them distinct in the code makes that a
# deliberate choice rather than an accident of exception handling.
_LOOKUP_FAILED = object()


class LedgerUnavailable(Exception):
    """order_intent_ledger.csv exists but could not be safely read."""


class DuplicateIntentError(Exception):
    """An existing intent for this (symbol, trade_date) blocks a new reservation."""


def _load(path):
    if not path.exists():
        return pd.DataFrame(columns=LEDGER_COLUMNS)
    try:
        df = pd.read_csv(path)
    except Exception as exc:
        raise LedgerUnavailable(f"CORRUPTED_LEDGER: failed to parse {path}: {exc}")
    missing_columns = [c for c in LEDGER_COLUMNS if c not in df.columns]
    if missing_columns:
        raise LedgerUnavailable(f"CORRUPTED_LEDGER: {path} is missing required columns {missing_columns}")
    return df.astype({col: "object" for col in LEDGER_COLUMNS})


def _atomic_write(path, dataframe):
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


def _save(path, dataframe):
    try:
        _atomic_write(path, dataframe)
        return True
    except Exception as exc:
        print(f"Failed to save {path}: {exc}")
        return False


@contextmanager
def _locked(lock_path, timeout):
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
                        f"Could not acquire order intent ledger lock ({lock_path}) within {timeout}s"
                    )
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        finally:
            lock_file.close()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _rows_for(df, symbol, trade_date):
    if df.empty:
        return df
    mask = (df["symbol"].astype(str) == str(symbol)) & (df["trade_date"].astype(str) == str(trade_date))
    return df[mask]


def _lookup_broker_order(broker, client_order_id):
    try:
        return broker.get_order_by_client_order_id(client_order_id)
    except Exception as exc:
        print(f"Order intent ledger broker re-check failed for {client_order_id}: {exc}")
        return _LOOKUP_FAILED


def reserve(ledger_path, lock_path, symbol, trade_date, client_order_id=None,
            broker=None, lock_timeout=DEFAULT_LOCK_TIMEOUT_SECONDS):
    """Atomically reserve a new order intent for (symbol, trade_date).

    Must be called before the order is submitted. Raises DuplicateIntentError
    if an existing row for this (symbol, trade_date) is COMMITTED, or is
    RESERVED and cannot be proven safe to retry (see module docstring).
    ABORTED rows never block. Returns the reserved client_order_id.
    """
    with _locked(lock_path, lock_timeout):
        df = _load(ledger_path)
        existing = _rows_for(df, symbol, trade_date)

        for idx, row in existing.iterrows():
            state = row["state"]
            if state == STATE_ABORTED:
                continue
            if state == STATE_COMMITTED:
                raise DuplicateIntentError(
                    f"{symbol}/{trade_date} is already committed (client_order_id={row['client_order_id']})"
                )
            # STATE_RESERVED left over from a run that never reached commit/abort.
            if broker is not None:
                broker_order = _lookup_broker_order(broker, row["client_order_id"])
                if broker_order not in (None, _LOOKUP_FAILED):
                    df.at[idx, "state"] = STATE_COMMITTED
                    df.at[idx, "updated_at"] = _now_iso()
                    if not _save(ledger_path, df):
                        raise RuntimeError("Order intent ledger update failed; order submission blocked")
                    raise DuplicateIntentError(
                        f"{symbol}/{trade_date} confirmed at broker for a stale reservation "
                        f"(client_order_id={row['client_order_id']})"
                    )
            # No broker, a miss, or a failed lookup: not proven safe. Fail closed.
            raise DuplicateIntentError(
                f"{symbol}/{trade_date} has an unresolved reservation from a prior run "
                f"(client_order_id={row['client_order_id']}); blocked fail-closed"
            )

        new_client_order_id = client_order_id or f"intent-{symbol}-{trade_date}-{uuid.uuid4().hex[:10]}"
        now = _now_iso()
        new_row = pd.DataFrame(
            [
                {
                    "client_order_id": new_client_order_id,
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "state": STATE_RESERVED,
                    "created_at": now,
                    "updated_at": now,
                }
            ]
        ).astype({col: "object" for col in LEDGER_COLUMNS})
        updated = pd.concat([df, new_row], ignore_index=True)
        if not _save(ledger_path, updated):
            raise RuntimeError("Order intent ledger reservation failed; order submission blocked")
        return new_client_order_id


def _transition(ledger_path, lock_path, client_order_id, new_state, lock_timeout=DEFAULT_LOCK_TIMEOUT_SECONDS):
    with _locked(lock_path, lock_timeout):
        df = _load(ledger_path)
        mask = df["client_order_id"].astype(str) == str(client_order_id)
        if not mask.any():
            raise LedgerUnavailable(f"No order intent reservation found for client_order_id={client_order_id}")
        df.loc[mask, "state"] = new_state
        df.loc[mask, "updated_at"] = _now_iso()
        if not _save(ledger_path, df):
            raise RuntimeError(f"Order intent ledger {new_state.lower()} update failed")


def commit(ledger_path, lock_path, client_order_id, lock_timeout=DEFAULT_LOCK_TIMEOUT_SECONDS):
    """Mark a reservation settled: the broker gave a definitive response (accepted or rejected)."""
    _transition(ledger_path, lock_path, client_order_id, STATE_COMMITTED, lock_timeout=lock_timeout)


def abort(ledger_path, lock_path, client_order_id, lock_timeout=DEFAULT_LOCK_TIMEOUT_SECONDS):
    """Mark a reservation explicitly cancelled: the submission attempt never reached the broker."""
    _transition(ledger_path, lock_path, client_order_id, STATE_ABORTED, lock_timeout=lock_timeout)
