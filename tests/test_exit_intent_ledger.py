"""CODEX-024: durable exit-intent ledger tests. Isolated to a tmp_path
SQLite database -- never touches the real TRADING_STATE.db."""
import pytest

from state_store import db, exit_intent_ledger as eil


@pytest.fixture
def conn(tmp_path):
    connection = db.open_db(tmp_path / "test_state.db")
    yield connection
    connection.close()


def test_migration_2_creates_exit_intents_table(conn):
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "exit_intents" in tables
    assert db.get_schema_version(conn) >= 2  # >= : later migrations (e.g. CODEX-028) may have applied too


def test_reserve_creates_intent_in_reserved_state(conn):
    intent_id = eil.reserve(conn, "pos_1", "STOP_LOSS", 10, "exit-coid-1")
    intent = eil.get_by_id(conn, intent_id)
    assert intent["state"] == eil.STATE_RESERVED
    assert intent["position_id"] == "pos_1"
    assert intent["requested_qty"] == 10
    assert intent["client_order_id"] == "exit-coid-1"


def test_reserve_duplicate_active_intent_rejected(conn):
    eil.reserve(conn, "pos_1", "STOP_LOSS", 10, "exit-coid-1")
    with pytest.raises(eil.DuplicateExitIntentError):
        eil.reserve(conn, "pos_1", "TARGET_2", 10, "exit-coid-2")


def test_reserve_after_prior_intent_confirmed_succeeds(conn):
    first = eil.reserve(conn, "pos_1", "STOP_LOSS", 10, "exit-coid-1")
    eil.mark_submitted(conn, first, broker_order_id="broker-1")
    eil.mark_confirmed(conn, first, confirmed_filled_qty=10)

    second = eil.reserve(conn, "pos_1", "PARTIAL_TARGET_1", 5, "exit-coid-2")
    assert second != first
    assert eil.get_active_intent(conn, "pos_1")["intent_id"] == second


def test_reserve_after_prior_intent_aborted_succeeds(conn):
    first = eil.reserve(conn, "pos_1", "STOP_LOSS", 10, "exit-coid-1")
    eil.mark_aborted(conn, first)
    second = eil.reserve(conn, "pos_1", "STOP_LOSS", 10, "exit-coid-2")
    assert second != first


def test_get_active_intent_none_when_no_intent_exists(conn):
    assert eil.get_active_intent(conn, "pos_unknown") is None


def test_get_active_intent_returns_reserved_or_submitted_but_not_terminal(conn):
    intent_id = eil.reserve(conn, "pos_1", "STOP_LOSS", 10, "exit-coid-1")
    assert eil.get_active_intent(conn, "pos_1")["intent_id"] == intent_id

    eil.mark_submitted(conn, intent_id)
    assert eil.get_active_intent(conn, "pos_1")["intent_id"] == intent_id

    eil.mark_confirmed(conn, intent_id, confirmed_filled_qty=10)
    assert eil.get_active_intent(conn, "pos_1") is None


def test_submission_unknown_then_reconciliation_required_then_confirmed(conn):
    intent_id = eil.reserve(conn, "pos_1", "STOP_LOSS", 10, "exit-coid-1")
    eil.mark_submission_unknown(conn, intent_id)
    assert eil.get_by_id(conn, intent_id)["state"] == eil.STATE_SUBMISSION_UNKNOWN
    assert eil.get_active_intent(conn, "pos_1")["intent_id"] == intent_id  # still active/unresolved

    eil.mark_reconciliation_required(conn, intent_id)
    assert eil.get_by_id(conn, intent_id)["state"] == eil.STATE_RECONCILIATION_REQUIRED

    eil.mark_confirmed(conn, intent_id, confirmed_filled_qty=10)
    intent = eil.get_by_id(conn, intent_id)
    assert intent["state"] == eil.STATE_CONFIRMED
    assert intent["confirmed_filled_qty"] == 10


def test_cannot_transition_out_of_terminal_state(conn):
    intent_id = eil.reserve(conn, "pos_1", "STOP_LOSS", 10, "exit-coid-1")
    eil.mark_aborted(conn, intent_id)
    with pytest.raises(eil.ExitIntentError):
        eil.mark_submitted(conn, intent_id)


def test_transition_unknown_intent_id_raises(conn):
    with pytest.raises(eil.ExitIntentError):
        eil.mark_submitted(conn, "does-not-exist")


def test_get_by_client_order_id(conn):
    intent_id = eil.reserve(conn, "pos_1", "STOP_LOSS", 10, "exit-coid-1")
    found = eil.get_by_client_order_id(conn, "exit-coid-1")
    assert found["intent_id"] == intent_id


def test_client_order_id_must_be_unique(conn):
    eil.reserve(conn, "pos_1", "STOP_LOSS", 10, "exit-coid-1")
    eil.mark_aborted(conn, eil.get_active_intent(conn, "pos_1")["intent_id"])
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        eil.reserve(conn, "pos_2", "STOP_LOSS", 5, "exit-coid-1")


def test_mark_submitted_records_broker_order_id(conn):
    intent_id = eil.reserve(conn, "pos_1", "STOP_LOSS", 10, "exit-coid-1")
    eil.mark_submitted(conn, intent_id, broker_order_id="broker-order-42")
    assert eil.get_by_id(conn, intent_id)["broker_order_id"] == "broker-order-42"
