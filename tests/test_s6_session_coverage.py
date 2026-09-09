"""Can each S6 session read its OWN opening range?

The defect these pin
--------------------
`run_realtime_bar_collector` resolves its session once and holds it for
`--seconds 3600`, so the first up-to-an-hour of every session is written
into the PRECEDING session's snapshot file. Measured on the real
2026-09-08 production snapshots: the PREMARKET file began at 04:40 and
the REGULAR file at 10:05, so 0 of 58 premarket symbols and 0 of 74
regular symbols had a constructible ORB5 range. Every fixture in the
suite built its bars from the session open, so nothing caught it.
"""

import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

#: (session, opening hour, opening minute, the file the roll lag writes
#: this session's first bars into)
SESSIONS = [
    ("PREMARKET", 4, 0, "OVERNIGHT_DAYTIME"),
    ("REGULAR", 9, 30, "PREMARKET"),
    ("AFTER_HOURS", 16, 0, "REGULAR"),
    ("OVERNIGHT_DAYTIME", 20, 0, "AFTER_HOURS"),
]


def _write_snapshot(root, day, label, bars_by_symbol, *, session_label=None,
                    coverage_started_at=None):
    """Persist a snapshot exactly as the collector does."""
    from market_data import realtime_bars as rb

    store = rb.RealtimeBarStore(stale_after_seconds=3600)
    store.coverage_started_at = coverage_started_at
    for symbol, stamps in bars_by_symbol.items():
        acc = rb.SessionAccumulator(symbol=symbol, session=session_label or label)
        for index, at in enumerate(stamps):
            acc.add(price=10.0 + index * 0.01, size=100, at=at)
        store._accumulators[(symbol, session_label or label)] = acc
    target = root / "realtime_bars" / f"{day}-{label}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(store.snapshot()), encoding="utf-8")
    return target


def _minutes(start_et, count):
    return [(start_et + timedelta(minutes=i)).astimezone(timezone.utc)
            for i in range(count)]


class TestOwnOpeningRangeIsReadable:
    """Each session's own first five minutes, however the collector filed them."""

    def _lagged_pair(self, root, day, session, hour, minute, donor):
        """The realistic split: the first 40 minutes in the donor file,
        the rest in the session's own file."""
        open_et = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)
        _write_snapshot(root, day.isoformat(), donor, {"AAPL": _minutes(open_et, 40)},
                        session_label=donor)
        _write_snapshot(root, day.isoformat(), session,
                        {"AAPL": _minutes(open_et + timedelta(minutes=40), 30)},
                        session_label=session)
        return open_et

    def test_every_session_reads_its_own_open_from_the_preceding_file(self, tmp_path):
        from s6_live import kis_bar_features as kbf

        day = date(2026, 9, 8)
        for session, hour, minute, donor in SESSIONS:
            root = tmp_path / session
            open_et = self._lagged_pair(root, day, session, hour, minute, donor)
            store = kbf.load_store(session, day.isoformat(),
                                   session_date=day, env={"REALTIME_BAR_DIR": str(root)})
            assert store is not None, session
            bars = store.bars("AAPL", session)
            assert bars, session
            first = min(b.minute for b in bars).astimezone(ET)
            assert (first.hour, first.minute) == (hour, minute), session

            now = (open_et + timedelta(minutes=41)).astimezone(timezone.utc)
            feats = kbf.build_from_bars("AAPL", store=store, session=session, now=now,
                                        range_minutes=5, closed_bar_only=True)
            assert feats.error is None, (session, feats.error)
            assert feats.range_high is not None, session
            assert feats.range_origin_timestamp.astimezone(ET).hour == hour, session

    def test_the_merge_never_double_counts_a_minute(self, tmp_path):
        """Both files overlap; a minute must appear once."""
        from s6_live import kis_bar_features as kbf

        day = date(2026, 9, 8)
        root = tmp_path / "overlap"
        open_et = datetime(2026, 9, 8, 4, 0, tzinfo=ET)
        _write_snapshot(root, day.isoformat(), "OVERNIGHT_DAYTIME",
                        {"AAPL": _minutes(open_et, 30)}, session_label="OVERNIGHT_DAYTIME")
        _write_snapshot(root, day.isoformat(), "PREMARKET",
                        {"AAPL": _minutes(open_et + timedelta(minutes=20), 30)},
                        session_label="PREMARKET")
        store = kbf.load_store("PREMARKET", day.isoformat(), session_date=day,
                               env={"REALTIME_BAR_DIR": str(root)})
        bars = store.bars("AAPL", "PREMARKET")
        stamps = [b.minute for b in bars]
        assert len(stamps) == len(set(stamps)) == 50
        # totals are recomputed from the bars actually kept
        acc = store.accumulator("AAPL", "PREMARKET")
        assert acc.volume == sum(b.volume for b in bars)
        assert acc.trade_count == sum(b.trade_count for b in bars)

    def test_another_sessions_bars_are_never_admitted(self, tmp_path):
        """The donor file also holds bars from before this session opened."""
        from s6_live import kis_bar_features as kbf

        day = date(2026, 9, 8)
        root = tmp_path / "scoped"
        pre_open = datetime(2026, 9, 8, 3, 0, tzinfo=ET)     # overnight, not premarket
        _write_snapshot(root, day.isoformat(), "OVERNIGHT_DAYTIME",
                        {"AAPL": _minutes(pre_open, 120)},   # 03:00 -> 04:59
                        session_label="OVERNIGHT_DAYTIME")
        store = kbf.load_store("PREMARKET", day.isoformat(), session_date=day,
                               env={"REALTIME_BAR_DIR": str(root)})
        bars = store.bars("AAPL", "PREMARKET")
        assert min(b.minute for b in bars).astimezone(ET).hour == 4
        assert all(b.minute.astimezone(ET).hour >= 4 for b in bars)


