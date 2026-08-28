"""A quiet market and a dead collector are different facts.

They produced the same artefact — an empty snapshot — because the
collector persisted only after processing a trade. In a system whose
whole premise is that "no data" and "no trades" must never be conflated,
the component supplying the data could not tell you which one it was.

Liveness is now written on a timer and market activity when it happens,
and neither is inferred from the other. CONNECTED_NO_TRADES is a normal
premarket state for an illiquid name; COLLECTOR_STALE is always our
problem.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_data import collector_status as cs  # noqa: E402

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
RUNNER = (REPO_ROOT / "scripts" / "run_realtime_bar_collector.py").read_text(
    encoding="utf-8")


def _status(**kw):
    base = dict(collector_started_at=NOW - timedelta(minutes=5),
                last_heartbeat_at=NOW - timedelta(seconds=5),
                connection_state=cs.CONNECTION_CONNECTED,
                subscription_requested=41, subscription_count=41)
    base.update(kw)
    return cs.CollectorStatus(**base)


class TestQuietIsNotDead:
    def test_connected_with_no_trades_is_its_own_state(self):
        assert _status(trades_observed=0).state(now=NOW) == cs.CONNECTED_NO_TRADES

    def test_it_is_not_reported_as_stale_or_failed(self):
        state = _status(trades_observed=0).state(now=NOW)
        assert state not in (cs.COLLECTOR_STALE, cs.FAILED, cs.DISCONNECTED)

    def test_trades_make_it_active(self):
        assert _status(trades_observed=12).state(now=NOW) == cs.CONNECTED_ACTIVE

    def test_a_quiet_premarket_still_heartbeats(self):
        """The heartbeat is written BEFORE the receive, so a socket that
        delivers nothing for minutes still proves the process is alive."""
        loop = RUNNER[RUNNER.index("while time.time() - started < seconds:"):]
        assert loop.index("beat()") < loop.index("message = ws.recv()")


class TestStaleIsAboutUsNotTheMarket:
    def test_an_old_heartbeat_is_collector_stale(self):
        old = _status(last_heartbeat_at=NOW - timedelta(minutes=10))
        assert old.state(now=NOW) == cs.COLLECTOR_STALE

    def test_a_stale_collector_outranks_having_seen_trades(self):
        """Trades an hour ago do not make a dead process alive."""
        old = _status(last_heartbeat_at=NOW - timedelta(minutes=10),
                      trades_observed=500)
        assert old.state(now=NOW) == cs.COLLECTOR_STALE

    def test_no_heartbeat_at_all_is_unknown_not_healthy(self):
        assert _status(last_heartbeat_at=None).state(now=NOW) == cs.UNKNOWN

    def test_the_reader_recomputes_the_age(self, tmp_path):
        """A collector that died five minutes ago left a file saying
        CONNECTED_NO_TRADES. Believing the stored state is the mistake
        this module exists to prevent."""
        path = tmp_path / "status.json"
        _status(trades_observed=0).write(path)
        later = NOW + timedelta(minutes=10)
        assert cs.describe(path, now=later)["state"] == cs.COLLECTOR_STALE

    def test_a_fresh_file_reads_as_written(self, tmp_path):
        path = tmp_path / "status.json"
        _status(trades_observed=3).write(path)
        assert cs.describe(path, now=NOW)["state"] == cs.CONNECTED_ACTIVE


class TestTheOtherStates:
    def test_a_dropped_socket_is_disconnected(self):
        st = _status(connection_state=cs.CONNECTION_DISCONNECTED)
        assert st.state(now=NOW) == cs.DISCONNECTED

    def test_fewer_subscriptions_than_asked_is_partial(self):
        st = _status(subscription_requested=41, subscription_count=12)
        assert st.state(now=NOW) == cs.SUBSCRIPTION_PARTIAL

    def test_a_failed_start_is_failed(self):
        st = _status(connection_state=cs.CONNECTION_FAILED)
        assert st.state(now=NOW) == cs.FAILED

    def test_a_missing_file_is_unknown_not_an_error(self, tmp_path):
        described = cs.describe(tmp_path / "absent.json", now=NOW)
        assert described["state"] == cs.UNKNOWN
        assert "no collector status" in described["reason"]


class TestTheRecordCarriesWhatAnOperatorNeeds:
    def test_every_required_field_is_present(self, tmp_path):
        path = tmp_path / "status.json"
        _status(trades_observed=4, market_session="PREMARKET",
                collector_sha="abc1234", subscribed_symbols=["AAPL", "MSFT"],
                last_trade_at=NOW - timedelta(seconds=20)).write(path)
        record = cs.describe(path, now=NOW)
        for key in ("collector_started_at", "last_heartbeat_at",
                    "heartbeat_age_seconds", "connection_state",
                    "subscription_count", "subscribed_symbols",
                    "last_message_at", "last_trade_at", "market_session",
                    "collector_sha", "data_source", "error_count",
                    "reconnect_count"):
            assert key in record, key

    def test_writing_never_raises(self, tmp_path):
        """Losing a heartbeat must not stop the collecting it describes."""
        unwritable = tmp_path / "nope" / "deeper"
        unwritable.mkdir(parents=True)
        unwritable.chmod(0o500)
        try:
            _status().write(unwritable / "status.json")
        finally:
            unwritable.chmod(0o700)

    def test_an_unreadable_file_is_unknown_rather_than_a_crash(self, tmp_path):
        path = tmp_path / "status.json"
        path.write_text("{{{ not json")
        assert cs.describe(path, now=NOW)["state"] == cs.UNKNOWN


class TestTheCollectorRecordsWhatItDid:
    def test_it_counts_trades_and_stamps_the_last_one(self):
        assert "st.trades_observed += len(trades)" in RUNNER
        assert "st.last_trade_at = datetime.now(timezone.utc)" in RUNNER

    def test_it_records_the_subscription_outcome(self):
        assert "st.subscription_count = subscribed" in RUNNER
        assert "st.subscription_requested = len(symbols)" in RUNNER

    def test_a_disconnect_is_recorded_with_its_reason(self):
        assert "st.connection_state = status_module.CONNECTION_DISCONNECTED" in RUNNER
        assert "st.last_error = str(exc)[:200]" in RUNNER

    def test_reconnect_count_survives_the_reconnect(self):
        """The status object is created once and threaded through, so a
        reconnect does not reset the history that explains it."""
        assert "status=st" in RUNNER
        assert "st.reconnect_count += 1" in RUNNER

    def test_a_beat_happens_on_the_way_out(self):
        """Including the failure paths, so the last thing written is the
        truth rather than the last healthy moment."""
        block = RUNNER[RUNNER.index("finally:"):]
        assert "beat()" in block[:200]


class TestTheCollectorUsesTheFeedThatDelivers:
    """Measured one feed at a time during REGULAR on 2026-08-28:
    RBAQ (realtime) returned SUBSCRIBE SUCCESS and 0 trades in seventy
    seconds; DNAS (delayed) returned 1124. The realtime subscription is a
    false positive -- KIS accepts it and sends nothing -- so
    "SUBSCRIBE SUCCESS" is not evidence that data will arrive.

    An earlier probe subscribed both at once and the trades were credited
    to realtime without proof. The collector was then configured for
    realtime alone and collected nothing while truthfully reporting
    41 of 41 subscribed.
    """

    def test_the_default_feed_is_the_one_that_delivered(self):
        from market_data import kis_hdfscnt0 as wire

        assert wire.DEFAULT_FEED == wire.FEED_DELAYED

    def test_the_collector_subscribes_with_it(self):
        assert "wire.DEFAULT_FEED" in RUNNER
        assert "wire.FEED_REALTIME" not in RUNNER

    def test_the_measurement_is_recorded_beside_the_constant(self):
        source = (REPO_ROOT / "market_data" / "kis_hdfscnt0.py").read_text(
            encoding="utf-8")
        assert "1124" in source
        assert "FALSE POSITIVE" in source

    def test_the_measured_lag_is_recorded(self):
        """Despite the name, the feed is effectively real time: median
        0.57s per trade at ingest.

        An earlier value of 70s was an artefact of the measurement -- it
        compared a probe summary written at the END of a run against
        trades from its start, so it measured the window, not the lag.
        Plausible for something called "delayed", which is why it went
        unchallenged."""
        from market_data import kis_hdfscnt0 as wire

        assert wire.OBSERVED_FEED_LAG_SECONDS == 0.57

    def test_the_bar_staleness_threshold_tolerates_the_lag(self):
        from market_data import kis_hdfscnt0 as wire
        from market_data import realtime_bars as rb

        assert rb.DEFAULT_STALE_AFTER_SECONDS > wire.OBSERVED_FEED_LAG_SECONDS
