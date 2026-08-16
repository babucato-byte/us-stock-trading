"""live_readiness/trusted_operator_config.py unit tests."""
import pytest

from live_readiness import trusted_operator_config as toc


def test_cash_usage_percent_ceiling_is_valid():
    value = toc.get_cash_usage_percent_ceiling()
    assert 0 < value <= 100


def test_get_cash_usage_percent_is_valid():
    value = toc.get_cash_usage_percent()
    assert 0 < value <= 100


def test_default_cash_usage_percent_is_90():
    # 2026-07-28 자동 운영 구조: 운영자 입력이 없으면 90을 사용한다는 요구사항의
    # 회귀 테스트 -- 이 상수가 실수로 다시 낮아지거나 바뀌면 즉시 실패해야 한다.
    assert toc.CASH_USAGE_PERCENT_CEILING == 90
    assert toc.get_cash_usage_percent() == 90


def test_get_cash_usage_percent_matches_ceiling_value():
    # CODEX-039: both currently return the same underlying trusted
    # constant -- get_cash_usage_percent() takes no caller input at all
    # (nothing to combine), while get_cash_usage_percent_ceiling() is the
    # legacy min()-with-caller-value contract. Same number, distinct
    # names/contracts.
    assert toc.get_cash_usage_percent() == toc.get_cash_usage_percent_ceiling()


def test_get_cash_usage_percent_takes_no_arguments():
    import inspect
    sig = inspect.signature(toc.get_cash_usage_percent)
    assert len(sig.parameters) == 0


@pytest.mark.parametrize("bad_value", [0, -1, 101, None, "50", True, float("inf")])
def test_get_cash_usage_percent_blocks_on_corrupted_config(monkeypatch, bad_value):
    monkeypatch.setattr(toc, "CASH_USAGE_PERCENT_CEILING", bad_value)
    with pytest.raises(toc.TrustedConfigError):
        toc.get_cash_usage_percent()


def test_max_concurrent_live_positions_is_valid():
    assert toc.get_max_concurrent_live_positions() >= 1


def test_max_daily_live_entries_is_valid():
    assert toc.get_max_daily_live_entries() >= 1


def test_corrupted_percent_ceiling_blocks(monkeypatch):
    monkeypatch.setattr(toc, "CASH_USAGE_PERCENT_CEILING", float("nan"))
    with pytest.raises(toc.TrustedConfigError):
        toc.get_cash_usage_percent_ceiling()


@pytest.mark.parametrize("bad_value", [0, -1, 101, None, "50", True, float("inf")])
def test_invalid_percent_ceiling_blocked(monkeypatch, bad_value):
    monkeypatch.setattr(toc, "CASH_USAGE_PERCENT_CEILING", bad_value)
    with pytest.raises(toc.TrustedConfigError):
        toc.get_cash_usage_percent_ceiling()


@pytest.mark.parametrize("bad_value", [0, -1, None, "1", True, 1.5])
def test_invalid_max_concurrent_positions_blocked(monkeypatch, bad_value):
    monkeypatch.setattr(toc, "MAX_CONCURRENT_LIVE_POSITIONS", bad_value)
    with pytest.raises(toc.TrustedConfigError):
        toc.get_max_concurrent_live_positions()


@pytest.mark.parametrize("bad_value", [0, -1, None, "1", True, 1.5])
def test_invalid_max_daily_entries_blocked(monkeypatch, bad_value):
    monkeypatch.setattr(toc, "MAX_DAILY_LIVE_ENTRIES", bad_value)
    with pytest.raises(toc.TrustedConfigError):
        toc.get_max_daily_live_entries()
