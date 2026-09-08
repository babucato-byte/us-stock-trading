"""The daytime route: how it is addressed, and how it becomes verified.

Two things are pinned here. First, the wire: OVERNIGHT_DAYTIME resolves
to the DAYTIME family and nothing else does; BUY is TTTS6036U, SELL is
TTTS6037U, CANCEL is TTTS6038U on their own endpoints; the order is a
whole-share limit order with a two-decimal price. Second, the evidence:
a pending daytime wire value becomes LIVE_RESPONSE_CONFIRMED only from
a record the one-shot wrote out of a real ACCEPTED response, and nothing
that is not that -- a rejection, a record without the broker's order
number, a record naming another TR, a file from another source, a test
fixture -- moves the gate.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from brokers import kis_broker as kb  # noqa: E402
from brokers import route_evidence as ev  # noqa: E402
from brokers.order_price import wire_price  # noqa: E402
from config import session_capability as sc  # noqa: E402
from execution import route_verification as rv  # noqa: E402
from execution.order_gate import (  # noqa: E402
    ROUTE_UNVERIFIED, OrderGateBlockedError, evaluate_buy_gate,
)
from live_pilot import route_verification_runner as runner  # noqa: E402
from tests import test_order_gate as fixtures  # noqa: E402
from tests import test_route_verification as rvt  # noqa: E402

DAYTIME = "OVERNIGHT_DAYTIME"
NOW = datetime(2026, 9, 8, 2, 0, tzinfo=timezone.utc)
BUY_TR = kb.TR_ID_DAYTIME_ORDER_US[("live", "buy")]
SELL_TR = kb.TR_ID_DAYTIME_ORDER_US[("live", "sell")]
CANCEL_TR = kb.TR_ID_DAYTIME_CANCEL["live"]


@pytest.fixture(autouse=True)
def no_evidence_leaks(tmp_path, monkeypatch):
    """No test reads or writes the real store. The store path is pointed
    at a temp file that does not exist, so "no evidence" is the default;
    the conftest's STATE_STORE_DB_FILE redirect is left in place (a
    test that unset it once created TRADING_STATE.db at the repo root)."""
    monkeypatch.setenv(ev.EVIDENCE_FILE_ENV, str(tmp_path / "absent" / ev.FILENAME))


def pending():
    return set(ev.pending_items_after_live_evidence(kb.REQUIRED_FOR_DAYTIME))


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "state" / ev.FILENAME
    monkeypatch.setenv(ev.EVIDENCE_FILE_ENV, str(path))
    return path


def _accept(name, value, odno="0000012345", **kw):
    kw.setdefault("rt_cd", "0")
    kw.setdefault("status", "ACCEPTED")
    return ev.record(name, wire_value=value, broker_order_id=odno, **kw)


def _daytime_ctx(**overrides):
    kwargs = dict(order_intent=fixtures._order_intent(session=DAYTIME))
    kwargs.update(overrides)
    return fixtures._buy_ctx(**kwargs)


# ---------------------------------------------------------------------
# 1-5. the wire
# ---------------------------------------------------------------------
class TestTheDaytimeWire:
    def test_daytime_resolves_to_the_daytime_family(self):
        assert kb.FAMILY_BY_SESSION[DAYTIME] == kb.FAMILY_DAYTIME
        assert sc.SESSION_BY_WINDOW[sc.schedule.WINDOW_DAYTIME] == DAYTIME

    def test_buy_is_TTTS6036U_on_the_daytime_endpoint(self):
        path, tr = kb.order_route_for(DAYTIME, "live", "buy")
        assert (path, tr) == (kb.DAYTIME_ORDER_PATH, "TTTS6036U")
        assert path == "/uapi/overseas-stock/v1/trading/daytime-order"

    def test_sell_is_TTTS6037U_on_the_daytime_endpoint(self):
        path, tr = kb.order_route_for(DAYTIME, "live", "sell")
        assert (path, tr) == (kb.DAYTIME_ORDER_PATH, "TTTS6037U")

    def test_cancel_is_TTTS6038U_on_the_daytime_cancel_endpoint(self):
        path, tr = kb.cancel_route_for(DAYTIME, "live")
        assert (path, tr) == (kb.DAYTIME_CANCEL_PATH, "TTTS6038U")
        assert path == "/uapi/overseas-stock/v1/trading/daytime-order-rvsecncl"

    @pytest.mark.parametrize("session", ["REGULAR", "PREMARKET", "AFTER_HOURS"])
    def test_the_general_sessions_never_use_the_daytime_endpoints(self, session):
        for side in ("buy", "sell"):
            path, tr = kb.order_route_for(session, "live", side)
            assert path != kb.DAYTIME_ORDER_PATH
            assert tr not in (BUY_TR, SELL_TR)
        path, tr = kb.cancel_route_for(session, "live")
        assert path != kb.DAYTIME_CANCEL_PATH and tr != CANCEL_TR

    def test_the_exchange_codes_are_the_order_code_space(self):
        assert kb._order_excg_for("NASDAQ") == "NASD"
        assert kb._order_excg_for("NYSE") == "NYSE"
        assert kb._order_excg_for("AMEX") == "AMEX"

    def test_the_order_payload_fields_match_the_official_daytime_shape(self):
        source = (REPO_ROOT / "brokers" / "kis_broker.py").read_text()
        body = source[source.index("def submit_order("):source.index("def cancel_order(")]
        for field in ("CANO", "ACNT_PRDT_CD", "OVRS_EXCG_CD", "PDNO", "ORD_QTY",
                      "OVRS_ORD_UNPR", "ORD_SVR_DVSN_CD", "ORD_DVSN"):
            assert f'"{field}"' in body, field
        assert '"ORD_DVSN": "00"' in body
        assert '"ORD_SVR_DVSN_CD": "0"' in body
        cancel = source[source.index("def cancel_order("):]
        assert '"RVSE_CNCL_DVSN_CD": "02"' in cancel
        assert '"OVRS_ORD_UNPR": "0"' in cancel
        assert '"ORGN_ODNO"' in cancel

    def test_daytime_is_limit_only(self):
        source = (REPO_ROOT / "brokers" / "kis_broker.py").read_text()
        body = source[source.index("def submit_order("):source.index("def cancel_order(")]
        assert 'if order_intent.order_type != "limit":' in body
        assert rv.VERIFICATION_ORDER_TYPE == "limit"

    def test_the_verification_order_is_one_whole_share(self):
        assert rv.VERIFICATION_QUANTITY == 1
        assert isinstance(rv.VERIFICATION_QUANTITY, int)
        # A fractional intent cannot even be constructed: the domain
        # object refuses before any gate or wire is reached.
        from domain.order_intent import OrderIntentError
        with pytest.raises(OrderIntentError, match="fractional"):
            fixtures._order_intent(session=DAYTIME, quantity=0.5)

    @pytest.mark.parametrize("price,expected", [
        (14.3 - 0.02, "14.28"), (60.855, "60.85"), (6.405, "6.40"),
        (319.97 - 0.02, "319.95"),
    ])
    def test_the_wire_price_has_two_decimals_above_a_dollar(self, price, expected):
        """APTR0057 ('1$이상 소수점 2자리') on 08-28 and 09-01 came from
        three-decimal strategy prices; the wire price is normalised."""
        assert wire_price(price, side="buy") == expected
        assert Decimal(wire_price(price, side="buy")).as_tuple().exponent == -2


# ---------------------------------------------------------------------
# 9-11. response parsing (the broker's own contract, pinned)
# ---------------------------------------------------------------------
class TestResponseParsing:
    def test_a_rejection_is_a_record_with_the_kis_codes(self):
        source = (REPO_ROOT / "brokers" / "kis_broker.py").read_text()
        body = source[source.index("def submit_order("):source.index("def cancel_order(")]
        assert 'if rt_cd != "0" or not broker_order_id:' in body
        assert 'status="REJECTED"' in body and 'error_code=body.get("msg_cd")' in body

    def test_an_acceptance_carries_the_broker_order_number(self):
        source = (REPO_ROOT / "brokers" / "kis_broker.py").read_text()
        body = source[source.index("def submit_order("):source.index("def cancel_order(")]
        assert 'broker_order_id = output.get("ODNO")' in body
        assert 'status="ACCEPTED"' in body

    def test_a_cancel_acceptance_is_CANCELLED_and_a_refusal_is_REJECTED(self):
        source = (REPO_ROOT / "brokers" / "kis_broker.py").read_text()
        cancel = source[source.index("def cancel_order("):]
        assert 'status = "CANCELLED" if rt_cd == "0" else "REJECTED"' in cancel


# ---------------------------------------------------------------------
# 12-15. evidence
# ---------------------------------------------------------------------
class TestEvidenceIsOnlyARealAcceptance:
    def test_without_a_store_nothing_is_read_and_the_route_stays_pending(self, monkeypatch):
        monkeypatch.delenv(ev.EVIDENCE_FILE_ENV, raising=False)
        monkeypatch.delenv("STATE_STORE_DB_FILE", raising=False)
        monkeypatch.delenv("TRADING_STATE_DB", raising=False)
        assert ev.evidence_path() is None
        assert ev.load() == {}
        assert "daytime_order_tr_id_live_buy" in pending()
        assert sc.route_awaiting_live_evidence(DAYTIME) is True

    def test_the_static_accessors_never_change(self, store):
        """`kis_broker.pending_items_for` is a pure projection of the
        matrix; evidence is applied by `route_evidence` on top."""
        _accept("daytime_order_tr_id_live_buy", BUY_TR)
        assert "daytime_order_tr_id_live_buy" in kb.pending_items_for(kb.REQUIRED_FOR_DAYTIME)
        assert "daytime_order_tr_id_live_buy" not in pending()

    def test_a_rejection_is_refused_at_record(self, store):
        with pytest.raises(ev.RouteEvidenceRefused):
            ev.record("daytime_order_tr_id_live_buy", wire_value=BUY_TR,
                      broker_order_id="0000012345", rt_cd="7", status="REJECTED")
        assert not store.exists()

    def test_a_response_without_an_order_number_is_refused(self, store):
        with pytest.raises(ev.RouteEvidenceRefused):
            ev.record("daytime_order_tr_id_live_buy", wire_value=BUY_TR,
                      broker_order_id=None, rt_cd="0", status="ACCEPTED")

    def test_an_acceptance_is_persisted_atomically_with_its_provenance(self, store):
        entry = _accept("daytime_order_tr_id_live_buy", BUY_TR, run_id="rtverify-1",
                        now=NOW)
        payload = json.loads(store.read_text())
        assert payload["schema"] == ev.SCHEMA
        stored = payload["records"]["daytime_order_tr_id_live_buy"]
        assert stored == entry
        assert stored["source"] == ev.SOURCE_RUNNER
        assert stored["broker_order_id"] == "0000012345"
        assert stored["recorded_at"] == NOW.isoformat()
        assert not list(store.parent.glob(".route_evidence.json.*"))

    def test_the_buy_evidence_verifies_the_buy_leg_only(self, store):
        _accept("daytime_order_tr_id_live_buy", BUY_TR)
        _accept("daytime_order_path", kb.DAYTIME_ORDER_PATH)
        left = pending()
        assert "daytime_order_tr_id_live_buy" not in left
        assert {"daytime_cancel_path", "daytime_cancel_tr_id_live"} <= left
        assert sc.route_awaiting_live_evidence(DAYTIME) is True

    def test_buy_and_cancel_evidence_clears_the_route(self, store):
        _accept("daytime_order_tr_id_live_buy", BUY_TR)
        _accept("daytime_order_path", kb.DAYTIME_ORDER_PATH)
        _accept("daytime_cancel_tr_id_live", CANCEL_TR, status="CANCELLED")
        _accept("daytime_cancel_path", kb.DAYTIME_CANCEL_PATH, status="CANCELLED")
        assert pending() == set()
        assert sc.route_awaiting_live_evidence(DAYTIME) is False
        confirmed = dict(ev.confirmed_by_live_evidence(kb.REQUIRED_FOR_DAYTIME))
        assert "0000012345" in confirmed["daytime_order_tr_id_live_buy"]
        assert "TTTS6036U" in confirmed["daytime_order_tr_id_live_buy"]

    def test_the_general_route_is_untouched_by_daytime_evidence(self, store):
        _accept("daytime_order_tr_id_live_buy", BUY_TR)
        assert list(ev.pending_items_after_live_evidence(kb.REQUIRED_FOR_ARMED)) == []
        assert sc.route_awaiting_live_evidence("REGULAR") is False

    @pytest.mark.parametrize("tamper", [
        {"wire_value": "TTTT1002U"},             # another TR
        {"session": "REGULAR"},                  # another session
        {"source": "operator"},                  # not the runner
        {"rt_cd": "7"},                          # rejection, edited in
        {"status": "UNKNOWN"},
        {"broker_order_id": ""},
        {"recorded_at": "yesterday"},
    ])
    def test_a_hand_edited_record_does_not_verify(self, store, tamper):
        _accept("daytime_order_tr_id_live_buy", BUY_TR)
        payload = json.loads(store.read_text())
        payload["records"]["daytime_order_tr_id_live_buy"].update(tamper)
        store.write_text(json.dumps(payload))
        assert ev.confirms("daytime_order_tr_id_live_buy", expected_value=BUY_TR) is False
        assert "daytime_order_tr_id_live_buy" in pending()

    def test_a_hand_written_file_without_the_runner_source_does_not_verify(self, store):
        store.parent.mkdir(parents=True)
        store.write_text(json.dumps({"schema": ev.SCHEMA, "records": {
            "daytime_order_tr_id_live_buy": {
                "wire_value": BUY_TR, "session": DAYTIME, "rt_cd": "0",
                "status": "ACCEPTED", "broker_order_id": "0000000001",
                "recorded_at": NOW.isoformat()}}}))
        assert ev.confirms("daytime_order_tr_id_live_buy", expected_value=BUY_TR) is False

    def test_a_confirmed_entry_is_never_downgraded(self, store):
        entry = next(e for e in kb.matrix_entries_for(kb.REQUIRED_FOR_DAYTIME)
                     if e.name == "daytime_order_tr_id_live_sell")
        assert entry.live_status == kb.LIVE_RESPONSE_CONFIRMED
        assert "0000001014" in entry.source
        assert "daytime_order_tr_id_live_sell" not in pending()

    def test_an_unreadable_store_is_no_evidence(self, store):
        store.parent.mkdir(parents=True)
        store.write_text("{not json")
        assert ev.load() == {}
        assert "daytime_order_tr_id_live_buy" in pending()


class TestTheGateOpensOnEvidenceAndNotBefore:
    def test_without_evidence_a_daytime_buy_is_ROUTE_UNVERIFIED(self):
        with pytest.raises(OrderGateBlockedError) as caught:
            evaluate_buy_gate(_daytime_ctx())
        assert caught.value.code == ROUTE_UNVERIFIED

    def test_with_buy_and_cancel_evidence_the_same_buy_passes(self, store):
        _accept("daytime_order_tr_id_live_buy", BUY_TR)
        _accept("daytime_order_path", kb.DAYTIME_ORDER_PATH)
        _accept("daytime_cancel_tr_id_live", CANCEL_TR, status="CANCELLED")
        _accept("daytime_cancel_path", kb.DAYTIME_CANCEL_PATH, status="CANCELLED")
        assert evaluate_buy_gate(_daytime_ctx()) is True

    def test_buy_evidence_alone_still_blocks(self, store):
        """The cancel legs are separate wire values; a proven BUY does
        not prove them."""
        _accept("daytime_order_tr_id_live_buy", BUY_TR)
        _accept("daytime_order_path", kb.DAYTIME_ORDER_PATH)
        with pytest.raises(OrderGateBlockedError) as caught:
            evaluate_buy_gate(_daytime_ctx())
        assert caught.value.code == ROUTE_UNVERIFIED

    def test_the_gate_itself_is_still_there(self):
        source = (REPO_ROOT / "execution" / "order_gate.py").read_text()
        assert "def _check_route_evidence(ctx):" in source
        assert "ROUTE_UNVERIFIED" in source

    def test_no_environment_flag_opens_the_route(self, store, monkeypatch):
        monkeypatch.setenv("LIVE_BOOTSTRAP_ENABLED", "true")
        monkeypatch.setenv("LIVE_BOOTSTRAP_ACK", "true")
        monkeypatch.setenv(rv.FLAG_ENABLED, "true")
        monkeypatch.setenv(rv.FLAG_ACK, "true")
        assert sc.route_awaiting_live_evidence(DAYTIME) is True


# ---------------------------------------------------------------------
# the runner writes evidence only from what KIS actually answered
# ---------------------------------------------------------------------
class TestTheRunnerRecordsRealResponses:
    def test_an_accepted_buy_and_a_confirmed_cancel_are_recorded(
            self, store, rvt_armed, rvt_conn, monkeypatch):
        rvt._stub_engine(monkeypatch)
        broker = rvt._OrchestratorBroker(open_orders=[rvt.SYMBOL], positions=[])
        report = rvt._run(monkeypatch, rvt_conn, broker)
        assert report["conclusion"] == runner.CONCLUSION_CANCELLED
        assert set(report["evidence"]["buy"]["recorded"]) == {
            "daytime_order_tr_id_live_buy", "daytime_order_path"}
        assert set(report["evidence"]["cancel"]["recorded"]) == {
            "daytime_cancel_tr_id_live", "daytime_cancel_path"}
        records = ev.load()
        assert records["daytime_order_tr_id_live_buy"]["broker_order_id"] == "0000009001"
        assert records["daytime_cancel_tr_id_live"]["status"] == "CANCELLED"
        assert sc.route_awaiting_live_evidence(DAYTIME) is False

    def test_a_filled_buy_records_the_buy_leg_but_not_the_cancel_legs(
            self, store, rvt_armed, rvt_conn, monkeypatch):
        rvt._stub_engine(monkeypatch)
        broker = rvt._OrchestratorBroker(open_orders=[], positions=[rvt._Position(rvt.SYMBOL, 1)])
        from execution import execution_engine

        def _sell(**kwargs):
            broker._positions = []
            return rvt._Executed(status="ACCEPTED")
        monkeypatch.setattr(execution_engine, "submit_sell_order", _sell)
        report = rvt._run(monkeypatch, rvt_conn, broker)
        assert report["conclusion"] == runner.CONCLUSION_FLATTENED
        assert "daytime_order_tr_id_live_buy" in report["evidence"]["buy"]["recorded"]
        assert "cancel" not in report["evidence"]
        assert sc.route_awaiting_live_evidence(DAYTIME) is True

    def test_a_rejected_buy_records_nothing(self, store, rvt_armed, rvt_conn, monkeypatch):
        rvt._stub_engine(monkeypatch, buy=rvt._Executed(broker_order_id=None, status="REJECTED"))
        broker = rvt._OrchestratorBroker(open_orders=[], positions=[])
        report = rvt._run(monkeypatch, rvt_conn, broker)
        assert report["evidence"]["buy"]["recorded"] == []
        assert not store.exists()
        assert sc.route_awaiting_live_evidence(DAYTIME) is True

    def test_an_ambiguous_cancel_leaves_the_cancel_legs_pending(
            self, store, rvt_armed, rvt_conn, monkeypatch):
        rvt._stub_engine(monkeypatch, cancel_raises=RuntimeError("timeout"))
        broker = rvt._OrchestratorBroker(open_orders=[rvt.SYMBOL], positions=[])
        report = rvt._run(monkeypatch, rvt_conn, broker)
        assert report["evidence"]["cancel"]["recorded"] == []
        assert "daytime_cancel_tr_id_live" in pending()

    def test_only_the_runner_module_writes_evidence(self):
        """`record` has exactly one production caller."""
        hits = []
        for path in REPO_ROOT.rglob("*.py"):
            if "venv" in path.parts or path.parts[0] == "tests" or "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "route_evidence.record(" in text:
                hits.append(path.relative_to(REPO_ROOT).as_posix())
        assert hits == ["live_pilot/route_verification_runner.py"]


@pytest.fixture
def rvt_armed(monkeypatch):
    monkeypatch.setenv(rv.FLAG_ENABLED, "true")
    monkeypatch.setenv(rv.FLAG_ACK, "true")
    return monkeypatch


@pytest.fixture
def rvt_conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection
