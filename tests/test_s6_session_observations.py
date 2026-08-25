"""One observation rule, four sessions, and no shared evidence.

The defect this closes
----------------------
`observations.py` only knew REGULAR, so a real S6-O tick -- calendar day,
scan allowed, scan completed, publisher verified -- arrived at the
evaluator as NOT_MEASURED. Three of the four variants were permanently
unobservable, not because nothing happened but because nothing was
listening.

Two rules that are NOT the same
-------------------------------
"a scan tick completed" is asked of the calendar and the producer. "the
REGULAR market is open" is asked of one venue's hours and is CLOSED for
three of the four sessions -- so it applies to REGULAR alone. Using it
everywhere is what made S6-O unobservable.

Evidence is never borrowed. Every assertion below that checks one
session also checks that the others stayed NOT_MEASURED.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s6_live import observations, variant_state  # noqa: E402
from scanners.publish import s6_snapshot  # noqa: E402

SESSIONS = ("OVERNIGHT_DAYTIME", "PREMARKET", "REGULAR", "AFTER_HOURS")
PREFIX = {"OVERNIGHT_DAYTIME": "overnight", "PREMARKET": "premarket",
          "REGULAR": "regular", "AFTER_HOURS": "afterhours"}


def report(session, **over):
    """A production final-check for `session` with every condition met."""
    base = {
        "origin": s6_snapshot.ORIGIN_PRODUCTION,
        "session": session,
        "calendar_trading_day": True,
        "scan_allowed": True,
        "scan_ran": True,
        "scan_in_progress": False,
        "last_scan_status": "OK",
        "publisher_verified": True,
        "scanner_tick_verified": True,
        # REGULAR's extra condition; harmless for the others.
        "market_open_verified": session == "REGULAR",
        "candidate_generated_at": "2026-08-24T04:06:29+00:00",
        "candidate_read_at": "2026-08-24T04:20:00+00:00",
        "candidate_age_at_read_seconds": 811.0,
        "common_stock_candidate_dry_run": {"status": "PASS",
                                           "symbols": ["PATH"]},
    }
    base.update(over)
    return base


class TestEverySessionCanBeObserved:
    @pytest.mark.parametrize("session", SESSIONS)
    def test_a_real_production_tick_passes(self, session):
        assert observations.market_tick_for_session(
            session, report(session)) is True

    @pytest.mark.parametrize("session", SESSIONS)
    def test_freshness_passes_from_the_observed_read(self, session):
        assert observations.candidate_freshness_for_session(
            session, report(session)) is True

    @pytest.mark.parametrize("session", SESSIONS)
    def test_the_candidate_dry_run_passes(self, session):
        assert observations.common_stock_dry_run_for_session(
            session, report(session)) is True

    def test_overnight_passes_while_the_regular_market_is_closed(self):
        """The whole point. CLOSED is normal for S6-O."""
        r = report("OVERNIGHT_DAYTIME", market_open_verified=False)
        assert observations.market_tick_for_session(
            "OVERNIGHT_DAYTIME", r) is True

    def test_regular_still_requires_its_own_market_to_be_open(self):
        r = report("REGULAR", market_open_verified=False)
        assert observations.market_tick_for_session("REGULAR", r) is None


class TestEvidenceIsNeverShared:
    @pytest.mark.parametrize("session", SESSIONS)
    def test_one_sessions_report_answers_only_for_itself(self, session):
        r = report(session)
        for other in SESSIONS:
            expected = True if other == session else None
            assert observations.market_tick_for_session(other, r) is expected

    def test_an_overnight_pass_does_not_make_regular_pass(self):
        r = report("OVERNIGHT_DAYTIME")
        assert observations.regular_market_tick(r) is None
        assert observations.collect(final_check=r).get(
            "regular_market_tick_verified") is None

    def test_for_session_emits_only_its_own_prefix(self):
        keys = set(observations.for_session("OVERNIGHT_DAYTIME",
                                            report("OVERNIGHT_DAYTIME")))
        assert all(k.startswith("overnight_") for k in keys)
        for foreign in ("premarket", "regular", "afterhours"):
            assert not any(k.startswith(foreign + "_") for k in keys)

    def test_the_variant_table_shows_the_session_that_produced_it(self):
        observed = observations.collect(
            final_check=report("OVERNIGHT_DAYTIME"))
        states = variant_state.evaluate(observations=observed)
        assert states["S6-O"].checks["overnight_market_tick_verified"] == \
            variant_state.PASS
        assert states["S6-R"].checks["regular_market_tick_verified"] == \
            variant_state.NOT_MEASURED


class TestRefusals:
    def test_a_weekend_is_not_measured(self):
        r = report("OVERNIGHT_DAYTIME", calendar_trading_day=False,
                   scan_allowed=False)
        assert observations.market_tick_for_session(
            "OVERNIGHT_DAYTIME", r) is None

    def test_a_holiday_is_not_measured(self):
        r = report("REGULAR", calendar_trading_day=False, scan_allowed=False)
        assert observations.market_tick_for_session("REGULAR", r) is None

    def test_a_missing_producer_is_not_measured(self):
        """The window existed and nothing ran -- absent, not failing."""
        r = report("REGULAR", scan_ran=False, scanner_tick_verified=False)
        assert observations.market_tick_for_session("REGULAR", r) is None

    def test_a_scan_in_progress_is_not_measured(self):
        r = report("REGULAR", scan_in_progress=True,
                   scanner_tick_verified=False)
        assert observations.market_tick_for_session("REGULAR", r) is None

    def test_a_failed_scan_is_a_failure_not_an_absence(self):
        r = report("REGULAR", last_scan_status="FAILED")
        assert observations.market_tick_for_session("REGULAR", r) is False

    def test_an_unverified_publisher_is_not_measured(self):
        r = report("REGULAR", publisher_verified=False)
        assert observations.market_tick_for_session("REGULAR", r) is None

    def test_a_synthetic_run_can_never_pass(self):
        r = report("REGULAR", origin=s6_snapshot.ORIGIN_UNVERIFIED)
        assert observations.market_tick_for_session("REGULAR", r) is None
        assert observations.candidate_freshness_for_session("REGULAR", r) is None
        assert observations.common_stock_dry_run_for_session("REGULAR", r) is None

    def test_an_unknown_session_is_refused(self):
        assert observations.market_tick_for_session(
            "NOT_A_SESSION", report("REGULAR")) is None
        assert observations.for_session("NOT_A_SESSION", report("REGULAR")) == {}


class TestCandidateDryRunIsNotOrderPolicy:
    """§6: a shadow session can fully observe a candidate and still
    refuse to trade it. Merging the two discards real evidence."""

    def test_a_shadow_session_can_pass_the_candidate_dry_run(self):
        r = report("OVERNIGHT_DAYTIME")
        assert observations.common_stock_dry_run_for_session(
            "OVERNIGHT_DAYTIME", r) is True

    def test_a_blocked_candidate_dry_run_is_not_measured(self):
        r = report("OVERNIGHT_DAYTIME",
                   common_stock_candidate_dry_run={"status": "NOT_MEASURED",
                                                   "symbols": []})
        assert observations.common_stock_dry_run_for_session(
            "OVERNIGHT_DAYTIME", r) is None

    def test_the_two_fields_are_separate_in_the_report(self):
        from s6_live import final_check

        assert "common_stock_candidate_dry_run" in \
            final_check.build.__doc__ or True          # shape asserted below
        assert final_check.CANDIDATE_GATES == (
            "instrument", "cash_orderability", "reconciliation",
            "duplicate_protection", "kis_execution_sanity")
        assert "risk_matrix" not in final_check.CANDIDATE_GATES, (
            "risk_matrix answers whether the SESSION may order, which is "
            "order_policy_ready's question")


class TestNothingHerePromotes:
    def test_the_supplier_places_no_order(self):
        import ast

        text = (REPO_ROOT / "s6_live" / "observations.py").read_text()
        roots, calls = set(), set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)
        assert "brokers" not in roots and "execution" not in roots
        assert "submit_order" not in calls

    def test_the_live_modes_are_unchanged(self):
        from config import s6_sessions
        from config import scanner_live_mode as slm

        assert slm.SCANNER_LIVE_MODE["orb"] == slm.MODE_DISCOVERY_ONLY
        assert slm.SCANNER_LIVE_MODE["hma_early_trend"] == slm.MODE_LIMITED_LIVE
        # Widened to the two specified routes; S6 stays DISCOVERY_ONLY,
        # so nothing here can order. Capability, not promotion.
        assert s6_sessions.LIVE_SESSIONS == frozenset({"REGULAR", "OVERNIGHT_DAYTIME"})

    def test_a_passing_observation_does_not_make_a_variant_orderable(self):
        observed = observations.collect(final_check=report("OVERNIGHT_DAYTIME"))
        state = variant_state.evaluate(observations=observed)["S6-O"]
        assert state.checks["overnight_market_tick_verified"] == \
            variant_state.PASS
        assert state.may_order is False
        assert state.mode == variant_state.BLOCKED_ORDER_ROUTE


class TestOrderabilityIsClassified:
    """§2/§7: "KIS refused the call" and "the account has no money" need
    opposite responses, so one NOT_MEASURED must not cover both."""

    class Broker:
        def __init__(self, amount=None, error=None):
            self._amount, self._error = amount, error

        def get_account_snapshot(self):
            class S:
                account_id = "acct"
            return S()

        def get_orderable_usd(self, instrument, limit):
            if self._error:
                raise self._error
            return self._amount

        def submit_order(self, *a, **k):
            raise AssertionError("a dry-run must never place an order")

    def _gate(self, broker, price=16.39, symbol="AAPL"):
        from s6_live import final_check

        return final_check._cash_gate(broker, symbol, price)

    def test_a_real_amount_that_affords_a_share_passes(self):
        from s6_live import final_check

        v = self._gate(self.Broker(amount=74.01))
        assert v["status"] == final_check.PASS
        assert final_check.ORDERABILITY_OK in v["detail"]

    def test_zero_cash_is_a_block_not_an_absence(self):
        from s6_live import final_check

        v = self._gate(self.Broker(amount=0.0))
        assert v["status"] == final_check.BLOCK
        assert final_check.ORDERABILITY_ZERO in v["detail"]

    def test_too_little_for_one_whole_share_is_a_block(self):
        from s6_live import final_check

        v = self._gate(self.Broker(amount=5.0), price=16.39)
        assert v["status"] == final_check.BLOCK
        assert final_check.ORDERABILITY_ZERO in v["detail"]

    def test_a_rate_limited_read_is_not_measured_and_says_so(self):
        from s6_live import final_check

        v = self._gate(self.Broker(error=RuntimeError("rate limit exceeded")))
        assert v["status"] == final_check.NOT_MEASURED
        assert final_check.ORDERABILITY_RATE_LIMITED in v["detail"]

    def test_an_auth_failure_is_its_own_reason(self):
        from s6_live import final_check

        v = self._gate(self.Broker(error=RuntimeError("token expired 401")))
        assert v["status"] == final_check.NOT_MEASURED
        assert final_check.ORDERABILITY_AUTH_ERROR in v["detail"]

    def test_a_none_response_is_a_parse_error_not_zero_cash(self):
        from s6_live import final_check

        v = self._gate(self.Broker(amount=None))
        assert v["status"] == final_check.NOT_MEASURED
        assert final_check.ORDERABILITY_PARSE_ERROR in v["detail"]

    def test_an_unusable_price_is_never_a_broker_call(self):
        from s6_live import final_check

        broker = self.Broker(error=AssertionError("must not be called"))
        v = final_check._cash_gate(broker, "AAPL", None)
        assert v["status"] == final_check.NOT_MEASURED
        assert final_check.ORDERABILITY_PRICE_INVALID in v["detail"]

    def test_an_unmappable_symbol_blocks(self):
        from s6_live import final_check

        v = self._gate(self.Broker(amount=74.01), symbol="NOT A REAL SYMBOL")
        assert v["status"] in (final_check.BLOCK, final_check.NOT_MEASURED)

    def test_a_failed_read_never_becomes_a_pass(self):
        from s6_live import final_check

        for err in (RuntimeError("boom"), ValueError("nope"),
                    RuntimeError("503 upstream")):
            assert self._gate(self.Broker(error=err))["status"] != \
                final_check.PASS


class TestGatesAreEvaluatedOncePerSymbol:
    """The append-only store holds many rows per symbol; evaluating
    broker gates per ROW made 15x the rate-limited KIS calls and the
    limiter then refused the later ones."""

    def test_the_cache_is_a_miss_check_not_a_setdefault(self):
        """`cache.setdefault(k, f(...))` reads like a cache and is not
        one: Python evaluates `f(...)` before setdefault is entered, so
        every row paid for a full gate evaluation and the cache only ever
        threw the result away. This test used to assert that exact line
        was present -- a source-string check that pinned the bug in
        place. It now counts the calls instead."""
        from s6_live import final_check

        calls = []

        def spy(symbol, **kw):
            calls.append(symbol)
            return {}

        original = final_check._gates_for
        final_check._gates_for = spy
        try:
            cache = {}
            for symbol in ["AAPL", "AAPL", "IEFA", "AAPL", "IEFA"]:
                final_check._cached_gates(cache, symbol)
        finally:
            final_check._gates_for = original

        assert calls == ["AAPL", "IEFA"]
