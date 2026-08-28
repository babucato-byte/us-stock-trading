"""What the Slack messages are allowed to imply about a session.

Wording is not cosmetic here. Premarket and after-hours carried no
volume line for months, and an operator reasonably read the silence as
absence -- which was true of the daily-bar provider and never true of
the market. The same shape of error is now available in the other
direction: calling the daytime session "scan only" would send someone
looking for a data problem that does not exist, when what it actually
lacks is one confirmed BUY response.

So each session says what is true of IT: where its data comes from, and
which specific thing is missing if anything is.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners.base import scan_session  # noqa: E402
from scanners.notify import labels, monitor  # noqa: E402


class TestTheDaytimeSessionIsNotCalledScanOnly:
    def test_it_reports_that_the_route_awaits_evidence(self, monkeypatch):
        monkeypatch.setattr(
            "config.session_capability.route_awaiting_live_evidence",
            lambda session: session == "OVERNIGHT_DAYTIME")
        assert scan_session.execution_status("OVERNIGHT_DAYTIME") == \
            scan_session.STATUS_ROUTE_AWAITING_EVIDENCE

    def test_that_is_a_different_status_from_scan_only(self):
        """SCAN_ONLY says "this cannot be traded", which a reader takes
        as a fact about the market. The daytime session has live data,
        live volume and valid features."""
        assert scan_session.STATUS_ROUTE_AWAITING_EVIDENCE != \
            scan_session.STATUS_SCAN_ONLY

    def test_a_route_with_evidence_stays_verified(self, monkeypatch):
        monkeypatch.setattr(
            "config.session_capability.route_awaiting_live_evidence",
            lambda session: False)
        assert scan_session.execution_status("REGULAR") == \
            scan_session.STATUS_ORDER_VERIFIED

    def test_an_unknown_session_is_still_scan_only(self):
        assert scan_session.execution_status("NOT_A_SESSION") == \
            scan_session.STATUS_SCAN_ONLY

    def test_losing_the_evidence_check_does_not_downgrade_a_session(self, monkeypatch):
        """Failing to REFERENCE_VERIFIED understates what is usable;
        failing to SCAN_ONLY would understate it much further."""
        monkeypatch.setattr(
            "config.session_capability.route_awaiting_live_evidence",
            lambda session: (_ for _ in ()).throw(RuntimeError("boom")))
        assert scan_session.execution_status("REGULAR") == \
            scan_session.STATUS_ORDER_VERIFIED

    def test_the_label_does_not_say_scan_only_in_korean_either(self):
        rendered = labels.status(scan_session.STATUS_ROUTE_AWAITING_EVIDENCE)
        assert "스캔 전용" not in rendered
        assert "데이터" in rendered


class TestExtendedSessionsNameTheirDataSource:
    def test_premarket_reports_the_stream_and_available_volume(self):
        lines = monitor._data_source_lines("PREMARKET")
        joined = " ".join(lines)
        assert "KIS_HDFSCNT0" in joined
        assert "AVAILABLE" in joined

    def test_after_hours_and_daytime_do_too(self):
        for session in ("AFTER_HOURS", "OVERNIGHT_DAYTIME"):
            assert monitor._data_source_lines(session), session

    def test_regular_keeps_its_existing_source_and_says_nothing_new(self):
        """REGULAR was never routed to the stream, so claiming it here
        would be a false provenance."""
        assert monitor._data_source_lines("REGULAR") == []

    def test_a_non_session_label_gets_no_line(self):
        assert monitor._data_source_lines("RUN") == []

    def test_the_line_appears_in_a_formatted_scan(self):
        message = monitor.format_scan(
            scanner_name="orb", session="PREMARKET", trading_day="2026-08-28",
            scanned=500, candidates=3, status="SUCCESS", live_candidates=2)
        assert "KIS_HDFSCNT0" in message

    def test_a_broken_provenance_lookup_does_not_lose_the_counts(self, monkeypatch):
        monkeypatch.setattr(
            "scanners.base.scan_session.normalize",
            lambda v: (_ for _ in ()).throw(RuntimeError("boom")))
        message = monitor.format_scan(
            scanner_name="orb", session="PREMARKET", trading_day="2026-08-28",
            scanned=500, candidates=3, status="SUCCESS")
        assert "500" in message


class TestTheZeroTradeableLineNamesTheFilter:
    def test_it_no_longer_calls_a_live_scan_research(self):
        """"연구용만" read as a statement about the scan's purpose. The
        scan is live; it was the instrument type that disqualified every
        candidate."""
        message = monitor.format_scan(
            scanner_name="orb", session="REGULAR", trading_day="2026-08-28",
            scanned=500, candidates=4, status="SUCCESS", live_candidates=0)
        assert "연구용만" not in message
        assert "COMMON_STOCK" in message
        assert "실거래 대상 없음" in message

    def test_the_old_phrase_survives_only_as_an_explanation(self):
        """It still appears in the comment that records why it went --
        that is the point of the comment. What must not exist is a line
        that emits it."""
        source = (REPO_ROOT / "scanners" / "notify" / "monitor.py").read_text(
            encoding="utf-8")
        emitting = [line for line in source.splitlines()
                    if "연구용만" in line and not line.strip().startswith("#")]
        assert emitting == []
