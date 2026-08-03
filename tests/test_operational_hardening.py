"""Operational hardening required before the Shadow timer may be armed.

All five come out of the Oracle read-only verification of 904b9ed:

  1. the installer put shared/state at 0770 root:trading. Since the
     limiter began failing closed on any file it could not have written,
     one `trading` group member could stop every service with one file.
  2. `us-stock-trading-live.service` carried
     `[Install] WantedBy=multi-user.target`, so a single stray
     `systemctl enable` would have armed real order placement at boot.
  3. the day's only candidate was IXN -- an ARCA listing with no KIS
     order exchange code -- so the KIS pipeline spent an analysis pass
     and a KIS read on something that could only end in
     UNSUPPORTED_EXCHANGE.
  4. every candidate reported `hypothetical=None`, because both the ARCA
     block and the unfunded-account block stop the evaluation before the
     Order Gate. The log answered "what would the live path have done?"
     with silence.
  5. no JSONL Shadow record was written at all; the durable record was
     in the database, which the runbook did not say.
"""
import configparser
import importlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import shadow_audit
from execution import idempotency
from market_data.exchange_registry import (
    ExchangeRegistry,
    partition_kis_executable,
    supported_analysis_exchanges,
)
from state_store import db as state_db

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
UNIT_DIR = REPO_ROOT / "deploy" / "systemd"
INSTALLER = SCRIPTS_DIR / "install_oracle_services.sh"
LIVE_UNIT = UNIT_DIR / "us-stock-trading-live.service"
RUNBOOK = REPO_ROOT / "docs" / "deployment" / "ORACLE_KIS_MIGRATION_RUNBOOK.md"

INSTALLER_SOURCE = INSTALLER.read_text(encoding="utf-8")
FAKE_SYSTEMCTL = Path(__file__).resolve().parent / "fake_systemctl.py"


def _stub(tmp_path, name, body="#!/bin/sh\nexit 0\n"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_systemctl(tmp_path):
    return _stub(tmp_path, "systemctl",
                 f"#!/bin/sh\nexec {sys.executable} {FAKE_SYSTEMCTL} \"$@\"\n")
SHADOW_SOURCE = (SCRIPTS_DIR / "run_shadow_mode.py").read_text(encoding="utf-8")

NOW = datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc)

UNIVERSE = """symbol,name,exchange,tradable,shortable
AAPL,Apple Inc,NASDAQ,True,True
KO,Coca-Cola,NYSE,True,True
IMO,Imperial Oil,NYSE_AMERICAN,True,True
IXN,iShares Global Tech ETF,ARCA,True,True
SPY,SPDR S&P 500,ARCA,True,True
PINK,Some OTC Name,OTC,True,False
"""


# =====================================================================
# 1. shared/state permissions
# =====================================================================

