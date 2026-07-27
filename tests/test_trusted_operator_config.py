"""live_readiness/trusted_operator_config.py unit tests."""
import pytest

from live_readiness import trusted_operator_config as toc


def test_cash_usage_percent_ceiling_is_valid():
    value = toc.get_cash_usage_percent_ceiling()
    assert 0 < value <= 100


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
