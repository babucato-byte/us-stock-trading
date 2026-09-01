"""A held position must not depend on a watchlist to stay manageable.

2026-09-01: JBS was an S6 OPEN position holding real money, with ZERO
subscription frames to its name -- it was never in the realtime stream.
In REGULAR the provider fallback covered it and the exit fired correctly
on VOLUME_DECAY_PRICE_WEAKNESS. In PREMARKET, AFTER_HOURS or
OVERNIGHT_DAYTIME the same position would have had no VWAP, no EMA and no
volume for as long as it was held, because `realtime_features.build`
reads the stream alone in those sessions when no provider is supplied.

Three of the seven exit rules would have been silently unevaluable on a
live position. That is the failure these tests exist to prevent.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_data.kis_bar_provider import (  # noqa: E402
    KIS_AUTHORITATIVE_SESSIONS, KISBarMarketDataProvider, provider_for_session,
)
from s6_live import exit_runtime, realtime_features  # noqa: E402


class TestHeldPositionsGetTheirOwnData:
    def test_the_runtime_hands_the_exit_path_a_session_provider(self):
        """Without this the extended sessions read the stream alone."""
        text = (REPO_ROOT / "scripts" / "run_s6_runtime.py").read_text()
        assert "provider_for_session" in text
        assert "provider=provider_for_session(session, broker=broker)" in text

    @pytest.mark.parametrize("session", sorted(KIS_AUTHORITATIVE_SESSIONS))
    def test_every_extended_session_gets_a_kis_provider(self, session):
        provider = provider_for_session(session, broker=object(),
                                        fallback=object())
        assert isinstance(provider, KISBarMarketDataProvider)

    def test_regular_keeps_the_provider_it_already_had(self):
        fallback = object()
        assert provider_for_session("REGULAR", broker=object(),
                                    fallback=fallback) is fallback

    def test_a_supplied_provider_bypasses_the_stream_only_branch(self):
        """The branch that made subscription membership decide."""
        source = (REPO_ROOT / "s6_live" / "realtime_features.py").read_text()
        assert "if resolved in KIS_AUTHORITATIVE_SESSIONS and provider is None:" in source

    def test_features_fn_forwards_the_provider_per_symbol(self):
        seen = {}

        def fake_build(symbol, **kwargs):
            seen[symbol] = kwargs.get("provider")
            return None

        original = realtime_features.build
        try:
            realtime_features.build = fake_build
            fn = realtime_features.make_features_fn(session="PREMARKET",
                                                    provider="THE_PROVIDER")
            fn("JBS")
        finally:
            realtime_features.build = original
        assert seen == {"JBS": "THE_PROVIDER"}


class TestOnlyHeldSymbolsAreFetched:
    def test_the_exit_path_fetches_per_position_not_per_universe(self):
        """`features_fn(symbol)` is called by the exit runtime once per
        held row; nothing in that path walks a universe."""
        text = (REPO_ROOT / "s6_live" / "exit_runtime.py").read_text()
        assert "features=features_fn(symbol)" in text
        for universe_word in ("load_symbols", "manifest", "active_symbols"):
            assert universe_word not in text


class TestAbsenceOfDataIsNotACalmMarket:
    def test_the_marker_exists_and_is_distinct(self):
        assert exit_runtime.POSITION_DATA_UNAVAILABLE == "POSITION_DATA_UNAVAILABLE"

    def test_a_missing_view_is_named_not_swallowed(self):
        text = (REPO_ROOT / "s6_live" / "exit_runtime.py").read_text()
        assert "POSITION_DATA_UNAVAILABLE" in text
        assert 'diagnostics["position_data_unavailable"]' in text

    def test_unavailable_rules_are_still_reported_individually(self):
        text = (REPO_ROOT / "s6_live" / "exit_runtime.py").read_text()
        assert "could not be evaluated this tick" in text


class TestTheExitRulesThemselvesAreUnchanged:
    def test_all_seven_families_are_still_present(self):
        from s6_live import exit_policy

        for reason in ("REASON_EMERGENCY", "REASON_HARD_RISK_CAP",
                       "REASON_RANGE_REENTRY", "REASON_VWAP_FAILURE",
                       "REASON_EMA_STRUCTURE_FAILURE",
                       "REASON_VOLUME_DECAY_PRICE_WEAKNESS",
                       "REASON_SESSION_EXIT"):
            assert hasattr(exit_policy, reason), reason

    def test_the_monitor_still_runs_every_minute(self):
        """Cadence is a deployment property; it lives in the wrapper."""
        wrapper = (REPO_ROOT / "deploy" / "cron"
                   / "s6_exit_monitor.sh").read_text()
        assert "run_s6_runtime.py" in wrapper
        assert "s6_positions" in wrapper  # cheap enough to run each minute