class TestSharedStatePermissions:
    def test_the_installer_creates_shared_state_as_0700_owner_only(self):
        assert 'install -d -m 0700 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${SHARED_DIR}/state"' \
            in INSTALLER_SOURCE

    def test_shared_state_is_no_longer_group_writable(self):
        for line in INSTALLER_SOURCE.splitlines():
            if "${SHARED_DIR}/state" in line and "install -d" in line:
                assert "0770" not in line, line
                assert "${SERVICE_GROUP}" not in line, line

    def test_the_installer_verifies_the_resulting_mode(self):
        """`install -d` re-applies to an existing directory, but a
        previous install may have left 0770 root:trading behind and a
        silent failure here is the whole exposure."""
        assert "stat -c '%a'" in INSTALLER_SOURCE
        assert 'if [ "${state_mode}" != "700" ]' in INSTALLER_SOURCE

    def test_the_log_directory_stays_group_shared(self):
        """Only shared/state is tightened; logs are read by other tools."""
        assert 'install -d -m 0770 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${LOG_DIR}"' \
            in INSTALLER_SOURCE

    def test_a_dry_run_plans_the_restricted_mode(self, tmp_path):
        """Executes the installer for real in DRY_RUN mode, so the
        assertion is about what it would run, not about its text."""
        env_dir = tmp_path / "etc"
        env_dir.mkdir()
        env_file = env_dir / "live-readonly.env"
        env_file.write_text(
            "KIS_LIVE_ORDER_ENABLED=false\nLIVE_ROLLOUT_ENABLED=false\n"
            "ALPACA_ORDER_ENABLED=false\nENTRY_DISABLED=true\n", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(INSTALLER)],
            env={**os.environ, "DRY_RUN": "1", "TRADING_RELEASE_ROOT": str(REPO_ROOT),
                 "TRADING_SHARED_ROOT": str(tmp_path / "shared"), "ENV_DIR": str(env_dir),
                 "ENV_FILE": str(env_file), "LOG_DIR": str(tmp_path / "logs"),
                 "UNIT_DIR": str(tmp_path / "units"), "PYTHON_BIN": sys.executable,
                 "SYSTEMD_ANALYZE_BIN": str(_stub(tmp_path, "systemd-analyze")),
                 "SYSTEMCTL_BIN": str(_fake_systemctl(tmp_path))},
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stderr
        planned = [line for line in result.stdout.splitlines()
                   if "shared/state" in line and "install -d" in line]
        assert planned, result.stdout
        assert all("-m 0700" in line for line in planned), planned

    def test_the_dry_run_never_enables_or_starts_anything_live(self, tmp_path):
        env_dir = tmp_path / "etc"
        env_dir.mkdir()
        env_file = env_dir / "live-readonly.env"
        env_file.write_text(
            "KIS_LIVE_ORDER_ENABLED=false\nLIVE_ROLLOUT_ENABLED=false\n"
            "ALPACA_ORDER_ENABLED=false\nENTRY_DISABLED=true\n", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(INSTALLER)],
            env={**os.environ, "DRY_RUN": "1", "TRADING_RELEASE_ROOT": str(REPO_ROOT),
                 "TRADING_SHARED_ROOT": str(tmp_path / "shared"), "ENV_DIR": str(env_dir),
                 "ENV_FILE": str(env_file), "LOG_DIR": str(tmp_path / "logs"),
                 "UNIT_DIR": str(tmp_path / "units"), "PYTHON_BIN": sys.executable,
                 "SYSTEMD_ANALYZE_BIN": str(_stub(tmp_path, "systemd-analyze")),
                 "SYSTEMCTL_BIN": str(_fake_systemctl(tmp_path))},
            capture_output=True, text=True, timeout=120,
        )
        for line in result.stdout.splitlines():
            if not line.startswith("DRY_RUN: systemctl"):
                continue
            if "us-stock-trading-live.service" in line:
                assert any(word in line for word in ("disable", "stop")), line


# =====================================================================
# 2. the live unit cannot be enabled
# =====================================================================

class TestLiveUnitIsNotEnableable:
    def _parsed(self):
        parser = configparser.ConfigParser(strict=False)
        parser.optionxform = str
        parser.read_string(LIVE_UNIT.read_text(encoding="utf-8"))
        return parser

    def test_it_has_no_install_section(self):
        """Without one, `systemctl enable` fails: there is no WantedBy
        target to link, so it cannot be pulled in at boot."""
        assert not self._parsed().has_section("Install")

    def test_no_wantedby_directive_survives_anywhere(self):
        for line in LIVE_UNIT.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert not stripped.startswith("WantedBy="), line
            assert not stripped.startswith("RequiredBy="), line

    def test_it_is_still_startable_by_hand(self):
        """Not enableable is not the same as unusable -- a reviewed,
        explicit start is the intended path to a first real order."""
        service = self._parsed()["Service"]
        assert service.get("Type") == "oneshot"
        assert "run_live_buy_entry.py" in service.get("ExecStart", "")
        assert "preflight_kis_live.py" in service.get("ExecStartPre", "")

    def test_the_installer_still_disables_a_legacy_enablement(self):
        """A previous install's symlink keeps working even after the
        [Install] section is gone, so the disable must stay."""
        assert 'disable "${LIVE_UNIT}"' in INSTALLER_SOURCE
        assert 'stop "${LIVE_UNIT}"' in INSTALLER_SOURCE

    def test_static_is_the_expected_state_not_a_failure(self):
        """Codex HIGH-1: the installer rejected `static`, which is
        exactly what a unit with no [Install] section reports, so every
        real install exited 1. `static` is the goal, and `disabled`
        would mean the [Install] section came back."""
        assert 'if [ "${sandbox_state}" != "static" ]' in INSTALLER_SOURCE
        assert "enabled|enabled-runtime|static|alias|indirect" not in INSTALLER_SOURCE
        assert 'report_state "${LIVE_UNIT}" "static"' in INSTALLER_SOURCE

    def test_the_installer_proves_enableability_rather_than_asserting_it(self):
        """A string check is what let HIGH-1 through; the installer now
        enables the unit in a throwaway --root sandbox and counts the
        symlinks it did not create."""
        assert '--root="${SANDBOX}" enable' in INSTALLER_SOURCE
        assert 'sandbox_links' in INSTALLER_SOURCE
        assert 'if [ "${sandbox_links}" != "0" ]' in INSTALLER_SOURCE

    def test_every_read_only_unit_is_still_enableable(self):
        for name in ("us-stock-trading-shadow.timer", "us-stock-trading-reconcile.timer",
                     "us-stock-trading-health.timer", "us-stock-trading-shadow-exit.timer",
                     "us-stock-trading-migrate.service"):
            parser = configparser.ConfigParser(strict=False)
            parser.read_string((UNIT_DIR / name).read_text(encoding="utf-8"))
            assert parser.has_section("Install"), name


