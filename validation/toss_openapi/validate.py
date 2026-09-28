"""Run isolated, read-only Toss Open API capability checks.

Run only after exporting credentials, for example:
    set -a; source ~/trading/.env.toss; set +a
    python3 -m validation.toss_openapi.validate
"""

from __future__ import annotations

import sys
from typing import Any, Iterable

from .client import TossReadOnlyClient, ValidationError, mask_identifier, safe_error_message
from .websocket_client import TossWebSocketClient, WebSocketResult


# A fixed, deliberately small liquid-US universe.  No symbol-master or full-universe scan occurs.
BENCHMARK_SYMBOLS = (
    "AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO BRK.B JPM V UNH XOM LLY WMT MA ORCL JNJ COST HD PG"
    " NFLX CRM BAC ABBV KO CVX AMD PEP TMO ACN MCD CSCO LIN ABT DIS WFC DHR PM IBM GE CAT QCOM NOW"
    " INTU TXN ISRG AMGN AMAT BKNG GS HON PFE SPGI RTX LOW SBUX BLK DE GILD ADP MDLZ PLD TGT C CI"
    " SYK MMC CB AXP MO SCHW SO ZTS USB CME CL UPS FISV APD NEE EQIX EOG REGN SLB ITW SHW DUKE WM"
    " BDX ICE MCK HCA EMR GD FDX PSX AON NSC KMB ELV MPC ORLY OXY MAR PCAR GM F ODFL EW MRNA ROK"
    " KDP VLO KMI D HI SRE AEP EXC XEL WMB CARR DOW HAL HPQ EA RCL DAL UAL LUV NKE BMY CVS HUM"
    " MCO TROW AIG ALL AFL TRV MET PRU ATO PEG ETR ES WEC DTE AEE CMS FE PPL NI CNP LNT EVRG"
    " AES NRG VST CEG FANG DVN MRO APA COP OKE TRGP ET ENB SU BABA TSM ASML SHOP MELI PDD JD"
    " ADBE MU LRCX KLAC PANW CRWD SNPS CDNS ABNB DASH UBER LYFT COIN RIVN LCID PLTR SOFI RBLX RDDT SPOT ROKU WBD"
).split()
WS_SYMBOLS = ["AAPL", "NVDA", "QQQ"]


def _payload(value: Any) -> Any:
    """Accommodate the documented API envelope without rendering its content."""
    if isinstance(value, dict):
        for key in ("result", "data", "accounts"):
            if key in value:
                return value[key]
    return value


def _first_account_seq(value: Any) -> object | None:
    payload = _payload(value)
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0].get("accountSeq")
    if isinstance(payload, dict):
        return payload.get("accountSeq")
    return None


def _line(section: str, message: str) -> None:
    print("[%s] %s" % (section, message), flush=True)


def _result(result: Any) -> str:
    return "status=%s latency_ms=%s" % (result.status, result.latency_ms)


def _ws_result(result: WebSocketResult) -> str:
    return (
        "connected=%s latency_ms=%s messages=%s disconnects=%s ack=%s rejected=%s"
        % (result.connected, result.latency_ms, result.messages, result.disconnects, result.ack_received, result.rejected)
    )


def _run_ws(token: str, subscriptions: list[dict[str, Any]]) -> WebSocketResult:
    return TossWebSocketClient(token).subscribe(subscriptions)


def _quote_batches(client: TossReadOnlyClient, sizes: Iterable[int]) -> tuple[int, int]:
    successes = rate_limited = 0
    for size in sizes:
        try:
            result = client.prices(list(BENCHMARK_SYMBOLS[:size]))
            successes += 1
            _line("T4_REST_QUOTE", "batch=%s %s success=true" % (size, _result(result)))
        except ValidationError as exc:
            if "429" in str(exc):
                rate_limited += 1
            _line("T4_REST_QUOTE", "batch=%s success=false error=%s" % (size, safe_error_message(exc)))
    return successes, rate_limited


