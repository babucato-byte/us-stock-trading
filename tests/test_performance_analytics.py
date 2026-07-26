import pandas as pd

import performance_analytics as analytics
from performance_analytics import (
    SUMMARY_COLUMNS,
    build_performance_summary,
    calculate_profit_factor,
    calculate_win_rate,
    write_performance_files,
)


def test_empty_order_data_is_handled():
    summary, trades = build_performance_summary(
        pd.DataFrame(),
        pd.DataFrame(),
        {},
        local_history=pd.DataFrame(),
    )

    assert summary["total_orders"] == 0
    assert summary["filled_orders"] == 0
    assert summary["open_positions"] == 0
    assert summary["win_rate"] == 0.0
    assert trades.empty


def test_win_rate_calculation():
    trades = pd.DataFrame(
        [
            {"status": "filled", "unrealized_pl": 10},
            {"status": "filled", "unrealized_pl": -5},
            {"status": "filled", "unrealized_pl": 3},
            {"status": "canceled", "unrealized_pl": 99},
        ]
    )

    assert calculate_win_rate(trades) == 66.67


def test_profit_factor_calculation():
    trades = pd.DataFrame(
        [
            {"unrealized_pl": 20},
            {"unrealized_pl": 10},
            {"unrealized_pl": -15},
        ]
    )

    assert calculate_profit_factor(trades) == 2.0


def test_summary_csv_generation(tmp_path, monkeypatch):
    summary = {
        "total_orders": 1,
        "filled_orders": 1,
        "canceled_orders": 0,
        "rejected_orders": 0,
        "open_positions": 1,
        "win_rate": 100.0,
        "avg_profit_pct": 2.5,
        "avg_loss_pct": 0.0,
        "profit_factor": 10.0,
        "total_unrealized_pl": 12.5,
        "daily_return_pct": 0.35,
        "best_symbol": "AAPL",
        "worst_symbol": "",
        "generated_at": "2026-06-01 09:30:00",
    }
    trades = pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "side": "buy",
                "qty": 1,
                "filled_avg_price": 100,
                "current_price": 102.5,
                "unrealized_pl": 2.5,
                "unrealized_plpc": 2.5,
                "status": "filled",
                "submitted_at": "2026-06-01T13:30:00Z",
                "filled_at": "2026-06-01T13:30:01Z",
            }
        ]
    )

    monkeypatch.setattr(analytics, "PERFORMANCE_SUMMARY_FILE", tmp_path / "performance_summary.csv")
    monkeypatch.setattr(analytics, "PERFORMANCE_TRADES_FILE", tmp_path / "performance_trades.csv")
    # CODEX-038: write_performance_files() also always writes a
    # strategy-level breakdown (build_strategy_performance() when
    # strategy_df isn't passed) -- without isolating this path too, it
    # silently wrote to the real repo-root strategy_performance.csv,
    # changing its mtime on every test run.
    monkeypatch.setattr(analytics, "STRATEGY_PERFORMANCE_FILE", tmp_path / "strategy_performance.csv")
    write_performance_files(summary, trades)

    result = pd.read_csv(tmp_path / "performance_summary.csv")
    assert result.columns.tolist() == SUMMARY_COLUMNS
    assert result.iloc[0]["win_rate"] == 100.0
    assert (tmp_path / "performance_trades.csv").exists()
    assert (tmp_path / "strategy_performance.csv").exists()


def test_dashboard_performance_route_importable(monkeypatch):
    import dashboard.app as dashboard_app

    def fake_report():
        return (
            {
                "total_orders": 1,
                "filled_orders": 1,
                "win_rate": 100.0,
                "profit_factor": 1.5,
                "total_unrealized_pl": 12.5,
                "daily_return_pct": 0.35,
                "open_positions": 1,
                "best_symbol": "AAPL",
                "worst_symbol": "",
                "api_error": "",
            },
            pd.DataFrame(
                [
                    {
                        "symbol": "AAPL",
                        "status": "filled",
                        "unrealized_pl": 12.5,
                    }
                ]
            ),
        )

    monkeypatch.setattr(dashboard_app, "generate_performance_report", fake_report)
    client = dashboard_app.app.test_client()
    response = client.get("/performance")

    assert response.status_code == 200
    assert "성과 분석".encode("utf-8") in response.data