# =====================================================================
# 3. analysis candidates vs KIS-executable candidates
# =====================================================================

@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "universe.csv"
    path.write_text(UNIVERSE, encoding="utf-8")
    return ExchangeRegistry(universe_file=path)


class TestKisExecutablePartition:
    def test_the_three_kis_venues_are_executable(self, registry):
        executable, excluded = partition_kis_executable(
            ["AAPL", "KO", "IMO"], registry=registry)
        assert [symbol for symbol, _ in executable] == ["AAPL", "KO", "IMO"]
        assert excluded == []

    def test_supported_venues_are_exactly_nasdaq_nyse_and_nyse_american(self):
        assert set(supported_analysis_exchanges()) == {"NASDAQ", "NYSE", "NYSE_AMERICAN"}

    def test_arca_is_excluded_with_a_reason(self, registry):
        executable, excluded = partition_kis_executable(["IXN"], registry=registry)
        assert executable == []
        assert [item.symbol for item in excluded] == ["IXN"]
        assert excluded[0].reason_code == "UNSUPPORTED_EXCHANGE"
        assert "ARCA" in excluded[0].detail

    def test_an_unknown_symbol_is_excluded_separately_from_an_unsupported_one(self, registry):
        _, excluded = partition_kis_executable(["NOSUCHTICKER"], registry=registry)
        assert excluded[0].reason_code == "EXCHANGE_UNKNOWN"

    def test_otc_is_excluded(self, registry):
        _, excluded = partition_kis_executable(["PINK"], registry=registry)
        assert excluded[0].reason_code == "UNSUPPORTED_EXCHANGE"

    def test_a_mixed_list_splits_and_keeps_order(self, registry):
        executable, excluded = partition_kis_executable(
            ["IXN", "AAPL", "SPY", "KO"], registry=registry)
        assert [symbol for symbol, _ in executable] == ["AAPL", "KO"]
        assert [item.symbol for item in excluded] == ["IXN", "SPY"]

    def test_the_resolved_record_comes_back_so_it_is_not_resolved_twice(self, registry):
        executable, _ = partition_kis_executable(["AAPL"], registry=registry)
        _, record = executable[0]
        assert record.exchange.value == "NASDAQ"
        assert record.kis_order_exchange_code == "NASD"

    def test_symbols_are_normalised(self, registry):
        executable, _ = partition_kis_executable([" aapl ", ""], registry=registry)
        assert [symbol for symbol, _ in executable] == ["AAPL"]

    def test_it_writes_nothing(self, registry, tmp_path):
        """The analysis artefacts are untouched: this only decides what
        the KIS pipeline is handed."""
        before = sorted(p.name for p in tmp_path.iterdir())
        partition_kis_executable(["IXN", "AAPL"], registry=registry)
        assert sorted(p.name for p in tmp_path.iterdir()) == before

    def test_an_excluded_record_is_audit_safe(self, registry):
        _, excluded = partition_kis_executable(["IXN"], registry=registry)
        payload = excluded[0].as_dict()
        assert set(payload) == {"symbol", "reason_code", "detail"}


# =====================================================================
# 4. the Shadow pipeline: exclusion and the pre-gate record
# =====================================================================

