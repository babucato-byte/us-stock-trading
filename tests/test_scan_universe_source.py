"""T8: the scanner reads the account-filtered universe when one exists.

`daily_candidate_scanner.load_scan_universe()` picks between
`universe_tradable.csv` (the affordable/liquid pool) and `universe.csv`
(the full listing). These tests pin the three cases that matter: prefer
the filtered file, honour an EMPTY filtered file, and fall back only when
the filtered file is genuinely unusable.
"""

import pandas as pd
import pytest

from daily_candidate_scanner import load_scan_universe

FULL = "symbol,name,exchange,tradable,shortable\nAAPL,Apple,NASDAQ,True,True\nBRKA,Berkshire,NYSE,True,False\n"
TRADABLE_HEADER = "symbol,name,exchange,tradable,shortable,price_usd,avg_dollar_volume_usd,price_ceiling_usd,max_affordable_shares\n"


def _write(tmp_path, full=FULL, tradable=None):
    (tmp_path / "universe.csv").write_text(full, encoding="utf-8")
    if tradable is not None:
        (tmp_path / "universe_tradable.csv").write_text(tradable, encoding="utf-8")
    return tmp_path


def test_filtered_universe_is_preferred_when_present(tmp_path):
    _write(tmp_path, tradable=TRADABLE_HEADER + "AAPL,Apple,NASDAQ,True,True,200.0,5e10,900.0,4\n")
    frame, source = load_scan_universe(tmp_path)
    assert source.name == "universe_tradable.csv"
    assert list(frame["symbol"]) == ["AAPL"]


def test_an_empty_filtered_universe_is_honoured_not_bypassed(tmp_path):
    """"Nothing is affordable right now" is a real answer. Falling back to
    the full listing here would re-admit exactly the symbols the account
    budget excluded."""
    _write(tmp_path, tradable=TRADABLE_HEADER)
    frame, source = load_scan_universe(tmp_path)
    assert source.name == "universe_tradable.csv"
    assert frame.empty


def test_missing_filtered_universe_falls_back_to_the_full_listing(tmp_path):
    _write(tmp_path)
    frame, source = load_scan_universe(tmp_path)
    assert source.name == "universe.csv"
    assert set(frame["symbol"]) == {"AAPL", "BRKA"}


@pytest.mark.parametrize("bad", ["", "no_symbol_column\n1\n"])
def test_unusable_filtered_universe_falls_back_to_the_full_listing(tmp_path, bad):
    _write(tmp_path, tradable=bad)
    frame, source = load_scan_universe(tmp_path)
    assert source.name == "universe.csv"
    assert set(frame["symbol"]) == {"AAPL", "BRKA"}


def test_fallback_is_reported_not_silent(tmp_path, capsys):
    _write(tmp_path, tradable="no_symbol_column\n1\n")
    load_scan_universe(tmp_path)
    assert "falling back" in capsys.readouterr().out


def test_default_base_dir_is_the_repo_root():
    frame, source = load_scan_universe()
    assert source.name in {"universe.csv", "universe_tradable.csv"}
    assert "symbol" in frame.columns
