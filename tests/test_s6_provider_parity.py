"""Tests for the isolated deployed-S6 provider-parity adapter."""

from datetime import datetime, timedelta, timezone

import pandas as pd

from validation.s6_provider_parity import (
    ALIGNMENT_FAILED,
    align_bars,
    bars_frame,
    candidate_parity,
    evaluate_symbol,
    execution_safety,
    normalize_bars,
    report,
)


NOW = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
START = datetime(2026, 9, 29, 13, 30, tzinfo=timezone.utc)  # 09:30 ET


def _daily():
    index = pd.date_range(end=NOW.date(), periods=220, freq="D", tz="UTC")
    close = [100.0 + i * 0.1 for i in range(len(index))]
    return pd.DataFrame({"Open": close, "High": [x + 1 for x in close], "Low": [x - 1 for x in close],
                         "Close": close, "Volume": [1000] * len(index)}, index=index)


def _rows(*, delta=0.0, count=25):
    rows = []
    for i in range(count):
        close = (10.0 + i * 0.02 if i < 5 else 10.35 + (i - 5) * 0.01) + delta
        rows.append({"at": START + timedelta(minutes=i), "open": close - .01, "high": close + .02,
                     "low": close - .02, "close": close, "volume": 100.0 if i < 5 else 250.0})
    return rows


def test_same_completed_bars_produce_same_production_signal_and_score():
    result = evaluate_symbol("AAPL", kis_rows=_rows(), toss_rows=_rows(), daily=_daily(), now=NOW)
    assert result["status"] == "OK"
    assert result["qualification_same"] is True
    assert result["kis"]["qualified"] is True
    assert result["kis"]["score"] == result["toss"]["score"]
    assert result["kis"]["signal"].scanner_name == "orb"


def test_forming_bar_is_excluded():
    rows = _rows() + [{"at": NOW, "open": 100, "high": 100, "low": 100, "close": 100, "volume": 9999}]
    assert len(normalize_bars(rows, now=NOW)) == 25


def test_different_completed_timestamp_grid_fails_closed():
    left, right = normalize_bars(_rows(), now=NOW), normalize_bars(_rows()[:-1], now=NOW)
    _, _, reason = align_bars(left, right)
    assert reason == ALIGNMENT_FAILED


def test_opening_range_is_production_orb5_configured_range():
    result = evaluate_symbol("NVDA", kis_rows=_rows(), toss_rows=_rows(), daily=_daily(), now=NOW)
    opening = result["opening_range"]
    assert opening["kis"]["bar_count"] == 5
    assert opening["classification"] == "MATCH"


def test_provider_only_candidate_identifies_rejection():
    # Toss never gets enough post-range volume expansion; both series retain
    # the same bar grid, so this is a gate result rather than alignment noise.
    toss = _rows()
    for row in toss[5:]:
        row["volume"] = 100.0
    result = evaluate_symbol("AMD", kis_rows=_rows(), toss_rows=toss, daily=_daily(), now=NOW)
    assert result["kis"]["qualified"] is True
    assert result["toss"]["qualified"] is False
    assert result["first_divergence"]


def test_production_ranking_is_used_for_candidate_jaccard():
    a = evaluate_symbol("AAA", kis_rows=_rows(), toss_rows=_rows(), daily=_daily(), now=NOW)
    b = evaluate_symbol("BBB", kis_rows=_rows(), toss_rows=_rows(delta=.01), daily=_daily(), now=NOW)
    parity = candidate_parity([a, b], trading_day="2026-09-29", session="REGULAR")
    assert parity["jaccard"] == 1.0
    assert parity["intersection"] == ["AAA", "BBB"]


def test_execution_surface_is_explicitly_blocked():
    assert execution_safety() == {"BUY": "BLOCKED", "SELL": "BLOCKED", "CANCEL": "BLOCKED", "MODIFY": "BLOCKED"}


def test_normalized_frame_has_only_market_columns():
    frame = bars_frame(normalize_bars(_rows(), now=NOW))
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_report_is_json_serializable_and_does_not_emit_signal_objects():
    import json
    item = evaluate_symbol("AAPL", kis_rows=_rows(), toss_rows=_rows(), daily=_daily(), now=NOW)
    payload = report([item], trading_day="2026-09-29", session="REGULAR")
    assert payload["action_pool_parity"]["status"] == "NOT_EVALUATED_RUNTIME_STATE_REQUIRED"
    assert "signal" not in payload["rows"][0]["kis"]
    json.dumps(payload)
