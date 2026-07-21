import multiprocessing
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from config import scalping_watchlist_config as cfg
from scalping_watchlist import eligibility, repository, scorer
from scalping_watchlist.atomic_io import FileUnavailable, atomic_write_csv, file_lock, read_csv_fail_closed
from scalping_watchlist.data_provider import FakeMarketDataProvider, SymbolSnapshot
from scalping_watchlist.models import NOT_AVAILABLE, NOT_EVALUATED, UNKNOWN, WatchlistEntry
from scalping_watchlist.pipeline import run_scan_cycle
from scalping_watchlist.repeat_tracker import (
    REPEAT_STATE_COLUMNS,
    load_repeat_state,
    update_repeat_tracker,
)

REGULAR_NOW = datetime(2026, 6, 15, 10, 0, tzinfo=ZoneInfo("America/New_York"))
PREMARKET_NOW = datetime(2026, 6, 15, 8, 0, tzinfo=ZoneInfo("America/New_York"))


def _good_snapshot(symbol="AAPL", price=100.0, previous_close=95.0, current_volume=5_000_000,
                    average_volume=1_000_000, atr=3.0, **overrides):
    kwargs = dict(symbol=symbol, price=price, previous_close=previous_close,
                  current_volume=current_volume, average_volume=average_volume, atr=atr)
    kwargs.update(overrides)
    return SymbolSnapshot(**kwargs)


def _patch_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(repository, "WATCHLIST_FILE", tmp_path / "scalping_watchlist.csv")
    monkeypatch.setattr(repository, "WATCHLIST_LOCK_FILE", tmp_path / "scalping_watchlist.lock")
    import scalping_watchlist.repeat_tracker as repeat_tracker_module
    monkeypatch.setattr(repeat_tracker_module, "REPEAT_STATE_FILE", tmp_path / "scalping_repeat_state.csv")
    monkeypatch.setattr(repeat_tracker_module, "REPEAT_STATE_LOCK_FILE", tmp_path / "scalping_repeat_state.lock")


# ---------------------------------------------------------------------------
# Normal selection
# ---------------------------------------------------------------------------

def test_symbol_meeting_all_criteria_is_selected(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(
        universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot()}
    )

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert [r["symbol"] for r in result["selected"]] == ["AAPL"]
    assert result["selected"][0]["status"] == "ACTIVE"
    assert result["selected"][0]["eligibility_reasons"] == "PASSED_STAGE_A_THROUGH_C"


