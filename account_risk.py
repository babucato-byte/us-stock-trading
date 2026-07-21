import math

from broker import AlpacaBroker
from risk_config import MAX_DAILY_LOSS_RATE, MAX_OPEN_POSITIONS, MAX_TOTAL_EXPOSURE_RATE


def get_account():
    return AlpacaBroker().get_account()


def check_daily_loss_limit(account=None):
    account = account or get_account()
    equity = float(account["equity"])
    last_equity = float(account["last_equity"])

    if last_equity <= 0:
        return True

    daily_return = (equity - last_equity) / last_equity
    print(f"Current equity: {equity}")
    print(f"Previous equity: {last_equity}")
    print(f"Daily return: {daily_return:.2%}")

    if daily_return <= MAX_DAILY_LOSS_RATE:
        raise Exception(f"Daily loss limit exceeded: {daily_return:.2%} <= {MAX_DAILY_LOSS_RATE:.2%}")

    return True


def _safe_nonnegative_float(value):
    """Parse `value` to a finite, non-negative float; None on any failure.

    Covers missing values, wrong types, NaN, infinities, and negative
    numbers -- all of which mean the input can't be trusted to compute an
    exposure figure, so callers must fail closed rather than treat it as 0.
    """
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed) or math.isinf(parsed) or parsed < 0:
        return None
    return parsed


def check_account_exposure_limits(positions, account):
    """Pure fail-closed check: is a new order allowed under the account-wide
    open-position-count (MAX_OPEN_POSITIONS) and total-exposure-rate
    (MAX_TOTAL_EXPOSURE_RATE) caps?

    `positions` must be a list of dicts, each carrying a numeric,
    non-negative `market_value`. `account` must be a dict carrying a
    numeric, positive `equity`. Both caps block at-or-above their
    threshold (reaching the cap exactly blocks, not just exceeding it).
    Any missing, malformed, NaN, or negative input blocks the order
    (returns False) instead of being coerced into a default -- there is no
    safe assumption to make about data that can't be trusted to enforce a
    risk cap.
    """
    if not isinstance(positions, list) or not isinstance(account, dict):
        return False

    equity = _safe_nonnegative_float(account.get("equity"))
    if equity is None or equity <= 0:
        return False

    if len(positions) >= MAX_OPEN_POSITIONS:
        return False

    total_exposure = 0.0
    for position in positions:
        if not isinstance(position, dict):
            return False
        market_value = _safe_nonnegative_float(position.get("market_value"))
        if market_value is None:
            return False
        total_exposure += market_value

    exposure_rate = total_exposure / equity
    if exposure_rate >= MAX_TOTAL_EXPOSURE_RATE:
        return False

    return True


if __name__ == "__main__":
    check_daily_loss_limit()
    print("Daily loss limit check passed")