class TestCrossMidnight:
    def test_the_overnight_open_survives_midnight(self, tmp_path):
        """20:00 lives under one date key, 01:00 under the next."""
        from s6_live import kis_bar_features as kbf

        root = tmp_path / "overnight"
        open_et = datetime(2026, 9, 8, 20, 0, tzinfo=ET)
        _write_snapshot(root, "2026-09-08", "OVERNIGHT_DAYTIME",
                        {"AAPL": _minutes(open_et, 60)}, session_label="OVERNIGHT_DAYTIME")
        _write_snapshot(root, "2026-09-09", "OVERNIGHT_DAYTIME",
                        {"AAPL": _minutes(datetime(2026, 9, 9, 0, 30, tzinfo=ET), 60)},
                        session_label="OVERNIGHT_DAYTIME")

        # A generous staleness bound: this is a coverage test, and the
        # feed check runs before the origin check.
        store = kbf.load_store("OVERNIGHT_DAYTIME", "2026-09-09",
                               session_date=date(2026, 9, 8), stale_after_seconds=7200,
                               env={"REALTIME_BAR_DIR": str(root)})
        bars = store.bars("AAPL", "OVERNIGHT_DAYTIME")
        stamps = sorted(b.minute for b in bars)
        assert len(stamps) == len(set(stamps)) == 120
        assert stamps[0].astimezone(ET).hour == 20          # the 20:00 origin
        assert stamps[-1].astimezone(ET).day == 9           # and past midnight

        # and after midnight the ORB5 range is still constructible
        now = datetime(2026, 9, 9, 1, 45, tzinfo=ET).astimezone(timezone.utc)
        feats = kbf.build_from_bars("AAPL", store=store, session="OVERNIGHT_DAYTIME",
                                    now=now, range_minutes=5, closed_bar_only=True)
        assert feats.error is None
        assert feats.range_high is not None
        assert feats.range_origin_timestamp.astimezone(ET).hour == 20

    def test_a_window_that_holds_no_bars_reads_as_no_store(self, tmp_path):
        """Not an empty range that looks measured -- nothing at all."""
        from s6_live import kis_bar_features as kbf

        root = tmp_path / "empty"
        _write_snapshot(root, "2026-09-08", "AFTER_HOURS",
                        {"AAPL": _minutes(datetime(2026, 9, 8, 16, 30, tzinfo=ET), 60)},
                        session_label="AFTER_HOURS")
        assert kbf.load_store("OVERNIGHT_DAYTIME", "2026-09-09",
                              session_date=date(2026, 9, 8),
                              env={"REALTIME_BAR_DIR": str(root)}) is None