def _shadow_module():
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        return importlib.import_module("run_shadow_mode")
    finally:
        sys.path.remove(str(SCRIPTS_DIR))


class _Broker:
    """Records every method the pipeline reaches for. Any order or cancel
    method raises: the transport must stay at zero."""

    def __init__(self, *, usd=0.0, price=100.0):
        self.calls = []
        self._usd = usd
        self._price = price

    def _account(self):
        class Snapshot:
            account_id = "44xxxxxx"
            usd_available_for_new_order = self._usd
        return Snapshot()

    def get_account_snapshot(self):
        self.calls.append("get_account_snapshot")
        return self._account()

    def get_open_orders(self):
        self.calls.append("get_open_orders")
        return []

    def get_current_price(self, instrument):
        self.calls.append(f"get_current_price:{instrument.symbol}")
        return self._price

    def get_positions(self):
        self.calls.append("get_positions")
        return []

    def get_fills(self, **kwargs):
        self.calls.append("get_fills")
        return []

    def submit_order(self, *args, **kwargs):       # pragma: no cover
        raise AssertionError("shadow reached an order transport")

    def cancel_order(self, *args, **kwargs):       # pragma: no cover
        raise AssertionError("shadow reached a cancel transport")


class _Rollout:
    allowed_symbols = frozenset()
    max_quantity_per_order = 1
    max_price_deviation_percent = 30.0
    regular_session_only = False


@pytest.fixture
def shadow_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(tmp_path / "SHADOW.jsonl"))
    monkeypatch.setenv("RECONCILIATION_STATE_FILE", str(tmp_path / "RECONCILIATION.json"))
    monkeypatch.setenv("KIS_LIVE_ORDER_ENABLED", "false")
    monkeypatch.setenv("ENTRY_DISABLED", "true")
    monkeypatch.delenv("SHADOW_ALLOWED_SYMBOLS", raising=False)
    universe = tmp_path / "universe.csv"
    universe.write_text(UNIVERSE, encoding="utf-8")
    monkeypatch.setenv("UNIVERSE_FILE", str(universe))
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "IDEMPOTENCY.lock")
    from market_data import exchange_registry

    exchange_registry.reset_registry()
    state_db.open_db().close()
    yield tmp_path
    exchange_registry.reset_registry()


def _events(symbol=None):
    conn = state_db.open_db()
    try:
        rows = conn.execute(
            "select symbol, event_type, result, reason_code, payload "
            "from shadow_audit_events order by rowid").fetchall()
    finally:
        conn.close()
    return [dict(symbol=r[0], event_type=r[1], result=r[2], reason_code=r[3],
                 payload=json.loads(r[4]) if r[4] else None)
            for r in rows if symbol is None or r[0] == symbol]


