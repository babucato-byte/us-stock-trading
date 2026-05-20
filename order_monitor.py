import os
import time
import requests
from dotenv import load_dotenv
from slack_utils import send_slack_alert

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL")

headers = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY
}


def get_recent_orders():

    url = f"{BASE_URL}/v2/orders?status=all&limit=10"

    response = requests.get(
        url,
        headers=headers,
        timeout=10
    )

    response.raise_for_status()

    return response.json()


def monitor_orders():

    print("주문 체결 모니터 시작")

    checked_orders = set()

    while True:

        try:
            orders = get_recent_orders()

            for order in orders:

                order_id = order["id"]

                if order_id in checked_orders:
                    continue

                status = order["status"]
                symbol = order["symbol"]

                print(symbol, status)

                if status in [
                    "new",
                    "accepted",
                    "pending_new"
                ]:
                    checked_orders.add(order_id)



                if status == "filled":

                    filled_qty = order.get("filled_qty", "0")
                    filled_avg_price = order.get(
                        "filled_avg_price",
                        "0"
                    )

                    msg = f"""
*Paper Trading 체결 완료*

종목: {symbol}
체결수량: {filled_qty}
평균체결가: ${filled_avg_price}

상태: FILLED
"""

                    send_slack_alert(msg)

                    checked_orders.add(order_id)

                elif status in [
                    "canceled",
                    "rejected",
                    "expired"
                ]:

                    msg = f"""
*Paper Trading 주문 실패*

종목: {symbol}
상태: {status.upper()}
"""

                    send_slack_alert(msg)

                    checked_orders.add(order_id)

            time.sleep(20)

        except Exception as e:

            print("모니터 오류")
            print(e)

            send_slack_alert(
                f"주문 모니터 오류 발생\n```{str(e)}```"
            )

            time.sleep(30)


if __name__ == "__main__":
    monitor_orders()