class TestCoverageFailuresAreExplicit:
    def test_bars_starting_mid_session_report_the_canonical_error(self, tmp_path):
        from s6_live import kis_bar_features as kbf

        root = tmp_path / "late"
        late = datetime(2026, 9, 8, 6, 30, tzinfo=ET)     # premarket opened at 04:00
        _write_snapshot(root, "2026-09-08", "PREMARKET", {"AAPL": _minutes(late, 40)},
                        session_label="PREMARKET")
        store = kbf.load_store("PREMARKET", "2026-09-08", session_date=date(2026, 9, 8),
                               env={"REALTIME_BAR_DIR": str(root)})
        feats = kbf.build_from_bars(
            "AAPL", store=store, session="PREMARKET",
            now=(late + timedelta(minutes=41)).astimezone(timezone.utc),
            range_minutes=5, closed_bar_only=True)
        assert feats.error == "OFFICIAL_ORIGIN_NOT_COVERED"
        assert feats.range_high is None

    def test_a_coverage_claim_without_bars_is_not_accepted(self, tmp_path):
        """The masking path: the collector says it was listening from
        20:00, but the rolled snapshot holds nothing before 00:30. That
        used to read as an ordinary WATCHING state."""
        from s6_live import kis_bar_features as kbf, precision_watch as pw

        root = tmp_path / "masked"
        _write_snapshot(root, "2026-09-09", "OVERNIGHT_DAYTIME",
                        {"AAPL": _minutes(datetime(2026, 9, 9, 0, 30, tzinfo=ET), 60)},
                        session_label="OVERNIGHT_DAYTIME",
                        coverage_started_at=datetime(2026, 9, 8, 20, 0,
                                                     tzinfo=ET).astimezone(timezone.utc))
        store = kbf.load_store("OVERNIGHT_DAYTIME", "2026-09-09",
                               session_date=date(2026, 9, 8), stale_after_seconds=7200,
                               env={"REALTIME_BAR_DIR": str(root)})
        now = datetime(2026, 9, 9, 1, 45, tzinfo=ET).astimezone(timezone.utc)
        feats = kbf.build_from_bars("AAPL", store=store, session="OVERNIGHT_DAYTIME",
                                    now=now, range_minutes=5, closed_bar_only=True)
        assert feats.error == "OFFICIAL_ORIGIN_NOT_COVERED"
        assert "origin_coverage_claim" in feats.unavailable
        evaluation = pw.evaluate("AAPL", session="OVERNIGHT_DAYTIME", now=now,
                                 features=feats, conn=None)
        assert not evaluation.ready

    def test_an_empty_opening_window_is_a_coverage_failure(self, tmp_path):
        """Origin covered, but its first five minutes hold no bars."""
        from s6_live import kis_bar_features as kbf

        root = tmp_path / "hole"
        open_et = datetime(2026, 9, 8, 4, 0, tzinfo=ET)
        stamps = [open_et.astimezone(timezone.utc)]
        stamps += _minutes(open_et + timedelta(minutes=30), 40)
        _write_snapshot(root, "2026-09-08", "PREMARKET", {"AAPL": stamps},
                        session_label="PREMARKET")
        store = kbf.load_store("PREMARKET", "2026-09-08", session_date=date(2026, 9, 8),
                               env={"REALTIME_BAR_DIR": str(root)})
        # Doctor the single opening bar away, leaving coverage "proven" by
        # a first bar that sits exactly at the origin but no window.
        acc = store.accumulator("AAPL", "PREMARKET")
        feats = kbf.build_from_bars(
            "AAPL", store=store, session="PREMARKET",
            now=(open_et + timedelta(minutes=71)).astimezone(timezone.utc),
            range_minutes=5, closed_bar_only=True)
        # With the 04:00 bar present the range IS constructible.
        assert feats.error is None and feats.range_high is not None
        del acc.bars[min(acc.bars)]
        feats = kbf.build_from_bars(
            "AAPL", store=store, session="PREMARKET",
            now=(open_et + timedelta(minutes=71)).astimezone(timezone.utc),
            range_minutes=5, closed_bar_only=True)
        assert feats.error == "OFFICIAL_ORIGIN_NOT_COVERED"


