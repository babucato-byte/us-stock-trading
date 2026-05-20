from order_safety import run_order_safety_check

try:
    run_order_safety_check(
        position_rate=0.05,
        today_trade_count=0,
        open_position_count=0
    )

    print("주문 안전검사 통과")

except Exception as e:
    print("주문 안전검사 실패")
    print(e)
