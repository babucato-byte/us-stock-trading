from broker import BrokerConfig
from risk_config import MAX_OPEN_POSITIONS, MAX_POSITION_RATE, MAX_TRADES_PER_DAY


def check_trading_mode():
    config = BrokerConfig()
    if config.is_live_mode and config.can_submit_live_order:
        raise Exception("Real live trading is blocked by the safety layer.")
    return True


def check_position_size(position_rate):
    if position_rate > MAX_POSITION_RATE:
        raise Exception(f"Position size exceeded: {position_rate:.2%} > {MAX_POSITION_RATE:.2%}")


def check_daily_trade_count(today_trade_count):
    if today_trade_count >= MAX_TRADES_PER_DAY:
        raise Exception(f"Daily trade count exceeded: {today_trade_count} >= {MAX_TRADES_PER_DAY}")


def check_open_positions(open_position_count):
    if open_position_count >= MAX_OPEN_POSITIONS:
        raise Exception(f"Open position count exceeded: {open_position_count} >= {MAX_OPEN_POSITIONS}")


def run_order_safety_check(position_rate, today_trade_count, open_position_count):
    check_trading_mode()
    check_position_size(position_rate)
    check_daily_trade_count(today_trade_count)
    check_open_positions(open_position_count)
    return True
