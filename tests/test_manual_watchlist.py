"""Manual Watchlist behaviour (track C-2 .. C-5).

The properties worth pinning down are the ones a reader would be misled
by if they broke quietly: the ranking has to be reproducible, an empty
day has to say it is empty rather than look like a quiet one, and the
Slack size ceiling has to hold even when a caller asks for more.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners.base import result_store  # noqa: E402
from scanners.base.models import ScannerSignal  # noqa: E402
from watchlist import builder, config, ranking, render, store  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path / "analytics"))
    monkeypatch.setenv(config.WATCHLIST_DIR_ENV, str(tmp_path / "watchlist"))
    return tmp_path


def signal(symbol, scanner, score, *, day="2026-08-14", price=100.0,
           ext200=5.0, ext89=3.0, change=1.0, to_high=-8.0, reasons=None):
    return ScannerSignal(
        timestamp=f"{day}T20:10:00+00:00", trading_day=day, symbol=symbol,
        scanner_name=scanner, scanner_version=f"{scanner}_v1.0",
        scanner_score=score, signal_price=price,
        extension_hma200_pct=ext200, extension_hma89_pct=ext89,
        price_change_pct=change, distance_52w_high=to_high,
        reasons=reasons or [f"{scanner} passed"])


def store_signals(signals, day="2026-08-14"):
    result_store.write_signals(signals, trading_day=day)


class TestRankingIsDeterministic:
    def test_identical_input_gives_identical_output(self):
        store_signals([signal("AAA", "hma_early_trend", 80.0),
                       signal("BBB", "accumulation", 70.0)])
        first = builder.build_tomorrow("2026-08-14", "2026-08-17")
        second = builder.build_tomorrow("2026-08-14", "2026-08-17")
        assert [e["symbol"] for e in first["entries"]] == \
               [e["symbol"] for e in second["entries"]]
        assert [e["manual_watch_score"] for e in first["entries"]] == \
               [e["manual_watch_score"] for e in second["entries"]]

    def test_ties_break_on_symbol_ascending(self):
        """Without this, two equally-scored names order by whatever the
        store happened to yield, and yesterday's file will not diff."""
        entries = [{"symbol": "ZZZ", "manual_watch_score": 50.0},
                   {"symbol": "AAA", "manual_watch_score": 50.0},
                   {"symbol": "MMM", "manual_watch_score": 50.0}]
        assert [e["symbol"] for e in ranking.rank(entries)] == ["AAA", "MMM", "ZZZ"]

    def test_rank_is_one_based_and_contiguous(self):
        entries = [{"symbol": f"S{i}", "manual_watch_score": float(i)} for i in range(5)]
        assert [e["rank"] for e in ranking.rank(entries)] == [1, 2, 3, 4, 5]


class TestIntersectionDrivesTheOrder:
    def test_more_daily_scanners_outranks_a_single_higher_score(self):
        """Three scanners agreeing is the whole reason six of them run."""
        store_signals([
            signal("TRIPLE", "hma_early_trend", 60.0),
            signal("TRIPLE", "accumulation", 60.0),
            signal("TRIPLE", "breakout_ready", 60.0),
            signal("SINGLE", "hma_early_trend", 99.0),
        ])
        payload = builder.build_tomorrow("2026-08-14", "2026-08-17")
        assert [e["symbol"] for e in payload["entries"]] == ["TRIPLE", "SINGLE"]
        assert payload["entries"][0]["intersection_count"] == 3

    def test_components_are_reported_alongside_the_total(self):
        store_signals([signal("AAA", "hma_early_trend", 80.0)])
        entry = builder.build_tomorrow("2026-08-14", "2026-08-17")["entries"][0]
        assert set(entry["components"]) == {
            "intersection", "max_scanner_score", "early_trend", "accumulation",
            "breakout_ready", "premarket_confirm", "overextended_penalty"}
        assert entry["manual_watch_score"] == pytest.approx(
            sum(entry["components"].values()), abs=1e-6)


class TestOnlyDailyScannersSeedTheEveningList:
    def test_intraday_scanners_do_not_create_entries(self):
        """Track C-2: S5/S6 are for observing behaviour, not for seeding
        a reading list -- by the time they fire the list has been read."""
        store_signals([signal("ORBONLY", "orb", 95.0),
                       signal("GAPONLY", "gap_pullback", 95.0),
                       signal("REAL", "hma_early_trend", 50.0)])
        payload = builder.build_tomorrow("2026-08-14", "2026-08-17")
        assert [e["symbol"] for e in payload["entries"]] == ["REAL"]

    def test_intraday_presence_is_recorded_as_an_observation(self):
        store_signals([signal("REAL", "hma_early_trend", 50.0),
                       signal("REAL", "orb", 90.0)])
        entry = builder.build_tomorrow("2026-08-14", "2026-08-17")["entries"][0]
        assert entry["intraday_observed"] == ["orb"]
        assert "orb" not in entry["daily_scanners"]

    def test_premarket_does_not_seed_the_evening_list(self):
        store_signals([signal("PMONLY", "premarket_momentum", 95.0)])
        payload = builder.build_tomorrow("2026-08-14", "2026-08-17")
        assert payload["entries"] == []


