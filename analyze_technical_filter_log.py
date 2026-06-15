from pathlib import Path

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "technical_filter_log.csv"
CHECK_COLUMNS = [
    "price_above_hma200",
    "hma200_rising",
    "hma_macd_bullish",
    "macd_histogram_rising",
    "sqzmom_green",
]
PERFORMANCE_COLUMNS = ["return_pct", "max_return_pct", "min_return_pct"]


def read_log(path=LOG_FILE):
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def bool_rate(series):
    if series.empty:
        return 0.0
    return float(series.astype(str).str.lower().isin({"true", "1", "yes"}).mean() * 100)


def build_report(df):
    lines = []
    if df.empty:
        return ["technical_filter_log.csv is empty or missing."]

    lines.append("Technical Filter Log Report")
    lines.append(f"Total rows: {len(df)}")
    lines.append("")

    lines.append("Score counts:")
    score_counts = df["technical_filter_score"].value_counts().sort_index()
    for score, count in score_counts.items():
        lines.append(f"- score {int(score)}: {int(count)}")
    lines.append("")

    lines.append("Pass/fail counts:")
    pass_counts = df["technical_filter_pass"].astype(str).str.lower().map(
        lambda value: "pass" if value in {"true", "1", "yes"} else "fail"
    ).value_counts()
    for label in ["pass", "fail"]:
        lines.append(f"- {label}: {int(pass_counts.get(label, 0))}")
    lines.append("")

    lines.append("Condition pass rates:")
    for column in CHECK_COLUMNS:
        if column in df.columns:
            lines.append(f"- {column}: {bool_rate(df[column]):.1f}%")
    lines.append("")

    lines.append("Recent scan_id counts:")
    recent = df["scan_id"].dropna().astype(str).drop_duplicates().tail(5).tolist()
    for scan_id in recent:
        count = int((df["scan_id"].astype(str) == scan_id).sum())
        lines.append(f"- {scan_id}: {count}")

    if "return_pct" in df.columns:
        performance_df = df.copy()
        for column in PERFORMANCE_COLUMNS:
            if column in performance_df.columns:
                performance_df[column] = pd.to_numeric(performance_df[column], errors="coerce")
        performance_df = performance_df.dropna(subset=["return_pct"])
        lines.append("")
        lines.extend(build_performance_report(performance_df))
    return lines


def build_performance_report(df):
    lines = ["Performance:"]
    if df.empty:
        lines.append("- No return_pct values yet.")
        return lines

    lines.append("Average return by score:")
    score_returns = df.groupby("technical_filter_score")["return_pct"].mean().sort_index()
    for score, value in score_returns.items():
        lines.append(f"- score {int(score)}: {value:.2f}%")

    lines.append("Average return by pass/fail:")
    labels = df["technical_filter_pass"].astype(str).str.lower().map(
        lambda value: "pass" if value in {"true", "1", "yes"} else "fail"
    )
    pass_returns = df.assign(pass_label=labels).groupby("pass_label")["return_pct"].mean()
    for label in ["pass", "fail"]:
        if label in pass_returns:
            lines.append(f"- {label}: {pass_returns[label]:.2f}%")

    lines.append("Condition performance when true:")
    for column in CHECK_COLUMNS:
        if column in df.columns:
            mask = df[column].astype(str).str.lower().isin({"true", "1", "yes"})
            subset = df.loc[mask]
            if not subset.empty:
                lines.append(
                    f"- {column}: hit_rate={mask.mean() * 100:.1f}% "
                    f"avg_return={subset['return_pct'].mean():.2f}% "
                    f"avg_max={subset['max_return_pct'].mean():.2f}% "
                    f"avg_min={subset['min_return_pct'].mean():.2f}%"
                )

    if "max_return_pct" in df.columns:
        lines.append("Top max_return_pct:")
        for _, row in df.sort_values("max_return_pct", ascending=False).head(10).iterrows():
            lines.append(
                f"- {row.get('symbol', '')}: max={row['max_return_pct']:.2f}% "
                f"score={int(row.get('technical_filter_score', 0))}"
            )

    if "min_return_pct" in df.columns:
        lines.append("Bottom min_return_pct:")
        for _, row in df.sort_values("min_return_pct", ascending=True).head(10).iterrows():
            lines.append(
                f"- {row.get('symbol', '')}: min={row['min_return_pct']:.2f}% "
                f"score={int(row.get('technical_filter_score', 0))}"
            )
    return lines


def main():
    df = read_log()
    report = build_report(df)
    print("\n".join(report))
    return report


if __name__ == "__main__":
    main()
