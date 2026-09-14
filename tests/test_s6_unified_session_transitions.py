"""Unified all-session live runtime -- automatic transitions, no stale
state, and the daytime route-evidence-once-recorded-stays-recorded
property.

Why this file exists
---------------------
Three earlier passes (the tick-budget-guard work, the full-system E2E
simulation, and the production architecture audit) already established
that `config.session_capability.capability_at()` is a pure function of
`now` with no persisted or cached session state, and that S6's transient
per-session files (`s6_live/active_watch.py`) are keyed by
(session-start-date, session), never by a bare filename that could
collide across sessions. This file is the one thing none of those
passes pinned down explicitly: that walking `now` through a full daily
cycle -- OVERNIGHT_DAYTIME -> PREMARKET -> REGULAR -> AFTER_HOURS -> the
next day's OVERNIGHT_DAYTIME -- in ONE process, with no restart, produces
the right answer at every step and never carries a stale answer forward.

It also pins the property this task is actually about: once
`brokers.route_evidence.record()` persists a real KIS acceptance for a
daytime wire value, `config.session_capability.route_awaiting_live_evidence`
must stop requiring the bootstrap/verification exception for that value
-- automatically, on the very next call, with no operator action, no
re-arming per session, and no re-arming across a simulated release
boundary (a fresh `STATE_STORE_DB_FILE` pointed at the SAME shared
directory, exactly like a real deploy). Nothing about the bootstrap
capability, the verification capability, or `brokers/kis_broker.py`'s
static VERIFICATION_MATRIX is touched by this file or by the change this
file was written to prove -- the manual one-time proof for
`daytime_order_tr_id_live_buy` / `daytime_cancel_path` /
`daytime_cancel_tr_id_live` still runs exactly as before; what this
proves is that ordinary S6 execution does not need to ask for it again
once it exists.
"""

from datetime import datetime, timezone

import pytest

from market_hours import EASTERN

# All in KST-equivalent UTC on 2026-08-26/27 (US DST in effect, matching
# config/kis_market_schedule.py's _DST table and this repo's other
# session-capability tests, which already use this week).
DAYTIME_MOMENT = datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc)      # 12:00 KST
PREMARKET_MOMENT = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)   # 19:00 KST
REGULAR_MOMENT = datetime(2026, 8, 26, 14, 30, tzinfo=timezone.utc)    # 23:30 KST
AFTER_HOURS_MOMENT = datetime(2026, 8, 26, 21, 0, tzinfo=timezone.utc)  # 06:00 KST Aug 27
NEXT_DAYTIME_MOMENT = datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc)  # 10:00 KST Aug 27

WEEKEND_MOMENT = datetime(2026, 8, 29, 12, 0, tzinfo=EASTERN)  # Saturday


class TestOneCommonPathAcrossAllFourSessions:
    """Item 1/2: `capability_at()` is the single session-resolution
    function every session uses; it is asked fresh each time and never
    consults a previous answer."""

    def test_all_four_sessions_resolve_from_the_same_pure_function(self):
        from config import session_capability as sc

        results = {
            "OVERNIGHT_DAYTIME": sc.capability_at(DAYTIME_MOMENT),
            "PREMARKET": sc.capability_at(PREMARKET_MOMENT),
            "REGULAR": sc.capability_at(REGULAR_MOMENT),
            "AFTER_HOURS": sc.capability_at(AFTER_HOURS_MOMENT),
        }
        for expected_session, cap in results.items():
            assert cap.session == expected_session, (expected_session, cap)

    def test_capability_at_holds_no_module_level_session_state(self):
        """Calling it for one session must not change what a DIFFERENT
        moment resolves to -- the only input is the argument."""
        from config import session_capability as sc

        before = sc.capability_at(PREMARKET_MOMENT)
        sc.capability_at(DAYTIME_MOMENT)
        sc.capability_at(REGULAR_MOMENT)
        sc.capability_at(AFTER_HOURS_MOMENT)
        after = sc.capability_at(PREMARKET_MOMENT)
        assert before == after