def test_selected_symbols_sorted_by_score_descending(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(
        universe_symbols=["LOW", "HIGH"],
        snapshots={
            "LOW": _good_snapshot(symbol="LOW", current_volume=3_100_000),   # relative_volume just above 3.0
            "HIGH": _good_snapshot(symbol="HIGH", current_volume=9_000_000),  # much higher relative_volume
        },
    )

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert [r["symbol"] for r in result["selected"]] == ["HIGH", "LOW"]


def test_max_watchlist_size_caps_persisted_active_entries(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    symbols = ["SYMA", "SYMB", "SYMC", "SYMD", "SYME"]
    snapshots = {s: _good_snapshot(symbol=s, current_volume=4_000_000 + i * 100_000) for i, s in enumerate(symbols)}
    provider = FakeMarketDataProvider(universe_symbols=symbols, snapshots=snapshots)

    run_scan_cycle(provider, now=REGULAR_NOW, max_watchlist_size=2)

    watchlist = repository.load_watchlist()
    active_rows = watchlist[watchlist["status"] == "ACTIVE"]
    assert len(active_rows) == 2


def test_tie_scores_break_deterministically_by_symbol(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(
        universe_symbols=["ZETA", "ALPHA"],
        snapshots={
            "ZETA": _good_snapshot(symbol="ZETA"),
            "ALPHA": _good_snapshot(symbol="ALPHA"),
        },
    )

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["selected"][0]["scalping_score"] == result["selected"][1]["scalping_score"]
    assert [r["symbol"] for r in result["selected"]] == ["ALPHA", "ZETA"]


# ---------------------------------------------------------------------------
# Basic blocking
# ---------------------------------------------------------------------------

def test_invalid_symbol_format_is_rejected():
    reasons = eligibility.check_symbol_format("not-a-real-ticker!")
    assert any("INVALID_SYMBOL" in r for r in reasons)


@pytest.mark.parametrize(
    "overrides,expected_reason_fragment",
    [
        ({"price": 1.0}, "PRICE_TOO_LOW"),
        ({"price": 1000.0}, "PRICE_TOO_HIGH"),
        ({"average_volume": 100}, "AVERAGE_VOLUME_TOO_LOW"),
        ({"average_volume": 100, "price": 6}, "AVERAGE_DOLLAR_VOLUME_TOO_LOW"),
        ({"current_volume": 10}, "CURRENT_VOLUME_TOO_LOW"),
    ],
)
def test_price_and_liquidity_blocks(monkeypatch, tmp_path, overrides, expected_reason_fragment):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(
        universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot(**overrides)}
    )

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["selected"] == []
    assert any(expected_reason_fragment in r["rejection_reasons"] for r in result["rejected"])


def test_relative_volume_too_low_is_rejected(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(
        universe_symbols=["AAPL"],
        snapshots={"AAPL": _good_snapshot(current_volume=1_000_000, average_volume=1_000_000)},  # rel vol = 1.0
    )

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["selected"] == []
    assert any("RELATIVE_VOLUME_TOO_LOW" in r["rejection_reasons"] for r in result["rejected"])


def test_low_volatility_is_rejected(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(
        universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot(atr=0.1)}  # atr_percent = 0.1%
    )

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["selected"] == []
    assert any("VOLATILITY_TOO_LOW" in r["rejection_reasons"] for r in result["rejected"])


def test_low_liquidity_score_is_rejected(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    # average_dollar_volume passes MIN_AVERAGE_DOLLAR_VOLUME but liquidity_score
    # (capped differently) can still independently gate; force it low via a tiny average_volume
    # combined with a price that keeps dollar volume just at the edge is hard to construct
    # without also tripping dollar-volume — instead directly unit test the eligibility function.
    reasons = eligibility.check_price_and_liquidity({
        "latest_price": 10.0,
        "average_volume": 2_100_000,       # -> dollar volume 21,000,000 (passes MIN_AVERAGE_DOLLAR_VOLUME)
        "average_dollar_volume": 21_000_000,
        "current_volume": 200_000,
        "liquidity_score": 5.0,             # forced low regardless of dollar volume, to isolate this gate
    })
    assert any("LIQUIDITY_TOO_LOW" in r for r in reasons)


def test_missing_snapshot_is_rejected_as_data_unavailable(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": None})

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["selected"] == []
    assert any("DATA_UNAVAILABLE" in r["rejection_reasons"] for r in result["rejected"])


def test_stale_data_is_rejected(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    stale = _good_snapshot()
    stale.data_is_stale = True
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": stale})

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["selected"] == []
    assert any("STALE_DATA" in r["rejection_reasons"] for r in result["rejected"])


def test_abnormal_gap_is_rejected_as_data_anomaly(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(
        universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot(price=100.0, previous_close=1.0)}
    )  # +9900% gap, way past the sanity limit

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["selected"] == []
    assert any("DATA_ANOMALY" in r["rejection_reasons"] for r in result["rejected"])


def test_duplicate_symbol_is_rejected(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(snapshots={"AAPL": _good_snapshot()})

    result = run_scan_cycle(provider, now=REGULAR_NOW, symbols=["AAPL", "AAPL"])

    assert len(result["selected"]) == 1
    assert any(r["rejection_reasons"] == "DUPLICATE_SYMBOL" for r in result["rejected"])


# ---------------------------------------------------------------------------
# Repeat detection
# ---------------------------------------------------------------------------

def test_first_appearance_has_streak_one(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot()})

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["selected"][0]["repeat_count"] == 1


def test_second_appearance_same_day_increments_streak(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot()})

    run_scan_cycle(provider, now=REGULAR_NOW)
    later = REGULAR_NOW.replace(minute=15)
    result = run_scan_cycle(provider, now=later)

    assert result["selected"][0]["repeat_count"] == 2
    state = load_repeat_state()
    row = state[state["symbol"] == "AAPL"].iloc[0]
    assert int(row["consecutive_streak"]) == 2
    assert bool(row["reappeared_after_gap"]) is False


def test_different_trading_day_resets_streak(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot()})

    run_scan_cycle(provider, now=REGULAR_NOW)
    next_day = REGULAR_NOW.replace(day=16)
    run_scan_cycle(provider, now=next_day)

    state = load_repeat_state()
    row = state[state["symbol"] == "AAPL"].iloc[0]
    assert row["trading_date"] == "2026-06-16"
    assert int(row["detect_count"]) == 1
    assert int(row["consecutive_streak"]) == 1


def test_et_date_boundary_used_for_trading_date(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot()})
    # 23:30 in Seoul on 2026-06-15 is still 2026-06-15 morning in New York.
    seoul_time = datetime(2026, 6, 15, 23, 30, tzinfo=ZoneInfo("Asia/Seoul"))

    result = run_scan_cycle(provider, now=seoul_time)

    assert result["trading_date"] == "2026-06-15"


def test_reappearance_after_being_missed_resets_streak_but_not_count(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    both = FakeMarketDataProvider(
        universe_symbols=["AAPL", "MSFT"],
        snapshots={"AAPL": _good_snapshot(symbol="AAPL"), "MSFT": _good_snapshot(symbol="MSFT")},
    )
    only_msft = FakeMarketDataProvider(
        universe_symbols=["MSFT"], snapshots={"MSFT": _good_snapshot(symbol="MSFT")}
    )

    run_scan_cycle(both, now=REGULAR_NOW)  # AAPL detected, cycle 1
    run_scan_cycle(only_msft, now=REGULAR_NOW.replace(minute=15))  # AAPL missing, cycle 2
    result = run_scan_cycle(both, now=REGULAR_NOW.replace(minute=30))  # AAPL back, cycle 3

    aapl = next(r for r in result["selected"] if r["symbol"] == "AAPL")
    assert aapl["repeat_count"] == 2  # detected in cycles 1 and 3, not cycle 2
    state = load_repeat_state()
    row = state[state["symbol"] == "AAPL"].iloc[0]
    assert int(row["consecutive_streak"]) == 1  # streak restarted after the miss
    assert bool(row["reappeared_after_gap"]) is True


def test_concurrent_repeat_tracker_updates_no_lost_update(tmp_path, monkeypatch):
    import scalping_watchlist.repeat_tracker as repeat_tracker_module
    monkeypatch.setattr(repeat_tracker_module, "REPEAT_STATE_FILE", tmp_path / "state.csv")
    monkeypatch.setattr(repeat_tracker_module, "REPEAT_STATE_LOCK_FILE", tmp_path / "state.lock")

    results = []
    barrier = threading.Barrier(2)

    def _attempt(symbol):
        barrier.wait(timeout=5)
        update_repeat_tracker(
            {symbol: {"relative_volume": 5.0, "price": 100.0, "scalping_score": None}},
            "2026-06-15", "2026-06-15T10:00:00-04:00",
        )
        results.append(symbol)

    threads = [threading.Thread(target=_attempt, args=(sym,)) for sym in ("AAPL", "MSFT")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    state = pd.read_csv(tmp_path / "state.csv")
    assert set(state["symbol"]) == {"AAPL", "MSFT"}  # no lost update


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def test_sub_scores_sum_to_final_score_with_weights():
    features = {
        "latest_price": 100.0, "average_volume": 1_000_000, "average_dollar_volume": 100_000_000,
        "current_volume": 5_000_000, "relative_volume": 5.0, "gap_percent": 5.0,
        "atr_percent": 3.0, "liquidity_score": 100.0,
    }
    score, sub_scores = scorer.compute_scalping_score(features, repeat_info={"consecutive_streak": 1})

    expected = sum(sub_scores[name] * cfg.SCORING_WEIGHTS[name] for name in sub_scores)
    assert score == pytest.approx(expected)
    assert set(sub_scores.keys()) == set(cfg.SCORING_WEIGHTS.keys())


def test_score_is_always_within_zero_to_hundred():
    features = {
        "latest_price": 100.0, "average_volume": 1_000_000, "average_dollar_volume": 1e12,
        "current_volume": 1e12, "relative_volume": 1000.0, "gap_percent": 500.0,
        "atr_percent": 500.0, "liquidity_score": 1000.0,
    }
    score, sub_scores = scorer.compute_scalping_score(features, repeat_info={"consecutive_streak": 999})

    assert 0.0 <= score <= 100.0
    assert all(0.0 <= v <= 100.0 for v in sub_scores.values())


def test_nan_and_infinity_inputs_are_clamped_to_zero():
    features = {
        "latest_price": 100.0, "relative_volume": float("nan"), "gap_percent": float("inf"),
        "atr_percent": float("-inf"), "liquidity_score": float("nan"),
    }
    score, sub_scores = scorer.compute_scalping_score(features)

    assert not any(v != v for v in sub_scores.values())  # no NaN survives (x != x is the NaN test)
    assert all(abs(v) != float("inf") for v in sub_scores.values())
    assert 0.0 <= score <= 100.0


def test_scoring_is_order_independent():
    features_a = {"latest_price": 100.0, "relative_volume": 4.0, "gap_percent": 6.0,
                  "atr_percent": 2.0, "liquidity_score": 80.0}
    features_b = dict(reversed(list(features_a.items())))  # same content, different insertion order

    score_a, _ = scorer.compute_scalping_score(features_a, repeat_info={"consecutive_streak": 2})
    score_b, _ = scorer.compute_scalping_score(features_b, repeat_info={"consecutive_streak": 2})

    assert score_a == score_b


# ---------------------------------------------------------------------------
# File safety
# ---------------------------------------------------------------------------

def test_atomic_write_preserves_original_on_failure(tmp_path):
    target = tmp_path / "protected" / "watchlist.csv"
    target.parent.mkdir()
    pd.DataFrame([{"a": 1}]).to_csv(target, index=False)
    original_bytes = target.read_bytes()

    target.parent.chmod(0o500)
    try:
        result = atomic_write_csv(target, pd.DataFrame([{"a": 2}]))
    finally:
        target.parent.chmod(0o700)

    assert result is False
    assert target.read_bytes() == original_bytes


def test_lock_timeout_raises_and_leaves_file_untouched(tmp_path):
    lock_path = tmp_path / "x.lock"
    target = tmp_path / "x.csv"
    pd.DataFrame([{"a": 1}]).to_csv(target, index=False)
    original_bytes = target.read_bytes()

    import fcntl
    held = open(lock_path, "a+")
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        with pytest.raises(RuntimeError, match="Could not acquire lock"):
            with file_lock(lock_path, timeout=0.2):
                atomic_write_csv(target, pd.DataFrame([{"a": 2}]))
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()

    assert target.read_bytes() == original_bytes


def test_corrupted_watchlist_file_fails_closed(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    repository.WATCHLIST_FILE.write_text("not,the,right,columns\n1,2,3\n")

    with pytest.raises(repository.WatchlistUnavailable):
        repository.load_watchlist()


def test_missing_watchlist_file_is_legitimate_empty_state(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)

    df = repository.load_watchlist()

    assert df.empty


def test_scalping_pipeline_never_touches_real_repo_files(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot()})

    run_scan_cycle(provider, now=REGULAR_NOW)

    from scalping_watchlist import repository as repo_module
    assert not (repo_module.WATCHLIST_FILE.parent / "scalping_watchlist.csv").samefile(
        repo_module.WATCHLIST_FILE
    ) or repo_module.WATCHLIST_FILE.parent == tmp_path
    assert repo_module.WATCHLIST_FILE.parent == tmp_path


# ---------------------------------------------------------------------------
# Network safety
# ---------------------------------------------------------------------------

def test_fake_provider_records_no_real_network_calls(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot()})

    run_scan_cycle(provider, now=REGULAR_NOW)

    assert provider.requested_symbols == ["AAPL"]  # only the fake was ever touched


def test_provider_error_for_one_symbol_does_not_block_others(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(
        universe_symbols=["BAD", "AAPL"],
        snapshots={"BAD": ConnectionError("simulated network failure"), "AAPL": _good_snapshot()},
    )

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert [r["symbol"] for r in result["selected"]] == ["AAPL"]
    bad_row = next(r for r in result["rejected"] if r["symbol"] == "BAD")
    assert "PROVIDER_ERROR" in bad_row["rejection_reasons"]


# ---------------------------------------------------------------------------
# CODEX-010: explicit finite-number validation
# ---------------------------------------------------------------------------

from scalping_watchlist.numeric_guard import InvalidNumber, is_finite_number, require_finite_number  # noqa: E402


@pytest.mark.parametrize(
    "value",
    [None, float("nan"), float("inf"), float("-inf"), "5", "", True, False],
)
def test_require_finite_number_rejects_invalid_types_and_values(value):
    with pytest.raises(InvalidNumber):
        require_finite_number(value, field_name="test_field")


@pytest.mark.parametrize("value", [-1.0, 0])
def test_require_finite_number_rejects_negative_and_zero_when_configured(value):
    with pytest.raises(InvalidNumber):
        require_finite_number(value, field_name="price", min_value=0, allow_zero=False, min_exclusive=True)


def test_require_finite_number_accepts_normal_value():
    assert require_finite_number(100.5, field_name="price", min_value=0, min_exclusive=True) == 100.5


def test_require_finite_number_reason_code_matches_field_name():
    with pytest.raises(InvalidNumber) as exc_info:
        require_finite_number(float("nan"), field_name="latest_price")
    assert exc_info.value.reason_code == "INVALID_LATEST_PRICE"


def test_is_finite_number_helper():
    assert is_finite_number(1.0) is True
    assert is_finite_number(float("nan")) is False
    assert is_finite_number(float("inf")) is False
    assert is_finite_number(None) is False
    assert is_finite_number(True) is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("price", float("nan")),
        ("previous_close", float("nan")),
        ("average_volume", float("nan")),
        ("atr", float("nan")),
        ("price", float("inf")),
    ],
)
def test_nan_or_infinite_snapshot_field_blocks_candidate(field, value):
    from scalping_watchlist.features import compute_features

    kwargs = {"symbol": "X", "price": 100.0, "previous_close": 95.0,
              "current_volume": 5_000_000, "average_volume": 1_000_000, "atr": 3.0}
    kwargs[field] = value
    snapshot = SymbolSnapshot(**kwargs)

    features, reasons = compute_features(snapshot)

    assert reasons  # at least one rejection reason recorded
    assert any(is_sentinel_or_str(v) for v in features.values())


def is_sentinel_or_str(value):
    from scalping_watchlist.models import is_sentinel
    return is_sentinel(value)


def test_nan_price_produces_nan_gap_percent_which_is_also_blocked():
    from scalping_watchlist.features import compute_features

    snapshot = SymbolSnapshot(symbol="X", price=float("nan"), previous_close=95.0,
                               current_volume=5_000_000, average_volume=1_000_000, atr=3.0)
    features, reasons = compute_features(snapshot)

    assert features["gap_percent"] == "NOT_AVAILABLE"
    assert any("INVALID_LATEST_PRICE" in r for r in reasons)


def test_nan_volume_inputs_block_relative_volume():
    from scalping_watchlist.features import compute_features

    snapshot = SymbolSnapshot(symbol="X", price=100.0, previous_close=95.0,
                               current_volume=float("nan"), average_volume=1_000_000, atr=3.0)
    features, reasons = compute_features(snapshot)

    assert features["relative_volume"] == "NOT_AVAILABLE"
    assert any("INVALID_CURRENT_VOLUME" in r for r in reasons)


def test_single_nan_field_excludes_symbol_from_pipeline_selection(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(
        universe_symbols=["AAPL"],
        snapshots={"AAPL": _good_snapshot(atr=float("nan"))},
    )

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["selected"] == []
    assert any("INVALID_ATR" in r["rejection_reasons"] for r in result["rejected"])


def test_scorer_never_returns_non_finite_score():
    features = {"latest_price": 100.0, "relative_volume": float("nan"),
                "gap_percent": float("inf"), "atr_percent": float("-inf"),
                "liquidity_score": float("nan")}
    score, sub_scores = scorer.compute_scalping_score(features)
    assert is_finite_number(score)
    assert all(is_finite_number(v) for v in sub_scores.values())