class TestExcludedCandidatesNeverEnterTheKisPipeline:
    def test_an_arca_candidate_costs_no_analysis_and_no_kis_read(self, shadow_env,
                                                                  monkeypatch):
        module = _shadow_module()
        analysed = []
        monkeypatch.setattr(module.pso, "analyze_stock",
                            lambda symbol: analysed.append(symbol) or None)
        broker = _Broker()
        outcomes = module.run_once(broker=broker, rollout=_Rollout(),
                                   watchlist=["IXN"], now=NOW)
        assert analysed == [], "an unexecutable candidate was still analysed"
        assert broker.calls == [], "an unexecutable candidate still reached KIS"
        assert [o["symbol"] for o in outcomes] == ["IXN"]

    def test_it_is_recorded_rather_than_silently_dropped(self, shadow_env, monkeypatch):
        module = _shadow_module()
        monkeypatch.setattr(module.pso, "analyze_stock", lambda symbol: None)
        module.run_once(broker=_Broker(), rollout=_Rollout(), watchlist=["IXN"], now=NOW)
        events = _events("IXN")
        types = [e["event_type"] for e in events]
        assert "KIS_PIPELINE_EXCLUDED" in types
        excluded = next(e for e in events if e["event_type"] == "KIS_PIPELINE_EXCLUDED")
        assert excluded["reason_code"] == "UNSUPPORTED_EXCHANGE"
        assert excluded["payload"]["kis_pipeline"] is False
        assert "ARCA" in excluded["payload"]["detail"]

    def test_it_still_ends_in_exactly_one_terminal_event(self, shadow_env, monkeypatch):
        module = _shadow_module()
        monkeypatch.setattr(module.pso, "analyze_stock", lambda symbol: None)
        module.run_once(broker=_Broker(), rollout=_Rollout(), watchlist=["IXN"], now=NOW)
        terminal = [e for e in _events("IXN")
                    if e["event_type"] in shadow_audit.TERMINAL_EVENT_TYPES]
        assert len(terminal) == 1

    def test_the_outcome_says_the_gate_does_not_apply(self, shadow_env, monkeypatch):
        module = _shadow_module()
        monkeypatch.setattr(module.pso, "analyze_stock", lambda symbol: None)
        outcomes = module.run_once(broker=_Broker(), rollout=_Rollout(),
                                   watchlist=["IXN"], now=NOW)
        assert outcomes[0]["hypothetical"] == "NOT_APPLICABLE:KIS_PIPELINE_EXCLUDED"
        assert outcomes[0]["reason_code"] == "UNSUPPORTED_EXCHANGE"

    def test_a_nasdaq_candidate_still_enters_the_pipeline(self, shadow_env, monkeypatch):
        module = _shadow_module()
        analysed = []
        monkeypatch.setattr(module.pso, "analyze_stock",
                            lambda symbol: analysed.append(symbol) or None)
        module.run_once(broker=_Broker(), rollout=_Rollout(), watchlist=["AAPL"], now=NOW)
        assert analysed == ["AAPL"], "an executable candidate was filtered out"

    def test_a_mixed_watchlist_splits(self, shadow_env, monkeypatch):
        module = _shadow_module()
        analysed = []
        monkeypatch.setattr(module.pso, "analyze_stock",
                            lambda symbol: analysed.append(symbol) or None)
        outcomes = module.run_once(broker=_Broker(), rollout=_Rollout(),
                                   watchlist=["IXN", "AAPL"], now=NOW)
        assert analysed == ["AAPL"]
        assert {o["symbol"] for o in outcomes} == {"IXN", "AAPL"}


