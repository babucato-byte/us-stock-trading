import json

import pytest

import kill_switch_state as kss


def _use_tmp_state(monkeypatch, tmp_path, name="KILL_SWITCH_STATE.json"):
    monkeypatch.delenv("KILL_SWITCH_STATE_FILE", raising=False)
    path = tmp_path / name
    monkeypatch.setattr(kss, "STATE_FILE", path)
    return path


# ---------------------------------------------------------------------------
# Default (no state file): ACTIVE, everything allowed
# ---------------------------------------------------------------------------

def test_default_unset_state_is_active(monkeypatch, tmp_path):
    _use_tmp_state(monkeypatch, tmp_path)

    assert kss.get_state() == kss.ACTIVE
    assert kss.is_entry_allowed() is True
    assert kss.is_liquidation_allowed() is True


# ---------------------------------------------------------------------------
# New entry blocked outside ACTIVE / queries always allowed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", [kss.ENTRY_DISABLED, kss.ALL_TRADING_DISABLED, kss.MANUAL_REVIEW])
def test_new_entry_blocked_in_every_non_active_state(monkeypatch, tmp_path, state):
    _use_tmp_state(monkeypatch, tmp_path)
    kss.activate(state, reason="incident", activated_by="ops1")

    assert kss.is_entry_allowed() is False


@pytest.mark.parametrize("state", [kss.ACTIVE, kss.ENTRY_DISABLED, kss.ALL_TRADING_DISABLED, kss.MANUAL_REVIEW])
def test_queries_allowed_in_every_state(monkeypatch, tmp_path, state):
    _use_tmp_state(monkeypatch, tmp_path)
    if state != kss.ACTIVE:
        kss.activate(state, reason="incident", activated_by="ops1")

    # get_state/get_current_record/get_history must never raise or be gated.
    assert kss.get_state() == state
    record = kss.get_current_record()
    assert record["state"] == state
    assert isinstance(kss.get_history(), list)


# ---------------------------------------------------------------------------
# Per-state liquidation (auto-exit) policy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state,expected", [
    (kss.ENTRY_DISABLED, True),
    (kss.ALL_TRADING_DISABLED, False),
    (kss.MANUAL_REVIEW, False),
])
def test_liquidation_policy_by_state(monkeypatch, tmp_path, state, expected):
    _use_tmp_state(monkeypatch, tmp_path)
    kss.activate(state, reason="incident", activated_by="ops1")

    assert kss.is_liquidation_allowed() is expected


def test_liquidation_allowed_when_active(monkeypatch, tmp_path):
    _use_tmp_state(monkeypatch, tmp_path)

    assert kss.is_liquidation_allowed() is True


# ---------------------------------------------------------------------------
# File-based persistence across "restart" (fresh reads, no in-memory cache)
# ---------------------------------------------------------------------------

def test_state_persists_across_restart_via_file(monkeypatch, tmp_path):
    path = _use_tmp_state(monkeypatch, tmp_path)
    kss.activate(kss.MANUAL_REVIEW, reason="incident-42", activated_by="ops1", incident_id="INC-42")

    # Simulate a process restart: nothing is held in memory, a fresh call
    # must re-derive state purely from the JSON file on disk.
    assert path.exists()
    raw = json.loads(path.read_text())
    assert raw["current"]["state"] == kss.MANUAL_REVIEW

    assert kss.get_state() == kss.MANUAL_REVIEW
    assert kss.get_current_record()["incident_id"] == "INC-42"


# ---------------------------------------------------------------------------
# Corrupted / unparseable state file fails closed
# ---------------------------------------------------------------------------

def test_corrupted_state_file_fails_closed(monkeypatch, tmp_path):
    path = _use_tmp_state(monkeypatch, tmp_path)
    path.write_text("{ not valid json ]")

    state = kss.get_state()
    assert state in (kss.ALL_TRADING_DISABLED, kss.MANUAL_REVIEW)
    assert kss.is_entry_allowed() is False
    assert kss.is_liquidation_allowed() is False


def test_state_file_with_unknown_state_value_fails_closed(monkeypatch, tmp_path):
    path = _use_tmp_state(monkeypatch, tmp_path)
    path.write_text(json.dumps({"current": {"state": "NOT_A_REAL_STATE"}, "history": []}))

    state = kss.get_state()
    assert state in (kss.ALL_TRADING_DISABLED, kss.MANUAL_REVIEW)
    assert kss.is_entry_allowed() is False


# ---------------------------------------------------------------------------
# expires_at is informational only -- never auto-reactivates
# ---------------------------------------------------------------------------

