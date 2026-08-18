"""S1's execution price is checked against today's range, not yesterday's close.

The bug this fixes: on 2026-08-18 all nine ranked S1 candidates were
refused by the 0.30% previous-close deviation gate -- the tightest of them
by 0.46%. That gate is correct for a scalping signal seconds old and
structurally wrong for one whose signal price IS the previous close, where
an overnight gap is the strategy rather than an anomaly.

The legacy path keeps the 0.30% check unchanged, and a test here asserts
that it does.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s1_live import execution_price as ep  # noqa: E402

PREV_CLOSE = 100.0


def detail(**overrides):
    """A well-formed price-detail, shaped like the observed live response."""
    base = {
        "symbol": "AMBA", "last": 108.0, "high": 109.0, "low": 96.0,
        "open": 97.0, "prev_close": PREV_CLOSE, "currency": "USD",
        "tick_size": 0.01, "orderable_text": "매매 가능",
        "today_volume": 361265.0, "kis_exchange_code": "NAS",
        "fetched_at": "2026-08-18T16:15:00+00:00",
    }
    base.update(overrides)
    return base


class TestTheGapItselfIsNotAnAnomaly:
    def test_eight_percent_above_the_previous_close_passes(self):
        """+8% overnight, but inside today's range."""
        verdict = ep.evaluate("AMBA", detail(last=108.0, low=96.0, high=109.0))
        assert verdict.passed
        assert verdict.reason_code == ep.REASON_OK
        gap = (108.0 - PREV_CLOSE) / PREV_CLOSE * 100
        assert gap > 8.0 - 0.001, "the fixture is supposed to be a real gap"

    def test_five_percent_below_the_previous_close_passes(self):
        verdict = ep.evaluate("AMBA", detail(last=95.0, low=94.0, high=101.0))
        assert verdict.passed

    def test_the_previous_close_is_recorded_but_not_used_as_a_bound(self):
        """It stays in provenance for gap analysis; it is not a gate."""
        verdict = ep.evaluate("AMBA", detail(last=108.0))
        assert verdict.prev_close == PREV_CLOSE
        assert verdict.passed

    def test_a_price_far_from_yesterday_but_inside_today_passes(self):
        verdict = ep.evaluate("X", detail(last=150.0, low=140.0, high=160.0,
                                          prev_close=100.0))
        assert verdict.passed, "a 50% gap inside today's range is still tradeable"


class TestOutsideTodaysRange:
    def test_above_the_day_high_blocks(self):
        verdict = ep.evaluate("AMBA", detail(last=109.5, high=109.0, low=96.0))
        assert not verdict.passed
        assert verdict.reason_code == ep.REASON_ABOVE_HIGH

    def test_below_the_day_low_blocks(self):
        verdict = ep.evaluate("AMBA", detail(last=95.9, high=109.0, low=96.0))
        assert not verdict.passed
        assert verdict.reason_code == ep.REASON_BELOW_LOW

    def test_exactly_at_the_bounds_passes(self):
        for price in (96.0, 109.0):
            assert ep.evaluate("AMBA", detail(last=price, low=96.0, high=109.0)).passed


class TestMalformedOrStaleData:
    @pytest.mark.parametrize("field", ["high", "low"])
    def test_a_missing_bound_blocks(self, field):
        verdict = ep.evaluate("AMBA", detail(**{field: None}))
        assert not verdict.passed
        assert verdict.reason_code == ep.REASON_RANGE_MISSING

    def test_an_inverted_range_blocks(self):
        verdict = ep.evaluate("AMBA", detail(high=90.0, low=100.0, last=95.0))
        assert not verdict.passed
        assert verdict.reason_code == ep.REASON_RANGE_DEGENERATE

    def test_a_zero_low_blocks(self):
        """The empty-string field KIS sends for a wrong exchange parses to
        None; a zero would make the band meaningless."""
        verdict = ep.evaluate("AMBA", detail(low=0.0, high=109.0, last=108.0))
        assert not verdict.passed
        assert verdict.reason_code == ep.REASON_RANGE_DEGENERATE

    @pytest.mark.parametrize("price", [None, 0.0, -1.0])
    def test_an_unusable_price_blocks(self, price):
        verdict = ep.evaluate("AMBA", detail(last=price))
        assert not verdict.passed
        assert verdict.reason_code == ep.REASON_PRICE_MISSING

    @pytest.mark.parametrize("volume", [None, 0.0])
    def test_no_trades_today_blocks(self, volume):
        """Without prints today, the range describes another session."""
        verdict = ep.evaluate("AMBA", detail(today_volume=volume))
        assert not verdict.passed
        assert verdict.reason_code == ep.REASON_NO_VOLUME

    def test_a_missing_response_blocks(self):
        for bad in (None, "", [], 42):
            verdict = ep.evaluate("AMBA", bad)
            assert not verdict.passed
            assert verdict.reason_code == ep.REASON_NO_DETAIL

    def test_a_non_usd_currency_blocks(self):
        verdict = ep.evaluate("AMBA", detail(currency="KRW"))
        assert not verdict.passed
        assert verdict.reason_code == ep.REASON_CURRENCY

    def test_an_empty_currency_blocks(self):
        assert ep.evaluate("AMBA", detail(currency="")).reason_code == ep.REASON_CURRENCY

    def test_a_broker_that_reports_not_tradeable_blocks(self):
        for text in ("", "매매 불가", "정지", "SOMETHING NEW"):
            verdict = ep.evaluate("AMBA", detail(orderable_text=text))
            assert not verdict.passed
            assert verdict.reason_code == ep.REASON_NOT_ORDERABLE

    def test_a_read_failure_blocks_rather_than_passing(self):
        class Broken:
            def get_price_detail(self, instrument):
                raise RuntimeError("KIS unreachable")

        class Instrument:
            kis_symbol, exchange = "AMBA", "NASDAQ"

        verdict = ep.evaluate_symbol("AMBA", broker=Broken(), instrument=Instrument())
        assert not verdict.passed
        assert verdict.reason_code == ep.REASON_NO_DETAIL


