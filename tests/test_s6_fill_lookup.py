"""Reading a KIS fill back, and the states that must not be confused.

The distinction that drives this file
-------------------------------------
`sync_buy_fills` ABANDONS a submission it believes never filled. So "the
inquiry failed" and "the order did not fill" must never produce the same
value -- the first would abandon a position the account is holding and
leave a real holding attributable to nobody.

    NO_FILL   asked, answered, nothing filled
    PARTIAL   asked, answered, some filled
    FILLED    asked, answered, all filled
    UNKNOWN   could not ask -- filled_quantity is None, never 0

Cumulative, never delta
-----------------------
KIS's `ft_ccld_qty` is per-execution-ROW (CODEX-044). Two 1-share rows
for one order are 2 filled, not 1, and re-reading them is still 2 -- so
a restart that sees the same report cannot double the position.

No order is placed anywhere in this file. The broker is a fake that
serves recorded row shapes and raises if anything asks it to trade.
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from brokers import kis_fill_inquiry as fq  # noqa: E402
from s6_live import exit_runtime, position_store  # noqa: E402

T0 = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)
ORDER = "0030412345"


def fill_row(order_id=ORDER, qty="1", price="100.5", symbol="AAPL",
             excg="NASD", tm="093015"):
    """A KIS fill row in the field shape the API returns."""
    return {"ODNO": order_id, "ft_ccld_qty": qty, "ft_ccld_unpr3": price,
            "pdno": symbol, "ovrs_excg_cd": excg, "ccld_tm": tm}


class FakeBroker:
    """Read-only. Placing an order through it is a test failure."""

    def __init__(self, fills=None, open_orders=None, fills_error=None,
                 open_error=None):
        self._fills = fills or []
        self._open = open_orders if open_orders is not None else []
        self._fills_error = fills_error
        self._open_error = open_error
        self.fill_calls = 0
        self.open_calls = 0

    def get_fills(self, *, start_date, end_date):
        self.fill_calls += 1
        if self._fills_error:
            raise self._fills_error
        return list(self._fills)

    def get_open_orders(self):
        self.open_calls += 1
        if self._open_error:
            raise self._open_error
        return list(self._open)

    def submit_order(self, *a, **k):
        raise AssertionError("a fill inquiry must never place an order")


def _code_names(path, functions):
    """Every identifier and string literal in the named functions,
    EXCLUDING docstrings. Prose may discuss what the code must not do."""
    import ast

    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in functions:
            continue
        body = list(node.body)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]                      # drop the docstring
        for inner in body:
            for sub in ast.walk(inner):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    names.add(sub.attr)
                elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    names.add(sub.value)
    return names


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


# ====================================================================
# BUY: no fill / partial / full
# ====================================================================
class TestBuyInquiry:
    def test_no_fill_while_the_order_is_still_open(self):
        broker = FakeBroker(fills=[], open_orders=[{"ODNO": ORDER}])
        r = fq.inquire(broker, broker_order_id=ORDER, ordered_quantity=1,
                       now=T0)
        assert r.status == fq.STATUS_NO_FILL
        assert r.filled_quantity == 0
        assert r.terminal is False, "still open -- not final"
        assert r.as_store_fill() is None, "no action while it may still fill"

    def test_no_fill_and_gone_from_open_orders_is_terminal(self):
        broker = FakeBroker(fills=[], open_orders=[])
        r = fq.inquire(broker, broker_order_id=ORDER, ordered_quantity=1,
                       now=T0)
        assert r.status == fq.STATUS_NO_FILL
        assert r.terminal is True
        assert r.as_store_fill()["terminal"] is True

    def test_partial_fill_reports_cumulative_and_remaining(self):
        broker = FakeBroker(fills=[fill_row(qty="1")],
                            open_orders=[{"ODNO": ORDER}])
        r = fq.inquire(broker, broker_order_id=ORDER, ordered_quantity=3,
                       now=T0)
        assert r.status == fq.STATUS_PARTIAL
        assert r.filled_quantity == 1
        assert r.remaining_quantity == 2
        assert r.terminal is False

    def test_two_execution_rows_sum_and_do_not_report_the_first(self):
        """CODEX-044: ft_ccld_qty is per-row, not cumulative."""
        broker = FakeBroker(fills=[fill_row(qty="1", price="100.0"),
                                   fill_row(qty="1", price="102.0")])
        r = fq.inquire(broker, broker_order_id=ORDER, ordered_quantity=2,
                       now=T0)
        assert r.filled_quantity == 2
        assert r.status == fq.STATUS_FILLED
        assert r.average_fill_price == pytest.approx(101.0), \
            "quantity-weighted, not the last row's price"

    def test_the_average_is_weighted_by_quantity(self):
        broker = FakeBroker(fills=[fill_row(qty="1", price="100.0"),
                                   fill_row(qty="3", price="200.0")])
        r = fq.inquire(broker, broker_order_id=ORDER, ordered_quantity=4,
                       now=T0)
        assert r.average_fill_price == pytest.approx(175.0)

    def test_rows_for_another_order_are_ignored(self):
        broker = FakeBroker(fills=[fill_row(order_id="9999999", qty="5")])
        r = fq.inquire(broker, broker_order_id=ORDER, ordered_quantity=1,
                       now=T0)
        assert r.filled_quantity == 0


# ====================================================================
# UNKNOWN must never look like "nothing filled"
# ====================================================================
class TestUnknownIsNotZero:
    def test_a_failed_fill_inquiry_is_unknown(self):
        broker = FakeBroker(fills_error=RuntimeError("KIS 500"))
        r = fq.inquire(broker, broker_order_id=ORDER, ordered_quantity=1,
                       now=T0)
        assert r.status == fq.STATUS_UNKNOWN
        assert r.filled_quantity is None, "None, never 0"
        assert r.usable is False
        assert r.as_store_fill() is None

    def test_a_missing_order_id_is_unknown_not_unfilled(self):
        broker = FakeBroker(fills=[])
        r = fq.inquire(broker, broker_order_id=None, ordered_quantity=1,
                       now=T0)
        assert r.status == fq.STATUS_UNKNOWN
        assert r.filled_quantity is None
        assert broker.fill_calls == 0

    def test_a_non_numeric_quantity_poisons_the_answer(self):
        """A partial sum reported as cumulative is worse than no answer."""
        broker = FakeBroker(fills=[fill_row(qty="1"), fill_row(qty="abc")])
        r = fq.inquire(broker, broker_order_id=ORDER, ordered_quantity=2,
                       now=T0)
        assert r.status == fq.STATUS_UNKNOWN
        assert r.filled_quantity is None

    def test_a_failed_open_order_read_never_makes_it_terminal(self):
        """Declining to abandon is the safe direction."""
        broker = FakeBroker(fills=[], open_error=RuntimeError("KIS 500"))
        r = fq.inquire(broker, broker_order_id=ORDER, ordered_quantity=1,
                       now=T0)
        assert r.status == fq.STATUS_NO_FILL
        assert r.terminal is False
        assert r.as_store_fill() is None


# ====================================================================
# Idempotency: the same report twice changes nothing
# ====================================================================
class TestCumulativeIsIdempotent:
    def _submitted(self, conn):
        return position_store.record_submission(
            conn, symbol="AAPL", variant="S6-R", entry_session="REGULAR",
            range_high=99.5, range_low=99.0, client_order_id="s6buy-AAPL-1",
            now=T0)

    def test_the_same_fill_applied_twice_does_not_double_the_position(self, conn):
        pid = self._submitted(conn)
        fill = {"filled_quantity": 2, "average_fill_price": 100.0,
                "venue": "NASD", "order_id": ORDER}
        exit_runtime.sync_buy_fills(conn, fills_for=lambda r: fill, now=T0)
        assert position_store.load(conn, pid)["quantity"] == 2
        exit_runtime.sync_buy_fills(conn, fills_for=lambda r: fill, now=T0)
        assert position_store.load(conn, pid)["quantity"] == 2

    def test_a_stale_smaller_cumulative_is_ignored(self, conn):
        pid = self._submitted(conn)
        exit_runtime.sync_buy_fills(
            conn, fills_for=lambda r: {"filled_quantity": 3,
                                       "average_fill_price": 100.0}, now=T0)
        assert position_store.load(conn, pid)["quantity"] == 3
        exit_runtime.sync_buy_fills(
            conn, fills_for=lambda r: {"filled_quantity": 1,
                                       "average_fill_price": 50.0}, now=T0)
        row = position_store.load(conn, pid)
        assert row["quantity"] == 3, "a stale report must not shrink it"
        assert row["entry_price"] == 100.0

    def test_a_growing_cumulative_is_applied(self, conn):
        pid = self._submitted(conn)
        exit_runtime.sync_buy_fills(
            conn, fills_for=lambda r: {"filled_quantity": 1,
                                       "average_fill_price": 100.0}, now=T0)
        exit_runtime.sync_buy_fills(
            conn, fills_for=lambda r: {"filled_quantity": 2,
                                       "average_fill_price": 101.0}, now=T0)
        row = position_store.load(conn, pid)
        assert row["quantity"] == 2
        assert row["entry_price"] == 101.0


# ====================================================================
# Restart / ambiguous submission recovery
# ====================================================================
class TestRecoveryAfterRestart:
    def test_a_submitted_buy_is_resolved_from_the_broker(self, conn):
        """The position existed only as SUBMITTED; the broker says filled."""
        pid = position_store.record_submission(
            conn, symbol="AAPL", variant="S6-R", entry_session="REGULAR",
            range_high=99.5, range_low=99.0, client_order_id="s6buy-AAPL-1",
            now=T0)
        broker = FakeBroker(fills=[fill_row(qty="1", price="100.25")],
                            open_orders=[])

        import scripts.run_s6_runtime as runtime

        conn.execute(
            "INSERT INTO kis_order_idempotency (internal_order_id, signal_id, "
            "symbol, side, trading_date, broker_order_id, status, created_at, "
            "updated_at, requested_quantity) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("s6buy-AAPL-1", "sig-1", "AAPL", "buy", "20260824", ORDER,
             "SUBMITTED", T0.isoformat(), T0.isoformat(), 1))
        conn.commit()

        exit_runtime.sync_buy_fills(
            conn, fills_for=runtime._buy_fill_lookup(conn, broker, now=T0),
            now=T0)
        row = position_store.load(conn, pid)
        assert row["status"] == position_store.OPEN
        assert row["quantity"] == 1
        assert row["entry_price"] == pytest.approx(100.25)

    def test_a_never_filled_terminal_buy_is_abandoned(self, conn):
        pid = position_store.record_submission(
            conn, symbol="AAPL", variant="S6-R", entry_session="REGULAR",
            range_high=99.5, range_low=99.0, client_order_id="s6buy-AAPL-1",
            now=T0)
        results = exit_runtime.sync_buy_fills(
            conn, fills_for=lambda r: {"filled_quantity": 0,
                                       "terminal": True}, now=T0)
        assert results[0]["status"] == "ABANDONED"
        assert position_store.load(conn, pid)["status"] == "CLOSED"

    def test_an_unknown_inquiry_leaves_a_submitted_buy_untouched(self, conn):
        """The failure that would otherwise abandon a real holding."""
        pid = position_store.record_submission(
            conn, symbol="AAPL", variant="S6-R", entry_session="REGULAR",
            range_high=99.5, range_low=99.0, client_order_id="s6buy-AAPL-1",
            now=T0)
        broker = FakeBroker(fills_error=RuntimeError("KIS 500"))

        import scripts.run_s6_runtime as runtime

        results = exit_runtime.sync_buy_fills(
            conn, fills_for=runtime._buy_fill_lookup(conn, broker, now=T0),
            now=T0)
        assert results[0]["status"] == "STILL_UNCONFIRMED"
        assert position_store.load(conn, pid)["status"] == "SUBMITTED"


# ====================================================================
# SELL: partial, full, and exit_price persistence
# ====================================================================
class TestSellInquiry:
    def _held(self, conn, quantity=2):
        pid = position_store.record_submission(
            conn, symbol="AAPL", variant="S6-R", entry_session="REGULAR",
            range_high=99.5, range_low=99.0, now=T0)
        position_store.open_from_fill(conn, pid, quantity=quantity,
                                      average_fill_price=100.0, now=T0)
        position_store.latch_pending_exit(conn, pid, "RANGE_REENTRY", now=T0)
        position_store.mark_exit_submitted(conn, pid, "RANGE_REENTRY", now=T0)
        return pid

    def test_a_full_sell_closes_and_records_the_exit_price(self, conn):
        pid = self._held(conn, quantity=2)
        exit_runtime.sync_sell_fills(
            conn, fills_for=lambda r: {"filled_quantity": 2,
                                       "average_fill_price": 101.75}, now=T0)
        row = position_store.load(conn, pid)
        assert row["status"] == "CLOSED"
        assert row["exit_price"] == pytest.approx(101.75), \
            "schema 17 exit_price -- realised P&L needs the real fill"

    def test_a_partial_sell_keeps_the_remainder_managed(self, conn):
        pid = self._held(conn, quantity=3)
        results = exit_runtime.sync_sell_fills(
            conn, fills_for=lambda r: {"filled_quantity": 1,
                                       "average_fill_price": 101.0}, now=T0)
        assert results[0]["remaining"] == 2
        row = position_store.load(conn, pid)
        assert row["quantity"] == 2
        assert row["status"] != "CLOSED"

    def test_an_unknown_sell_inquiry_never_closes_the_position(self, conn):
        pid = self._held(conn, quantity=2)
        broker = FakeBroker(fills_error=RuntimeError("KIS 500"))

        import scripts.run_s6_runtime as runtime

        results = exit_runtime.sync_sell_fills(
            conn, fills_for=runtime._sell_fill_lookup(conn, broker, now=T0),
            now=T0)
        assert results[0]["status"] == "AWAITING_SELL_FILL"
        assert position_store.load(conn, pid)["status"] == "EXIT_SUBMITTED"

    def test_the_sell_order_id_comes_from_the_exit_intent_ledger(self, conn):
        from state_store import exit_intent_ledger

        pid = self._held(conn, quantity=1)
        intent = exit_intent_ledger.reserve(conn, pid, "RANGE_REENTRY", 1,
                                            "s6exit-AAPL-1")
        exit_intent_ledger.mark_submitted(conn, intent, broker_order_id=ORDER)

        import scripts.run_s6_runtime as runtime

        row = position_store.load(conn, pid)
        order_id, _since = runtime._order_id_for(conn, row, side="sell")
        assert order_id == ORDER


# ====================================================================
# The lookup is session-independent (§9) and places nothing
# ====================================================================
class TestTheLookupIsSessionIndependent:
    def test_it_never_reads_the_variant(self):
        """Checked against the CODE, not the prose: the docstring is
        allowed to mention the variant in order to explain why the code
        does not read it."""
        assert _code_names(REPO_ROOT / "scripts" / "run_s6_runtime.py",
                           ("_fill_lookup", "_order_id_for")) \
            .isdisjoint({"variant", "entry_session"})

    def test_a_premarket_entry_is_read_back_by_the_same_lookup(self, conn):
        pid = position_store.record_submission(
            conn, symbol="AAPL", variant="S6-P", entry_session="PREMARKET",
            range_high=99.5, range_low=99.0, client_order_id="s6buy-AAPL-1",
            now=T0)
        broker = FakeBroker(fills=[fill_row(qty="1", price="100.0")],
                            open_orders=[])
        conn.execute(
            "INSERT INTO kis_order_idempotency (internal_order_id, signal_id, "
            "symbol, side, trading_date, broker_order_id, status, created_at, "
            "updated_at, requested_quantity) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("s6buy-AAPL-1", "sig-1", "AAPL", "buy", "20260824", ORDER,
             "SUBMITTED", T0.isoformat(), T0.isoformat(), 1))
        conn.commit()

        import scripts.run_s6_runtime as runtime

        # Read in REGULAR, hours after a PREMARKET entry.
        exit_runtime.sync_buy_fills(
            conn, fills_for=runtime._buy_fill_lookup(
                conn, broker, now=T0 + timedelta(hours=4)), now=T0)
        row = position_store.load(conn, pid)
        assert row["status"] == position_store.OPEN
        assert row["variant"] == "S6-P", "attribution is still the entry's"

    def test_the_inquiry_places_no_order(self):
        import ast

        text = (REPO_ROOT / "brokers" / "kis_fill_inquiry.py").read_text()
        calls = {n.func.attr for n in ast.walk(ast.parse(text))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert "submit_order" not in calls
        assert "cancel_order" not in calls
        assert calls & {"get_fills", "get_open_orders"}, \
            "it must use the EXISTING read paths"


class TestS1KeepsItsExactBehaviour:
    def test_the_legacy_shape_is_unchanged(self):
        broker = FakeBroker(fills=[fill_row(qty="1", price="100.0"),
                                   fill_row(qty="1", price="102.0")])
        assert fq.find_fill(broker, ORDER, now=T0) == {
            "filled_qty": 2.0, "average_fill_price": 101.0}

    def test_no_fill_is_still_none_for_s1(self):
        assert fq.find_fill(FakeBroker(fills=[]), ORDER, now=T0) is None

    def test_a_failed_inquiry_is_still_none_for_s1(self):
        broker = FakeBroker(fills_error=RuntimeError("boom"))
        assert fq.find_fill(broker, ORDER, now=T0) is None

    def test_s1s_helper_delegates_rather_than_duplicating(self):
        import inspect

        import kis_position_manager

        source = inspect.getsource(
            kis_position_manager._find_kis_fill_for_order)
        assert "kis_fill_inquiry.find_fill" in source
        # Against the code, not the docstring, which still explains the
        # per-execution-row hazard it delegates.
        assert _code_names(REPO_ROOT / "kis_position_manager.py",
                           ("_find_kis_fill_for_order",)) \
            .isdisjoint({"ft_ccld_qty", "FT_CCLD_QTY"}), \
            "no second copy of the parsing"


class TestFillQueryRuntimeReady:
    def test_it_is_ready_now_that_the_lookup_is_wired(self):
        from s6_live import session_capability

        verdict = session_capability.runtime_ready()
        assert verdict.verified is True

    def test_it_is_not_the_same_claim_as_an_observed_fill(self):
        """§11: implementation readiness is not a production observation."""
        from s6_live import variant_state

        states = variant_state.evaluate(observations={})
        regular = states["S6-R"]
        assert regular.checks["regular_fill_query_verified"] == \
            variant_state.PASS
        assert regular.checks["regular_market_tick_verified"] == \
            variant_state.NOT_MEASURED
        # The subject is that a code-readiness check is NOT a production
        # observation -- so the observation stays NOT_MEASURED above.
        # The variant mode reflects the (separate) promotion decision
        # and is deliberately not asserted here.