class TestPreGateRecordOnAnEarlyBlock:
    def _evaluate(self, module, broker, *, symbol="AAPL", conn=None):
        class _Quote:
            price_usd = 100.0
        return module._evaluate_symbol(
            symbol=symbol, broker=broker, rollout=_Rollout(), conn=conn,
            kis_validation=type("V", (), {"get_price_quote": staticmethod(
                lambda s: _Quote())})(),
            deployed_commit="abc", validated_commit="abc",
            allowed_account_no="44xxxxxx", is_regular_session=True, now=NOW,
        )

    def test_an_unfunded_account_no_longer_reports_nothing(self, shadow_env, monkeypatch):
        """The reported gap: `hypothetical=None` for every candidate."""
        module = _shadow_module()
        monkeypatch.setattr(module.pso, "analyze_stock",
                            lambda symbol: {"score": 999, "price": 100.0})
        outcome = self._evaluate(module, _Broker(usd=0.0))
        assert outcome["reason_code"] == "INSUFFICIENT_CASH"
        assert outcome["hypothetical"] == "NOT_EVALUATED:CASH"

    def test_the_audit_records_which_pre_gate_checks_ran(self, shadow_env, monkeypatch):
        module = _shadow_module()
        monkeypatch.setattr(module.pso, "analyze_stock",
                            lambda symbol: {"score": 999, "price": 100.0})
        self._evaluate(module, _Broker(usd=0.0))
        record = next(e for e in _events("AAPL")
                      if e["event_type"] == "HYPOTHETICAL_INCOMPLETE")
        payload = record["payload"]
        assert payload["pre_gate_stages_passed"] == ["EXCHANGE", "PRICE", "ACCOUNT_READ"]
        assert payload["blocked_at"] == "CASH"
        assert payload["blocked_reason"] == "INSUFFICIENT_CASH"
        assert payload["pre_gate_stages_not_evaluated"] == ["ORDER_INTENT", "RECONCILIATION"]

    def test_it_never_claims_a_gate_verdict(self, shadow_env, monkeypatch):
        """The gate did not run. Inventing an answer would be worse than
        recording none -- so the record says so explicitly."""
        module = _shadow_module()
        monkeypatch.setattr(module.pso, "analyze_stock",
                            lambda symbol: {"score": 999, "price": 100.0})
        self._evaluate(module, _Broker(usd=0.0))
        events = _events("AAPL")
        record = next(e for e in events if e["event_type"] == "HYPOTHETICAL_INCOMPLETE")
        assert record["payload"]["order_gate_evaluated"] is False
        assert record["result"] == "INFO"
        assert not [e for e in events if e["event_type"] in ("GATE_APPROVED", "GATE_REJECTED")]

    def test_the_gate_itself_is_not_bypassed(self):
        """Both evaluations still call the real gate with real context."""
        assert "order_gate.evaluate_buy_gate(_ctx(" in SHADOW_SOURCE
        assert "live_order_enabled=klt_live_order_enabled(), entry_disabled=klt_entry_disabled()" \
            in SHADOW_SOURCE
        assert "order_gate.evaluate_buy_gate(_ctx(live_order_enabled=True, entry_disabled=False))" \
            in SHADOW_SOURCE

    def test_no_order_transport_is_reached(self, shadow_env, monkeypatch):
        module = _shadow_module()
        monkeypatch.setattr(module.pso, "analyze_stock",
                            lambda symbol: {"score": 999, "price": 100.0})
        broker = _Broker(usd=0.0)
        self._evaluate(module, broker)
        assert not [c for c in broker.calls if "submit" in c or "cancel" in c]

    def test_an_exchange_block_records_a_zero_progress_run(self, shadow_env, monkeypatch):
        """Kept reachable as defence in depth even though the partition
        now stops these before the evaluation starts."""
        module = _shadow_module()
        monkeypatch.setattr(module.pso, "analyze_stock",
                            lambda symbol: {"score": 999, "price": 100.0})
        outcome = self._evaluate(module, _Broker(), symbol="IXN")
        assert outcome["reason_code"] == "UNSUPPORTED_EXCHANGE"
        assert outcome["hypothetical"] == "NOT_EVALUATED:EXCHANGE"
        record = next(e for e in _events("IXN")
                      if e["event_type"] == "HYPOTHETICAL_INCOMPLETE")
        assert record["payload"]["pre_gate_stages_passed"] == []
        assert record["payload"]["blocked_at"] == "EXCHANGE"

    def test_a_below_threshold_candidate_records_no_pre_gate_event(self, shadow_env,
                                                                    monkeypatch):
        """It was never blocked -- it never signalled. A record here
        would be noise, not evidence."""
        module = _shadow_module()
        monkeypatch.setattr(module.pso, "analyze_stock", lambda symbol: None)
        self._evaluate(module, _Broker())
        assert not [e for e in _events("AAPL")
                    if e["event_type"] == "HYPOTHETICAL_INCOMPLETE"]

    def test_the_event_type_is_registered(self):
        assert "HYPOTHETICAL_INCOMPLETE" in shadow_audit.EVENT_TYPES
        assert "KIS_PIPELINE_EXCLUDED" in shadow_audit.EVENT_TYPES
        assert "HYPOTHETICAL_INCOMPLETE" not in shadow_audit.TERMINAL_EVENT_TYPES
        assert "KIS_PIPELINE_EXCLUDED" not in shadow_audit.TERMINAL_EVENT_TYPES


# =====================================================================
# 5. the runbook says where the durable record actually is
# =====================================================================

