from datetime import datetime, timedelta, timezone

import pytest

from reconciliation import reconciliation_state

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "RECONCILIATION_STATE.json"


class TestIsCurrentAndClean:
    def test_no_result_ever_recorded_fails_closed(self, state_path):
        assert reconciliation_state.is_current_and_clean(max_age_seconds=300, now=NOW, path=state_path) is False

    def test_clean_and_fresh_result_passes(self, state_path):
        reconciliation_state.record_result(clean=True, mismatch_count=0, now=NOW, path=state_path, unknown_count=0, halt=False)
        assert reconciliation_state.is_current_and_clean(
            max_age_seconds=300, now=NOW + timedelta(seconds=10), path=state_path,
        ) is True

    def test_dirty_result_fails_closed(self, state_path):
        reconciliation_state.record_result(clean=False, mismatch_count=1, now=NOW, path=state_path, unknown_count=0, halt=False)
        assert reconciliation_state.is_current_and_clean(max_age_seconds=300, now=NOW, path=state_path) is False

    def test_stale_result_fails_closed(self, state_path):
        reconciliation_state.record_result(clean=True, mismatch_count=0, now=NOW, path=state_path, unknown_count=0, halt=False)
        assert reconciliation_state.is_current_and_clean(
            max_age_seconds=300, now=NOW + timedelta(seconds=301), path=state_path,
        ) is False

    def test_clock_moved_backwards_fails_closed(self, state_path):
        reconciliation_state.record_result(clean=True, mismatch_count=0, now=NOW, path=state_path, unknown_count=0, halt=False)
        assert reconciliation_state.is_current_and_clean(
            max_age_seconds=300, now=NOW - timedelta(seconds=10), path=state_path,
        ) is False

    def test_corrupted_state_file_fails_closed(self, state_path):
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("not valid json{{{")
        assert reconciliation_state.is_current_and_clean(max_age_seconds=300, now=NOW, path=state_path) is False

    def test_new_clean_result_overwrites_previous_dirty_result(self, state_path):
        reconciliation_state.record_result(clean=False, mismatch_count=2, now=NOW, path=state_path, unknown_count=0, halt=False)
        reconciliation_state.record_result(
            clean=True, mismatch_count=0, unknown_count=0, halt=False,
            now=NOW + timedelta(seconds=5), path=state_path,
        )
        assert reconciliation_state.is_current_and_clean(
            max_age_seconds=300, now=NOW + timedelta(seconds=10), path=state_path,
        ) is True


class TestGetLastResult:
    def test_none_when_never_recorded(self, state_path):
        assert reconciliation_state.get_last_result(path=state_path) is None

    def test_returns_recorded_values(self, state_path):
        reconciliation_state.record_result(clean=False, mismatch_count=3, now=NOW, path=state_path, unknown_count=0, halt=False)
        record = reconciliation_state.get_last_result(path=state_path)
        assert record.clean is False
        assert record.mismatch_count == 3
        assert record.checked_at == NOW
