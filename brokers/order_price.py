"""The price KIS will accept, derived from the price the strategy chose.

The rejection this exists for
-----------------------------
2026-09-01 13:55:25Z, the first natural S6 order to reach the broker:

    KIS_ORDER_REJECTED symbol=LUMN side=buy qty=19 limit=6.405
    tr_id=TTTT1002U rt_cd=7 msg_cd=APTR0057
    "주문 가격을 확인 하시기 바랍니다. 1$이상 소수점 2자리까지만 가능 합니다."
    -- check the order price; at $1 and above only 2 decimal places.

`6.405` is three decimals. PCVX was queued behind it at `60.855` and would
have been refused the same way. JBS went through in the same minute at
`13.75` -- two decimals by luck, not by construction. So whether a live
order was accepted depended on how many decimals the last trade happened
to print, which is not a policy anyone chose.

What is normalized, and what deliberately is not
------------------------------------------------
Only the rule the broker actually stated: at $1 and above, two decimals.

Below $1 the message says nothing beyond implying more decimals are
allowed, and no other evidence in this repository establishes the limit.
A guessed sub-dollar rule would be the same class of mistake as the one
being fixed, so sub-dollar prices pass through untouched -- exactly the
behaviour they have today, which is not known to be broken.

Direction
---------
Never against the strategy's intent:

  BUY  -> floor. A buy limit that rounded UP would pay more than the
          strategy authorised.
  SELL -> ceiling. A sell limit that rounded DOWN would accept less than
          the strategy authorised.

Both make a fill marginally less likely, which is the correct side to err
on: the alternative is transacting at a price nobody approved. The
adjustment is at most one cent.

`Decimal`, not `round()`: binary floating point rounds 1.005 to 1.0 on
this interpreter, and half-even would round a price toward whichever
neighbour is even rather than toward the side the order can afford.
"""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Optional

#: Proven by the broker's own refusal message. Not a guess.
TWO_DECIMAL_FLOOR_USD = Decimal("1")
TWO_DECIMALS = Decimal("0.01")

SIDE_BUY = "buy"
SIDE_SELL = "sell"


def _as_decimal(value) -> Optional[Decimal]:
    if isinstance(value, Decimal):
        return value
    if value is None or isinstance(value, bool):
        return None
    try:
        # str() first: Decimal(float) would carry the binary
        # representation error this function exists to avoid.
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def normalize_limit_price(price, *, side):
    """The strategy's price, expressed the way KIS accepts it.

    Returns a `Decimal` at or above $1, and the input unchanged
    otherwise (including anything unparseable, which belongs to the
    caller's own validation rather than to this function).
    """
    amount = _as_decimal(price)
    if amount is None or amount < TWO_DECIMAL_FLOOR_USD:
        return price
    rounding = ROUND_CEILING if str(side).lower() == SIDE_SELL else ROUND_FLOOR
    return amount.quantize(TWO_DECIMALS, rounding=rounding)


def wire_price(price, *, side) -> str:
    """The string that goes on the wire, with its decimals intact.

    `str(Decimal("6.40"))` keeps the trailing zero; `str(6.40)` does not,
    and "6.4" is a different number of decimal places than the broker
    was asked about.
    """
    return str(normalize_limit_price(price, side=side))
