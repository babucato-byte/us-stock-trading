"""The S2 tick: orchestration that owns no safety decision.

The properties under test are all about what this entrypoint does NOT
do. It must not place an order itself, must not re-implement a gate,
must not let an entry failure cost the exits, and must not let anything
about entry risk stop a held position from leaving.

The last one has a name in this codebase's history: a risk control that
also blocked liquidation would trap the account in the position the
control exists to escape.
"""

import ast
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.run_s2_live_cycle as cycle  # noqa: E402
from s2_live import exit_runtime, position_store as ps  # noqa: E402

SCRIPT = REPO_ROOT / "scripts" / "run_s2_live_cycle.py"
BASE = 1_000_000
# 16:00 UTC == 12:00 ET, inside REGULAR and far from the session-exit lead.
REGULAR = datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc)
PREMARKET = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


class Features:
    def __init__(self, price=105.0, hma200=95.0, hma200_slope=0.4,
                 vwap=100.0, volume=6 * BASE):
        self.price, self.hma200, self.hma200_slope = price, hma200, hma200_slope
        self.vwap, self.volume = vwap, volume


class Adapter:
    """Stands in for the broker adapter. Records; never sends."""

    def __init__(self, status=200):
        self.calls, self._status = [], status

    def submit_order(self, symbol, quantity, *, side, client_order_id=None):
        self.calls.append({"symbol": symbol, "quantity": quantity,
                           "side": side, "client_order_id": client_order_id})
        return type("R", (), {"status_code": self._status, "text": "ok"})()


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def held(conn, price_at_volume_peak=None, **kw):
    """An open S2 position, optionally with a volume peak already set.

    The peak price is written through `observe()` rather than passed to
    `open_position()`, because that is how it is set in production: the
    peak is a fact discovered by a later tick, not something known at
    entry.
    """
    kw.setdefault("symbol", "ABC")
    kw.setdefault("quantity", 1)
    kw.setdefault("average_fill_price", 100.0)
    kw.setdefault("entry_volume_multiple", 6.0)
    kw.setdefault("baseline_volume", BASE)
    kw.setdefault("now", REGULAR)
    pid = ps.open_position(conn, **kw)
    if price_at_volume_peak is not None:
        ps.observe(conn, pid,
                   volume_multiple=kw["entry_volume_multiple"] + 0.001,
                   price=price_at_volume_peak, now=REGULAR)
    return pid


class TestItOwnsNoSafetyDecision:
    def test_it_never_calls_a_broker_submit_directly(self):
        """Every submit must come from the shared paths."""
        source = SCRIPT.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"submit_order", "place_order",
                                         "submit_buy", "submit_sell"}, node.attr

    def test_the_entry_goes_through_the_shared_buy_cycle(self):
        source = SCRIPT.read_text()
        assert "run_live_buy_entry_cycle" in source
        assert "candidate_source=source" in source

    def test_it_reimplements_no_gate(self):
        """The gates live in the shared paths and are not re-checked
        here -- a second copy would be a second idea of what is safe."""
        source = SCRIPT.read_text()
        for gate in ("require_live_eligible", "orderable_cash",
                     "BuyGateContext", "evaluate_buy_gate", "kill_switch",
                     "check_entry"):
            assert gate not in source, gate

    def test_it_touches_no_s1_position(self):
        """If this script fails entirely, S1 is unaffected."""
        source = SCRIPT.read_text()
        assert "s1_live.position_store" not in source
        assert "s1_positions" not in source


class TestSessionPolicy:
    # `conn` is required even where the test never touches the store:
    # `run_once` opens the state DB, and without the fixture's temp path
    # that means creating a real database at the repo root. Three
    # existing guard tests exist precisely to catch that.
    def test_regular_is_the_only_session_that_may_order(self, conn):
        report = cycle.run_once(now=PREMARKET)
        assert report["session"] == "PREMARKET"
        assert report["status"] == cycle.STATUS_SESSION_CLOSED
        assert report["entry"]["enabled"] == ["REGULAR"]

    def test_a_quiet_regular_session_is_a_normal_result(self, conn,
                                                        monkeypatch):
        """No candidate is not an error, and not a reason to relax
        anything."""
        monkeypatch.setattr(cycle, "_run_entry",
                            lambda **kw: {"status": cycle.STATUS_NO_CANDIDATE})
        report = cycle.run_once(now=REGULAR)
        assert report["status"] == cycle.STATUS_NO_CANDIDATE
        assert report["errors"] == []