class TestTwoStageFlow:
    def test_today_confirms_but_never_adds_symbols(self):
        """A name whose only evidence is this morning's gap has no
        overnight thesis; the morning pass re-ranks, it does not recruit."""
        store_signals([signal("HELD", "hma_early_trend", 70.0)], day="2026-08-14")
        tomorrow = builder.build_tomorrow("2026-08-14", "2026-08-17")
        store.write_json(tomorrow, trading_day="2026-08-17",
                         stage=config.STAGE_TOMORROW)

        store_signals([signal("HELD", "premarket_momentum", 80.0, day="2026-08-17"),
                       signal("NEWCOMER", "premarket_momentum", 99.0, day="2026-08-17")],
                      day="2026-08-17")
        today = builder.build_today("2026-08-17")

        symbols = [e["symbol"] for e in today["entries"]]
        assert symbols == ["HELD"], "premarket must not add a new symbol"
        assert today["entries"][0]["premarket_confirmed"] is True
        assert today["premarket_confirmations"] == 1

    def test_premarket_confirmation_raises_the_score(self):
        store_signals([signal("AAA", "hma_early_trend", 70.0)], day="2026-08-14")
        tomorrow = builder.build_tomorrow("2026-08-14", "2026-08-17")
        before = tomorrow["entries"][0]["manual_watch_score"]
        store.write_json(tomorrow, trading_day="2026-08-17",
                         stage=config.STAGE_TOMORROW)

        store_signals([signal("AAA", "premarket_momentum", 70.0, day="2026-08-17")],
                      day="2026-08-17")
        after = builder.build_today("2026-08-17")["entries"][0]["manual_watch_score"]
        assert after > before

    def test_unconfirmed_entries_keep_their_evening_score(self):
        store_signals([signal("AAA", "hma_early_trend", 70.0)], day="2026-08-14")
        tomorrow = builder.build_tomorrow("2026-08-14", "2026-08-17")
        before = tomorrow["entries"][0]["manual_watch_score"]
        store.write_json(tomorrow, trading_day="2026-08-17",
                         stage=config.STAGE_TOMORROW)

        today = builder.build_today("2026-08-17")
        assert today["entries"][0]["manual_watch_score"] == before
        assert today["entries"][0]["premarket_confirmed"] is False

    def test_a_missing_tomorrow_list_says_so(self):
        """The first day of a month, the day after a holiday, or a day
        whose evening scan failed. Not an error, and not a silent blank."""
        payload = builder.build_today("2026-08-17")
        assert payload["entries"] == []
        assert payload["empty_reason"]
        assert "Tomorrow Watchlist" in payload["empty_reason"]


class TestEmptyInput:
    def test_no_signals_produces_an_empty_but_valid_payload(self):
        payload = builder.build_tomorrow("2026-08-14", "2026-08-17")
        assert payload["entries"] == []
        assert payload["symbols_considered"] == 0
        assert payload["trading_day"] == "2026-08-17"
        assert payload["manual_watch_version"] == config.MANUAL_WATCH_VERSION

    def test_empty_renders_without_raising(self):
        payload = builder.build_tomorrow("2026-08-14", "2026-08-17")
        for text in (render.format_slack(payload), render.format_markdown(payload),
                     render.format_console(payload)):
            assert render.BANNER in text or "Manual Watchlist" in text


class TestOverextension:
    def test_a_stretched_name_is_flagged_and_penalised(self):
        store_signals([signal("CALM", "hma_early_trend", 80.0, ext200=5.0),
                       signal("HOT", "hma_early_trend", 80.0, ext200=60.0)])
        payload = builder.build_tomorrow("2026-08-14", "2026-08-17")
        entries = {e["symbol"]: e for e in payload["entries"]}
        assert entries["HOT"]["overextended"] is True
        assert entries["CALM"]["overextended"] is False
        assert entries["HOT"]["manual_watch_score"] < entries["CALM"]["manual_watch_score"]

    def test_overextended_is_a_flag_not_a_filter(self):
        """Track C-5: the scanner said PASS. This layer may reorder that
        judgement, never overturn it."""
        store_signals([signal("HOT", "hma_early_trend", 90.0, ext200=99.0,
                              ext89=99.0, change=50.0)])
        payload = builder.build_tomorrow("2026-08-14", "2026-08-17")
        assert [e["symbol"] for e in payload["entries"]] == ["HOT"]
        assert payload["entries"][0]["overextended_reasons"]

    def test_near_52w_high_alone_is_not_overextension(self):
        """A breakout is the setup several of these scanners look for."""
        store_signals([signal("BREAK", "breakout_ready", 80.0, ext200=2.0,
                              ext89=1.0, change=1.0, to_high=-0.2)])
        entry = builder.build_tomorrow("2026-08-14", "2026-08-17")["entries"][0]
        assert entry["overextended"] is False


