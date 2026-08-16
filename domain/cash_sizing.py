"""How many WHOLE shares a known orderable-cash figure pays for.

Separate from both the broker and the entry pipelines because two
pipelines (`scripts/run_shadow_mode.py`, `kis_live_trading.py`) size the
same way and must not each grow their own rounding.

Why Decimal
-----------
`floor(orderable / price)` in binary floating point can round the ratio
UP across an integer boundary, and that direction is unsafe: it buys one
more share than the cash covers. The sizing therefore happens in Decimal,
built from `str(value)` -- Python's float repr is the shortest string
that round-trips, so `str(30.99)` is `"30.99"`, the same decimal KIS put
on the wire. A binary float that merely APPROXIMATES 30.99 never widens
into 31.00 here.

There is no fractional path (project principle: 소수점 매수 금지) and no
"round to nearest": a ratio of 5.999... is five shares.
"""

import math
from decimal import ROUND_FLOOR, Decimal, InvalidOperation

# The one reason code for "we could not establish orderable cash". It is
# deliberately NOT the same code as a genuine zero balance: a failed or
# malformed read must never be recorded as "the account has $0", which is
# what makes an outage look like an ordinary insufficient-funds day.
ORDERABLE_CASH_UNAVAILABLE = "ORDERABLE_CASH_UNAVAILABLE"

# A real, successfully-read balance that cannot pay for one share.
INSUFFICIENT_CASH = "INSUFFICIENT_CASH"


def is_usable_amount(value):
    """True only for a real, non-negative, finite number.

    bool is excluded on purpose: `True` is an int in Python, and a
    cash figure that arrived as a boolean is a bug, not $1.
    """
    return (
        isinstance(value, (int, float, Decimal))
        and not isinstance(value, bool)
        and _is_finite(value)
        and value >= 0
    )


def _is_finite(value):
    if isinstance(value, Decimal):
        return value.is_finite()
    return math.isfinite(value)


def _to_decimal(value):
    """Decimal via the shortest round-tripping string -- see module docstring."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def whole_shares_affordable(orderable_usd, limit_price_usd):
    """floor(orderable / limit), in Decimal, never negative.

    Returns 0 for any unusable input rather than raising: the caller's
    next step is a "< 1 share" block either way, and a sizing helper that
    raises would turn a bad number into an unhandled error deep inside a
    candidate loop. What must NOT happen -- returning a share count for an
    unknown cash figure -- cannot happen here, because None is unusable.
    """
    if not is_usable_amount(orderable_usd):
        return 0
    if not is_usable_amount(limit_price_usd) or limit_price_usd <= 0:
        return 0
    try:
        shares = _to_decimal(orderable_usd) / _to_decimal(limit_price_usd)
    except (InvalidOperation, ZeroDivisionError, ArithmeticError):
        return 0
    return int(shares.to_integral_value(rounding=ROUND_FLOOR))
