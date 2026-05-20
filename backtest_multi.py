import yfinance as yf
import pandas as pd

tickers = [
    "AAPL", "MSFT", "STLA", "C", "PLTR",
    "GM", "F", "CVX", "PRU"
]

INITIAL_CASH = 10000
TAKE_PROFIT = 0.15
STOP_LOSS = -0.08

MAX_ALLOWED_MDD = -25
MAX_ALLOWED_CONSECUTIVE_LOSSES = 5

def calculate_rsi(df, period=14):
    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def max_drawdown(equity_curve):
    peak = equity_curve.cummax()
    drawdown = (equity_curve - peak) / peak
    return drawdown.min() * 100


def backtest_symbol(symbol):
    df = yf.Ticker(symbol).history(period="5y")
    spy_df = yf.Ticker("SPY").history(period="5y")

    if df.empty or len(df) < 220:
        return None

    df["MA200"] = df["Close"].rolling(window=200).mean()
    df["RSI"] = calculate_rsi(df)
    df["AVG_VOLUME_20"] = df["Volume"].rolling(window=20).mean()

    cash = INITIAL_CASH
    position = 0
    buy_price = 0
    trades = []
    equity_values = []
    equity_dates = []

    for i in range(200, len(df)):
        price = df["Close"].iloc[i]
        ma200 = df["MA200"].iloc[i]
        rsi = df["RSI"].iloc[i]

        volume = df["Volume"].iloc[i]
        avg_volume_20 = df["AVG_VOLUME_20"].iloc[i]
        volume_ratio = volume / avg_volume_20

        current_value = cash + position * price
        equity_values.append(current_value)
        equity_dates.append(df.index[i])

        # 매수 조건
        if position == 0:
            if price > ma200 and 40 <= rsi <= 65 and volume_ratio >= 1.2:
                position = cash / price
                buy_price = price
                cash = 0
                trades.append({
                    "type": "BUY",
                    "date": df.index[i],
                    "price": price
                })

        # 매도 조건
        else:
            profit_rate = (price - buy_price) / buy_price

            if profit_rate >= TAKE_PROFIT or profit_rate <= STOP_LOSS or price < ma200:
                cash = position * price
                position = 0

                trades.append({
                    "type": "SELL",
                    "date": df.index[i],
                    "price": price,
                    "profit_rate": profit_rate
                })

    if position > 0:
        final_price = df["Close"].iloc[-1]
        cash = position * final_price

    final_value = cash
    total_return = (final_value - INITIAL_CASH) / INITIAL_CASH * 100

    years = 5

    cagr = ((final_value / INITIAL_CASH) ** (1 / years) - 1) * 100

    sell_trades = [t for t in trades if t["type"] == "SELL"]

    wins = [t for t in sell_trades if t["profit_rate"] > 0]
    losses = [t for t in sell_trades if t["profit_rate"] <= 0]

    # 최대 연속 손실 계산

    max_consecutive_losses = 0
    current_losses = 0

    for t in sell_trades:

        if t["profit_rate"] <= 0:
            current_losses += 1

            if current_losses > max_consecutive_losses:
                max_consecutive_losses = current_losses

        else:
            current_losses = 0

    win_rate = (len(wins) / len(sell_trades) * 100) if sell_trades else 0

    avg_profit = (
        sum(t["profit_rate"] for t in sell_trades) / len(sell_trades) * 100
        if sell_trades else 0
    )

    equity_series = pd.Series(
        equity_values,
        index=equity_dates
    )
    mdd = max_drawdown(equity_series) if not equity_series.empty else 0
    monthly_returns = equity_series.resample("ME").last().pct_change()

    positive_months = (monthly_returns > 0).sum()
    negative_months = (monthly_returns <= 0).sum()

    return {
        "symbol": symbol,
        "final_value": final_value,
        "total_return": total_return,
        "trade_count": len(sell_trades),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": win_rate,
        "avg_profit": avg_profit,
        "max_drawdown": mdd,
        "max_consecutive_losses": max_consecutive_losses,
        "cagr": cagr,
        "positive_months": int(positive_months),
        "negative_months": int(negative_months),
    }


results = []

for symbol in tickers:
    print(f"{symbol} 백테스트 중...")
    result = backtest_symbol(symbol)
    if result:
        results.append(result)

results = sorted(results, key=lambda x: x["total_return"], reverse=True)

print("\n=== 여러 종목 백테스트 결과 ===\n")

for r in results:
    risk_status = "통과"

    if r["max_drawdown"] < MAX_ALLOWED_MDD:
        risk_status = "위험 제외 후보"

    if r["max_consecutive_losses"] > MAX_ALLOWED_CONSECUTIVE_LOSSES:
        risk_status = "위험 제외 후보"
    print(f"""
[{r['symbol']}]
위험 판정: {risk_status}
최종 자금: ${r['final_value']:.2f}
총 수익률: {r['total_return']:.2f}%
거래 횟수: {r['trade_count']}
승리: {r['win_count']}
손실: {r['loss_count']}
승률: {r['win_rate']:.2f}%
평균 거래 수익률: {r['avg_profit']:.2f}%
최대손실 MDD: {r['max_drawdown']:.2f}%
최대 연속 손실: {r['max_consecutive_losses']}회
연평균 수익률 CAGR: {r['cagr']:.2f}%
수익 월: {r['positive_months']}개월
손실 월: {r['negative_months']}개월
----------------------------
""")

if results:
    avg_return = sum(r["total_return"] for r in results) / len(results)
    avg_win_rate = sum(r["win_rate"] for r in results) / len(results)
    avg_mdd = sum(r["max_drawdown"] for r in results) / len(results)

    # CSV 파일로 저장
    df_results = pd.DataFrame(results)
    df_results.to_csv("backtest_results.csv", index=False)
    print("\nCSV 저장 완료: backtest_results.csv")
    # 통과 종목만 watchlist로 저장
    passed_results = [
        r for r in results
        if r.get("risk_status") == "통과"
    ]

    df_watchlist = pd.DataFrame(passed_results)
    df_watchlist.to_csv("watchlist.csv", index=False)

    print("Watchlist 저장 완료: watchlist.csv")

    print("=== 전체 요약 ===")
    print(f"테스트 종목 수: {len(results)}")
    print(f"평균 수익률: {avg_return:.2f}%")
    print(f"평균 승률: {avg_win_rate:.2f}%")
    print(f"평균 최대손실 MDD: {avg_mdd:.2f}%")