class TestTopN:
    def _payload(self, count):
        store_signals([signal(f"S{i:03d}", "hma_early_trend", float(100 - i))
                       for i in range(count)])
        return builder.build_tomorrow("2026-08-14", "2026-08-17")

    @staticmethod
    def _listed_symbols(text):
        """Symbols actually rendered as numbered lines, `1. *AAA* ...`."""
        import re

        return re.findall(r"^\d+\.\s+\*([A-Z0-9]+)\*", text, flags=re.MULTILINE)

    def test_slack_defaults_to_five(self):
        assert config.SLACK_TOP_N == 5
        listed = self._listed_symbols(render.format_slack(self._payload(30)))
        assert len(listed) == 5
        assert listed == ["S000", "S001", "S002", "S003", "S004"]

    def test_slack_is_capped_at_ten_even_when_more_is_requested(self):
        assert config.slack_top_n(999) == config.SLACK_TOP_N_MAX == 10
        listed = self._listed_symbols(render.format_slack(self._payload(30), top_n=999))
        assert len(listed) == config.SLACK_TOP_N_MAX

    @pytest.mark.parametrize("requested,expected", [
        (None, 5), (1, 1), (7, 7), (10, 10), (11, 10), (0, 1), (-3, 1),
        ("abc", 5), (999999, 10)])
    def test_slack_top_n_clamps_every_input(self, requested, expected):
        assert config.slack_top_n(requested) == expected

    def test_the_file_may_carry_more_than_slack(self):
        payload = self._payload(30)
        assert len(payload["entries"]) == 30
        rows = [line for line in render.format_markdown(payload).splitlines()
                if line.startswith("| ") and not line.startswith("|---")
                and not line.startswith("| # ")]
        assert len(rows) == config.FILE_TOP_N == 20
        assert len(rows) > config.SLACK_TOP_N_MAX

    def test_the_stored_file_is_bounded(self):
        payload = self._payload(config.STORE_TOP_N + 25)
        assert len(payload["entries"]) == config.STORE_TOP_N
        assert payload["truncated_from"] == config.STORE_TOP_N + 25


class TestRenderingAlwaysSaysWhatItIs:
    @pytest.mark.parametrize("renderer", ["format_slack", "format_markdown"])
    def test_the_banner_is_present(self, renderer):
        store_signals([signal("AAA", "hma_early_trend", 80.0)])
        payload = builder.build_tomorrow("2026-08-14", "2026-08-17")
        text = getattr(render, renderer)(payload)
        assert "자동주문 아님" in text
        assert "Candidate Decision: disabled" in text

    def test_no_order_language_leaks_into_slack(self):
        store_signals([signal("AAA", "hma_early_trend", 80.0)])
        payload = builder.build_tomorrow("2026-08-14", "2026-08-17")
        text = render.format_slack(payload)
        for word in ("매수", "주문 실행", "진입하세요", "BUY NOW"):
            assert word not in text


class TestStore:
    def test_files_are_keyed_by_the_day_the_list_is_for(self):
        payload = builder.build_tomorrow("2026-08-14", "2026-08-17")
        path = Path(store.write_json(payload, trading_day="2026-08-17",
                                     stage=config.STAGE_TOMORROW))
        assert path.name == "2026-08-17.tomorrow.json"
        assert json.loads(path.read_text())["source_session_day"] == "2026-08-14"

    def test_a_missing_file_reads_as_none_not_an_error(self):
        assert store.read_json("2099-01-01", config.STAGE_TODAY) is None

    def test_write_is_atomic_leaving_no_temp_files(self):
        payload = builder.build_tomorrow("2026-08-14", "2026-08-17")
        store.write_json(payload, trading_day="2026-08-17", stage=config.STAGE_TOMORROW)
        leftovers = [p.name for p in store.watchlist_dir().iterdir()
                     if p.name.startswith(".")]
        assert leftovers == []


class TestScannerConfigIsNotTouched:
    def test_the_watchlist_does_not_read_scanner_thresholds(self):
        """Month 1 freezes the scanner parameters. This layer reads
        stored signals, never a scanner's config."""
        import ast

        for path in (REPO_ROOT / "watchlist").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert "scanners.base.config" != node.module, path.name
                    assert not node.module.startswith("scanners.hma_early_trend"), path.name

    def test_manual_watch_version_is_stamped_on_every_payload(self):
        store_signals([signal("AAA", "hma_early_trend", 80.0)])
        payload = builder.build_tomorrow("2026-08-14", "2026-08-17")
        assert payload["manual_watch_version"] == config.MANUAL_WATCH_VERSION
        assert payload["entries"][0]["manual_watch_version"] == config.MANUAL_WATCH_VERSION
