import time

from broker import AlpacaBroker
from slack_utils import send_slack_alert


def get_recent_orders(broker=None):
    broker = broker or AlpacaBroker()
    return broker.get_recent_orders(limit=10)


def monitor_orders():
    broker = AlpacaBroker()
    print(f"Order monitor started. Broker mode: {broker.config.status_label}")
    checked_orders = set()

    while True:
        try:
            orders = get_recent_orders(broker)
            for order in orders:
                order_id = order["id"]
                if order_id in checked_orders:
                    continue

                status = order["status"]
                symbol = order["symbol"]
                print(symbol, status)

                if status in ["new", "accepted", "pending_new"]:
                    checked_orders.add(order_id)
                    continue

                if status == "filled":
                    filled_qty = order.get("filled_qty", "0")
                    filled_avg_price = order.get("filled_avg_price", "0")
                    send_slack_alert(
                        f"*Paper Trading Fill*\n- Symbol: {symbol}\n- Filled qty: {filled_qty}\n"
                        f"- Avg price: ${filled_avg_price}\n- Status: FILLED"
                    )
                    checked_orders.add(order_id)

                elif status in ["canceled", "rejected", "expired"]:
                    send_slack_alert(
                        f"*Paper Trading Order Update*\n- Symbol: {symbol}\n- Status: {status.upper()}"
                    )
                    checked_orders.add(order_id)

            time.sleep(20)

        except Exception as exc:
            print("Order monitor error")
            print(exc)
            send_slack_alert(f"*Order monitor error*\n```{str(exc)[:1000]}```")
            time.sleep(30)


if __name__ == "__main__":
    monitor_orders()
