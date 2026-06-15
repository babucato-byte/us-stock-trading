import pandas as pd

from score_scanner.premarket_momentum_score import (
    OUTPUT_COLUMNS,
    evaluate_symbol,
    run_scan,
    sample_data,
)


def test_evaluate_symbol_scores_required_and_bonus_conditions():
    intraday, daily = sample_data()

    candidate = evaluate_symbol("SAMPLE", intraday, daily, timestamp="2026-06-15 09:00:00")

    assert candidate is not None
    assert candidate["symbol"] == "SAMPLE"
    assert candidate["score"] >= 70
    assert candidate["break_prev_high"] is True
    assert candidate["near_or_break_52w_high"] is True
    assert set(OUTPUT_COLUMNS) == set(candidate.keys())


def test_evaluate_symbol_excludes_when_required_condition_fails():
    intraday, daily = sample_data()
    intraday = intraday.copy()
    intraday["Volume"] = 1000

    candidate = evaluate_symbol("SAMPLE", intraday, daily)

    assert candidate is None


def test_run_scan_sample_writes_candidate_csv(tmp_path, monkeypatch):
    import score_scanner.premarket_momentum_score as scanner

    output = tmp_path / "score_scanner_candidates.csv"
    paper_log = tmp_path / "paper_trades_score_scanner.csv"
    monkeypatch.setattr(scanner, "CANDIDATES_FILE", output)
    monkeypatch.setattr(scanner, "PAPER_TRADES_FILE", paper_log)
    monkeypatch.setattr(scanner, "LOG_DIR", tmp_path)

    df = run_scan(sample=True)

    saved = pd.read_csv(output)
    assert len(df) == 1
    assert len(saved) == 1
    assert paper_log.exists()
