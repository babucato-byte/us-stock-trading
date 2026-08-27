"""The fill message has to answer "why", not just "what".

An operator reading that DT filled at 52.75 could not see that the
candidate behind it described a market three hours old, that the session
had no volume to judge, or that the price was 4% above the range the
strategy claimed to be trading. Every one of those facts existed
somewhere; none of them were in the message a person actually reads at
the moment the money moves.

Formatting only -- nothing here sends anything or decides anything.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from operations import live_notifications as ln  # noqa: E402
from s6_live import fill_notice, precision_watch as pw  # noqa: E402
from s6_live import realtime_features as rf  # noqa: E402


class TestBothTimestampsAlwaysAppear:
    def test_the_gap_that_allowed_the_DT_buy_is_visible(self):
        fields = fill_notice.build(
            symbol="DT", quantity=1, fill_price=52.75,
            candidate_generated_at="2026-08-26T20:38:49+00:00",
            market_data_asof="2026-08-26T19:55:00+00:00")
        assert fields["candidate_generated_at"] == "2026-08-26T20:38:49+00:00"
        assert fields["market_data_asof"] == "2026-08-26T19:55:00+00:00"

    def test_both_lines_are_present_even_when_unknown(self):
        """A missing line reads as 'fine'; 'unknown' reads as 'nobody
        checked', which is the true statement."""
        fields = fill_notice.build(symbol="DT", quantity=1, fill_price=52.75)
        assert fields["candidate_generated_at"] == fill_notice.UNKNOWN
        assert fields["market_data_asof"] == fill_notice.UNKNOWN

    def test_neither_can_be_dropped_by_the_formatter(self):
        message = ln._format(ln.FILL_COMPLETED, fill_notice.build(
            symbol="DT", quantity=1, fill_price=52.75,
            candidate_generated_at="A", market_data_asof="B"))
        assert "candidate_generated_at: A" in message
        assert "market_data_asof: B" in message


class TestEachGateIsItsOwnLine:
    def test_an_unavailable_gate_cannot_hide_in_a_summary(self):
        fields = fill_notice.build(
            symbol="DT", quantity=1, fill_price=52.75,
            conditions={"MARKET_DATA_FRESH": "FAIL",
                        "VOLUME_DATA_VALID": "UNAVAILABLE",
                        "EMA9_ABOVE_EMA21": "PASS"})
        assert fields["gate_market_data_fresh"] == "FAIL"
        assert fields["gate_volume_data_valid"] == "UNAVAILABLE"
        assert fields["gate_ema9_above_ema21"] == "PASS"

    def test_the_message_shows_them_individually(self):
        message = ln._format(ln.FILL_COMPLETED, fill_notice.build(
            symbol="DT", quantity=1, fill_price=52.75,
            conditions={"VOLUME_DATA_VALID": "UNAVAILABLE"}))
        assert "gate_volume_data_valid: UNAVAILABLE" in message


class TestItReportsTheDecisionThatWasMade:
    def test_from_watch_takes_the_evaluations_own_values(self):
        from datetime import datetime, timezone

        feats = rf.SessionFeatures(
            symbol="DT", session="AFTER_HOURS",
            market_data_asof=datetime(2026, 8, 26, 19, 55, tzinfo=timezone.utc))
        evaluation = pw.WatchEvaluation(
            symbol="DT", session="AFTER_HOURS", state=pw.READY_TO_BUY,
            conditions={n: pw.PASS for n in pw.CONDITION_ORDER},
            features=feats)
        fields = fill_notice.from_watch(
            evaluation, symbol="DT", quantity=1, fill_price=52.75,
            candidate={"rank": 4, "score": 72.82,
                       "generated_at": "2026-08-26T20:38:49+00:00"},
            broker_order_id="0030809002")
        assert fields["entry_state"] == pw.READY_TO_BUY
        assert fields["market_data_asof"] == "2026-08-26T19:55:00+00:00"
        assert fields["candidate_rank"] == "4"
        assert fields["kis_order"] == "0030809002"
        assert fields["gate_market_data_fresh"] == pw.PASS


class TestARealOrderIsNeverFiledAsATest:
    def test_live_carries_the_kis_live_prefix(self):
        headline = ln._format(ln.FILL_COMPLETED, {"symbol": "DT"}).splitlines()[0]
        assert headline.startswith(ln.KIS_LIVE_PREFIX)
        assert "[TEST]" not in headline
        assert "[VALIDATION]" not in headline

    def test_a_validation_order_is_not_labelled_TEST(self):
        """Validation is REAL money proving a route works. An operator
        who reads TEST and looks away has been told the wrong thing."""
        headline = ln._format(ln.FILL_COMPLETED, {"symbol": "DT"},
                              validation=True).splitlines()[0]
        assert "[VALIDATION]" in headline
        assert "[TEST]" not in headline

    def test_a_test_order_is_still_labelled_TEST(self):
        headline = ln._format(ln.FILL_COMPLETED, {"symbol": "DT"},
                              test=True).splitlines()[0]
        assert "[TEST]" in headline
        assert "[VALIDATION]" not in headline

    def test_notify_accepts_the_validation_flag(self):
        sent = []
        assert ln.notify(ln.FILL_COMPLETED, {"symbol": "DT"}, validation=True,
                         send_fn=lambda m: sent.append(m) or True,
                         track_health=False) is True
        assert "[VALIDATION]" in sent[0]
