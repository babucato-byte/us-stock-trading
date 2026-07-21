import pandas as pd

from broker import AlpacaBroker

UNIVERSE_OUTPUT_PATH = "universe.csv"


def fetch_active_us_equity_rows(broker=None):
    """Fetch tradable US equity assets via the broker's safety-gated GET.

    Replaces a previous version that built the Alpaca base URL directly
    from ALPACA_PAPER_BASE_URL/ALPACA_BASE_URL and called requests.get()
    with no endpoint validation (CODEX-009) — AlpacaBroker.get_assets()
    goes through the same validate_order_allowed() gate as every other
    broker call, so a misconfigured or malicious endpoint is rejected
    before any network access, exactly like account/position/order calls.
    """
    broker = broker or AlpacaBroker()
    assets = broker.get_assets()
    rows = []
    for asset in assets:
        if (
            asset.get("status") == "active"
            and asset.get("tradable") is True
            and asset.get("class") == "us_equity"
        ):
            rows.append({
                "symbol": asset.get("symbol"),
                "name": asset.get("name"),
                "exchange": asset.get("exchange"),
                "tradable": asset.get("tradable"),
                "shortable": asset.get("shortable"),
            })
    return rows


def build_universe(broker=None, output_path=UNIVERSE_OUTPUT_PATH):
    rows = fetch_active_us_equity_rows(broker)
    df = pd.DataFrame(rows)
    df = df.dropna(subset=["symbol"])
    df = df.drop_duplicates(subset=["symbol"])
    df.to_csv(output_path, index=False)
    print(f"거래 가능 종목 저장 완료: {len(df)}개")
    print(f"파일: {output_path}")
    return df


if __name__ == "__main__":
    build_universe()