class TestNoInventedThreshold:
    def test_the_module_declares_no_percentage_tolerance(self):
        """The band is today's own high and low. A percentage here would be
        the invented constant this design exists to avoid."""
        import ast

        source = (REPO_ROOT / "s1_live" / "execution_price.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    name = getattr(target, "id", "")
                    assert "PERCENT" not in name.upper(), name
                    assert "TOLERANCE" not in name.upper(), name
                    assert "THRESHOLD" not in name.upper(), name

    def test_it_does_not_read_the_legacy_deviation_limit(self):
        source = (REPO_ROOT / "s1_live" / "execution_price.py").read_text()
        assert "max_price_deviation" not in source
        assert "MAX_PRICE_DEVIATION" not in source


class TestProvenanceStatesWhatWasNotDone:
    def test_the_cross_feed_status_is_recorded_as_unavailable(self):
        """A report must not be able to imply a cross-feed gate ran."""
        prov = ep.evaluate("AMBA", detail()).provenance
        assert prov["external_validation_status"] == "UNAVAILABLE_FOR_HARD_GATE"
        assert prov["alpaca_feed"] == "IEX"
        assert prov["sip_entitlement"] is False
        assert prov["source"] == "KIS_PRICE_DETAIL"

    def test_the_verdict_carries_the_range_it_judged_on(self):
        verdict = ep.evaluate("AMBA", detail())
        assert verdict.day_low == 96.0 and verdict.day_high == 109.0
        assert verdict.tick_size == 0.01
        assert verdict.today_volume == 361265.0


class TestTheGateUsesTheVerdict:
    def test_a_failed_verdict_blocks_the_buy_gate(self):
        from execution import order_gate

        source = (REPO_ROOT / "execution" / "order_gate.py").read_text()
        assert "execution_price_check" in source
        assert "EXECUTION_PRICE" in source
        assert hasattr(order_gate.BuyGateContext, "__dataclass_fields__")
        assert "execution_price_check" in order_gate.BuyGateContext.__dataclass_fields__

    def test_an_absent_verdict_falls_back_to_the_legacy_check(self):
        """Fail-closed: a forgotten verdict blocks, it does not open."""
        source = (REPO_ROOT / "execution" / "order_gate.py").read_text()
        branch = source[source.index("verdict = getattr(ctx"):]
        assert "if verdict is None:" in branch
        assert "max_price_deviation_percent" in branch.split("else:")[0]

    def test_a_verdict_for_another_symbol_is_rejected(self):
        source = (REPO_ROOT / "execution" / "order_gate.py").read_text()
        assert "is for" in source and "not" in source

    def test_the_legacy_default_is_still_thirty_basis_points(self):
        from config.live_rollout_config import LiveRolloutConfig

        assert LiveRolloutConfig.from_env({}).max_price_deviation_percent == 0.30

    def test_only_the_s1_source_gets_the_new_check(self):
        source = (REPO_ROOT / "kis_live_trading.py").read_text()
        assert "s1_candidate_source.SOURCE_S1" in source
        block = source[source.index("execution_price_verdict = None"):][:900]
        assert "SOURCE_S1" in block
