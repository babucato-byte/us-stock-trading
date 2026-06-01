from broker import AlpacaBroker
from risk_config import MAX_DAILY_LOSS_RATE


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


if __name__ == "__main__":
    check_daily_loss_limit()
    print("Daily loss limit check passed")