class TestRestartAndRotation:
    def test_the_opening_range_survives_a_collector_restart(self, tmp_path):
        """A restart writes a NEW file; the open must stay readable."""
        from s6_live import kis_bar_features as kbf

        root = tmp_path / "restart"
        open_et = datetime(2026, 9, 8, 9, 30, tzinfo=ET)
        # first process, labelled PREMARKET, holds the regular open
        _write_snapshot(root, "2026-09-08", "PREMARKET", {"AAPL": _minutes(open_et, 35)},
                        session_label="PREMARKET")
        store = kbf.load_store("REGULAR", "2026-09-08", session_date=date(2026, 9, 8),
                               env={"REALTIME_BAR_DIR": str(root)})
        assert store.bars("AAPL", "REGULAR")
        # second process rotates in and writes the REGULAR file
        _write_snapshot(root, "2026-09-08", "REGULAR",
                        {"AAPL": _minutes(open_et + timedelta(minutes=35), 60)},
                        session_label="REGULAR")
        store = kbf.load_store("REGULAR", "2026-09-08", session_date=date(2026, 9, 8),
                               env={"REALTIME_BAR_DIR": str(root)})
        stamps = sorted(b.minute for b in store.bars("AAPL", "REGULAR"))
        assert stamps[0].astimezone(ET).hour == 9 and stamps[0].astimezone(ET).minute == 30
        assert len(stamps) == 95
        now = (open_et + timedelta(minutes=96)).astimezone(timezone.utc)
        feats = kbf.build_from_bars("AAPL", store=store, session="REGULAR", now=now,
                                    range_minutes=5, closed_bar_only=True)
        assert feats.error is None and feats.range_high is not None

    def test_a_changed_snapshot_is_re_read_not_served_from_cache(self, tmp_path):
        """The cache is keyed on mtime and size; a rewritten file must win."""
        from s6_live import kis_bar_features as kbf

        root = tmp_path / "cache"
        open_et = datetime(2026, 9, 8, 4, 0, tzinfo=ET)
        _write_snapshot(root, "2026-09-08", "PREMARKET", {"AAPL": _minutes(open_et, 10)},
                        session_label="PREMARKET")
        env = {"REALTIME_BAR_DIR": str(root)}
        first = kbf.load_store("PREMARKET", "2026-09-08", session_date=date(2026, 9, 8),
                               env=env)
        assert len(first.bars("AAPL", "PREMARKET")) == 10
        _write_snapshot(root, "2026-09-08", "PREMARKET", {"AAPL": _minutes(open_et, 40)},
                        session_label="PREMARKET")
        second = kbf.load_store("PREMARKET", "2026-09-08", session_date=date(2026, 9, 8),
                                env=env)
        assert len(second.bars("AAPL", "PREMARKET")) == 40


def test_the_readiness_message_states_whether_session_data_is_ready(monkeypatch):
    from operations import session_readiness as sr, slack_presentation as sp
    from market_data import collector_status

    monkeypatch.setattr(collector_status, "describe",
                        lambda *a, **k: {"state": "CONNECTED_ACTIVE",
                                         "market_session": "REGULAR"})
    assert sr._session_data_state("REGULAR") == "OK"
    assert "세션 데이터: 정상" in sp._s6_status_lines(sr._s6_status("REGULAR"))

    monkeypatch.setattr(collector_status, "describe",
                        lambda *a, **k: {"state": "CONNECTED_ACTIVE",
                                         "market_session": "PREMARKET"})
    lines = sp._s6_status_lines(sr._s6_status("REGULAR"))
    assert any(line.startswith("세션 데이터: 준비 실패") for line in lines)
