"""One scanner signal, one durable BUY attempt.

The defect this closes
----------------------
`execution/idempotency.py` has always refused a second attempt on
`(signal_id, symbol, side, trading_date)` -- a durable UNIQUE constraint
that survives a process restart. `kis_live_trading` then called
`build_signal()` with no `signal_id`, so every entry cycle minted a fresh
`sig-<uuid4>` and handed that guard a different key each time. It could
never fire.

SLGN, 2026-09-03, is what that looks like in production. One published
candidate bought twice, three minutes apart:

    19:26:42  sig-dd73fb3399f84167  0030973277  cancelled, zero filled
    19:32:56  sig-e37f9c095891414f  0030974162  FILLED 3 @ 41.61

The second order filled and was then lost to BUY_NEVER_FILLED, leaving
three real shares with no position row. A zero-fill cancellation must not
license a new runtime identity for the same scanner observation.

The fix is to pass the scanner's own id through. No cooldown, no retry
counter, no new state: the guard already existed and was simply being
handed a key that could never repeat.
"""

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from execution import idempotency  # noqa: E402

DAY = "2026-09-03"
SOURCE_SIGNAL = "sig-scanner-generation-1"


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _register(conn, *, order_id, signal_id=SOURCE_SIGNAL, symbol="SLGN",
              side="buy", day=DAY, qty=3.0):
    return idempotency.register(
        conn, internal_order_id=order_id, signal_id=signal_id, symbol=symbol,
        side=side, trading_date=day, requested_quantity=qty,
        strategy_id="S6_ORB_BREAKOUT_V1")


def _cancel_zero_filled(conn, order_id):
    """The SLGN shape: accepted, then cancelled having filled nothing."""
    conn.execute("UPDATE kis_order_idempotency SET status='CANCELLED' "
                 "WHERE internal_order_id = ?", (order_id,))
    conn.commit()


class TestOneSourceSignalIsOneAttempt:
    def test_a_second_buy_on_the_same_source_signal_is_refused(self, conn):
        _register(conn, order_id="kislive-SLGN-first")
        with pytest.raises(idempotency.DuplicateOrderAttemptError):
            _register(conn, order_id="kislive-SLGN-second")

    def test_a_zero_fill_cancel_does_not_license_a_retry(self, conn):
        """The exact production sequence: cancelled zero-filled, then the
        same candidate tries again."""
        _register(conn, order_id="kislive-SLGN-d62f6090fb3e")
        _cancel_zero_filled(conn, "kislive-SLGN-d62f6090fb3e")
        with pytest.raises(idempotency.DuplicateOrderAttemptError):
            _register(conn, order_id="kislive-SLGN-d69921b1dddb")

    def test_a_new_scanner_generation_may_enter(self, conn):
        """A genuinely new source signal is a different observation and is
        still allowed -- this narrows retries, it does not stop trading."""
        _register(conn, order_id="kislive-SLGN-first")
        _cancel_zero_filled(conn, "kislive-SLGN-first")
        row = _register(conn, order_id="kislive-SLGN-next",
                        signal_id="sig-scanner-generation-2")
        assert row is None or row is not False

    def test_the_protection_survives_a_process_restart(self, conn):
        """It is a durable UNIQUE constraint, not in-memory state."""
        _register(conn, order_id="kislive-SLGN-first")
        conn.commit()
        # A fresh connection to the same database is what a restart sees.
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        reopened = sqlite3.connect(path)
        reopened.row_factory = sqlite3.Row
        with pytest.raises(idempotency.DuplicateOrderAttemptError):
            _register(reopened, order_id="kislive-SLGN-after-restart")
        reopened.close()

    def test_an_unrelated_symbol_is_unaffected(self, conn):
        _register(conn, order_id="kislive-SLGN-first")
        _register(conn, order_id="kislive-AAPL-first",
                  signal_id="sig-other", symbol="AAPL")

    def test_the_same_signal_on_another_day_is_a_new_attempt(self, conn):
        """The key includes the trading date, unchanged."""
        _register(conn, order_id="kislive-SLGN-d1")
        _register(conn, order_id="kislive-SLGN-d2", day="2026-09-04")

    def test_a_sell_is_not_blocked_by_the_buy(self, conn):
        """Side is part of the key: exiting must never be refused because
        the entry exists."""
        _register(conn, order_id="kislive-SLGN-buy")
        _register(conn, order_id="s6exit-SLGN-sell", side="sell")

    def test_a_literal_retry_of_one_attempt_is_still_refused(self, conn):
        """The other uniqueness guard, unchanged."""
        _register(conn, order_id="kislive-SLGN-first")
        with pytest.raises(idempotency.DuplicateOrderAttemptError):
            _register(conn, order_id="kislive-SLGN-first",
                      signal_id="sig-different")


class TestTheEntryPathSuppliesTheScannerId:
    def test_build_signal_is_called_with_the_source_signal_id(self):
        source = (REPO_ROOT / "kis_live_trading.py").read_text(encoding="utf-8")
        # Up to the dedented close of the call, not the first ")" -- the
        # explanatory comment inside it contains parentheses.
        call = source.split("signal = build_signal(")[1].split("\n                    )")[0]
        assert "signal_id=" in call, "build_signal is still minting its own id"
        assert "source_signal_id" in call

    def test_build_signal_still_generates_one_when_none_is_supplied(self):
        """Sources that publish no id keep exactly today's behaviour."""
        from domain.signal import build_signal

        made = build_signal(
            strategy_id="S", strategy_version="v1", config_version="c",
            code_commit="abc", symbol="AAPL", exchange="NASDAQ",
            signal_price=1.0, score=1.0, entry_reason="r",
            valid_for_seconds=60, signal_id=None)
        assert made.signal_id.startswith("sig-")

    def test_a_supplied_id_is_used_verbatim(self):
        from domain.signal import build_signal

        made = build_signal(
            strategy_id="S", strategy_version="v1", config_version="c",
            code_commit="abc", symbol="AAPL", exchange="NASDAQ",
            signal_price=1.0, score=1.0, entry_reason="r",
            valid_for_seconds=60, signal_id=SOURCE_SIGNAL)
        assert made.signal_id == SOURCE_SIGNAL

    def test_no_cooldown_or_retry_counter_was_introduced(self):
        """The guard already existed. Adding a second mechanism beside it
        would be two things to keep in step."""
        source = (REPO_ROOT / "kis_live_trading.py").read_text(encoding="utf-8")
        for invented in ("retry_count", "cooldown", "last_attempt_at",
                         "attempts_remaining"):
            assert invented not in source
