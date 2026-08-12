"""The report CLI: default ranges, validation, and exit codes.

Reports are run by cron, where nobody reads stdout unless something
already went wrong. That makes two properties load-bearing:

* the bare command has to work, so an operator does not have to compute
  dates in a crontab line, and
* a misuse has to FAIL rather than render an empty-but-plausible report.
  An empty intersection table and a genuinely quiet month look identical
  on screen; only the exit code tells them apart.

The date-normalisation tests are the ones worth reading twice. The store
compares trading-day keys as strings, so an unpadded date does not
merely look untidy -- it sorts into the wrong place and silently
excludes the days it was meant to include.
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_scanner_report as cli  # noqa: E402
from scanners.analytics import date_range  # noqa: E402
from scanners.analytics import intersection_analysis  # noqa: E402
from scanners.base import result_store  # noqa: E402
from scanners.base.models import ScannerSignal  # noqa: E402


@pytest.fixture
def captured(monkeypatch):
    """Record the ranges each report module is actually asked for."""
    seen = {}

    def record(name):
        def fake(start, end, **kwargs):
            seen.setdefault(name, []).append((start, end))
            return {
                "report": name, "start_day": start, "end_day": end,
                "generated_at": "now", "hit_horizon": kwargs.get("hit_horizon"),
                "trading_days": [], "total_signals": 0, "scanners": [],
                "experiment_splits": [], "scope": kwargs.get("scope", "day"),
                "symbol_day_count": 0, "combinations": [],
                "by_confirmation_count": [], "note": "",
            }
        return fake

    monkeypatch.setattr(cli.weekly_report, "build", record("weekly"))
    monkeypatch.setattr(cli.weekly_report, "format_report", lambda r: "")
    monkeypatch.setattr(cli.monthly_report, "build", record("monthly"))
    monkeypatch.setattr(cli.monthly_report, "format_report", lambda r: "")
    monkeypatch.setattr(cli.intersection_analysis, "analyse_range", record("intersections"))
    monkeypatch.setattr(cli.intersection_analysis, "format_report", lambda r: "")
    return seen


class TestIntersectionDefaultRange:
    def test_no_arguments_succeeds(self, captured):
        """Section: the bare command must be usable from cron."""
        assert cli.main(["intersections"]) == 0
        assert captured["intersections"]

    def test_no_arguments_covers_the_last_30_days(self, captured):
        cli.main(["intersections"])
        start, end = captured["intersections"][0]
        assert end == date_range.today().isoformat()
        span = date.fromisoformat(end) - date.fromisoformat(start)
        assert span == timedelta(days=date_range.DEFAULT_WINDOW_DAYS - 1)

    def test_the_window_is_inclusive_of_both_ends(self):
        """30 days means 30 days, not 31."""
        start, end = date_range.recent_bounds(30, end="2026-08-30")
        assert start == "2026-08-01"
        assert end == "2026-08-30"
        assert (date.fromisoformat(end) - date.fromisoformat(start)).days == 29

    def test_the_window_is_configurable(self, captured):
        cli.main(["intersections", "--days", "7"])
        start, end = captured["intersections"][0]
        assert (date.fromisoformat(end) - date.fromisoformat(start)).days == 6

    def test_both_scopes_use_the_same_resolved_range(self, captured):
        """The default must be computed once, not per scope -- otherwise
        a run spanning midnight would report two different windows."""
        cli.main(["intersections"])
        assert len(captured["intersections"]) == 2
        assert captured["intersections"][0] == captured["intersections"][1]


class TestExplicitArgumentsWin:
    def test_both_endpoints_are_used_as_given(self, captured):
        assert cli.main(
            ["intersections", "--start", "2026-08-01", "--end", "2026-08-31"]) == 0
        assert captured["intersections"][0] == ("2026-08-01", "2026-08-31")

    def test_start_only_runs_from_start_until_today(self, captured):
        cli.main(["intersections", "--start", "2026-01-05"])
        start, end = captured["intersections"][0]
        assert start == "2026-01-05"
        assert end == date_range.today().isoformat()

    def test_end_only_runs_the_window_ending_there(self, captured):
        cli.main(["intersections", "--end", "2026-08-31"])
        start, end = captured["intersections"][0]
        assert end == "2026-08-31"
        assert start == "2026-08-02"  # 30 days inclusive

    def test_explicit_range_is_unaffected_by_days(self, captured):
        cli.main(["intersections", "--start", "2026-08-01",
                  "--end", "2026-08-31", "--days", "3"])
        assert captured["intersections"][0] == ("2026-08-01", "2026-08-31")


class TestInvalidInput:
    @pytest.mark.parametrize("value", ["not-a-date", "2026-13-45", "", "08-2026", "20260801"])
    def test_a_malformed_start_exits_with_the_usage_code(self, value, capsys):
        assert cli.main(["intersections", "--start", value]) == cli.USAGE_ERROR
        assert "--start" in capsys.readouterr().out

    @pytest.mark.parametrize("value", ["not-a-date", "2026-02-30"])
    def test_a_malformed_end_exits_with_the_usage_code(self, value, capsys):
        assert cli.main(["intersections", "--end", value]) == cli.USAGE_ERROR
        assert "--end" in capsys.readouterr().out

    def test_a_backwards_range_is_refused(self, capsys):
        """An empty result from a reversed range is indistinguishable
        from a quiet month; refusing says which one it is."""
        assert cli.main(
            ["intersections", "--start", "2026-08-31", "--end", "2026-08-01"]
        ) == cli.USAGE_ERROR
        assert "after" in capsys.readouterr().out

    def test_a_zero_or_negative_window_is_refused(self, capsys):
        assert cli.main(["intersections", "--days", "0"]) == cli.USAGE_ERROR
        assert "at least 1 day" in capsys.readouterr().out

    def test_usage_errors_are_distinct_from_operational_failure(self):
        """2 vs 1: a typo in a crontab line must be tellable from a run
        that genuinely could not produce a report."""
        assert cli.USAGE_ERROR == 2
        assert cli.main(["export"]) == cli.USAGE_ERROR

    def test_a_malformed_month_is_refused(self, capsys):
        assert cli.main(["monthly", "--month", "2026-99"]) == cli.USAGE_ERROR
        assert "--month" in capsys.readouterr().out

    def test_a_malformed_week_of_is_refused(self, capsys):
        assert cli.main(["weekly", "--week-of", "nope"]) == cli.USAGE_ERROR


class TestDateNormalisation:
    """The store compares day keys as strings, so padding is semantic."""

    def test_lexical_comparison_is_why_padding_matters(self):
        """Pins the underlying hazard, so the reason for normalising
        cannot be refactored away as cosmetic."""
        assert "2026-8-1" > "2026-08-05"
        assert not ("2026-08-01" > "2026-08-05")

    def test_an_unpadded_endpoint_is_normalised_before_it_reaches_a_report(
            self, captured):
        cli.main(["weekly", "--start", "2026-8-1", "--end", "2026-8-31",
                  "--no-write"])
        assert captured["weekly"][0] == ("2026-08-01", "2026-08-31")

    def test_normalisation_applies_to_intersections_too(self, captured):
        cli.main(["intersections", "--start", "2026-8-1", "--end", "2026-8-31"])
        assert captured["intersections"][0] == ("2026-08-01", "2026-08-31")

    def test_an_unpadded_range_actually_selects_the_right_days(self):
        """End to end against the real store, not a recorded call."""
        signal = ScannerSignal(
            timestamp="2026-08-05T14:00:00+00:00", trading_day="2026-08-05",
            symbol="TEST", scanner_name="orb", scanner_version="orb_v1.0",
            scanner_score=80.0, signal_price=100.0)
        result_store.write_signals([signal], trading_day="2026-08-05")

        start, end = date_range.resolve_range("2026-8-1", "2026-8-31")
        rows = result_store.joined_rows(start, end)
        assert len(rows) == 1, "the 05 Aug signal fell outside an unpadded range"


class TestSharedTradingDayAnchor:
    """All three reports anchor on the same "today"."""

    def test_today_is_the_eastern_trading_day_not_the_local_date(self, monkeypatch):
        """On the UTC server the local date rolls over around 19:00-20:00
        ET. A report anchored on `date.today()` run by a Sunday-evening
        cron would resolve to the following week and render empty, with
        nothing in the output to say why."""
        from scanners.analytics import date_range as dr

        monkeypatch.setattr(dr, "us_trading_day", lambda now=None: "2026-08-09")
        assert dr.today() == date(2026, 8, 9)

    def test_weekly_uses_the_shared_anchor(self, captured, monkeypatch):
        monkeypatch.setattr(cli.date_range, "today", lambda now=None: date(2026, 8, 12))
        cli.main(["weekly", "--no-write"])
        start, end = captured["weekly"][0]
        assert (start, end) == ("2026-08-10", "2026-08-16")

    def test_monthly_uses_the_shared_anchor(self, captured, monkeypatch):
        monkeypatch.setattr(cli.date_range, "today", lambda now=None: date(2026, 8, 12))
        cli.main(["monthly", "--no-write"])
        assert captured["monthly"][0] == ("2026-08-01", "2026-08-31")

    def test_intersections_uses_the_shared_anchor(self, captured, monkeypatch):
        monkeypatch.setattr(cli.date_range, "today", lambda now=None: date(2026, 8, 12))
        cli.main(["intersections", "--days", "5"])
        assert captured["intersections"][0] == ("2026-08-08", "2026-08-12")

    def test_the_anchor_tracks_the_signal_labelling_rule(self):
        """`date_range.today()` and the day every signal is stamped with
        must be the same date, or a report's default window can exclude
        the signals written minutes earlier."""
        from scanners.base.trading_calendar import us_trading_day

        assert date_range.today().isoformat() == us_trading_day()


class TestRangeHelperUnits:
    def test_resolve_range_returns_iso_strings(self):
        start, end = date_range.resolve_range("2026-08-01", "2026-08-31")
        assert isinstance(start, str) and isinstance(end, str)
        assert start == "2026-08-01" and end == "2026-08-31"

    def test_equal_endpoints_are_a_valid_single_day_range(self):
        assert date_range.resolve_range("2026-08-12", "2026-08-12") == (
            "2026-08-12", "2026-08-12")

    def test_a_one_day_window_is_allowed(self):
        start, end = date_range.recent_bounds(1, end="2026-08-12")
        assert start == end == "2026-08-12"

    def test_parse_day_accepts_a_date_object_unchanged(self):
        assert date_range.parse_day(date(2026, 8, 12), label="x") == date(2026, 8, 12)

    def test_parse_day_names_the_offending_argument(self):
        with pytest.raises(date_range.DateRangeError, match="--whatever"):
            date_range.parse_day("bad", label="--whatever")
