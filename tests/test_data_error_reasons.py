"""Why a DATA_ERROR happened, not just how many.

2026-09-02: six consecutive S6 scans reported DATA_ERROR on 592 of 593
symbols with `rejected=0` -- every symbol failed before the strategy was
consulted. The aggregate could not say whether that was thin overnight
liquidity behaving exactly as it always does, or a fetch path that had
silently stopped working. Both had happened in production days apart, and
they call for opposite responses.

Classification is OBSERVATIONAL. Nothing branches on it, no symbol's fate
changes, and the DATA_ERROR total is untouched.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners.base import reject_reasons as rr  # noqa: E402


class TestEachCategoryIsRecognised:
    @pytest.mark.parametrize("message,expected", [
        ("MTCH: 1 bars since the 15m opening range, need 3",
         rr.INSUFFICIENT_POST_RANGE_BARS),
        ("AAPL: opening range (15m) not computable",
         rr.INSUFFICIENT_POST_RANGE_BARS),
        ("X: no regular-session bars today", rr.INSUFFICIENT_POST_RANGE_BARS),
        ("AAPL: session VWAP not computable", rr.MISSING_VWAP),
        ("NVDA: session EMA9/EMA21 not computable", rr.MISSING_EMA),
        ("TSLA: insufficient_or_stale_data", rr.STALE_DATA),
        ("F: no usable current price", rr.NO_EXECUTABLE_PRICE),
        ("GM: session bars have no usable closes", rr.NO_EXECUTABLE_PRICE),
        ("X: KIS returned no minute bars", rr.KIS_FETCH_ERROR),
        ("Y: minute chart unavailable", rr.KIS_FETCH_ERROR),
        ("Z: rate limit exceeded", rr.RATE_LIMIT),
        ("W: EGW00201", rr.RATE_LIMIT),
    ])
    def test_message_maps_to_category(self, message, expected):
        assert rr.classify_data_error(message) == expected

    def test_an_unknown_message_is_other_not_a_guess(self):
        assert rr.classify_data_error("something nobody has seen") == rr.OTHER
        assert rr.classify_data_error("") == rr.OTHER
        assert rr.classify_data_error(None) == rr.OTHER

    def test_every_category_is_declared(self):
        assert len(rr.DATA_ERROR_CATEGORIES) == 8
        assert len(set(rr.DATA_ERROR_CATEGORIES)) == 8

    def test_exactly_one_category_per_message(self):
        """First match wins, so counts sum to the DATA_ERROR total."""
        for message in ("KIS minute chart unavailable and VWAP not computable",
                        "stale data with no usable current price"):
            assert rr.classify_data_error(message) in rr.DATA_ERROR_CATEGORIES

    def test_it_never_raises(self):
        for junk in (None, "", 0, 3.14, object(), b"bytes"):
            assert rr.classify_data_error(junk) in rr.DATA_ERROR_CATEGORIES


class TestAcquisitionVersusInsufficient:
    """The distinction the aggregate could not make."""

    def test_fetch_failures_are_acquisition(self):
        assert rr.is_acquisition_failure(rr.KIS_FETCH_ERROR)
        assert rr.is_acquisition_failure(rr.RATE_LIMIT)

    def test_thin_data_is_not_acquisition(self):
        for reason in (rr.INSUFFICIENT_POST_RANGE_BARS, rr.MISSING_VWAP,
                       rr.MISSING_EMA, rr.STALE_DATA,
                       rr.NO_EXECUTABLE_PRICE, rr.OTHER):
            assert not rr.is_acquisition_failure(reason)

    def test_a_fetch_failure_is_not_read_as_thin_liquidity(self):
        """The ordering that matters: a failed fetch produces no bars, and
        'no bars' would otherwise classify as insufficient data."""
        assert rr.classify_data_error(
            "KIS returned no minute bars") == rr.KIS_FETCH_ERROR


class TestTheCountsReconcile:
    def test_reasons_sum_to_the_data_error_total(self):
        from scanners.base.scanner_base import ScanOutcome
        from scanners.base.scanner_base import count_reject_reason

        outcome = ScanOutcome(scanner_name="orb", scanner_version="v1",
                              config_fingerprint="x", trading_day="2026-09-02")
        messages = [
            "1 bars since the 15m opening range, need 3",
            "2 bars since the 15m opening range, need 3",
            "session VWAP not computable",
            "KIS returned no minute bars",
            "something new",
        ]
        for message in messages:
            outcome.data_errors += 1
            count_reject_reason(outcome.data_error_reasons,
                                rr.classify_data_error(message))

        assert sum(outcome.data_error_reasons.values()) == outcome.data_errors
        assert outcome.data_error_reasons[rr.INSUFFICIENT_POST_RANGE_BARS] == 2
        assert outcome.data_error_reasons[rr.OTHER] == 1

    def test_a_clean_scan_records_no_reasons(self):
        from scanners.base.scanner_base import ScanOutcome

        outcome = ScanOutcome(scanner_name="orb", scanner_version="v1",
                              config_fingerprint="x", trading_day="2026-09-02")
        assert outcome.data_error_reasons == {}
        assert sum(outcome.data_error_reasons.values()) == outcome.data_errors


class TestItChangesNoDecision:
    def test_classification_is_never_used_in_a_branch(self):
        """A diagnostic that steers behaviour is not a diagnostic."""
        base = (REPO_ROOT / "scanners" / "base" / "scanner_base.py").read_text()
        for forbidden in ("if reject_reasons.classify_data_error",
                          "== reject_reasons.KIS_FETCH_ERROR",
                          "if classify_data_error"):
            assert forbidden not in base

    def test_the_data_error_total_is_still_counted_the_same_way(self):
        base = (REPO_ROOT / "scanners" / "base" / "scanner_base.py").read_text()
        assert "outcome.data_errors += 1" in base
        assert "count_reject_reason(outcome.reject_reasons, reject_reasons.DATA_ERROR)" in base

    def test_a_strategy_rejection_is_not_a_data_error(self):
        """`rejected` and `data_errors` stay separate counters."""
        base = (REPO_ROOT / "scanners" / "base" / "scanner_base.py").read_text()
        assert "outcome.rejected += 1" in base

    def test_the_summary_is_aggregate_not_per_symbol(self):
        runner = (REPO_ROOT / "scanners" / "runner.py").read_text()
        assert "DATA_ERROR_SUMMARY" in runner
        block = runner[runner.index("def _log_data_error_summary"):]
        assert "for outcome in outcomes" in block
        assert "for symbol" not in block
