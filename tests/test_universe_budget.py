"""T8: universe account budget persistence + keep-previous-on-failure.

The KIS read is exercised end-to-end through a real KISBroker driven by a
fake requests.Session double (this project's established pattern -- see
tests/test_kis_broker.py); no test here opens a socket or reads the real
state file.
"""

import json
from datetime import datetime, timezone

import pytest
import requests

import universe_budget as ub
from brokers.kis_broker import KISBroker
from brokers.kis_config import KISConfig
from domain.account_snapshot import AccountSnapshot

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
BALANCE_PATH = "/uapi/overseas-stock/v1/trading/inquire-balance"


class _StubResponse:
    def __init__(self, status_code=200, json_body=None, text="ok"):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}
        self.text = text

    def json(self):
        return self._json_body


class _FakeSession:
    def __init__(self):
        self.responses = {}
        self.requests = []

    def queue(self, path, response):
        self.responses[path] = response

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        for path, response in self.responses.items():
            if url.endswith(path):
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"no stubbed response for {method} {url}")


TOKEN_OK = _StubResponse(200, {"access_token": "tok-1", "expires_in": 3600})


def _kis_config(**overrides):
    kwargs = dict(
        kis_env="paper", app_key="key", app_secret="secret", account_no="12345678",
        account_product_cd="01", account_read_enabled=True, live_order_enabled=False,
    )
    kwargs.update(overrides)
    return KISConfig(**kwargs)


def _kis_broker(session):
    return KISBroker(config=_kis_config(), session=session, now_fn=lambda: NOW)


# ORACLE-CASH-01: the REAL output2 a live TTTS3012R balance read returns
# (Oracle, 2026-08-06) -- purchase/valuation/P&L only, no cash field. The
# previous fixture invented `frcr_dncl_amt1`/`frcr_use_psbl_amt`, so this
# file tested a budget refresh that cannot happen against a real account.
LIVE_BALANCE_OUTPUT2 = {
    "frcr_pchs_amt1": "0.00000",
    "ovrs_rlzt_pfls_amt": "0.00000",
    "ovrs_tot_pfls": "0.00000",
    "rlzt_erng_rt": "0.00000000",
    "tot_evlu_pfls_amt": "0.00000000",
    "tot_pftrt": "0.00000000",
    "frcr_buy_amt_smtl1": "0.000000",
    "ovrs_rlzt_pfls_amt2": "0.00000",
    "frcr_buy_amt_smtl2": "0.000000",
}


def _balance_session(output2=None):
    session = _FakeSession()
    session.queue("/oauth2/tokenP", TOKEN_OK)
    session.queue(BALANCE_PATH, _StubResponse(200, {
        "output1": [],
        "output2": dict(LIVE_BALANCE_OUTPUT2 if output2 is None else output2),
    }))
    return session


def _snapshot(cash=1000.0, orderable=900.0, reserved=0.0):
    return AccountSnapshot(
        krw_cash=None, usd_cash=cash, usd_orderable_cash=orderable,
        usd_reserved_in_open_orders=reserved, as_of=NOW, source="kis_balance",
        account_id="12345678", cash_source="test",
    )