class TestAutomaticSessionTransitionsNoRestart:
    """Items 8/9: walking through the full daily cycle, and into a new
    trading day, in one process -- no restart, no manual reset."""

    def test_E_daytime_to_premarket_without_restart(self):
        from config import session_capability as sc

        assert sc.capability_at(DAYTIME_MOMENT).session == "OVERNIGHT_DAYTIME"
        assert sc.capability_at(PREMARKET_MOMENT).session == "PREMARKET"

    def test_F_premarket_to_regular_without_restart(self):
        from config import session_capability as sc

        assert sc.capability_at(PREMARKET_MOMENT).session == "PREMARKET"
        assert sc.capability_at(REGULAR_MOMENT).session == "REGULAR"

    def test_G_regular_to_after_hours_without_restart(self):
        from config import session_capability as sc

        assert sc.capability_at(REGULAR_MOMENT).session == "REGULAR"
        assert sc.capability_at(AFTER_HOURS_MOMENT).session == "AFTER_HOURS"

    def test_full_cycle_returns_to_the_next_days_daytime_session(self):
        from config import session_capability as sc

        sequence = [DAYTIME_MOMENT, PREMARKET_MOMENT, REGULAR_MOMENT,
                    AFTER_HOURS_MOMENT, NEXT_DAYTIME_MOMENT]
        sessions = [sc.capability_at(m).session for m in sequence]
        assert sessions == ["OVERNIGHT_DAYTIME", "PREMARKET", "REGULAR",
                             "AFTER_HOURS", "OVERNIGHT_DAYTIME"]
        # The two DAYTIME calls are a full day apart and must not be
        # treated as "the same session still running".
        first_day = sc.capability_at(DAYTIME_MOMENT).trading_day
        next_day = sc.capability_at(NEXT_DAYTIME_MOMENT).trading_day
        assert first_day != next_day

    def test_H_stale_previous_session_watch_file_does_not_block_current_session(
            self, tmp_path, monkeypatch):
        """A leftover watchlist file from a PRIOR session must not be
        read while evaluating the CURRENT one -- `active_watch.path_for`
        is keyed by (session_start_date, session), so a stale file for
        one key is simply never the path resolved for another."""
        from s6_live import active_watch

        monkeypatch.setenv("S6_ACTIVE_WATCH_DIR", str(tmp_path))
        stale_path = active_watch.path_for("2026-08-25", session="OVERNIGHT_DAYTIME")
        stale_path.parent.mkdir(parents=True, exist_ok=True)
        stale_path.write_text('{"poisoned": true}', encoding="utf-8")

        current_path = active_watch.path_for("2026-08-26", session="PREMARKET")
        assert current_path != stale_path
        assert not current_path.exists()


class TestWeekendAndNextTradingDay:
    """Item 9: no separate weekend architecture -- the same pure
    resolution function correctly reports no session/no capability on a
    weekend, and does not need any reset to work again on the next
    trading day."""

    def test_weekend_resolves_closed_not_a_leftover_session(self):
        from config import session_capability as sc

        cap = sc.capability_at(WEEKEND_MOMENT)
        assert cap.entry_supported is False
        assert cap.exit_supported is False

    def test_next_trading_day_after_a_weekend_needs_no_reset(self):
        from config import session_capability as sc

        sc.capability_at(WEEKEND_MOMENT)
        # Monday regular session, same week's Wednesday fixture shifted
        # is not needed -- any later weekday moment proves the point.
        monday_regular = datetime(2026, 8, 31, 14, 30, tzinfo=timezone.utc)
        cap = sc.capability_at(monday_regular)
        assert cap.session == "REGULAR"
        assert cap.entry_supported is True


class TestDaytimeRouteEvidencePersistsWithoutReArming:
    """The actual point of this task: once the sanctioned one-shot
    records real evidence for the daytime BUY/CANCEL wire values,
    ordinary S6 execution must see the route as proven automatically --
    on the very next call, forever, with no per-session or per-release
    re-arming. The bootstrap/verification exception mechanism itself is
    NOT touched or exercised here; this only proves what happens once
    evidence already exists."""

    def _record_all_pending_daytime_values(self, path, *, session="OVERNIGHT_DAYTIME"):
        from brokers import kis_broker as kb
        from brokers import route_evidence

        values = {entry.name: entry.value for entry in kb.VERIFICATION_MATRIX}
        for name in kb.pending_items_for(kb.FAMILY_DAYTIME):
            route_evidence.record(
                name, wire_value=values[name], broker_order_id="0099999999",
                rt_cd="0", status="ACCEPTED", session=session,
                now=datetime(2026, 8, 27, 21, 0, tzinfo=timezone.utc), path=path,
            )

    def test_before_recording_the_daytime_buy_leg_is_still_awaiting(self, tmp_path):
        from brokers import route_evidence
        from config import session_capability as sc

        path = tmp_path / "route_evidence.json"
        pending = route_evidence.pending_items_after_live_evidence(
            sc.evidence_posture_for_family(sc.FAMILY_DAYTIME), path=path)
        assert "daytime_order_tr_id_live_buy" in pending

    def test_after_recording_no_operator_action_is_needed_on_the_next_tick(self, tmp_path):
        from brokers import route_evidence
        from config import session_capability as sc

        path = tmp_path / "route_evidence.json"
        self._record_all_pending_daytime_values(path)

        pending_after = route_evidence.pending_items_after_live_evidence(
            sc.evidence_posture_for_family(sc.FAMILY_DAYTIME), path=path)
        assert pending_after == ()

    def test_evidence_survives_a_simulated_release_switch(self, tmp_path):
        """A deploy swaps `TRADING_PROJECT_ROOT` and starts a fresh
        process with a fresh `STATE_STORE_DB_FILE` value -- but that
        value points at the SAME shared state directory in production
        (`shared/state/TRADING_STATE.db`), never at a per-release path.
        Modelled here as two independent reads of the same file path,
        exactly as two different releases' processes would see it."""
        from brokers import route_evidence
        from config import session_capability as sc

        shared_state_dir = tmp_path / "shared" / "state"
        shared_state_dir.mkdir(parents=True)
        path = shared_state_dir / "route_evidence.json"

        # "Release A" records the evidence.
        self._record_all_pending_daytime_values(path)

        # "Release B" -- a different process, a different release
        # checkout, but the SAME shared path -- reads it fresh.
        pending = route_evidence.pending_items_after_live_evidence(
            sc.evidence_posture_for_family(sc.FAMILY_DAYTIME), path=path)
        assert pending == ()

    def test_recording_does_not_touch_the_static_verification_matrix(self, tmp_path):
        """The matrix itself never gets mutated -- only the evidence
        file does. This is what keeps `brokers/kis_broker.py` a
        hand-reviewed, static source of truth."""
        from brokers import kis_broker as kb

        path = tmp_path / "route_evidence.json"
        before = kb.pending_items_for(kb.FAMILY_DAYTIME)
        self._record_all_pending_daytime_values(path)
        after = kb.pending_items_for(kb.FAMILY_DAYTIME)
        assert before == after  # unchanged: still statically PENDING

    def test_a_rejection_response_records_nothing(self, tmp_path):
        from brokers import route_evidence

        path = tmp_path / "route_evidence.json"
        with pytest.raises(route_evidence.RouteEvidenceRefused):
            route_evidence.record(
                "daytime_order_tr_id_live_buy", wire_value="TTTS6036U",
                broker_order_id="0099999999", rt_cd="1", status="REJECTED",
                session="OVERNIGHT_DAYTIME", path=path,
            )
        assert route_evidence.load(path) == {}


class TestNoNewOperatorPreconditionIntroduced:
    """Item 10/11: this task must not add any NEW manual gate. The only
    manual steps remaining are the four the task explicitly carves out
    (UNKNOWN broker state, reconciliation halt, an actual broker/API
    defect, and the explicit kill switch) -- none of which this file
    touches."""

    def test_session_capability_module_takes_no_new_environment_flag(self):
        """A crude but effective regression: the session-resolution path
        must not gain a dependency on a manual env flag this task didn't
        already have. `LIVE_BOOTSTRAP_ENABLED`/`LIVE_BOOTSTRAP_ACK` are
        the pre-existing ones for the manual one-shot and are
        deliberately allowed to still exist -- this only checks that
        nothing NEW was added to `config/session_capability.py`."""
        import inspect

        from config import session_capability as sc

        source = inspect.getsource(sc)
        # The one-shot's own flags belong to bootstrap_capability.py /
        # live_pilot/bootstrap.py, not to session resolution -- if they
        # ever show up here, session resolution has grown a manual
        # dependency it didn't have before.
        assert "LIVE_BOOTSTRAP_ENABLED" not in source
        assert "LIVE_BOOTSTRAP_ACK" not in source
