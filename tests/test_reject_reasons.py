"""A scan that rejects 202 symbols and cannot say why is not evidence.

Production ran all day reporting `symbols_seen: 202, rejected: 202,
top_reject_reasons: []`. The tally existed; nothing fed it. `evaluate()`
caught `Rejected`, logged the sentence and returned None, so
`evaluate_into` could only count that SOMETHING had refused the symbol.
Month-one calibration -- "is volume_expansion_min=1.2 refusing 5% of
names or 60%?" -- had no input.

These tests pin both halves: the sentence is classified into a stable
gate code, and the code reaches the summary.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners.base import reject_reasons as rr  # noqa: E402


class TestTheRealSentencesTheScannersEmit:
    """Every string below was produced by the deployed ORB scanner on
    2026-08-24 against real bars. If the wording changes, these fail --
    which is the point: a pattern table that silently stops matching
    reports every rejection as UNCLASSIFIED rather than lying."""

    @pytest.mark.parametrize("message,code,observed,threshold", [
        # BMNR and MARA, the two names this whole investigation started from.
        ("volume expansion 0.31x below 1.20x", rr.VOLUME_EXPANSION, 0.31, 1.2),
        ("volume expansion 0.57x below 1.20x", rr.VOLUME_EXPANSION, 0.57, 1.2),
        # NCTY and XPON: never closed above the range high.
        ("no bar has CLOSED above the opening range high 7.75",
         rr.CLOSE_BREAKOUT, None, 7.75),
        ("no bar has CLOSED above the opening range high 8.90 (wick only)",
         rr.CLOSE_BREAKOUT, None, 8.90),
        # SDOT: it DID close above, then faded. A different finding.
        ("broke the opening range high 25.90 but has fallen back inside "
         "the range (now 24.68)", rr.CURRENT_ABOVE_RANGE, 24.68, None),
        ("already 7.40% above the opening range high, past the 6.00% limit",
         rr.EXTENSION, 7.40, 6.00),
        ("price 6.78 at/below VWAP 6.85", rr.VWAP, 6.78, 6.85),
        ("EMA9 25.35 at/below EMA21 26.02", rr.EMA_STRUCTURE, 25.35, 26.02),
        ("2 bars since the 15m opening range, need 3",
         rr.POST_RANGE_BARS, 2, 3),
    ])
    def test_it_reads_the_gate_and_the_numbers(self, message, code, observed,
                                               threshold):
        got_code, got_obs, got_thr = rr.classify(message)
        assert got_code == code
        assert got_obs == observed
        assert got_thr == threshold

    def test_a_faded_breakout_is_not_a_failed_one(self):
        """SDOT closed above 25.90 and fell back; NCTY never closed above
        7.75. Bucketing both as CLOSE_BREAKOUT would merge a setup that
        triggered with one that never did."""
        faded, _, _ = rr.classify(
            "broke the opening range high 25.90 but has fallen back inside "
            "the range (now 24.68)")
        never, _, _ = rr.classify(
            "no bar has CLOSED above the opening range high 7.75")
        assert faded != never


class TestAnUnknownSentenceSaysSo:
    @pytest.mark.parametrize("message", [
        "something nobody has written yet", "", None, 42,
    ])
    def test_it_is_unclassified_not_nearest_match(self, message):
        code, observed, threshold = rr.classify(message)
        assert code == rr.UNCLASSIFIED
        assert observed is None and threshold is None

    def test_classify_never_raises(self):
        for bad in (object(), b"bytes", [], {"a": 1}):
            assert rr.classify(bad)[0] == rr.UNCLASSIFIED


class TestTheReasonReachesTheSummary:
    """The wiring, not the table. A rejection must arrive in
    `top_reject_reasons` and as one `first_rejects` row."""

    def _outcome(self):
        from scanners.base.scanner_base import ScanOutcome

        return ScanOutcome(scanner_name="orb", scanner_version="orb_v1.0",
                           config_fingerprint="test", trading_day="2026-08-24")

    def test_one_rejection_is_counted_and_recorded(self):
        outcome = self._outcome()
        outcome.note_first_reject("BMNR", rr.VOLUME_EXPANSION,
                                  observed=0.309, threshold=1.2)
        assert outcome.first_rejects == [
            {"symbol": "BMNR", "reason": rr.VOLUME_EXPANSION,
             "observed": 0.309, "threshold": 1.2}]

    def test_the_detail_is_kept_only_when_unclassified(self):
        """A recognised code plus two numbers says everything the
        sentence did. 202 sentences every 15 minutes is how a useful
        record becomes a log nobody opens."""
        outcome = self._outcome()
        outcome.note_first_reject("AAA", rr.VOLUME_EXPANSION, observed=0.3,
                                  threshold=1.2, detail="volume expansion ...")
        outcome.note_first_reject("BBB", rr.UNCLASSIFIED,
                                  detail="a sentence nobody planned for")
        assert "detail" not in outcome.first_rejects[0]
        assert outcome.first_rejects[1]["detail"] == "a sentence nobody planned for"

    def test_the_summary_reports_the_tally(self):
        from scanners.base.scanner_base import count_reject_reason

        outcome = self._outcome()
        for _ in range(113):
            count_reject_reason(outcome.reject_reasons, rr.VOLUME_EXPANSION)
        for _ in range(54):
            count_reject_reason(outcome.reject_reasons, rr.CLOSE_BREAKOUT)
        count_reject_reason(outcome.reject_reasons, rr.VWAP)

        top = dict(outcome.summary()["top_reject_reasons"])
        assert top[rr.VOLUME_EXPANSION] == 113
        assert top[rr.CLOSE_BREAKOUT] == 54
        assert top[rr.VWAP] == 1
        # Ordered most-common first, which is what makes it readable.
        assert outcome.summary()["top_reject_reasons"][0][0] == rr.VOLUME_EXPANSION

    def test_a_rejected_symbol_produces_a_reason_end_to_end(self):
        """The regression that mattered: `evaluate_into` used to tally
        nothing on the gate path, so this list stayed empty."""
        from scanners.base.scanner_base import BaseScanner, Rejected, require

        class _Refuses(BaseScanner):
            scanner_name = "fake"
            scanner_version = "v1"
            config_schema = {}

            def __init__(self):
                from scanners.base.config import ScannerConfig
                self.config = ScannerConfig(scanner_name="fake",
                                            version="v1", params={})

            @property
            def config_fingerprint(self):
                return "test"

            def build_features(self, data, shared=None):
                return None

            def check(self, features, data, context):
                require(False, "volume expansion 0.31x below 1.20x")

            def score(self, features, data, context):
                return 0.0

        class _Data:
            symbol = "BMNR"

        outcome = self._outcome()
        scanner = _Refuses()
        scanner.log = __import__("logging").getLogger("test")
        result = scanner.evaluate_into(outcome, _Data(),
                                       trading_day="2026-08-24")

        assert result is None
        assert outcome.reject_reasons.get(rr.VOLUME_EXPANSION) == 1
        assert outcome.first_rejects[0]["symbol"] == "BMNR"
        assert outcome.first_rejects[0]["observed"] == 0.31

    def test_a_data_error_is_its_own_bucket_not_a_gate_failure(self):
        """"We could not judge this symbol" and "we judged it and it
        failed" must never share a count."""
        from scanners.base.scanner_base import BaseScanner
        from scanners.base.models import ScannerDataError

        class _NoBars(BaseScanner):
            scanner_name = "fake"
            scanner_version = "v1"
            config_schema = {}

            def __init__(self):
                from scanners.base.config import ScannerConfig
                self.config = ScannerConfig(scanner_name="fake",
                                            version="v1", params={})

            @property
            def config_fingerprint(self):
                return "test"

            def build_features(self, data, shared=None):
                raise ScannerDataError("XYZ: no regular-session bars today")

            def check(self, features, data, context):
                return []

            def score(self, features, data, context):
                return 0.0

        class _Data:
            symbol = "XYZ"

        outcome = self._outcome()
        scanner = _NoBars()
        scanner.log = __import__("logging").getLogger("test")
        scanner.evaluate_into(outcome, _Data(), trading_day="2026-08-24")

        assert outcome.data_errors == 1
        assert outcome.reject_reasons.get(rr.DATA_ERROR) == 1
        assert outcome.reject_reasons.get(rr.VOLUME_EXPANSION) is None
