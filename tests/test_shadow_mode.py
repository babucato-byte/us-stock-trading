from datetime import datetime, timezone

import pytest

import shadow_mode

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(tmp_path / "SHADOW_MODE_LOG.jsonl"))
    yield


def _record(**overrides):
    kwargs = dict(
        signal_id="sig-1", strategy_id="strat-1", strategy_version="v1", code_commit="abc123",
        symbol="AAPL", side="buy", alpaca_signal_price=100.0, kis_validation_price=100.1,
        price_difference_percent=0.1, planned_quantity=1, planned_limit_price=100.1,
        stop_price=92.0, target_price=108.0, risk_gate_result="APPROVED", rejection_reason=None,
        account_available_usd=1000.0, existing_position_quantity=0, existing_open_order=False, now=NOW,
    )
    kwargs.update(overrides)
    return shadow_mode.build_record(**kwargs)


class TestBuildRecord:
    def test_all_required_fields_present(self):
        record = _record()
        for field_name in (
            "signal_id", "strategy_id", "strategy_version", "code_commit", "symbol", "side",
            "alpaca_signal_price", "kis_validation_price", "price_difference_percent",
            "planned_quantity", "planned_limit_price", "stop_price", "target_price",
            "risk_gate_result", "rejection_reason", "account_available_usd",
            "existing_position_quantity", "existing_open_order", "created_at",
        ):
            assert hasattr(record, field_name)


class TestPersistAndReadAll:
    def test_persist_then_read_all_roundtrips(self):
        shadow_mode.persist(_record())
        rows = shadow_mode.read_all()
        assert len(rows) == 1
        assert rows[0]["symbol"] == "AAPL"
        assert rows[0]["risk_gate_result"] == "APPROVED"

    def test_append_only_across_multiple_persists(self):
        shadow_mode.persist(_record(signal_id="sig-1"))
        shadow_mode.persist(_record(signal_id="sig-2", risk_gate_result="BLOCKED", rejection_reason="test"))
        rows = shadow_mode.read_all()
        assert len(rows) == 2
        assert rows[1]["risk_gate_result"] == "BLOCKED"
        assert rows[1]["rejection_reason"] == "test"

    def test_read_all_on_missing_file_returns_empty(self):
        assert shadow_mode.read_all() == []

    def test_read_all_skips_malformed_trailing_line(self, tmp_path, monkeypatch):
        path = tmp_path / "SHADOW_MODE_LOG.jsonl"
        monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(path))
        shadow_mode.persist(_record())
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("not valid json\n")
        rows = shadow_mode.read_all()
        assert len(rows) == 1

    def test_persist_failure_raises_shadow_mode_error(self, tmp_path, monkeypatch):
        bad_path = tmp_path / "no_such_dir" / "readonly"
        monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(shadow_mode.ShadowModeError):
            shadow_mode.persist(_record(), path=bad_path)


class TestSecretRedaction:
    def test_rejection_reason_with_embedded_account_number_is_redacted(self):
        # CODEX-050: a rejection_reason built from an underlying
        # OrderGateBlockedError-style message must never persist a full
        # account number to the durable Shadow Mode log.
        shadow_mode.persist(_record(
            risk_gate_result="BLOCKED",
            rejection_reason="KIS account 'cano=12345678' is not the allowed account 'cano=99999999'",
        ))
        rows = shadow_mode.read_all()
        assert "12345678" not in rows[0]["rejection_reason"]
        assert "99999999" not in rows[0]["rejection_reason"]

    def test_normal_rejection_reason_unaffected(self):
        shadow_mode.persist(_record(risk_gate_result="BLOCKED", rejection_reason="insufficient cash"))
        rows = shadow_mode.read_all()
        assert rows[0]["rejection_reason"] == "insufficient cash"


class TestRotation:
    """Rotation applies to SHADOW_MODE_LOG_DIR. There is deliberately no
    unconfigured default: the module used to fall back to its own
    directory, i.e. the release root, and Oracle verification caught it
    writing there."""

    def test_default_path_rotates_by_calendar_day(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SHADOW_MODE_LOG_FILE", raising=False)
        monkeypatch.setenv("SHADOW_MODE_LOG_DIR", str(tmp_path))
        day_1 = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
        day_2 = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
        shadow_mode.persist(_record(signal_id="sig-day1", now=day_1))
        shadow_mode.persist(_record(signal_id="sig-day2", now=day_2))
        assert (tmp_path / "shadow-2026-07-29.jsonl").exists()
        assert (tmp_path / "shadow-2026-07-30.jsonl").exists()

    def test_read_all_without_override_reads_across_all_rotated_files(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SHADOW_MODE_LOG_FILE", raising=False)
        monkeypatch.setenv("SHADOW_MODE_LOG_DIR", str(tmp_path))
        day_1 = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
        day_2 = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
        shadow_mode.persist(_record(signal_id="sig-day1", now=day_1))
        shadow_mode.persist(_record(signal_id="sig-day2", now=day_2))
        rows = shadow_mode.read_all()
        assert {r["signal_id"] for r in rows} == {"sig-day1", "sig-day2"}

    def test_read_all_with_date_reads_only_that_day(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SHADOW_MODE_LOG_FILE", raising=False)
        monkeypatch.setenv("SHADOW_MODE_LOG_DIR", str(tmp_path))
        day_1 = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
        day_2 = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
        shadow_mode.persist(_record(signal_id="sig-day1", now=day_1))
        shadow_mode.persist(_record(signal_id="sig-day2", now=day_2))
        rows = shadow_mode.read_all(date=day_1.date())
        assert [r["signal_id"] for r in rows] == ["sig-day1"]

    def test_explicit_env_override_disables_rotation(self, tmp_path, monkeypatch):
        # The SHADOW_MODE_LOG_FILE override (test isolation's normal
        # mode) must always win over date-based rotation.
        single_file = tmp_path / "SHADOW_MODE_LOG.jsonl"
        monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(single_file))
        day_1 = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
        day_2 = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
        shadow_mode.persist(_record(signal_id="sig-day1", now=day_1))
        shadow_mode.persist(_record(signal_id="sig-day2", now=day_2))
        assert single_file.exists()
        rows = shadow_mode.read_all()
        assert len(rows) == 2


class TestLocking:
    def test_lock_file_created_alongside_target(self, tmp_path, monkeypatch):
        target = tmp_path / "SHADOW_MODE_LOG.jsonl"
        monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(target))
        shadow_mode.persist(_record())
        assert (tmp_path / "SHADOW_MODE_LOG.jsonl.lock").exists()

    def test_held_lock_blocks_concurrent_writer(self, tmp_path, monkeypatch):
        import fcntl
        target = tmp_path / "SHADOW_MODE_LOG.jsonl"
        monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(target))
        lock_path = tmp_path / "SHADOW_MODE_LOG.jsonl.lock"
        holder = open(lock_path, "a+")
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            with pytest.raises(BlockingIOError):
                with open(lock_path, "a+") as second:
                    fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