class TestRunbookRecordsWhereTheAuditLives:
    def test_it_names_the_database_table(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        assert "shadow_audit_events" in text

    def test_it_says_the_jsonl_may_be_absent(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        assert "SHADOW_MODE_LOG_FILE" in text
        assert "JSONL" in text

    def test_it_explains_when_the_jsonl_is_written(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        lowered = text.lower()
        assert "order gate" in lowered

    def test_it_documents_the_configured_jsonl_path(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        assert "SHADOW_MODE_LOG_DIR" in text
        assert "release root fallback" in text or "release 루트" in text

    def test_it_says_live_is_static_not_disabled(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        assert "static 이어야 한다" in text
        assert "# disabled 이어야 한다" not in text

    def test_it_separates_installation_from_timer_activation(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        assert "enable_oracle_shadow_timer.sh" in text
        assert "ALLOW_SHADOW_TIMER_ENABLE" in text
        assert "단계 A" in text and "단계 B" in text


class TestTheFullPathIsUnchanged:
    """The pre-gate record must not displace the real thing: when a
    candidate does reach the Order Gate, both verdicts are still
    recorded and no incomplete record is written."""

    def test_a_funded_candidate_reaches_the_gate(self, shadow_env, monkeypatch):
        module = _shadow_module()
        monkeypatch.setattr(module.pso, "analyze_stock",
                            lambda symbol: {"score": 999, "price": 100.0})

        class _Quote:
            price_usd = 100.0

        conn = state_db.open_db()
        try:
            outcome = module._evaluate_symbol(
                symbol="AAPL", broker=_Broker(usd=1000.0), rollout=_Rollout(), conn=conn,
                kis_validation=type("V", (), {"get_price_quote": staticmethod(
                    lambda s: _Quote())})(),
                deployed_commit="abc", validated_commit="abc",
                allowed_account_no="44xxxxxx", is_regular_session=True, now=NOW,
            )
        finally:
            conn.close()

        events = _events("AAPL")
        types = [e["event_type"] for e in events]
        assert "HYPOTHETICAL_INCOMPLETE" not in types, (
            "the pre-gate record fired even though the gate ran")
        assert outcome["hypothetical"] is not None
        assert outcome["hypothetical"] != "NOT_EVALUATED:None"
        assert any(t in types for t in ("GATE_APPROVED", "GATE_REJECTED"))

    def test_the_real_flags_still_block_it(self, shadow_env, monkeypatch):
        """KIS_LIVE_ORDER_ENABLED=false / ENTRY_DISABLED=true is the
        honest record of what the deployment would do right now."""
        module = _shadow_module()
        monkeypatch.setattr(module.pso, "analyze_stock",
                            lambda symbol: {"score": 999, "price": 100.0})

        class _Quote:
            price_usd = 100.0

        conn = state_db.open_db()
        try:
            outcome = module._evaluate_symbol(
                symbol="AAPL", broker=_Broker(usd=1000.0), rollout=_Rollout(), conn=conn,
                kis_validation=type("V", (), {"get_price_quote": staticmethod(
                    lambda s: _Quote())})(),
                deployed_commit="abc", validated_commit="abc",
                allowed_account_no="44xxxxxx", is_regular_session=True, now=NOW,
            )
        finally:
            conn.close()
        assert outcome["result"] == "BLOCKED"
        assert outcome["reason_code"].startswith("GATE:")


# =====================================================================
# 6. The Shadow JSONL never lands in the release directory.
# =====================================================================

class TestJsonlPathIsConfiguredNotGuessed:
    """The reviewer's own probe wrote `shadow-2026-08-04.jsonl` into the
    repository root, because an unset SHADOW_MODE_LOG_FILE fell back to
    this module's directory -- which on a deployed host is the release
    root. Records landed where the next release cannot see them and
    where the stray-artifact check trips over them."""

    def _record(self, **overrides):
        import shadow_mode

        kwargs = dict(
            signal_id="sig-1", strategy_id="s", strategy_version="v1",
            code_commit="abc", symbol="AAPL", side="buy", alpaca_signal_price=100.0,
            kis_validation_price=100.0, price_difference_percent=0.0,
            planned_quantity=1, planned_limit_price=100.0, stop_price=92.0,
            target_price=108.0, risk_gate_result="BLOCKED", rejection_reason=None,
            account_available_usd=0.0, existing_position_quantity=0,
            existing_open_order=False, now=NOW,
        )
        kwargs.update(overrides)
        return shadow_mode.build_record(**kwargs)

    def test_unset_writes_nothing_anywhere(self, monkeypatch, tmp_path):
        import shadow_mode

        monkeypatch.delenv("SHADOW_MODE_LOG_FILE", raising=False)
        monkeypatch.delenv("SHADOW_MODE_LOG_DIR", raising=False)
        before = sorted(p.name for p in REPO_ROOT.glob("shadow-*.jsonl"))
        assert shadow_mode.persist(self._record()) is None
        after = sorted(p.name for p in REPO_ROOT.glob("shadow-*.jsonl"))
        assert after == before, "a JSONL file appeared in the release root"
        assert sorted(tmp_path.glob("*.jsonl")) == []

    def test_unset_means_jsonl_disabled(self, monkeypatch):
        import shadow_mode

        monkeypatch.delenv("SHADOW_MODE_LOG_FILE", raising=False)
        monkeypatch.delenv("SHADOW_MODE_LOG_DIR", raising=False)
        assert shadow_mode.jsonl_enabled() is False
        assert shadow_mode._resolve_log_path() is None

    def test_unset_announces_the_database_backend(self, monkeypatch, caplog):
        import shadow_mode

        monkeypatch.delenv("SHADOW_MODE_LOG_FILE", raising=False)
        monkeypatch.delenv("SHADOW_MODE_LOG_DIR", raising=False)
        monkeypatch.setattr(shadow_mode, "_DB_ONLY_ANNOUNCED", False)
        with caplog.at_level("INFO"):
            shadow_mode.persist(self._record())
        assert "shadow_audit_backend=database" in caplog.text
        assert "jsonl_enabled=false" in caplog.text

    def test_unset_reads_back_empty_rather_than_globbing_the_release(self, monkeypatch):
        import shadow_mode

        monkeypatch.delenv("SHADOW_MODE_LOG_FILE", raising=False)
        monkeypatch.delenv("SHADOW_MODE_LOG_DIR", raising=False)
        records, corruption = shadow_mode.read_all_with_integrity()
        assert records == [] and corruption == []

    def test_a_configured_directory_receives_the_file(self, monkeypatch, tmp_path):
        import shadow_mode

        monkeypatch.delenv("SHADOW_MODE_LOG_FILE", raising=False)
        monkeypatch.setenv("SHADOW_MODE_LOG_DIR", str(tmp_path))
        shadow_mode.persist(self._record())
        written = sorted(p.name for p in tmp_path.glob("shadow-*.jsonl"))
        assert written == [f"shadow-{NOW.date().isoformat()}.jsonl"], written
        assert sorted(REPO_ROOT.glob("shadow-*.jsonl")) == []

    def test_a_configured_file_receives_exactly_that_path(self, monkeypatch, tmp_path):
        import shadow_mode

        target = tmp_path / "shadow-mode.jsonl"
        monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(target))
        monkeypatch.delenv("SHADOW_MODE_LOG_DIR", raising=False)
        shadow_mode.persist(self._record())
        assert target.exists()
        assert sorted(REPO_ROOT.glob("shadow-*.jsonl")) == []

    def test_an_unwritable_configured_path_raises_rather_than_falling_back(
            self, monkeypatch, tmp_path):
        import shadow_mode

        monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(tmp_path / "nope" / "x" / "f.jsonl"))
        monkeypatch.setattr(shadow_mode.Path, "mkdir",
                            lambda *a, **k: (_ for _ in ()).throw(PermissionError("no")))
        with pytest.raises(Exception):
            shadow_mode.persist(self._record())
        assert sorted(REPO_ROOT.glob("shadow-*.jsonl")) == []

    def test_purging_is_a_no_op_when_jsonl_is_off(self, monkeypatch):
        import shadow_mode

        monkeypatch.delenv("SHADOW_MODE_LOG_FILE", raising=False)
        monkeypatch.delenv("SHADOW_MODE_LOG_DIR", raising=False)
        assert shadow_mode.purge_old_files() == []

    def test_the_db_audit_still_records_with_jsonl_off(self, shadow_env, monkeypatch):
        """The point of the policy: turning the file off must not turn
        the durable record off."""
        monkeypatch.delenv("SHADOW_MODE_LOG_FILE", raising=False)
        monkeypatch.delenv("SHADOW_MODE_LOG_DIR", raising=False)
        module = _shadow_module()
        monkeypatch.setattr(module.pso, "analyze_stock", lambda s: None)
        module.run_once(broker=_Broker(), rollout=_Rollout(), watchlist=["IXN"], now=NOW)
        assert [e for e in _events("IXN")
                if e["event_type"] == "KIS_PIPELINE_EXCLUDED"]
        assert sorted(REPO_ROOT.glob("shadow-*.jsonl")) == []

    def test_the_module_has_no_implicit_default_path(self):
        import shadow_mode

        source = Path(shadow_mode.__file__).read_text(encoding="utf-8")
        assert "DEFAULT_LOG_FILE" not in source
        assert 'BASE_DIR / f"shadow-' not in source
        assert 'BASE_DIR.glob' not in source