class _FakeBroker:
    def __init__(self, snapshot=None, error=None):
        self._snapshot = snapshot
        self._error = error
        self.calls = 0

    def get_account_snapshot(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._snapshot


# -- persistence --------------------------------------------------------

def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "universe_budget.json"
    state = ub.BudgetState(available_cash_usd=900.25, as_of=NOW.isoformat(),
                           source=ub.SOURCE_KIS, account_id="12345678")
    ub.save_budget_state(state, path)
    loaded = ub.load_budget_state(path)
    assert loaded.available_cash_usd == pytest.approx(900.25)
    assert loaded.source == ub.SOURCE_KIS
    assert loaded.account_id == "12345678"
    assert loaded.stale is False


def test_load_returns_none_when_nothing_persisted_yet(tmp_path):
    assert ub.load_budget_state(tmp_path / "missing.json") is None


@pytest.mark.parametrize("content", [
    "",
    "not json",
    "[]",
    json.dumps({"available_cash_usd": -1, "as_of": "x", "source": "kis_balance"}),
    json.dumps({"available_cash_usd": "900", "as_of": "x", "source": "kis_balance"}),
    json.dumps({"available_cash_usd": 900}),
    json.dumps({"available_cash_usd": 900, "as_of": "  ", "source": "kis_balance"}),
    json.dumps({"available_cash_usd": 900, "as_of": "x", "source": ""}),
])
def test_corrupt_state_file_fails_closed_rather_than_reading_as_empty(tmp_path, content):
    path = tmp_path / "universe_budget.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ub.UniverseBudgetError):
        ub.load_budget_state(path)


def test_save_leaves_no_temp_files_behind(tmp_path):
    path = tmp_path / "universe_budget.json"
    ub.save_budget_state(
        ub.BudgetState(available_cash_usd=1.0, as_of=NOW.isoformat(), source=ub.SOURCE_KIS), path)
    assert [p.name for p in tmp_path.iterdir()] == ["universe_budget.json"]


def test_env_override_selects_the_state_path(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "budget.json"
    monkeypatch.setenv("UNIVERSE_BUDGET_STATE_FILE", str(target))
    ub.save_budget_state(
        ub.BudgetState(available_cash_usd=5.0, as_of=NOW.isoformat(), source=ub.SOURCE_KIS))
    assert target.exists()
    assert ub.load_budget_state().available_cash_usd == pytest.approx(5.0)


# -- snapshot conversion ------------------------------------------------

def test_orderable_cash_minus_reservations_is_the_figure_used():
    state = ub.state_from_account_snapshot(_snapshot(cash=5000.0, orderable=900.0, reserved=100.0))
    assert state.available_cash_usd == pytest.approx(800.0)


def test_total_deposit_is_never_used_as_the_budget():
    # usd_cash (5000) is much larger than orderable (900); using it would
    # size the universe against money that cannot be ordered with.
    state = ub.state_from_account_snapshot(_snapshot(cash=5000.0, orderable=900.0))
    assert state.available_cash_usd == pytest.approx(900.0)


# -- refresh: success ---------------------------------------------------

def test_refresh_persists_a_successful_read(tmp_path):
    path = tmp_path / "universe_budget.json"
    broker = _FakeBroker(_snapshot(orderable=900.0))
    state, error = ub.refresh_budget(broker, path=path, logger=lambda *_: None)
    assert error is None
    assert state.stale is False
    assert state.source == ub.SOURCE_KIS
    assert ub.load_budget_state(path).available_cash_usd == pytest.approx(900.0)


def test_a_real_balance_shape_yields_no_budget_at_all(tmp_path):
    """ORACLE-CASH-01, the defect end to end.

    A real live balance response carries no cash field. The refresh must
    end with NO budget -- not a $0 budget, which is what the old
    `.get(field, 0) or 0` produced and what silently emptied the
    entry-side universe.
    """
    session = _balance_session()
    path = tmp_path / "b.json"
    state, error = ub.refresh_budget(
        _kis_broker(session), path=path, logger=lambda *_: None)
    assert state is None
    assert error is not None
    assert "cash" in error.lower()
    # Nothing was persisted from an unknown balance.
    assert not path.exists()
    # token + balance sweep; no other endpoint was touched.
    assert all(url.endswith(("/oauth2/tokenP", BALANCE_PATH)) for _, url, _ in session.requests)


def test_an_unknown_balance_keeps_the_previous_budget_untouched(tmp_path):
    """The previous figure survives byte-for-byte: unknown must not widen,
    narrow or erase a budget that was read successfully before."""
    path = tmp_path / "b.json"
    ub.save_budget_state(
        ub.BudgetState(available_cash_usd=777.0, as_of="2026-08-05T00:00:00+00:00",
                       source=ub.SOURCE_KIS), path)
    before = path.read_bytes()
    state, error = ub.refresh_budget(
        _kis_broker(_balance_session()), path=path, logger=lambda *_: None)
    assert error is not None
    assert state is not None and state.stale is True
    assert state.available_cash_usd == pytest.approx(777.0)
    assert path.read_bytes() == before


def test_a_balance_that_DOES_carry_cash_still_drives_the_price_ceiling(tmp_path):
    """The fail-closed path is about UNKNOWN, not about refusing numbers:
    an explicitly-reported figure is still used."""
    payload = dict(LIVE_BALANCE_OUTPUT2)
    payload["frcr_use_psbl_amt"] = "10000"
    state, _ = ub.refresh_budget(
        _kis_broker(_balance_session(payload)), path=tmp_path / "b.json",
        logger=lambda *_: None)
    budget = state.to_budget()
    # 10,000 USD * 90% trusted usage * 0.10 position rate = 900 USD/share ceiling.
    assert budget.price_ceiling_usd == pytest.approx(900.0)


# -- refresh: failure keeps the previous value --------------------------

def test_failed_read_keeps_the_previous_value_and_marks_it_stale(tmp_path):
    path = tmp_path / "universe_budget.json"
    ub.save_budget_state(
        ub.BudgetState(available_cash_usd=777.0, as_of="2026-08-05T00:00:00+00:00",
                       source=ub.SOURCE_KIS), path)
    before = path.read_bytes()

    state, error = ub.refresh_budget(
        _FakeBroker(error=RuntimeError("network down")), path=path, logger=lambda *_: None)

    assert error is not None and "network down" in error
    assert state.available_cash_usd == pytest.approx(777.0)
    assert state.stale is True
    assert state.source == "cached:kis_balance"
    assert state.as_of == "2026-08-05T00:00:00+00:00"  # NOT restamped as fresh
    # The persisted file itself is untouched -- a failed read must not
    # rewrite state.
    assert path.read_bytes() == before


def test_repeated_failures_do_not_stack_the_cached_prefix(tmp_path):
    path = tmp_path / "universe_budget.json"
    ub.save_budget_state(
        ub.BudgetState(available_cash_usd=10.0, as_of=NOW.isoformat(),
                       source="cached:kis_balance"), path)
    state, _ = ub.refresh_budget(
        _FakeBroker(error=RuntimeError("still down")), path=path, logger=lambda *_: None)
    assert state.source == "cached:kis_balance"


def test_failed_read_with_no_previous_value_returns_none(tmp_path):
    state, error = ub.refresh_budget(
        _FakeBroker(error=RuntimeError("boom")), path=tmp_path / "missing.json",
        logger=lambda *_: None)
    assert state is None
    assert "boom" in error


def test_failed_read_with_a_corrupt_previous_value_returns_none(tmp_path):
    path = tmp_path / "universe_budget.json"
    path.write_text("{{{", encoding="utf-8")
    state, error = ub.refresh_budget(
        _FakeBroker(error=RuntimeError("boom")), path=path, logger=lambda *_: None)
    assert state is None
    assert error is not None


def test_kis_transport_failure_falls_back_rather_than_propagating(tmp_path):
    path = tmp_path / "universe_budget.json"
    ub.save_budget_state(
        ub.BudgetState(available_cash_usd=42.0, as_of=NOW.isoformat(), source=ub.SOURCE_KIS), path)
    session = _FakeSession()
    session.queue("/oauth2/tokenP", TOKEN_OK)
    session.queue(BALANCE_PATH, requests.ConnectionError("connection reset"))

    state, error = ub.refresh_budget(_kis_broker(session), path=path, logger=lambda *_: None)

    assert state.available_cash_usd == pytest.approx(42.0)
    assert state.stale is True
    assert error is not None


def test_read_disabled_config_falls_back_and_issues_no_request(tmp_path):
    path = tmp_path / "universe_budget.json"
    ub.save_budget_state(
        ub.BudgetState(available_cash_usd=42.0, as_of=NOW.isoformat(), source=ub.SOURCE_KIS), path)
    session = _FakeSession()
    broker = KISBroker(config=_kis_config(account_read_enabled=False), session=session,
                       now_fn=lambda: NOW)

    state, error = ub.refresh_budget(broker, path=path, logger=lambda *_: None)

    assert session.requests == []
    assert state.stale is True
    assert error is not None


def test_failure_is_logged_not_silent(tmp_path):
    messages = []
    ub.refresh_budget(_FakeBroker(error=RuntimeError("boom")), path=tmp_path / "x.json",
                      logger=messages.append)
    assert any("KIS balance read failed" in m for m in messages)


# -- resolve_budget -----------------------------------------------------

def test_resolve_budget_returns_none_when_nothing_persisted(tmp_path):
    assert ub.resolve_budget(tmp_path / "missing.json") is None


def test_resolve_budget_reads_the_persisted_figure(tmp_path):
    path = tmp_path / "universe_budget.json"
    ub.save_budget_state(
        ub.BudgetState(available_cash_usd=10_000.0, as_of=NOW.isoformat(),
                       source=ub.SOURCE_KIS), path)
    budget = ub.resolve_budget(path)
    assert budget.available_cash_usd == pytest.approx(10_000.0)
    assert budget.validation_error() is None
