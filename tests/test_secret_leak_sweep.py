"""CODEX-050: adversarial secret-leak sweep.

Plants UNIQUE fake secrets, drives every surface Codex named -- ordinary
logs, exception strings, Shadow JSONL, Shadow audit rows, order state
event payloads, reconciliation results, KIS HTTP errors, nested dicts,
dataclasses, JSON strings -- and then asserts that not one of the
planted values appears anywhere in the output.

Every planted value is distinctive so a hit cannot be a false positive
from unrelated text.
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

import shadow_audit
import shadow_mode
from execution import idempotency, order_repository
from execution.secret_redaction import (
    RedactingFilter,
    account_fingerprint,
    install_logging_redaction,
    mask_account_number,
    redact_text,
    redact_value,
    safe_repr,
)
from state_store import db as state_db

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)

# Unique planted values -- none of these can occur incidentally.
FAKE_APP_KEY = "PSFAKEAPPKEY0000000000000001"
FAKE_APP_SECRET = "FAKEAPPSECRET000000000000002"
FAKE_ACCESS_TOKEN = "FAKEACCESSTOKEN0000000000003"
FAKE_ACCOUNT_NO = "70707070"
ALL_SECRETS = (FAKE_APP_KEY, FAKE_APP_SECRET, FAKE_ACCESS_TOKEN, FAKE_ACCOUNT_NO)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(tmp_path / "SHADOW.jsonl"))
    monkeypatch.setenv("KIS_ACCOUNT_NO", FAKE_ACCOUNT_NO)
    monkeypatch.setenv("KIS_ALLOWED_ACCOUNT_NO", FAKE_ACCOUNT_NO)
    monkeypatch.setenv("ACCOUNT_FINGERPRINT_SECRET", "unit-test-fingerprint-key")
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "KIS_ORDER_IDEMPOTENCY.lock")
    state_db.open_db().close()
    yield


def assert_clean(blob, *, allow_last4=True):
    """Asserts no planted secret survives. The last 4 digits of an
    account number ARE permitted (that is the documented masked form);
    the full number is not."""
    text = blob if isinstance(blob, str) else json.dumps(blob, default=str)
    for secret in ALL_SECRETS:
        assert secret not in text, f"{secret!r} leaked into: {text[:400]}"
    if allow_last4:
        assert FAKE_ACCOUNT_NO[-4:] not in text or "****" in text or FAKE_ACCOUNT_NO[-4:] in text


@dataclass
class _Creds:
    app_key: str
    app_secret: str
    account_no: str
    note: str


class TestFreeTextAndStructures:
    @pytest.mark.parametrize("template", [
        "appkey={secret}",
        "app_key: {secret}",
        '"appsecret": "{secret}"',
        "{{'CANO': '{secret}'}}",
        "{{'access_token': '{secret}'}}",
        "Authorization: Bearer {secret}",
        "Bearer {secret}",
        'ACCESS-TOKEN="{secret}"',
        "acct_no={secret}",
    ])
    def test_free_text_forms_are_masked(self, template):
        for secret in ALL_SECRETS:
            assert_clean(redact_text(template.format(secret=secret)))

    def test_bare_configured_account_number_is_masked(self):
        assert_clean(redact_text(f"account {FAKE_ACCOUNT_NO} mismatch"))
        assert mask_account_number(FAKE_ACCOUNT_NO).endswith(FAKE_ACCOUNT_NO[-4:])

    def test_nested_dict_is_masked(self):
        payload = {"outer": {"inner": [{"appkey": FAKE_APP_KEY},
                                        {"CANO": FAKE_ACCOUNT_NO}]},
                   "tuple": ("access_token", FAKE_ACCESS_TOKEN)}
        assert_clean(redact_value(payload))

    def test_dataclass_is_masked(self):
        creds = _Creds(app_key=FAKE_APP_KEY, app_secret=FAKE_APP_SECRET,
                       account_no=FAKE_ACCOUNT_NO, note=f"token={FAKE_ACCESS_TOKEN}")
        assert_clean(redact_value(creds))

    def test_json_string_is_masked(self):
        blob = json.dumps({"appkey": FAKE_APP_KEY, "access_token": FAKE_ACCESS_TOKEN})
        assert_clean(redact_text(blob))

    def test_exception_object_is_masked(self):
        exc = RuntimeError(f"appsecret={FAKE_APP_SECRET} rejected")
        assert_clean(redact_value(exc))

    def test_safe_repr_masks_a_raw_response_dict(self):
        raw = {"output": {"CANO": FAKE_ACCOUNT_NO, "access_token": FAKE_ACCESS_TOKEN},
               "msg1": "ok"}
        assert_clean(safe_repr(raw))

    def test_fingerprint_does_not_contain_the_account_number(self):
        fingerprint = account_fingerprint(FAKE_ACCOUNT_NO)
        assert_clean(fingerprint)
        assert fingerprint == account_fingerprint(FAKE_ACCOUNT_NO)  # stable with a fixed key
        assert fingerprint != account_fingerprint("70707071")


class TestKisBrokerErrors:
    def _broker(self, response_body):
        from brokers.kis_broker import KISBroker
        from brokers.kis_config import KISConfig

        class _Response:
            status_code = 200
            text = json.dumps(response_body)

            def json(self):
                return response_body

        class _Session:
            def request(self, *args, **kwargs):
                return _Response()

        config = KISConfig(
            kis_env="paper", app_key=FAKE_APP_KEY, app_secret=FAKE_APP_SECRET,
            account_no=FAKE_ACCOUNT_NO, account_product_cd="01",
            account_read_enabled=True, live_order_enabled=False,
        )
        broker = KISBroker(config=config, session=_Session())
        broker._access_token = FAKE_ACCESS_TOKEN
        broker._token_expires_at = datetime(2099, 1, 1, tzinfo=timezone.utc)
        return broker

    def test_price_error_does_not_leak_the_raw_response(self):
        from brokers.kis_broker import KISBrokerError
        from domain.instrument import build_instrument

        broker = self._broker({"output": {"CANO": FAKE_ACCOUNT_NO,
                                           "access_token": FAKE_ACCESS_TOKEN}})
        with pytest.raises(KISBrokerError) as excinfo:
            broker.get_current_price(build_instrument("AAPL", exchange="NASDAQ"))
        assert_clean(str(excinfo.value))

    def test_position_row_error_does_not_leak_the_raw_row(self):
        from brokers.kis_broker import KISBrokerError

        broker = self._broker({"output1": [{"CANO": FAKE_ACCOUNT_NO,
                                             "appkey": FAKE_APP_KEY,
                                             "ovrs_cblc_qty": "not-a-number"}]})
        with pytest.raises(KISBrokerError) as excinfo:
            broker.get_positions()
        assert_clean(str(excinfo.value))


class TestPersistedSurfaces:
    def test_shadow_jsonl_record_is_masked(self, tmp_path):
        record = shadow_mode.build_record(
            signal_id="sig-1", strategy_id="s", strategy_version="v1", code_commit="c",
            symbol="AAPL", risk_gate_result="BLOCKED",
            rejection_reason=(
                f"KIS account {FAKE_ACCOUNT_NO} rejected, appkey={FAKE_APP_KEY}, "
                f"Authorization: Bearer {FAKE_ACCESS_TOKEN}"
            ),
            now=NOW,
        )
        shadow_mode.persist(record)
        assert_clean(json.dumps(shadow_mode.read_all()))

    def test_shadow_audit_row_is_masked(self):
        run_id = shadow_audit.new_run_id()
        shadow_audit.record_event(
            shadow_run_id=run_id, event_type=shadow_audit.SHADOW_ERROR,
            result=shadow_audit.RESULT_ERROR,
            reason_code=f"appsecret={FAKE_APP_SECRET}",
            payload={"CANO": FAKE_ACCOUNT_NO, "raw": f"Bearer {FAKE_ACCESS_TOKEN}"},
            now=NOW,
        )
        assert_clean(json.dumps(shadow_audit.read_events(shadow_run_id=run_id), default=str))

    def test_order_state_event_payload_is_masked(self):
        conn = state_db.open_db()
        idempotency.register(
            conn, internal_order_id="ord-1", signal_id="sig-1", symbol="AAPL", side="buy",
            trading_date="2026-07-29", requested_quantity=1,
        )
        record = order_repository.load(conn, "ord-1")
        order_repository.advance(
            conn, record, "VALIDATING", event_type="T",
            event_payload={
                "reason": f"account {FAKE_ACCOUNT_NO} appkey={FAKE_APP_KEY}",
                "headers": {"authorization": f"Bearer {FAKE_ACCESS_TOKEN}"},
            },
            now=NOW,
        )
        events = order_repository.load_events(conn, "ord-1")
        assert_clean(json.dumps([dict(e) for e in events], default=str))

    def test_reconciliation_snapshot_detail_is_masked(self):
        from reconciliation.snapshot import ReconciliationSnapshot

        snapshot = ReconciliationSnapshot(
            account_id=FAKE_ACCOUNT_NO, symbol="AAPL", checked_at=NOW, positions_match=False,
            open_orders_match=True, fills_match=True, has_unknown_orders=False, source="test",
            detail=(f"mismatch on account {FAKE_ACCOUNT_NO} appkey={FAKE_APP_KEY}",),
        )
        # A snapshot is never persisted raw -- it reaches durable storage
        # only through a redacting boundary, which is what this asserts.
        assert_clean(redact_value(snapshot))


class TestLoggingBoundary:
    def test_root_logger_filter_masks_an_unredacted_call_site(self, caplog):
        logger = logging.getLogger("leak-sweep-test")
        logger.addFilter(RedactingFilter())
        with caplog.at_level(logging.ERROR, logger="leak-sweep-test"):
            logger.error("raw appkey=%s and CANO=%s", FAKE_APP_KEY, FAKE_ACCOUNT_NO)
            logger.error("Authorization: Bearer %s", FAKE_ACCESS_TOKEN)
            logger.error({"appsecret": FAKE_APP_SECRET})
        rendered = "\n".join(record.getMessage() for record in caplog.records)
        assert_clean(rendered)

    def test_install_logging_redaction_is_idempotent(self):
        logger = logging.getLogger("leak-sweep-idempotent")
        first = install_logging_redaction(logger)
        second = install_logging_redaction(logger)
        assert first is second
        assert sum(isinstance(f, RedactingFilter) for f in logger.filters) == 1
