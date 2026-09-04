"""One real DAYTIME order, placed to prove a route rather than to trade.

Why a second one-shot exists
----------------------------
`TTTS6036U` has never carried a buy and can only be confirmed by one. The
bootstrap cannot supply it: its candidate must be a genuine published
strategy row, and S6-O produced none across a five-hour daytime window
(595 of 595 symbols short of the data to form an opening range). Waiting
for the strategy to offer a candidate is not a route to verification.

So this is the other half, and it differs from the bootstrap at every
point that matters: an explicitly named symbol, a price chosen NOT to
fill, and an account left flat rather than a position opened.

The property that carries the risk
----------------------------------
A fill must never be unowned. SLGN on 2026-09-03 filled 3 @ 41.61 into a
row already closed BUY_NEVER_FILLED, and the shares sat with no stop
until a human noticed. The answer here is not an exit engine for a
position nobody wants -- it is to not hold one. An unexpected fill is
flattened on TTTS6037U, and only a FAILED flatten hands the remainder to
S6's exit monitor, marked so it is managed without entering S6's
performance record.
"""

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from brokers import kis_broker as kb  # noqa: E402
from config import session_capability as sc  # noqa: E402
from execution import route_verification as rv  # noqa: E402
from execution.order_gate import (  # noqa: E402
    ROUTE_UNVERIFIED, BuyGateContext, OrderGateBlockedError, evaluate_buy_gate,
)
from live_pilot import route_verification_runner as runner  # noqa: E402

from tests import test_order_gate as fixtures  # noqa: E402

DAYTIME = "OVERNIGHT_DAYTIME"
SYMBOL = "AAPL"
NOW = datetime(2026, 9, 5, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv(rv.FLAG_ENABLED, "true")
    monkeypatch.setenv(rv.FLAG_ACK, "true")
    return monkeypatch


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _capability(**overrides):
    kwargs = dict(
        mode=rv.MODE_ROUTE_VERIFICATION, symbol=SYMBOL,
        side=rv.VERIFICATION_SIDE, quantity=rv.VERIFICATION_QUANTITY,
        order_type=rv.VERIFICATION_ORDER_TYPE, session=rv.VERIFICATION_SESSION,
        allowed_symbols=frozenset({SYMBOL}), token="t" * 32)
    kwargs.update(overrides)
    return rv.RouteVerificationCapability(**kwargs)


def _ctx(session=DAYTIME, capability=None, intent_overrides=None, **overrides):
    intent = dict(session=session, symbol=SYMBOL, side="buy", quantity=1,
                  order_type="limit")
    intent.update(intent_overrides or {})
    kwargs = dict(order_intent=fixtures._order_intent(**intent),
                  allowed_symbols=frozenset({SYMBOL}))
    if capability is not None:
        kwargs["route_verification_capability"] = capability
    kwargs.update(overrides)
    return fixtures._buy_ctx(**kwargs)


def _blocked(ctx):
    with pytest.raises(OrderGateBlockedError) as caught:
        evaluate_buy_gate(ctx)
    return caught.value


def _detail(last=41.61, low=41.20, tick=0.01, orderable="매매 가능"):
    return {"last": last, "low": low, "tick_size": tick,
            "orderable_text": orderable}


# --- the capability is narrow ------------------------------------------

class TestOnlyTheDaytimeBuyIsAuthorised:
    def test_the_capability_opens_the_daytime_route(self, armed):
        assert evaluate_buy_gate(_ctx(capability=_capability())) is True

    def test_without_it_the_daytime_route_is_still_refused(self, armed):
        assert _blocked(_ctx()).code == ROUTE_UNVERIFIED

    @pytest.mark.parametrize("session", ["PREMARKET", "REGULAR", "AFTER_HOURS"])
    def test_it_cannot_excuse_a_general_route(self, armed, session, monkeypatch):
        """Proven by making GENERAL unverified too: the capability must
        not rescue it."""
        monkeypatch.setattr(sc, "route_awaiting_live_evidence", lambda s: True)
        assert _blocked(_ctx(session=session,
                             capability=_capability())).code == ROUTE_UNVERIFIED

    def test_a_capability_naming_another_session_is_refused(self, armed):
        assert _blocked(_ctx(
            capability=_capability(session="REGULAR"))).code == ROUTE_UNVERIFIED

    def test_wrong_symbol_is_refused(self, armed):
        assert _blocked(_ctx(capability=_capability(
            symbol="MSFT", allowed_symbols=frozenset({"MSFT"})))).code == ROUTE_UNVERIFIED

    def test_wrong_quantity_is_refused(self, armed):
        assert _blocked(_ctx(capability=_capability(quantity=2),
                             intent_overrides={"quantity": 2})).code == ROUTE_UNVERIFIED

    def test_an_order_the_capability_does_not_describe_is_refused(self, armed):
        assert _blocked(_ctx(capability=_capability(),
                             intent_overrides={"quantity": 5})).code == ROUTE_UNVERIFIED

    def test_a_sell_never_reaches_the_exception(self, armed):
        ctx = _ctx(capability=_capability(side="sell"),
                   intent_overrides={"side": "sell"})
        try:
            evaluate_buy_gate(ctx)
        except OrderGateBlockedError as exc:
            assert exc.code != ROUTE_UNVERIFIED

    def test_a_disarmed_environment_cannot_submit(self, armed):
        armed.setenv(rv.FLAG_ACK, "false")
        assert _blocked(_ctx(capability=_capability())).code == ROUTE_UNVERIFIED

    def test_a_look_alike_object_is_refused(self, armed):
        class _LooksRight:
            mode = rv.MODE_ROUTE_VERIFICATION
            symbol, side, quantity = SYMBOL, "buy", 1
            order_type, session = "limit", DAYTIME
            allowed_symbols = frozenset({SYMBOL})
            token = "t" * 32

            def describes(self, _intent):
                return True

        assert _blocked(_ctx(capability=_LooksRight())).code == ROUTE_UNVERIFIED

    def test_the_bootstrap_flags_do_not_arm_this(self, monkeypatch):
        """Dedicated flags. Arming one must never arm the other."""
        monkeypatch.setenv("LIVE_BOOTSTRAP_ENABLED", "true")
        monkeypatch.setenv("LIVE_BOOTSTRAP_ACK", "true")
        monkeypatch.delenv(rv.FLAG_ENABLED, raising=False)
        monkeypatch.delenv(rv.FLAG_ACK, raising=False)
        assert _blocked(_ctx(capability=_capability())).code == ROUTE_UNVERIFIED

    def test_minting_requires_exactly_one_allow_listed_symbol(self, armed):
        with pytest.raises(rv.RouteVerificationError):
            rv.mint(symbol=SYMBOL, allowed_symbols={SYMBOL, "MSFT"})
        with pytest.raises(rv.RouteVerificationError):
            rv.mint(symbol=SYMBOL, allowed_symbols=set())
        with pytest.raises(rv.RouteVerificationError):
            rv.mint(symbol="MSFT", allowed_symbols={SYMBOL})

    def test_minting_requires_both_flags(self, monkeypatch):
        monkeypatch.setenv(rv.FLAG_ENABLED, "true")
        monkeypatch.delenv(rv.FLAG_ACK, raising=False)
        with pytest.raises(rv.RouteVerificationError):
            rv.mint(symbol=SYMBOL, allowed_symbols={SYMBOL})

    def test_a_minted_capability_has_the_fixed_shape(self, armed):
        cap = rv.mint(symbol="aapl", allowed_symbols={"AAPL"})
        assert (cap.side, cap.quantity, cap.order_type, cap.session) == \
            ("buy", 1, "limit", DAYTIME)
        assert cap.symbol == "AAPL" and cap.token


# --- the price comes from KIS's own facts -------------------------------

class TestThePriceIsNotInvented:
    def test_it_is_two_ticks_under_the_lower_of_last_and_today_low(self):
        assert rv.limit_price_from(_detail(last=41.61, low=41.20, tick=0.01)) \
            == pytest.approx(41.18)

    def test_the_reference_is_the_lower_of_the_two(self):
        assert rv.limit_price_from(_detail(last=41.20, low=41.61, tick=0.01)) \
            == pytest.approx(41.18)

    def test_the_offset_is_two_ticks(self):
        assert rv.OFFSET_TICKS == 2
        assert rv.limit_price_from(_detail(last=50.0, low=50.0, tick=0.05)) \
            == pytest.approx(49.90)

    @pytest.mark.parametrize("missing", ["last", "low", "tick_size"])
    def test_a_missing_fact_refuses(self, missing):
        detail = _detail()
        detail.pop(missing)
        with pytest.raises(rv.VerificationPriceUnavailable):
            rv.limit_price_from(detail)

    @pytest.mark.parametrize("bad", [0, -1, "n/a", None, float("nan")])
    def test_an_unusable_fact_refuses(self, bad):
        with pytest.raises(rv.VerificationPriceUnavailable):
            rv.limit_price_from(_detail(last=bad))

    def test_a_non_orderable_instrument_refuses(self):
        with pytest.raises(rv.VerificationPriceUnavailable):
            rv.limit_price_from(_detail(orderable="거래정지"))
        with pytest.raises(rv.VerificationPriceUnavailable):
            rv.limit_price_from(_detail(orderable=""))

    def test_a_price_that_would_go_non_positive_refuses(self):
        with pytest.raises(rv.VerificationPriceUnavailable):
            rv.limit_price_from(_detail(last=0.01, low=0.01, tick=0.01))

    def test_no_percentage_offset_exists_in_the_module(self):
        """The rule is ticks. A percentage would be a guess about a price
        band nobody has documented."""
        source = (REPO_ROOT / "execution" / "route_verification.py").read_text()
        code = "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith(("#", '"""', "*")))
        for invented in ("0.95", "* 0.9", "percent", "DISCOUNT_PCT", "PCT"):
            assert invented not in code
        assert rv.OFFSET_TICKS == 2

    def test_the_result_is_wire_normalised(self):
        """KIS refuses more than two decimals at >=$1 (APTR0057), and
        subtracting ticks can produce them."""
        price = rv.limit_price_from(_detail(last=10.0, low=10.0, tick=0.0001))
        assert len(str(price).split(".")[-1]) <= 2


# --- ownership: flat by intent, managed only on failure -----------------

class TestAnUnflattenedFillBecomesManaged:
    def test_it_is_recorded_with_the_real_quantity_and_basis(self, conn):
        position_id = runner.adopt_exposure(
            conn, symbol="SLGN", quantity=3, basis=41.61,
            broker_order_id="0030974162", client_order_id="rtverify-SLGN-x",
            now=NOW)
        from s6_live import position_store

        row = position_store.load(conn, position_id)
        assert row["quantity"] == 3
        assert row["entry_price"] == pytest.approx(41.61)
        assert row["status"] == "OPEN"

    def test_it_carries_the_route_verification_marker(self, conn):
        position_id = runner.adopt_exposure(
            conn, symbol="SLGN", quantity=3, basis=41.61,
            broker_order_id="x", client_order_id="y", now=NOW)
        from s6_live import position_store

        assert rv.is_route_verification(position_store.load(conn, position_id))

    def test_s6_exit_monitoring_can_see_it(self, conn):
        """The whole point of the fallback: it must be MANAGED."""
        from s6_live import position_store

        runner.adopt_exposure(conn, symbol="SLGN", quantity=3, basis=41.61,
                              broker_order_id="x", client_order_id="y", now=NOW)
        live = position_store.load_live(conn)
        assert [symbol for _pid, row in live for symbol in [row["symbol"]]] == ["SLGN"]

    def test_it_is_excluded_from_s6_performance(self, conn):
        from s6_live import position_store

        runner.adopt_exposure(conn, symbol="SLGN", quantity=3, basis=41.61,
                              broker_order_id="x", client_order_id="y", now=NOW)
        ordinary = position_store.record_submission(
            conn, symbol="DT", variant="S6-R", entry_session="REGULAR",
            client_order_id="kislive-DT-1", now=NOW)
        position_store.open_from_fill(conn, ordinary, quantity=1,
                                      average_fill_price=50.0, now=NOW)
        rows = [row for _pid, row in position_store.load_live(conn)]
        counted = rv.exclude_from_performance(rows)
        assert [r["symbol"] for r in counted] == ["DT"]

    def test_an_ordinary_s6_row_is_never_treated_as_verification(self, conn):
        from s6_live import position_store

        pid = position_store.record_submission(
            conn, symbol="SLGN", variant="S6-R", entry_session="REGULAR",
            client_order_id="kislive-SLGN-1", now=NOW)
        assert not rv.is_route_verification(position_store.load(conn, pid))

    def test_an_unusable_basis_refuses_rather_than_inventing_one(self, conn):
        from s6_live.position_store import S6PositionError

        with pytest.raises(S6PositionError):
            runner.adopt_exposure(conn, symbol="SLGN", quantity=3, basis=0,
                                  broker_order_id="x", client_order_id="y",
                                  now=NOW)


# --- one of each verb, ever ---------------------------------------------

class _Broker:
    def __init__(self):
        self.submits, self.cancels = [], []

    def submit_order(self, order_intent, instrument, *a, **k):
        self.submits.append(order_intent)
        return "ok"

    def cancel_order(self, *a, **k):
        self.cancels.append(a)
        return "ok"


class TestTheTransportBudget:
    def _intent(self, side="buy", quantity=1):
        return fixtures._order_intent(side=side, quantity=quantity,
                                      symbol=SYMBOL, session=DAYTIME)

    def test_one_buy_only(self):
        guard = runner._TransportBudget(_Broker())
        guard.submit_order(self._intent(), object())
        with pytest.raises(runner.RouteVerificationBlocked):
            guard.submit_order(self._intent(), object())

    def test_one_cancel_only(self):
        guard = runner._TransportBudget(_Broker())
        guard.cancel_order()
        with pytest.raises(runner.RouteVerificationBlocked):
            guard.cancel_order()

    def test_one_flatten_only(self):
        guard = runner._TransportBudget(_Broker())
        guard.submit_order(self._intent(side="sell", quantity=3), object())
        with pytest.raises(runner.RouteVerificationBlocked):
            guard.submit_order(self._intent(side="sell", quantity=3), object())

    def test_a_flatten_does_not_consume_the_buy_budget(self):
        """They are separate verbs: a fill must always be flattenable."""
        guard = runner._TransportBudget(_Broker())
        guard.submit_order(self._intent(), object())
        guard.submit_order(self._intent(side="sell", quantity=1), object())
        assert guard.submit_calls == 1 and guard.flatten_calls == 1

    def test_a_buy_of_more_than_one_share_is_refused(self):
        guard = runner._TransportBudget(_Broker())
        with pytest.raises(runner.RouteVerificationBlocked):
            guard.submit_order(self._intent(quantity=2), object())


# --- structure -----------------------------------------------------------

class TestTheShapeOfTheChange:
    def test_execution_engine_remains_the_only_submit_boundary(self):
        """The runner must never call broker.submit_order directly."""
        source = (REPO_ROOT / "live_pilot" / "route_verification_runner.py").read_text()
        assert "self._broker.submit_order" in source  # the guard proxy only
        assert source.count("broker.submit_order(") <= 2

    def test_the_order_intent_stamps_the_session(self):
        from domain.instrument import build_instrument

        intent = runner.build_intent(
            symbol=SYMBOL, instrument=build_instrument(SYMBOL, exchange="NASDAQ"),
            limit_price=41.18, now=NOW)
        assert intent.session == DAYTIME
        assert intent.side == "buy" and intent.quantity == 1
        assert intent.order_type == "limit"

    def test_it_is_not_attributed_to_s6(self):
        assert runner.VERIFICATION_STRATEGY_ID != "S6_ORB_BREAKOUT_V1"
        assert "ROUTE_VERIFICATION" in runner.VERIFICATION_STRATEGY_ID

    def test_the_gate_field_defaults_to_absent(self):
        assert BuyGateContext.__dataclass_fields__[
            "route_verification_capability"].default is None

    def test_no_generic_bypass_boolean_exists(self):
        source = (REPO_ROOT / "execution" / "order_gate.py").read_text()
        code = "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith(("#", '"""', "*")))
        for forbidden in ("allow_unverified_route", "skip_route_evidence"):
            assert forbidden not in code

    def test_nothing_is_marked_verified_by_permitting_the_order(self, armed):
        before = set(kb.pending_items_for(
            sc.evidence_posture_for_family(kb.FAMILY_DAYTIME)))
        evaluate_buy_gate(_ctx(capability=_capability()))
        after = set(kb.pending_items_for(
            sc.evidence_posture_for_family(kb.FAMILY_DAYTIME)))
        assert before == after
        assert sc.route_awaiting_live_evidence(DAYTIME) is True

    def test_the_ordinary_s6_path_is_unchanged(self, armed):
        """No capability anywhere near an ordinary entry."""
        for session in ("REGULAR", "PREMARKET", "AFTER_HOURS"):
            assert evaluate_buy_gate(_ctx(session=session)) is True


# --- the orchestration, branch by branch --------------------------------

class _Position:
    def __init__(self, symbol, quantity, avg=41.61):
        self.symbol, self.quantity, self.average_fill_price = symbol, quantity, avg


class _OrchestratorBroker:
    """Enough KIS for the branches: price detail, open orders, positions."""

    def __init__(self, *, open_orders=(), positions=(), submit=None,
                 cancel=None, detail=None):
        self._open = list(open_orders)
        self._positions = list(positions)
        self._submit, self._cancel = submit, cancel
        self._detail = detail or _detail()
        self.submitted, self.cancelled = [], []
        self.config = type("C", (), {"account_no": "12345678"})()

    def get_price_detail(self, instrument):
        if isinstance(self._detail, Exception):
            raise self._detail
        return self._detail

    def get_open_orders(self):
        return [{"pdno": s, "odno": "0000009001"} for s in self._open]

    def get_positions(self):
        return list(self._positions)

    def submit_order(self, order_intent, instrument, *a, **k):
        self.submitted.append(order_intent)
        if isinstance(self._submit, Exception):
            raise self._submit
        return self._submit

    def cancel_order(self, *a, **k):
        self.cancelled.append(a)
        if isinstance(self._cancel, Exception):
            raise self._cancel
        return self._cancel


def _run(monkeypatch, conn, broker, *, symbols=(SYMBOL,), session=DAYTIME):
    from config import session_capability as scap

    monkeypatch.setattr(scap, "route_session", lambda **k: session)
    return runner.run_route_verification(
        broker=broker, conn=conn, allowed_symbols=symbols,
        account_id="12345678", now=NOW, env=dict(os.environ))


class TestOrchestrationPreconditions:
    def test_a_non_daytime_window_blocks_before_any_transport(
            self, armed, conn, monkeypatch):
        broker = _OrchestratorBroker()
        with pytest.raises(runner.RouteVerificationBlocked) as caught:
            _run(monkeypatch, conn, broker, session="REGULAR")
        assert "NOT_DAYTIME_WINDOW" in caught.value.reason_codes
        assert broker.submitted == []

    @pytest.mark.parametrize("symbols", [(), (SYMBOL, "MSFT")])
    def test_the_allow_list_must_hold_exactly_one(self, armed, conn,
                                                  monkeypatch, symbols):
        broker = _OrchestratorBroker()
        with pytest.raises(runner.RouteVerificationBlocked) as caught:
            _run(monkeypatch, conn, broker, symbols=symbols)
        assert "ALLOWLIST_NOT_EXACTLY_ONE" in caught.value.reason_codes
        assert broker.submitted == []

    def test_an_unusable_price_blocks_before_any_transport(
            self, armed, conn, monkeypatch):
        broker = _OrchestratorBroker(detail=_detail(orderable="거래정지"))
        with pytest.raises(runner.RouteVerificationBlocked) as caught:
            _run(monkeypatch, conn, broker)
        assert "PRICE_NOT_ESTABLISHED" in caught.value.reason_codes
        assert broker.submitted == []

    def test_an_unreadable_price_blocks_before_any_transport(
            self, armed, conn, monkeypatch):
        broker = _OrchestratorBroker(detail=RuntimeError("KIS down"))
        with pytest.raises(runner.RouteVerificationBlocked) as caught:
            _run(monkeypatch, conn, broker)
        assert "PRICE_DETAIL_UNAVAILABLE" in caught.value.reason_codes
        assert broker.submitted == []

    def test_disarmed_flags_block_at_the_mint(self, conn, monkeypatch):
        monkeypatch.delenv(rv.FLAG_ENABLED, raising=False)
        monkeypatch.delenv(rv.FLAG_ACK, raising=False)
        broker = _OrchestratorBroker()
        with pytest.raises(rv.RouteVerificationError):
            _run(monkeypatch, conn, broker)
        assert broker.submitted == []


class TestDisarmIsAtomicAndScoped:
    def _env_file(self, tmp_path):
        env = tmp_path / "env" / "kis-readonly.env"
        env.parent.mkdir(parents=True)
        (tmp_path / "backups").mkdir()
        env.write_text(
            "KIS_LIVE_ORDER_ENABLED=true\n"
            f"{rv.FLAG_ENABLED}=true\n{rv.FLAG_ACK}=true\n"
            f"{runner.ALLOWLIST_KEY}=AAPL\nLIVE_ROLLOUT_ENABLED=true\n")
        return env

    def test_it_turns_both_flags_off_and_clears_the_allow_list(self, tmp_path):
        env = self._env_file(tmp_path)
        runner.disarm(env, now=NOW)
        text = env.read_text()
        assert f"{rv.FLAG_ENABLED}=false" in text
        assert f"{rv.FLAG_ACK}=false" in text
        assert f"{runner.ALLOWLIST_KEY}=\n" in text or \
            text.rstrip().endswith(f"{runner.ALLOWLIST_KEY}=")

    def test_it_leaves_unrelated_flags_alone(self, tmp_path):
        env = self._env_file(tmp_path)
        runner.disarm(env, now=NOW)
        text = env.read_text()
        assert "KIS_LIVE_ORDER_ENABLED=true" in text
        assert "LIVE_ROLLOUT_ENABLED=true" in text

    def test_it_writes_a_backup(self, tmp_path):
        env = self._env_file(tmp_path)
        result = runner.disarm(env, now=NOW)
        assert result["disarmed"] is True
        assert result["backup"] and Path(result["backup"]).exists()

    def test_a_disarmed_file_can_no_longer_mint(self, tmp_path):
        env = self._env_file(tmp_path)
        runner.disarm(env, now=NOW)
        mapping = dict(
            line.split("=", 1) for line in env.read_text().splitlines() if "=" in line)
        with pytest.raises(rv.RouteVerificationError):
            rv.mint(symbol=SYMBOL, allowed_symbols={SYMBOL}, env=mapping)


class _Executed:
    def __init__(self, broker_order_id="0000009001", status="ACCEPTED"):
        self.broker_order_id, self.status = broker_order_id, status
        self.internal_order_id = "rtverify-AAPL-x"


def _stub_engine(monkeypatch, *, buy=None, buy_raises=None, sell=None,
                 sell_raises=None, cancel_raises=None):
    """Stub the ENGINE, not the broker: the orchestration must be shown to
    branch on what KIS says, without a real submit."""
    from execution import execution_engine
    from live_pilot import bootstrap

    def _buy(**kwargs):
        if buy_raises:
            raise buy_raises
        return buy or _Executed()

    def _sell(**kwargs):
        if sell_raises:
            raise sell_raises
        return sell or _Executed(status="ACCEPTED")

    monkeypatch.setattr(execution_engine, "submit_buy_order", _buy)
    monkeypatch.setattr(execution_engine, "submit_sell_order", _sell)

    def _cancel(**kwargs):
        if cancel_raises:
            raise cancel_raises
        return {"cancelled": True, "reason_code": None}

    monkeypatch.setattr(bootstrap, "cancel_if_open", lambda **k: _cancel(**k))


class TestOrchestrationBranches:
    def test_accepted_and_resting_is_cancelled_and_ends_flat(
            self, armed, conn, monkeypatch):
        _stub_engine(monkeypatch)
        broker = _OrchestratorBroker(open_orders=[SYMBOL], positions=[])
        report = _run(monkeypatch, conn, broker)
        assert report["kis_conclusion"] == "OPEN_UNFILLED"
        assert report["conclusion"] == runner.CONCLUSION_CANCELLED
        assert report["filled_quantity"] == 0
        assert report["transport"]["flatten"] == 0

    def test_a_fill_is_flattened_and_ends_flat(self, armed, conn, monkeypatch):
        _stub_engine(monkeypatch)
        # Held at verification time, gone after the flatten.
        broker = _OrchestratorBroker(open_orders=[], positions=[_Position(SYMBOL, 1)])
        # Flat only AFTER the flatten is submitted -- flipped by the sell
        # stub rather than by counting reads, so the test asserts the
        # sequence rather than a call count.
        from execution import execution_engine

        def _sell(**kwargs):
            broker._positions = []
            return _Executed(status="ACCEPTED")

        monkeypatch.setattr(execution_engine, "submit_sell_order", _sell)
        report = _run(monkeypatch, conn, broker)
        assert report["kis_conclusion"] == "FILLED"
        assert report["filled_quantity"] == 1
        assert report["conclusion"] == runner.CONCLUSION_FLATTENED
        assert report["remaining_quantity"] == 0

    def test_a_failed_flatten_adopts_the_exposure(self, armed, conn, monkeypatch):
        _stub_engine(monkeypatch, sell_raises=RuntimeError("KIS refused"))
        broker = _OrchestratorBroker(open_orders=[], positions=[_Position(SYMBOL, 1)])
        with pytest.raises(runner.RouteVerificationExposed) as caught:
            _run(monkeypatch, conn, broker)
        assert caught.value.remaining_qty == 1
        assert caught.value.position_id

        from s6_live import position_store

        row = position_store.load(conn, caught.value.position_id)
        assert row["quantity"] == 1
        assert row["entry_price"] == pytest.approx(41.61)
        assert rv.is_route_verification(row)

    def test_an_ambiguous_flatten_never_assumes_flat(self, armed, conn,
                                                     monkeypatch):
        """The SELL may or may not have gone through. The broker is
        re-read, and shares still reported are adopted."""
        from brokers.kis_broker import KISAmbiguousResponseError

        _stub_engine(monkeypatch,
                     sell_raises=KISAmbiguousResponseError("timeout"))
        broker = _OrchestratorBroker(open_orders=[], positions=[_Position(SYMBOL, 1)])
        with pytest.raises(runner.RouteVerificationExposed):
            _run(monkeypatch, conn, broker)

    def test_an_unreadable_broker_after_flatten_assumes_still_held(
            self, armed, conn, monkeypatch):
        """Assuming flat is the one error that leaves an orphan."""
        _stub_engine(monkeypatch)
        broker = _OrchestratorBroker(open_orders=[], positions=[_Position(SYMBOL, 1)])
        from execution import execution_engine

        def _sell(**kwargs):
            # The SELL goes out, then the book becomes unreadable.
            def _boom():
                raise RuntimeError("KIS unreadable")
            broker.get_positions = _boom
            return _Executed(status="ACCEPTED")

        monkeypatch.setattr(execution_engine, "submit_sell_order", _sell)
        with pytest.raises(runner.RouteVerificationExposed) as caught:
            _run(monkeypatch, conn, broker)
        assert caught.value.remaining_qty == 1
        # No basis could be read, so adoption legitimately refused -- but
        # the exposure is still reported as exposure, not as a store error.
        assert caught.value.position_id is None

    def test_an_ambiguous_buy_stops_without_a_second_submit(
            self, armed, conn, monkeypatch):
        from brokers.kis_broker import KISAmbiguousResponseError

        _stub_engine(monkeypatch, buy_raises=KISAmbiguousResponseError("timeout"))
        broker = _OrchestratorBroker()
        with pytest.raises(KISAmbiguousResponseError):
            _run(monkeypatch, conn, broker)
        assert broker.submitted == []  # the engine was stubbed; no retry loop

    def test_indeterminate_neither_cancels_nor_flattens(
            self, armed, conn, monkeypatch):
        """KIS could not tell us. Acting on ignorance is how a filled
        order gets cancelled out from under a position."""
        _stub_engine(monkeypatch)
        broker = _OrchestratorBroker(open_orders=[], positions=[])
        report = _run(monkeypatch, conn, broker)
        assert report["kis_conclusion"] == "INDETERMINATE"
        assert report["transport"]["flatten"] == 0
        assert report["conclusion"] == "NO_FILL_NOT_CANCELLED"

    def test_s6_statistics_stay_clean_after_an_adoption(
            self, armed, conn, monkeypatch):
        _stub_engine(monkeypatch, sell_raises=RuntimeError("refused"))
        broker = _OrchestratorBroker(open_orders=[], positions=[_Position(SYMBOL, 1)])
        with pytest.raises(runner.RouteVerificationExposed):
            _run(monkeypatch, conn, broker)

        from s6_live import position_store

        rows = [row for _pid, row in position_store.load_live(conn)]
        assert rows, "the exposure must be MANAGED"
        assert rv.exclude_from_performance(rows) == []


class TestTheCliCannotExpressAnythingElse:
    SCRIPT = REPO_ROOT / "scripts" / "run_daytime_route_verification.py"

    def test_it_exists_and_parses(self):
        import ast

        ast.parse(self.SCRIPT.read_text())

    def test_it_offers_no_order_shaping_arguments(self):
        source = self.SCRIPT.read_text()
        for forbidden in ("--symbol", "--side", "--quantity", "--qty",
                          "--price", "--limit", "--session", "--force",
                          "--retry", "--bypass", "--market"):
            assert forbidden not in source, forbidden

    def test_it_never_calls_the_broker_directly(self):
        """Checked against CODE, not prose -- the module docstring names
        the thing it rules out."""
        import ast

        tree = ast.parse(self.SCRIPT.read_text())
        calls = {node.func.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)}
        assert "submit_order" not in calls
        assert "cancel_order" not in calls

    def test_it_is_covered_by_the_execution_boundary_guard(self):
        """Not on the allow-list, so the boundary test polices it."""
        boundary = (REPO_ROOT / "tests" / "test_execution_boundary.py").read_text()
        assert "scripts/run_daytime_route_verification.py" not in boundary

    def test_it_disarms_on_every_path(self):
        source = self.SCRIPT.read_text()
        assert "finally:" in source
        assert "runner.disarm(" in source

    def test_it_reports_a_failed_disarm_loudly(self):
        source = self.SCRIPT.read_text()
        assert "DISARM FAILED" in source
        assert "STILL BE ARMED" in source
