"""The daily profile pays for the data it reads, not for the clock.

`registry.DAILY_SCANNERS` has always said these three "only need daily
bars… should not pay for minute data it will not read". Provider
selection did not ask. It asked the SESSION -- and `scanner_daily` runs
at 16:17 ET, inside AFTER_HOURS, which is KIS-authoritative:

    2026-09-02  profile=daily  universe=11047  provider=kis
                duration=48798.8s (13.55h)  fetch_failures=540

Eleven thousand symbols, one at a time, serialised on the shared ~3s KIS
read interval, for minute bars none of the three scanners open. That is
the budget S6 needs live, and it is why the job was disabled.

The routing now answers from the REQUEST. Intraday profiles, S6's own
`--scanners orb`, and every session rule below them are untouched.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
for path in (str(REPO_ROOT), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import run_scanners  # noqa: E402
from scanners.registry import DAILY_SCANNERS, INTRADAY_SCANNERS  # noqa: E402
from scanners.runner import PROFILES  # noqa: E402


class TestDailyIsRoutedByRequirement:
    def test_the_daily_profile_is_daily_bars_only(self):
        assert run_scanners.daily_bars_only(["--profile", "daily"]) is True

    def test_equals_form_is_understood_too(self):
        assert run_scanners.daily_bars_only(["--profile=daily"]) is True

    def test_an_explicit_daily_scanner_list_counts(self):
        assert run_scanners.daily_bars_only(
            ["--scanners", ",".join(DAILY_SCANNERS)]) is True

    def test_daily_chooses_the_bulk_provider_in_every_session(self,
                                                              monkeypatch):
        """The whole point: AFTER_HOURS must not make daily pay for KIS."""
        from scanners.base import scan_session

        for session in ("PREMARKET", "REGULAR", "AFTER_HOURS",
                        "OVERNIGHT_DAYTIME"):
            monkeypatch.setattr(scan_session, "session_at",
                                lambda _s=session: _s)
            assert run_scanners.session_provider(["--profile", "daily"]) is None, (
                f"daily took a session provider in {session}")

    def test_after_hours_daily_never_constructs_a_broker(self, monkeypatch):
        """Not merely unused -- never built. The 13.55 hours were spent
        inside that client."""
        import brokers.kis_broker as kb

        def _explode(*a, **k):
            raise AssertionError("daily must not construct a KIS broker")

        monkeypatch.setattr(kb, "KISBroker", _explode)
        from scanners.base import scan_session
        monkeypatch.setattr(scan_session, "session_at",
                            lambda: "AFTER_HOURS")
        assert run_scanners.session_provider(["--profile", "daily"]) is None


class TestIntradayRoutingIsUnchanged:
    @pytest.mark.parametrize("profile", ["premarket", "open", "intraday", "all"])
    def test_intraday_profiles_are_not_daily_only(self, profile):
        assert run_scanners.daily_bars_only(["--profile", profile]) is False

    def test_s6_orb_scan_is_not_daily_only(self):
        """S6 LIVE's own invocation must keep its session routing."""
        assert run_scanners.daily_bars_only(["--scanners", "orb"]) is False

    def test_a_mixed_list_keeps_the_session_provider(self):
        mixed = [DAILY_SCANNERS[0], INTRADAY_SCANNERS[0]]
        assert run_scanners.daily_bars_only(["--scanners", ",".join(mixed)]) is False, (
            "one intraday scanner means minute bars really are needed")

    def test_no_profile_and_no_scanners_changes_nothing(self):
        assert run_scanners.daily_bars_only([]) is False

    def test_an_authoritative_session_still_builds_kis_for_orb(self,
                                                               monkeypatch):
        from scanners.base import scan_session
        monkeypatch.setattr(scan_session, "session_at",
                            lambda: "PREMARKET")
        built = {}

        class _Broker:
            def __init__(self, *a, **k):
                built["yes"] = True

        monkeypatch.setattr("brokers.kis_broker.KISBroker", _Broker)
        monkeypatch.setattr(
            "market_data.kis_bar_provider.provider_for_session",
            lambda session, broker=None, **k: "KIS_PROVIDER")
        assert run_scanners.session_provider(["--scanners", "orb"]) == "KIS_PROVIDER"
        assert built.get("yes"), "S6's premarket routing must be untouched"

    def test_regular_session_still_falls_back_for_intraday(self, monkeypatch):
        from scanners.base import scan_session
        monkeypatch.setattr(scan_session, "session_at", lambda: "REGULAR")
        assert run_scanners.session_provider(["--scanners", "orb"]) is None


class TestTheRankingContractIsUnchanged:
    def test_the_ranking_key_is_still_canonical(self):
        from scanners.base import activity as act

        assert act.RANKING_KEY == "daily_liquidity"
        assert act.store_path().name == "daily_liquidity.json"

    def test_a_bulk_written_ranking_is_readable_by_open(self, monkeypatch,
                                                        tmp_path):
        """DAILY writes on the bulk provider; OPEN reads on yfinance."""
        from datetime import date, timedelta

        from scanners.base import activity as act

        monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path))
        today = date(2026, 9, 3)
        writer = act.ActivityStore("yfinance")
        for i, sym in enumerate(["AAA", "BBB"]):
            writer.note(sym, trading_day=(today - timedelta(days=1)).isoformat(),
                        price=10.0 + i, avg_volume=1_000_000 + i)
        writer.save()
        assert act.ActivityStore.load("yfinance").active_symbols(
            limit=10, today=today) == ["BBB", "AAA"]

    def test_a_missing_ranking_still_fails_loudly(self, monkeypatch, tmp_path):
        from datetime import date

        from scanners.base import activity as act

        monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path))
        assert act.ActivityStore.load("yfinance").active_symbols(
            limit=10, today=date(2026, 9, 3)) == []


class TestTheScannerStillDoesNotTrade:
    def test_daily_routing_imports_no_broker_at_module_scope(self):
        source = (SCRIPTS / "run_scanners.py").read_text()
        head = source[:source.index("def daily_bars_only")]
        assert "KISBroker" not in head, (
            "the scanner observes; it does not acquire the ability to trade")

    def test_the_daily_check_touches_no_market_data(self):
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(
            inspect.getsource(run_scanners.daily_bars_only)))
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not (called & {"get_bars", "get_daily_bars", "submit_order"})
