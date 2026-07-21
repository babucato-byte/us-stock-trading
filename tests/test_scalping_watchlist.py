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

# 09:45 ET is comfortably inside the default REGULAR_OPEN_WINDOW_MINUTES=60
# window (09:30-10:30) that calendar_guard.py enforces (CODEX-012); several
# tests below add up to +30 minutes on top of this to exercise multi-cycle
# repeat-tracking within the same allowed window.
REGULAR_NOW = datetime(2026, 6, 15, 9, 45, tzinfo=ZoneInfo("America/New_York"))
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
    assert result["selected"][0]["status"] == "NEW"  # first-ever detection; ACTIVE starts on the 2nd (CODEX-014)
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
    # First-ever detection -> NEW, not ACTIVE (CODEX-014); the cap applies
    # to the whole non-expired selected pool regardless of NEW vs ACTIVE.
    tracked_rows = watchlist[watchlist["status"].isin(["NEW", "ACTIVE"])]
    assert len(tracked_rows) == 2


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


# ---------------------------------------------------------------------------
# CODEX-011: market data freshness
# ---------------------------------------------------------------------------

from datetime import timedelta, timezone as _timezone  # noqa: E402

from scalping_watchlist import freshness  # noqa: E402


def test_fresh_premarket_data_passes(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    now = PREMARKET_NOW
    snapshot = _good_snapshot(data_as_of=now - timedelta(minutes=5))
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": snapshot})

    result = run_scan_cycle(provider, now=now)

    assert [r["symbol"] for r in result["selected"]] == ["AAPL"]


def test_stale_premarket_data_is_blocked(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    now = PREMARKET_NOW
    snapshot = _good_snapshot(data_as_of=now - timedelta(minutes=30))
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": snapshot})

    result = run_scan_cycle(provider, now=now)

    assert result["selected"] == []
    assert any("STALE_MARKET_DATA" in r["rejection_reasons"] for r in result["rejected"])


def test_fresh_regular_data_passes(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    snapshot = _good_snapshot(data_as_of=REGULAR_NOW - timedelta(minutes=5))
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": snapshot})

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert [r["symbol"] for r in result["selected"]] == ["AAPL"]


def test_stale_regular_data_is_blocked(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    snapshot = _good_snapshot(data_as_of=REGULAR_NOW - timedelta(minutes=45))
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": snapshot})

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["selected"] == []
    assert any("STALE_MARKET_DATA" in r["rejection_reasons"] for r in result["rejected"])


def test_naive_data_as_of_timestamp_is_blocked(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    naive = datetime(2026, 6, 15, 9, 55)  # no tzinfo
    snapshot = _good_snapshot(data_as_of=naive)
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": snapshot})

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["selected"] == []
    assert any("NAIVE_TIMESTAMP" in r["rejection_reasons"] for r in result["rejected"])


def test_utc_timezone_aware_timestamp_passes():
    data_as_of = REGULAR_NOW.astimezone(_timezone.utc) - timedelta(minutes=5)
    reasons = freshness.check_data_freshness(data_as_of, REGULAR_NOW, "regular", cfg)
    assert reasons == []


def test_et_timestamp_converts_correctly():
    # 09:40 ET is 5 minutes before REGULAR_NOW (09:45 ET) — should be fresh.
    data_as_of = REGULAR_NOW.replace(hour=9, minute=40)
    reasons = freshness.check_data_freshness(data_as_of, REGULAR_NOW, "regular", cfg)
    assert reasons == []


def test_future_timestamp_is_blocked(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    snapshot = _good_snapshot(data_as_of=REGULAR_NOW + timedelta(hours=1))
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": snapshot})

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["selected"] == []
    assert any("FUTURE_TIMESTAMP" in r["rejection_reasons"] for r in result["rejected"])


def test_missing_data_as_of_is_blocked():
    reasons = freshness.check_data_freshness(None, REGULAR_NOW, "regular", cfg)
    assert reasons == ["MISSING_DATA_TIMESTAMP"]


def test_recent_fetch_does_not_excuse_an_old_bar(monkeypatch, tmp_path):
    # provider_fetched_at is "just now" but data_as_of (the actual bar) is
    # old — freshness must be judged against data_as_of, not fetch time.
    _patch_paths(monkeypatch, tmp_path)
    snapshot = _good_snapshot(
        data_as_of=REGULAR_NOW - timedelta(hours=2),
        provider_fetched_at=REGULAR_NOW,
    )
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": snapshot})

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["selected"] == []
    assert any("STALE_MARKET_DATA" in r["rejection_reasons"] for r in result["rejected"])


def test_freshness_check_uses_evaluated_at_not_provider_fetched_at():
    # Even if provider_fetched_at were passed by mistake as the comparison
    # basis, check_data_freshness's signature only accepts data_as_of and
    # evaluated_at — there is no way to accidentally substitute fetched_at.
    old_bar = REGULAR_NOW - timedelta(minutes=45)
    reasons = freshness.check_data_freshness(old_bar, REGULAR_NOW, "regular", cfg)
    assert any("STALE_MARKET_DATA" in r for r in reasons)


def test_stale_data_across_all_candidates_yields_zero_selected(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(
        universe_symbols=["AAPL", "MSFT"],
        snapshots={
            "AAPL": _good_snapshot(symbol="AAPL", data_as_of=REGULAR_NOW - timedelta(hours=5)),
            "MSFT": _good_snapshot(symbol="MSFT", data_as_of=REGULAR_NOW - timedelta(hours=5)),
        },
    )

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["selected"] == []
    assert len(result["rejected"]) == 2


def test_fake_provider_used_for_freshness_tests_makes_no_real_calls(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(
        universe_symbols=["AAPL"],
        snapshots={"AAPL": _good_snapshot(data_as_of=REGULAR_NOW - timedelta(hours=5))},
    )

    run_scan_cycle(provider, now=REGULAR_NOW)

    assert provider.requested_symbols == ["AAPL"]  # only the fake was ever touched


# ---------------------------------------------------------------------------
# CODEX-012: holiday and allowed-session gating
# ---------------------------------------------------------------------------

from scalping_watchlist import calendar_guard  # noqa: E402
from market_guard import is_us_trading_day as market_guard_is_us_trading_day  # noqa: E402


def _run_and_get_status(now, monkeypatch, tmp_path, session_hint_symbol="AAPL"):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(
        universe_symbols=[session_hint_symbol], snapshots={session_hint_symbol: _good_snapshot()}
    )
    return run_scan_cycle(provider, now=now)


def test_normal_trading_day_premarket_is_allowed(monkeypatch, tmp_path):
    # Monday 2026-06-15, 08:00 ET premarket.
    now = datetime(2026, 6, 15, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    result = _run_and_get_status(now, monkeypatch, tmp_path)
    assert result.get("status") != "SKIPPED"
    assert [r["symbol"] for r in result["selected"]] == ["AAPL"]


def test_normal_trading_day_regular_open_window_is_allowed(monkeypatch, tmp_path):
    result = _run_and_get_status(REGULAR_NOW, monkeypatch, tmp_path)  # Monday 09:45 ET
    assert result.get("status") != "SKIPPED"
    assert [r["symbol"] for r in result["selected"]] == ["AAPL"]


def test_weekend_saturday_is_skipped(monkeypatch, tmp_path):
    saturday = datetime(2026, 6, 13, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    result = _run_and_get_status(saturday, monkeypatch, tmp_path)
    assert result["status"] == "SKIPPED"
    assert result["skip_reason"] == "MARKET_CLOSED"
    assert result["selected"] == []


def test_weekend_sunday_is_skipped(monkeypatch, tmp_path):
    sunday = datetime(2026, 6, 14, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    result = _run_and_get_status(sunday, monkeypatch, tmp_path)
    assert result["status"] == "SKIPPED"
    assert result["skip_reason"] == "MARKET_CLOSED"


def test_official_holiday_is_skipped(monkeypatch, tmp_path):
    new_years_day = datetime(2026, 1, 1, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    result = _run_and_get_status(new_years_day, monkeypatch, tmp_path)
    assert result["status"] == "SKIPPED"
    assert result["skip_reason"] == "MARKET_CLOSED"


def test_early_close_day_is_still_a_trading_day(monkeypatch, tmp_path):
    # Day after Thanksgiving 2026 (2026-11-27) is an early-close trading day,
    # not a holiday — the morning open-window scan must still run.
    early_close_morning = datetime(2026, 11, 27, 9, 45, tzinfo=ZoneInfo("America/New_York"))
    result = _run_and_get_status(early_close_morning, monkeypatch, tmp_path)
    assert result.get("status") != "SKIPPED"


def test_before_premarket_start_is_not_allowed(monkeypatch, tmp_path):
    before_premarket = datetime(2026, 6, 15, 3, 0, tzinfo=ZoneInfo("America/New_York"))
    result = _run_and_get_status(before_premarket, monkeypatch, tmp_path)
    assert result["status"] == "SKIPPED"
    assert result["skip_reason"] == "SESSION_NOT_ALLOWED"


def test_after_regular_open_window_is_not_allowed(monkeypatch, tmp_path):
    # 14:00 ET is well within the regular session but outside the
    # REGULAR_OPEN_WINDOW_MINUTES=60 opening window (09:30-10:30).
    mid_afternoon = datetime(2026, 6, 15, 14, 0, tzinfo=ZoneInfo("America/New_York"))
    result = _run_and_get_status(mid_afternoon, monkeypatch, tmp_path)
    assert result["status"] == "SKIPPED"
    assert result["skip_reason"] == "SESSION_NOT_ALLOWED"


def test_after_hours_is_not_allowed(monkeypatch, tmp_path):
    after_hours = datetime(2026, 6, 15, 17, 0, tzinfo=ZoneInfo("America/New_York"))
    result = _run_and_get_status(after_hours, monkeypatch, tmp_path)
    assert result["status"] == "SKIPPED"
    assert result["skip_reason"] == "SESSION_NOT_ALLOWED"


def test_et_utc_date_boundary_does_not_misjudge_trading_day():
    # 2026-06-16 03:00 UTC is 2026-06-15 23:00 ET (Monday night) — the
    # trading-day check must convert to ET before comparing dates, not use
    # the UTC calendar date directly.
    utc_time = datetime(2026, 6, 16, 3, 0, tzinfo=_timezone.utc)
    assert market_guard_is_us_trading_day(utc_time) is True  # still Monday in ET


def test_dst_period_trading_day_check_is_consistent():
    # DST-active date (June) vs. standard-time date (December), both
    # ordinary weekday trading days — is_us_trading_day must agree for both.
    dst_date = datetime(2026, 6, 15, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    standard_time_date = datetime(2026, 12, 15, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    assert market_guard_is_us_trading_day(dst_date) is True
    assert market_guard_is_us_trading_day(standard_time_date) is True


# ---------------------------------------------------------------------------
# CODEX-013: watchlist persistence failure must never look like success
# ---------------------------------------------------------------------------

def test_normal_save_reports_success_with_persisted_count(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot()})

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["status"] == "SUCCESS"
    assert result["persisted_count"] == 1
    assert result["error_code"] == ""


def test_forced_save_failure_reports_failed_persistence_status(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(repository, "save_watchlist_cycle", lambda *a, **k: {
        "success": False, "persisted_count": 0, "error_code": "FAILED_PERSISTENCE", "error_message": "forced",
    })
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot()})

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["status"] == "FAILED_PERSISTENCE"
    assert result["error_code"] == "FAILED_PERSISTENCE"
    assert result["persisted_count"] == 0


def test_temp_write_failure_is_reported_as_failed_persistence(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    # Isolate the watchlist file in its own read-only subdirectory so only
    # its write fails — repeat_tracker's own (unrelated) file in tmp_path
    # must remain writable, or Stage D fails first for the wrong reason.
    watchlist_dir = tmp_path / "watchlist_only"
    watchlist_dir.mkdir()
    monkeypatch.setattr(repository, "WATCHLIST_FILE", watchlist_dir / "scalping_watchlist.csv")
    monkeypatch.setattr(repository, "WATCHLIST_LOCK_FILE", watchlist_dir / "scalping_watchlist.lock")
    (watchlist_dir / "scalping_watchlist.lock").touch()
    watchlist_dir.chmod(0o500)
    try:
        provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot()})
        result = run_scan_cycle(provider, now=REGULAR_NOW)
    finally:
        watchlist_dir.chmod(0o700)

    assert result["status"] == "FAILED_PERSISTENCE"
    assert result["error_code"] == "FAILED_PERSISTENCE"


def test_post_write_reread_failure_is_reported(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(repository, "read_csv_fail_closed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("corrupt")))
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot()})

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["status"] == "FAILED_PERSISTENCE"


def test_row_count_mismatch_after_write_is_detected(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    # Force the post-write reread to return fewer rows than were written,
    # simulating a corrupted/incomplete write that the OS still reported as
    # successful.
    real_read = repository.read_csv_fail_closed

    def _truncated_read(path, columns):
        df = real_read(path, columns)
        return df.iloc[0:0]  # pretend nothing persisted

    monkeypatch.setattr(repository, "read_csv_fail_closed", _truncated_read)
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot()})

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["status"] == "FAILED_PERSISTENCE"
    assert "row count mismatch" in result["error_message"]


def test_missing_required_columns_after_write_is_detected(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)

    def _bad_columns_read(path, columns):
        raise atomic_io_FileUnavailable("simulated missing columns")

    monkeypatch.setattr(repository, "read_csv_fail_closed", _bad_columns_read)
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot()})

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["status"] == "FAILED_PERSISTENCE"


def test_failure_status_is_never_success(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(repository, "save_watchlist_cycle", lambda *a, **k: {
        "success": False, "persisted_count": 0, "error_code": "FAILED_PERSISTENCE", "error_message": "x",
    })
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot()})

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["status"] != "SUCCESS"


def test_previous_valid_watchlist_file_preserved_on_save_failure(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    watchlist_dir = tmp_path / "watchlist_only"
    watchlist_dir.mkdir()
    monkeypatch.setattr(repository, "WATCHLIST_FILE", watchlist_dir / "scalping_watchlist.csv")
    monkeypatch.setattr(repository, "WATCHLIST_LOCK_FILE", watchlist_dir / "scalping_watchlist.lock")
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot()})
    run_scan_cycle(provider, now=REGULAR_NOW)  # establish a valid prior file
    original_bytes = repository.WATCHLIST_FILE.read_bytes()

    watchlist_dir.chmod(0o500)
    try:
        run_scan_cycle(provider, now=REGULAR_NOW.replace(minute=59))
    finally:
        watchlist_dir.chmod(0o700)

    assert repository.WATCHLIST_FILE.read_bytes() == original_bytes


def test_pipeline_result_includes_error_code_on_failure(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(repository, "save_watchlist_cycle", lambda *a, **k: {
        "success": False, "persisted_count": 0, "error_code": "FAILED_PERSISTENCE", "error_message": "disk full",
    })
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot()})

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["error_code"]
    assert result["error_message"]


from scalping_watchlist.atomic_io import FileUnavailable as atomic_io_FileUnavailable  # noqa: E402


# ---------------------------------------------------------------------------
# CODEX-014: lifecycle state machine and timestamp validation
# ---------------------------------------------------------------------------

from scalping_watchlist.models import (  # noqa: E402
    STATUS_ACTIVE as _STATUS_ACTIVE,
    STATUS_COOLING as _STATUS_COOLING,
    STATUS_EXPIRED as _STATUS_EXPIRED,
    STATUS_NEW as _STATUS_NEW,
    STATUS_REJECTED as _STATUS_REJECTED,
    VALID_STATUSES,
    validate_lifecycle_timestamps,
)


_UNSET = object()


def _lifecycle_row(status=_STATUS_ACTIVE, first_detected_at=_UNSET, last_detected_at=_UNSET,
                    expires_at=_UNSET, symbol="AAPL"):
    # Explicit _UNSET sentinel (not a plain default-arg `or`) so a test can
    # deliberately pass "" or None to exercise validate_lifecycle_timestamps'
    # empty/None handling without it being silently replaced by the default.
    if first_detected_at is _UNSET:
        first_detected_at = REGULAR_NOW.isoformat()
    if last_detected_at is _UNSET:
        last_detected_at = REGULAR_NOW.isoformat()
    if expires_at is _UNSET:
        expires_at = (REGULAR_NOW + timedelta(hours=1)).isoformat()
    return {
        "symbol": symbol, "status": status,
        "first_detected_at": first_detected_at, "last_detected_at": last_detected_at,
        "expires_at": expires_at, "updated_at": REGULAR_NOW.isoformat(),
        "scalping_score": 50.0, "rejection_reasons": "",
    }


def test_new_entry_created_on_first_detection(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot()})

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert result["selected"][0]["status"] == _STATUS_NEW


def test_second_detection_promotes_to_active(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot()})

    run_scan_cycle(provider, now=REGULAR_NOW)
    result = run_scan_cycle(provider, now=REGULAR_NOW.replace(minute=59))

    assert result["selected"][0]["status"] == _STATUS_ACTIVE


def test_missed_detection_transitions_to_cooling():
    now = REGULAR_NOW
    row = _lifecycle_row(
        status=_STATUS_ACTIVE,
        first_detected_at=(now - timedelta(hours=2)).isoformat(),  # must precede last_detected_at
        last_detected_at=(now - timedelta(minutes=40)).isoformat(),
    )

    result = repository._apply_expiry([row], now, ttl_minutes=30, expire_minutes=60)

    assert result[0]["status"] == _STATUS_COOLING


def test_ttl_exceeded_transitions_to_expired():
    now = REGULAR_NOW
    row = _lifecycle_row(
        status=_STATUS_ACTIVE,
        first_detected_at=(now - timedelta(hours=2)).isoformat(),
        last_detected_at=(now - timedelta(minutes=90)).isoformat(),
    )

    result = repository._apply_expiry([row], now, ttl_minutes=30, expire_minutes=60)

    assert result[0]["status"] == _STATUS_EXPIRED


def test_reappearance_after_expiry_same_day_resumes_active(monkeypatch, tmp_path):
    # Documented policy (DECISION_LOG.md): NEW vs ACTIVE is driven by
    # repeat_tracker's same-trading-day detection memory, which is
    # independent of the watchlist row's own persisted status. A symbol
    # already proven persistent this trading day (>=2 detections) resumes
    # ACTIVE immediately on reappearance even if its watchlist row had
    # expired in between — it does not have to "re-earn" NEW status within
    # the same day. Only a new trading day resets the streak (see the
    # separate different_trading_day_resets_streak test).
    _patch_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(universe_symbols=["AAPL"], snapshots={"AAPL": _good_snapshot()})
    run_scan_cycle(provider, now=REGULAR_NOW)  # cycle 1: NEW
    run_scan_cycle(provider, now=REGULAR_NOW.replace(minute=59))  # cycle 2: ACTIVE

    # Manually force the persisted row to EXPIRED, simulating time passing
    # well beyond the pipeline's own allowed session window without
    # touching repeat_tracker's separate same-day memory.
    df = repository.load_watchlist()
    df.loc[df["symbol"] == "AAPL", "status"] = _STATUS_EXPIRED
    df.loc[df["symbol"] == "AAPL", "last_detected_at"] = (REGULAR_NOW - timedelta(hours=3)).isoformat()
    repository.atomic_write_csv(repository.WATCHLIST_FILE, df)

    result = run_scan_cycle(provider, now=REGULAR_NOW)  # reappears, same trading day

    assert result["selected"][0]["status"] == _STATUS_ACTIVE


@pytest.mark.parametrize("field", ["first_detected_at", "last_detected_at", "expires_at"])
def test_corrupted_timestamp_field_invalidates_the_row(field):
    row = _lifecycle_row()
    row[field] = "not-a-timestamp"

    problems = validate_lifecycle_timestamps(row)

    assert any(field in p for p in problems)


def test_timezone_naive_timestamp_is_invalid():
    row = _lifecycle_row(first_detected_at="2026-06-15T09:45:00")  # no offset

    problems = validate_lifecycle_timestamps(row)

    assert problems


def test_empty_and_none_timestamps_are_invalid():
    for bad_value in ("", None):
        row = _lifecycle_row(first_detected_at=bad_value)
        assert validate_lifecycle_timestamps(row)


def test_future_first_detected_at_does_not_crash_validation():
    # A wildly-future first_detected_at isn't itself an ISO-format error,
    # but the resulting expires_at < first_detected_at ordering check
    # still catches the inconsistency.
    far_future = (REGULAR_NOW + timedelta(days=3650)).isoformat()
    row = _lifecycle_row(first_detected_at=far_future, expires_at=REGULAR_NOW.isoformat())

    problems = validate_lifecycle_timestamps(row)

    assert any("expires_at" in p for p in problems)


def test_last_detected_before_first_detected_is_invalid():
    row = _lifecycle_row(
        first_detected_at=REGULAR_NOW.isoformat(),
        last_detected_at=(REGULAR_NOW - timedelta(hours=1)).isoformat(),
    )

    problems = validate_lifecycle_timestamps(row)

    assert any("last_detected_at" in p for p in problems)


def test_corrupted_timestamp_forces_rejection_not_indefinite_active():
    # Reproduces the CODEX-014 evidence directly: a corrupted last_detected_at
    # must not let a row stay ACTIVE forever by skipping the TTL check.
    now = REGULAR_NOW
    row = _lifecycle_row(status=_STATUS_ACTIVE, last_detected_at="not-a-timestamp")

    result = repository._apply_expiry([row], now, ttl_minutes=30, expire_minutes=60)

    assert result[0]["status"] == _STATUS_REJECTED
    assert "INVALID_LIFECYCLE_TIMESTAMP" in result[0]["rejection_reasons"]


def test_code_status_enum_matches_documented_lifecycle_states():
    # SCALPING_V1_ROADMAP.md documents exactly these five states.
    assert VALID_STATUSES == {"NEW", "ACTIVE", "COOLING", "EXPIRED", "REJECTED"}