def main() -> int:
    printed_sections: set[str] = set()

    def section(name: str) -> None:
        printed_sections.add(name)
        print("\n[%s]" % name)

    print("[VALIDATION_IDENTITY]")
    print("isolated_read_only_runner=true production_integration=false")
    client = TossReadOnlyClient()
    account_seq: object | None = None
    token = ""
    critical: list[str] = []
    rate_limited = 0
    disconnects = 0
    try:
        section("T0_AUTH")
        auth = client.authenticate()
        token = client.access_token or ""
        print("%s success=true token=REDACTED" % _result(auth))

        section("T1_ACCOUNT")
        accounts = client.accounts()
        account_seq = _first_account_seq(accounts.data)
        if account_seq is None:
            raise ValidationError("ACCOUNT_SEQUENCE_NOT_FOUND")
        print("%s success=true accountSeq=%s" % (_result(accounts), mask_identifier(account_seq)))

        section("T2_BUYING_POWER")
        result = client.buying_power(account_seq)
        print("%s success=true" % _result(result))

        section("T3_COMMISSION")
        result = client.commissions(account_seq)
        print("%s success=true" % _result(result))

        section("T4_REST_QUOTE")
        _, count = _quote_batches(client, (1, 10, 50, 100, 200))
        rate_limited += count

        section("T5_1M_CANDLE")
        result = client.candles_1m()
        print("symbol=AAPL %s success=true" % _result(result))

        section("T6_TRADE_WS")
        result = _run_ws(token, [{"id": "t6"}, {"type": "trade:us", "codes": WS_SYMBOLS}])
        disconnects += result.disconnects
        print(_ws_result(result))

        section("T7_ORDERBOOK_WS")
        result = _run_ws(token, [{"id": "t7"}, {"type": "orderbook:us", "codes": WS_SYMBOLS}])
        disconnects += result.disconnects
        print(_ws_result(result))

        section("T8_CAPACITY")
        result = _run_ws(token, [{"id": "t8"}, {"type": "trade:us", "codes": WS_SYMBOLS}, {"type": "orderbook:us", "codes": WS_SYMBOLS}])
        disconnects += result.disconnects
        print("controlled_symbols=3 %s" % _ws_result(result))

        section("T9_DISCOVERY")
        result = client.prices(list(BENCHMARK_SYMBOLS[:50]))
        print("controlled_symbols=50 %s success=true universe_scan=false" % _result(result))

        section("T10_PERSONAL_ORDER")
        result = _run_ws(token, [{"id": "t10"}, {"type": "personal:order", "codes": [str(account_seq)]}])
        disconnects += result.disconnects
        print("accountSeq=%s %s" % (mask_identifier(account_seq), _ws_result(result)))
    except ValidationError as exc:
        critical.append(safe_error_message(exc))
        print("validation_stopped=true error=%s" % safe_error_message(exc))
    except Exception as exc:  # fail safely, without exposing potentially sensitive transport detail
        critical.append(safe_error_message(exc))
        print("validation_stopped=true error=%s" % safe_error_message(exc))
    for name in (
        "T0_AUTH", "T1_ACCOUNT", "T2_BUYING_POWER", "T3_COMMISSION", "T4_REST_QUOTE",
        "T5_1M_CANDLE", "T6_TRADE_WS", "T7_ORDERBOOK_WS", "T8_CAPACITY", "T9_DISCOVERY",
        "T10_PERSONAL_ORDER",
    ):
        if name not in printed_sections:
            section(name)
            print("not_run=true")
    section("CRITICAL_FINDINGS")
    print("429_count=%s disconnect_count=%s" % (rate_limited, disconnects))
    print("none" if not critical else "; ".join(critical))
    section("FINAL")
    print("Production changed: NO")
    print("Live order executed: NO")
    print("Secrets exposed: NO")
    return 0 if not critical else 1


if __name__ == "__main__":
    sys.exit(main())
