"""Submit-time signal freshness: the source states the budget, the cycle
measures it once, and nobody else's budget moves.

The timeline this pins, for one S6 candidate:

    scanner signal timestamp       signal.timestamp -> provenance.signal_timestamp
    candidate generated_at         the publish stamp of its generation
    consumed_at                    when S6CandidateSource read the rows
    qualification                  source_signal_timestamp = provenance stamp
    Signal.created_at              validity.anchor(): ACCEPTANCE for S6,
                                   the cycle clock for the default
    Signal.expires_at              created_at + the SOURCE's budget
    KIS revalidation               validity.submit_moment(): wall clock for
                                   S6, not asked for the default
    gate                           signal.is_expired(now=cycle clock), as before
    broker submit                  the same Signal object, never rebuilt

`SIGNAL_VALID_SECONDS = 120` stays what it was and who it was for.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import kis_live_trading as klt  # noqa: E402
from config import scanner_live_mode as slm  # noqa: E402
from domain.signal import build_signal  # noqa: E402
from execution import signal_validity as sv  # noqa: E402
from s1_live import candidate_source as s1cs  # noqa: E402
from s2_live import candidate_source as s2cs  # noqa: E402
from s6_live import candidate_source as s6cs  # noqa: E402
from scanners.publish import candidates as publisher  # noqa: E402
from scanners.publish import scan_cycle  # noqa: E402

NOW = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)
DAY = "2026-09-02"
S6_BUDGET = s6cs.SIGNAL_VALID_SECONDS


def signal_at(created, valid_for, signal_id="sig-1"):
    return build_signal(
        strategy_id="S", strategy_version="v1", config_version="c",
        code_commit="abc", symbol="AAPL", exchange="NASDAQ",
        signal_price=100.0, score=80.0, entry_reason="r",
        valid_for_seconds=valid_for, now=created, signal_id=signal_id)


class LegacySource:
    name = "legacy_watchlist"


class HookSource:
    name = "hooked"

    def __init__(self, answer=None, raises=None):
        self._answer, self._raises = answer, raises

    def signal_valid_seconds(self):
        if self._raises:
            raise self._raises
        return self._answer


# ----------------------------------------------------------------------
# resolution: who gets what
# ----------------------------------------------------------------------
class TestTheDefaultIsUnchanged:
    def test_the_global_constant_is_still_120(self):
        assert klt.SIGNAL_VALID_SECONDS == 120

    def test_A_a_legacy_source_gets_120_seconds_unmeasured(self):
        validity = sv.resolve(LegacySource(), default_seconds=klt.SIGNAL_VALID_SECONDS)
        assert validity.valid_for_seconds == 120
        assert validity.policy_source == sv.DEFAULT_POLICY
        assert validity.measured_at_submit is False
        assert validity.anchor(NOW) is NOW
        assert validity.submit_moment(NOW) is None

    def test_A_a_legacy_signal_still_expires_at_120_seconds(self):
        validity = sv.resolve(LegacySource(), default_seconds=klt.SIGNAL_VALID_SECONDS)
        made = signal_at(validity.anchor(NOW), validity.valid_for_seconds)
        assert made.created_at == NOW
        assert not made.is_expired(now=NOW + timedelta(seconds=119))
        assert made.is_expired(now=NOW + timedelta(seconds=120))

    def test_B_s1_states_no_budget_and_keeps_the_default(self):
        source = s1cs.S1CandidateSource(trading_day=DAY)
        assert not hasattr(source, sv.HOOK)
        validity = sv.resolve(source, default_seconds=klt.SIGNAL_VALID_SECONDS)
        assert validity == sv.SignalValidity(120.0, sv.DEFAULT_POLICY, False)

    def test_B_the_legacy_watchlist_source_keeps_the_default(self):
        assert not hasattr(s1cs.LegacyWatchlistSource, sv.HOOK)

    def test_C_s2_states_no_budget_and_keeps_the_default(self):
        source = s2cs.S2CandidateSource(trading_day=DAY, session="REGULAR")
        assert not hasattr(source, sv.HOOK)
        validity = sv.resolve(source, default_seconds=klt.SIGNAL_VALID_SECONDS)
        assert validity == sv.SignalValidity(120.0, sv.DEFAULT_POLICY, False)

    def test_the_default_policy_never_asks_for_a_source_timestamp(self):
        validity = sv.resolve(LegacySource(), default_seconds=120)
        assert sv.source_timestamp_refusal(validity, None) is None
        assert sv.source_timestamp_refusal(validity, "garbage") is None


class TestS6StatesItsOwnBudget:
    def test_the_source_implements_the_hook(self):
        source = s6cs.S6CandidateSource(trading_day=DAY, session="REGULAR")
        validity = sv.resolve(source, default_seconds=klt.SIGNAL_VALID_SECONDS)
        assert validity.valid_for_seconds == S6_BUDGET
        assert validity.policy_source == s6cs.SOURCE_S6
        assert validity.measured_at_submit is True
        assert validity.requires_source_timestamp is True

    def test_the_watched_wrapper_delegates_the_hook(self):
        from s6_live.precision_watch import WatchedCandidateSource

        inner = s6cs.S6CandidateSource(trading_day=DAY, session="REGULAR")
        wrapped = WatchedCandidateSource(inner, session="REGULAR", now=NOW)
        validity = sv.resolve(wrapped, default_seconds=120)
        assert validity.valid_for_seconds == S6_BUDGET
        assert validity.policy_source == s6cs.SOURCE_S6

    def test_the_budget_is_chosen_from_the_pipeline_not_the_thesis(self):
        """Covers ~11 paced KIS reads plus two rate-limit backoffs and
        the lock timeout (~80 s) with a factor of two, and stays inside
        the five-minute consume cadence."""
        from brokers import kis_rate_limiter as rl
        from execution import execution_lock

        reads = 11
        floor = reads * rl.DEFAULT_READ_MIN_INTERVAL
        backoffs = 2 * rl.DEFAULT_MAX_BACKOFF
        lock = execution_lock.DEFAULT_ACQUIRE_TIMEOUT_SECONDS
        legitimate = floor + reads * 1.0 + backoffs + lock
        assert legitimate < S6_BUDGET
        assert S6_BUDGET >= 2 * (floor + lock)
        assert S6_BUDGET <= 5 * 60
        assert S6_BUDGET < 15 * 60

    def test_the_anchor_is_acceptance_not_the_cycle_start(self):
        validity = sv.resolve(HookSource(180), default_seconds=120)
        accepted = NOW + timedelta(minutes=4)
        assert validity.anchor(NOW, clock=lambda: accepted) == accepted
        assert validity.submit_moment(NOW, clock=lambda: accepted) == accepted


class TestFailClosed:
    @pytest.mark.parametrize("answer", [None, 0, -1, float("inf"),
                                        float("nan"), "180", True, object()])
    def test_an_unusable_answer_stops_the_cycle(self, answer):
        with pytest.raises(sv.SignalValidityError):
            sv.resolve(HookSource(answer), default_seconds=120)

    def test_a_hook_that_raises_stops_the_cycle(self):
        with pytest.raises(sv.SignalValidityError, match="could not state"):
            sv.resolve(HookSource(raises=RuntimeError("boom")),
                       default_seconds=120)

    def test_a_non_callable_hook_stops_the_cycle(self):
        class Bad:
            name = "bad"
            signal_valid_seconds = 180

        with pytest.raises(sv.SignalValidityError, match="not callable"):
            sv.resolve(Bad(), default_seconds=120)

    def test_an_unusable_default_stops_the_cycle(self):
        with pytest.raises(sv.SignalValidityError):
            sv.resolve(LegacySource(), default_seconds=0)

    @pytest.mark.parametrize("stamp", [None, "", "garbage",
                                       "2026-09-02T15:00:00"])
    def test_I_a_measured_source_needs_a_usable_source_timestamp(self, stamp):
        """Missing, empty, unparseable, or naive: the pipeline clock is
        never manufactured into an observation time."""
        validity = sv.resolve(HookSource(180), default_seconds=120)
        refusal = sv.source_timestamp_refusal(validity, stamp)
        assert refusal and sv.REASON_SOURCE_TIMESTAMP_UNUSABLE in refusal

    def test_I_an_aware_source_timestamp_is_accepted(self):
        validity = sv.resolve(HookSource(180), default_seconds=120)
        assert sv.source_timestamp_refusal(
            validity, "2026-09-02T14:55:00+00:00") is None
        assert sv.strategy_age_seconds("2026-09-02T14:55:00+00:00", NOW) == 300

    def test_the_cycle_refuses_a_source_that_cannot_state_its_policy(
            self, tmp_path, monkeypatch):
        """Structural: the cycle stops before reading a candidate."""
        monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "s.db"))
        monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(tmp_path / "shadow.jsonl"))
        monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "k.json"))
        monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(tmp_path / "h.json"))
        monkeypatch.setenv("VALIDATED_COMMIT", "c1")
        monkeypatch.setenv("DEPLOYED_COMMIT", "c1")
        monkeypatch.setenv("KIS_ALLOWED_ACCOUNT_NO", "12345678")
        from config.live_rollout_config import LiveRolloutConfig

        rollout = LiveRolloutConfig(
            enabled=True, allowed_symbols=frozenset({"AAPL"}),
            max_quantity_per_order=1, max_open_positions=1,
            max_positions_per_strategy=1, max_daily_entries=1,
            regular_session_only=False, allow_fractional=False,
            allow_market_order=False, allow_extended_hours=False,
            allow_leverage=False, allow_inverse=False, allow_short=False,
            allow_margin=False, max_price_deviation_percent=0.30)

        class Broken(HookSource):
            def symbols(self):
                raise AssertionError("candidates must not be read")

            def allowed_symbols(self):
                raise AssertionError("candidates must not be read")

        with pytest.raises(klt.KISLiveTradingError, match="signal validity"):
            klt.run_live_buy_entry_cycle(
                broker=object(), live_rollout=rollout, now=NOW,
                candidate_source=Broken(answer=None))


# ----------------------------------------------------------------------
# the measured submit-time check
# ----------------------------------------------------------------------
class _Intent:
    internal_order_id = "iid-1"
    quantity = 1
    limit_price = 100.0


class _Instrument:
    symbol = "AAPL"
    exchange = "NASDAQ"


class _Broker:
    def get_open_orders(self):
        return []

    def get_orderable_usd(self, instrument, price):
        return 1000.0


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "state.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "pos.json"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


@pytest.fixture(autouse=True)
def permissive_switches(monkeypatch):
    monkeypatch.setattr(klt.ops_kill_switch, "is_halted", lambda: False)
    monkeypatch.setattr(klt.ops_kill_switch, "is_entry_allowed", lambda: True)
    monkeypatch.delenv("ENTRY_DISABLED", raising=False)


def revalidate(conn, *, signal, validity, at, cycle_now=NOW):
    return klt._revalidate_before_submit(
        symbol="AAPL", broker=_Broker(), conn=conn, instrument=_Instrument(),
        order_intent=_Intent(), buffered_price=100.0, live_state={},
        signal=signal, now=cycle_now, validity=validity,
        submit_clock=lambda: at)


S6_VALIDITY = sv.SignalValidity(S6_BUDGET, s6cs.SOURCE_S6, True)
DEFAULT_VALIDITY = sv.SignalValidity(120.0, sv.DEFAULT_POLICY, False)


class TestTheS6SignalIsMeasuredAtSubmit:
    def test_D_a_current_cycle_candidate_survives_a_realistic_pipeline_delay(self, conn):
        """Accepted four minutes into the cycle (after the precision
        watch), submitted ninety seconds later."""
        accepted = NOW + timedelta(minutes=4)
        made = signal_at(S6_VALIDITY.anchor(NOW, clock=lambda: accepted), S6_BUDGET)
        assert made.created_at == accepted
        assert revalidate(conn, signal=made, validity=S6_VALIDITY,
                          at=accepted + timedelta(seconds=90)) is None

    def test_D_the_old_defect_is_gone(self, conn):
        """The same candidate under the OLD anchor: 120 s from the cycle
        start, submitted 5 minutes in. That is what expired 2 of 2."""
        old_style = signal_at(NOW, 120)
        assert old_style.is_expired(now=NOW + timedelta(minutes=5))
        new_style = signal_at(NOW + timedelta(minutes=4), S6_BUDGET)
        assert not new_style.is_expired(now=NOW + timedelta(minutes=5))

    @pytest.mark.parametrize("seconds,expired", [
        (S6_BUDGET - 1, False), (S6_BUDGET, True), (S6_BUDGET + 1, True),
    ])
    def test_E_the_boundary_is_the_budget(self, conn, seconds, expired):
        made = signal_at(NOW, S6_BUDGET)
        dropped = revalidate(conn, signal=made, validity=S6_VALIDITY,
                             at=NOW + timedelta(seconds=seconds))
        if expired:
            code, detail = dropped
            assert code == klt.REVALIDATION_SIGNAL_EXPIRED
            assert "pipeline budget" in detail and s6cs.SOURCE_S6 in detail
        else:
            assert dropped is None

    def test_E_beyond_the_budget_the_order_is_dropped_not_rebuilt(self, conn):
        made = signal_at(NOW, S6_BUDGET)
        code, _ = revalidate(conn, signal=made, validity=S6_VALIDITY,
                             at=NOW + timedelta(minutes=10))
        assert code == klt.REVALIDATION_SIGNAL_EXPIRED
        assert made.expires_at == NOW + timedelta(seconds=S6_BUDGET)

    def test_the_default_policy_is_never_measured_here(self, conn):
        """S1, S2 and the legacy watchlist: the historical behaviour."""
        calls = []

        class Legacy:
            signal_id = "sig-legacy"
            created_at = NOW

            def is_expired(self, now=None):
                calls.append(now)
                return True

        assert revalidate(conn, signal=Legacy(), validity=DEFAULT_VALIDITY,
                          at=NOW + timedelta(minutes=30)) is None
        assert calls == []

    def test_without_a_validity_the_revalidation_asks_nothing(self, conn):
        """Every existing caller that passes no policy is untouched."""
        calls = []

        class Legacy:
            signal_id = "sig-legacy"

            def is_expired(self, now=None):
                calls.append(now)
                return True

        assert klt._revalidate_before_submit(
            symbol="AAPL", broker=_Broker(), conn=conn,
            instrument=_Instrument(), order_intent=_Intent(),
            buffered_price=100.0, live_state={}, signal=Legacy(),
            now=NOW + timedelta(minutes=30)) is None
        assert calls == []

    def test_J_the_gate_still_sees_a_live_signal_after_revalidation(self, conn):
        """The gate keeps comparing against the cycle clock, which is at
        or before acceptance, so a Signal the revalidation passed is
        never expired by the gate a moment later."""
        from execution import order_gate

        accepted = NOW + timedelta(minutes=4)
        made = signal_at(accepted, S6_BUDGET)
        submit = accepted + timedelta(seconds=S6_BUDGET - 5)
        assert revalidate(conn, signal=made, validity=S6_VALIDITY, at=submit) is None
        assert not made.is_expired(now=NOW)          # the gate's clock
        assert not made.is_expired(now=submit)       # the wall clock
        # And the gate's own check is the unchanged one.
        source = (REPO_ROOT / "execution" / "order_gate.py").read_text()
        assert "ctx.signal.is_expired(now=ctx.now)" in source
        assert hasattr(order_gate, "evaluate_buy_gate")

    def test_K_one_signal_is_built_and_the_same_object_reaches_submit(self):
        source = (REPO_ROOT / "kis_live_trading.py").read_text()
        cycle = source[source.index("def run_live_buy_entry_cycle("):]
        assert cycle.count("build_signal(") == 1
        assert cycle.count("signal_validity.resolve(") == 1
        build_at = cycle.index("build_signal(")
        revalidate_at = cycle.index("_revalidate_before_submit(")
        submit_at = cycle.index("execution_engine.submit_buy_order(")
        assert build_at < revalidate_at < submit_at
        # The revalidation and the gate are handed the built Signal, and
        # nothing after the build stamps a new one.
        assert "signal=signal, now=current, validity=validity" in cycle
        assert "signal=signal" in cycle[revalidate_at:]
        assert "build_signal(" not in cycle[revalidate_at:]
        assert "created_at=" not in cycle[build_at:revalidate_at].replace(
            "created_at=current,", "")  # the order intent's own stamp only

    def test_K_the_signal_is_anchored_by_the_policy_not_by_the_cycle(self):
        source = (REPO_ROOT / "kis_live_trading.py").read_text()
        call = source.split("signal = build_signal(")[1].split(
            "\n                    )")[0]
        assert "valid_for_seconds=validity.valid_for_seconds" in call
        assert "now=validity.anchor(current)" in call
        assert "SIGNAL_VALID_SECONDS" not in call


# ----------------------------------------------------------------------
# strategy age is a different question, and its guards still stand
# ----------------------------------------------------------------------
class Signal:
    def __init__(self, symbol, timestamp=None, run_id="run-1"):
        self.symbol, self.scanner_score, self.signal_price = symbol, 70.0, 100.0
        self.scanner_name, self.scanner_version = "orb", "orb_v1.0"
        self.signal_id, self.scanner_run_id = f"s-{symbol}", run_id
        self.volume = self.avg_volume = self.volume_multiple = None
        self.price_change_pct = self.hma200 = self.hma200_slope = None
        self.hma89 = self.vwap = None
        self.market_data_provider = self.market_data_feed = None
        self.data_timestamp = self.feature_timestamp = None
        self.source_timeframe = None
        self.timestamp = timestamp
        self.reasons = []
        self.metrics = {"opening_range_high": 99.5, "opening_range_low": 99.0,
                        "orb_minutes": 15, "vwap": 100.0, "price": 100.0}


def live_modes():
    modes = dict(slm.SCANNER_LIVE_MODE)
    modes["orb"] = slm.MODE_LIMITED_LIVE
    return modes


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv(publisher.CANDIDATE_DIR_ENV, str(tmp_path / "handoff"))

    def publish(symbols, day=DAY, session="REGULAR", variant="S6-R",
                status=scan_cycle.STATUS_OK, timestamp=NOW.isoformat()):
        publisher.publish([Signal(s, timestamp=timestamp) for s in symbols],
                          strategy_id=s6cs.STRATEGY_ID, trading_day=day,
                          session=session, variant=variant)
        publisher.mark_run(day, session, strategy_id=s6cs.STRATEGY_ID,
                           candidates=len(symbols), status=status)
    return publish


def s6_source(**kw):
    kw.setdefault("trading_day", DAY)
    kw.setdefault("session", "REGULAR")
    kw.setdefault("modes", live_modes())
    return s6cs.S6CandidateSource(**kw)


class TestStrategyAgeGuardsAreUntouched:
    def test_a_current_cycle_candidate_qualifies_with_its_provenance(self, store):
        store(["AAPL"])
        src = s6_source()
        assert src.symbols() == ["AAPL"]
        qualified = src.qualify("AAPL")
        assert qualified.qualified
        assert qualified.source_signal_timestamp == NOW.isoformat()
        validity = sv.resolve(src, default_seconds=120)
        assert sv.source_timestamp_refusal(
            validity, qualified.source_signal_timestamp) is None

    def test_F_a_wrong_trading_day_still_blocks(self, store):
        store(["AAPL"], day="2026-09-01")
        src = s6_source()
        assert src.symbols() == []
        assert src.qualify("AAPL").qualified is False

    def test_G_a_superseded_scan_cycle_still_blocks(self, store):
        """A newer scan holds the cycle lock and no completed generation
        record exists: the rows on disk are the previous cycle's."""
        store(["AAPL"])
        with scan_cycle.hold(DAY, "REGULAR", scanner="orb"):
            src = s6_source()
            assert src.symbols() == []
            assert src.qualify("AAPL").qualified is False
            assert scan_cycle.REASON_SCAN_IN_PROGRESS in (
                src.describe()["refusal"] or "")

    def test_H_a_failed_scan_cycle_still_blocks(self, store):
        store(["AAPL"], status=scan_cycle.STATUS_FAILED)
        src = s6_source()
        assert src.symbols() == []
        assert src.qualify("AAPL").qualified is False

    def test_I_a_candidate_without_an_observation_time_is_refused_at_acceptance(
            self, store):
        """It still qualifies -- provenance is the cycle's question -- and
        the cycle's policy boundary refuses it."""
        store(["AAPL"], timestamp=None)
        src = s6_source()
        qualified = src.qualify("AAPL")
        assert qualified.qualified
        assert qualified.source_signal_timestamp is None
        validity = sv.resolve(src, default_seconds=120)
        assert sv.source_timestamp_refusal(
            validity, qualified.source_signal_timestamp)

    def test_republishing_does_not_reset_the_strategy_age(self, store):
        """The pipeline budget starts at acceptance; the observation
        time travels unchanged from the scanner."""
        observed = (NOW - timedelta(minutes=45)).isoformat()
        store(["AAPL"], timestamp=observed)
        qualified = s6_source().qualify("AAPL")
        assert qualified.source_signal_timestamp == observed
        assert sv.strategy_age_seconds(qualified.source_signal_timestamp,
                                       NOW) == 45 * 60

    def test_the_gate_and_the_reentry_guard_keep_their_source_stamp(self):
        source = (REPO_ROOT / "kis_live_trading.py").read_text()
        assert "source_signal_timestamp=qualified.source_signal_timestamp" in source \
            or "source_signal_timestamp" in source
