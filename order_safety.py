from risk_config import (
    ENABLE_REAL_TRADING,
    ENABLE_PAPER_TRADING,
    MAX_POSITION_RATE,
    MAX_TRADES_PER_DAY,
    MAX_OPEN_POSITIONS,
)


def check_trading_mode():
    if ENABLE_REAL_TRADING:
        raise Exception("실거래 모드가 활성화되어 있습니다. 현재 단계에서는 금지입니다.")

    if not ENABLE_PAPER_TRADING:
        raise Exception("Paper Trading도 아직 비활성화되어 있습니다.")


def check_position_size(position_rate):
    if position_rate > MAX_POSITION_RATE:
        raise Exception(
            f"포지션 비중 초과: {position_rate:.2%} > {MAX_POSITION_RATE:.2%}"
        )


def check_daily_trade_count(today_trade_count):
    if today_trade_count >= MAX_TRADES_PER_DAY:
        raise Exception(
            f"하루 거래 횟수 초과: {today_trade_count} >= {MAX_TRADES_PER_DAY}"
        )


def check_open_positions(open_position_count):
    if open_position_count >= MAX_OPEN_POSITIONS:
        raise Exception(
            f"보유 종목 수 초과: {open_position_count} >= {MAX_OPEN_POSITIONS}"
        )


def run_order_safety_check(
    position_rate,
    today_trade_count,
    open_position_count
):
    check_trading_mode()
    check_position_size(position_rate)
    check_daily_trade_count(today_trade_count)
    check_open_positions(open_position_count)

    return True