def test_expired_expires_at_does_not_auto_reactivate(monkeypatch, tmp_path):
    _use_tmp_state(monkeypatch, tmp_path)
    kss.activate(
        kss.ALL_TRADING_DISABLED,
        reason="incident",
        activated_by="ops1",
        expires_at="2000-01-01T00:00:00+00:00",  # far in the past
    )

    assert kss.get_state() == kss.ALL_TRADING_DISABLED
    assert kss.is_entry_allowed() is False
    assert kss.is_liquidation_allowed() is False


# ---------------------------------------------------------------------------
# Release requires explicit operator approval
# ---------------------------------------------------------------------------

def test_release_without_operator_raises(monkeypatch, tmp_path):
    _use_tmp_state(monkeypatch, tmp_path)
    kss.activate(kss.ALL_TRADING_DISABLED, reason="incident", activated_by="ops1")

    with pytest.raises(kss.KillSwitchStateError):
        kss.release(released_by=None)

    # State must remain unchanged after the failed release attempt.
    assert kss.get_state() == kss.ALL_TRADING_DISABLED


def test_release_with_operator_returns_to_active(monkeypatch, tmp_path):
    _use_tmp_state(monkeypatch, tmp_path)
    kss.activate(kss.MANUAL_REVIEW, reason="incident", activated_by="ops1")

    record = kss.release(released_by="ops2", reason="incident resolved")

    assert record["state"] == kss.ACTIVE
    assert record["released_by"] == "ops2"
    assert kss.get_state() == kss.ACTIVE
    assert kss.is_entry_allowed() is True


# ---------------------------------------------------------------------------
# Repeated activation of the same state is idempotent
# ---------------------------------------------------------------------------

def test_repeated_activation_of_same_state_is_idempotent(monkeypatch, tmp_path):
    _use_tmp_state(monkeypatch, tmp_path)
    first = kss.activate(kss.ENTRY_DISABLED, reason="first", activated_by="ops1")
    second = kss.activate(kss.ENTRY_DISABLED, reason="second", activated_by="ops1")

    assert kss.get_state() == kss.ENTRY_DISABLED
    # activated_at is preserved from the original activation, not reset.
    assert second["activated_at"] == first["activated_at"]
    assert second["reason"] == "second"


# ---------------------------------------------------------------------------
# Audit history accumulates and is preserved across transitions
# ---------------------------------------------------------------------------

def test_audit_history_accumulates_across_transitions(monkeypatch, tmp_path):
    _use_tmp_state(monkeypatch, tmp_path)
    kss.activate(kss.ENTRY_DISABLED, reason="step1", activated_by="ops1")
    kss.activate(kss.ENTRY_DISABLED, reason="step2", activated_by="ops1")  # repeat, still audited
    kss.activate(kss.ALL_TRADING_DISABLED, reason="step3", activated_by="ops1")
    kss.release(released_by="ops2", reason="resolved")

    history = kss.get_history()
    assert len(history) == 4
    assert [entry["reason"] for entry in history] == ["step1", "step2", "step3", "resolved"]
    assert [entry["state"] for entry in history] == [
        kss.ENTRY_DISABLED, kss.ENTRY_DISABLED, kss.ALL_TRADING_DISABLED, kss.ACTIVE,
    ]


def test_activate_requires_reason_and_activated_by(monkeypatch, tmp_path):
    _use_tmp_state(monkeypatch, tmp_path)

    with pytest.raises(kss.KillSwitchStateError):
        kss.activate(kss.MANUAL_REVIEW, reason="", activated_by="ops1")

    with pytest.raises(kss.KillSwitchStateError):
        kss.activate(kss.MANUAL_REVIEW, reason="incident", activated_by="")


def test_activate_rejects_unknown_state(monkeypatch, tmp_path):
    _use_tmp_state(monkeypatch, tmp_path)

    with pytest.raises(kss.KillSwitchStateError):
        kss.activate("NOT_A_REAL_STATE", reason="incident", activated_by="ops1")


# ---------------------------------------------------------------------------
# kill_switch.py re-exports the state machine
# ---------------------------------------------------------------------------

def test_kill_switch_module_reexports_state_machine():
    import kill_switch

    assert kill_switch.ACTIVE == kss.ACTIVE
    assert kill_switch.ENTRY_DISABLED == kss.ENTRY_DISABLED
    assert kill_switch.ALL_TRADING_DISABLED == kss.ALL_TRADING_DISABLED
    assert kill_switch.MANUAL_REVIEW == kss.MANUAL_REVIEW
    assert kill_switch.activate is kss.activate
    assert kill_switch.release is kss.release