class TestExitsAreNeverGatedByEntry:
    def test_an_entry_failure_does_not_cost_the_exits(self, conn, monkeypatch):
        recorded = {}

        def explode(**kw):
            raise RuntimeError("entry stage down")

        monkeypatch.setattr(cycle, "_run_entry", explode)
        monkeypatch.setattr(cycle, "_run_exits",
                            lambda c, **kw: recorded.setdefault("ran", True) or [])
        report = cycle.run_once(now=REGULAR)
        assert recorded.get("ran") is True, "the exits ran first"
        assert any("entry" in e for e in report["errors"])

    def test_exits_are_evaluated_outside_regular_too(self, conn, monkeypatch):
        """A held position must be evaluated in every session; only the
        SUBMISSION is restricted."""
        seen = {}
        monkeypatch.setattr(cycle, "_run_exits",
                            lambda c, **kw: seen.setdefault("session",
                                                            kw["session"]) or [])
        cycle.run_once(now=PREMARKET)
        assert seen["session"] == "PREMARKET"

    def test_an_unorderable_session_latches_rather_than_drops(self, conn):
        """A position that should be leaving must not be forgotten
        because the clock was wrong."""
        pid = held(conn, price_at_volume_peak=110.0)
        adapter = Adapter()
        outcomes = exit_runtime.run_exits(
            conn, broker_adapter=adapter,
            features_fn=lambda s: Features(price=104.0, volume=2 * BASE),
            price_fn=lambda s: 104.0, session="PREMARKET", now=REGULAR,
            orders_allowed=False)
        assert adapter.calls == [], "no order in an unorderable session"
        assert outcomes[0]["action"] == exit_runtime.ACTION_LATCHED
        row = ps.load_by_symbol(conn, "ABC")
        assert row["status"] == ps.EXIT_PENDING
        assert row["pending_exit_reason"] == "VOLUME_DECAY_PRICE_WEAKNESS"


class TestTheExitRuntimeUsesTheSharedSubmission:
    def test_a_sell_goes_through_s1s_submit_path(self):
        source = (REPO_ROOT / "s2_live" / "exit_runtime.py").read_text()
        assert "from s1_live.exit_runtime import ExitOutcome, _submit_sell" in source

    def test_a_decayed_and_weak_position_sells(self, conn):
        held(conn, price_at_volume_peak=110.0)
        adapter = Adapter()
        outcomes = exit_runtime.run_exits(
            conn, broker_adapter=adapter,
            features_fn=lambda s: Features(price=104.0, volume=2 * BASE),
            price_fn=lambda s: 104.0, session="REGULAR", now=REGULAR)
        assert len(adapter.calls) == 1
        assert adapter.calls[0]["side"] == "sell"
        assert adapter.calls[0]["client_order_id"].startswith("s2exit-")
        assert outcomes[0]["reason"] == "VOLUME_DECAY_PRICE_WEAKNESS"
        assert ps.load_by_symbol(conn, "ABC")["exit_submitted"] == 1

    def test_a_healthy_position_is_held(self, conn):
        held(conn)
        adapter = Adapter()
        outcomes = exit_runtime.run_exits(
            conn, broker_adapter=adapter, features_fn=lambda s: Features(),
            price_fn=lambda s: 105.0, session="REGULAR", now=REGULAR)
        assert adapter.calls == []
        assert outcomes[0]["action"] == exit_runtime.ACTION_HELD

    def test_one_position_cannot_produce_two_sells(self, conn):
        held(conn, price_at_volume_peak=110.0)
        adapter = Adapter()
        for _ in range(3):
            exit_runtime.run_exits(
                conn, broker_adapter=adapter,
                features_fn=lambda s: Features(price=104.0, volume=2 * BASE),
                price_fn=lambda s: 104.0, session="REGULAR", now=REGULAR)
        assert len(adapter.calls) == 1, "exit_submitted is one-way"

    def test_the_observation_is_recorded_before_the_decision(self, conn):
        """Asking first would judge a position against a peak it had
        already exceeded."""
        held(conn, entry_volume_multiple=2.0)
        exit_runtime.run_exits(
            conn, broker_adapter=Adapter(),
            features_fn=lambda s: Features(price=112.0, volume=8 * BASE),
            price_fn=lambda s: 112.0, session="REGULAR", now=REGULAR)
        row = ps.load_by_symbol(conn, "ABC")
        assert row["peak_volume_multiple"] == 8.0
        assert row["price_at_volume_peak"] == 112.0

    def test_one_positions_failure_does_not_cost_the_others(self, conn):
        held(conn, symbol="GOOD")
        held(conn, symbol="BAD", average_fill_price=50.0)

        def features(symbol):
            if symbol == "BAD":
                raise RuntimeError("provider down")
            return Features()

        outcomes = exit_runtime.run_exits(
            conn, broker_adapter=Adapter(), features_fn=features,
            price_fn=lambda s: 105.0, session="REGULAR", now=REGULAR)
        actions = {o["symbol"]: o["action"] for o in outcomes}
        assert actions["GOOD"] == exit_runtime.ACTION_HELD
        assert actions["BAD"] == exit_runtime.ACTION_BLOCKED


class TestExitOwnershipIsSeparated:
    def test_the_legacy_manager_does_not_exit_s2_positions(self):
        """A position evaluated by two exit policies gets two SELLs for
        one holding."""
        import kis_position_manager as pm

        assert "S2_VOLUME_ACCUMULATION_V1" in pm.EXIT_MANAGED_ELSEWHERE_STRATEGY_IDS
        assert "S1_HMA_EARLY_TREND_V1" in pm.EXIT_MANAGED_ELSEWHERE_STRATEGY_IDS

    def test_the_guard_covers_exit_only_not_fill_sync(self):
        """Conflating the two is what cost S1 its bookkeeping once
        already: the exit guard excluded S1 wholesale and took fill
        synchronisation with it."""
        source = (REPO_ROOT / "kis_position_manager.py").read_text()
        marker = source.index("EXIT_MANAGED_ELSEWHERE_STRATEGY_IDS = frozenset")
        block = source[marker:marker + 800]
        assert "fill synchronisation is deliberately NOT gated" in block
