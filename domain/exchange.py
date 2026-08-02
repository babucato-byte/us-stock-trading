"""HIGH-1: the single source of truth for what exchange a US symbol trades
on, and how that becomes a KIS wire code.

Every KIS request that names an exchange -- price, buy, sell, cancel, and
the normalization of balance/open-order/fill responses -- goes through
this module. Nothing else may build an exchange code.

Why this exists at all: Oracle read-only verification found that KIS
answers a WRONG-exchange price query with success, not an error:

    BBVA EXCD=NAS -> rt_cd=0, last=''      (BBVA is NYSE-listed)
    BBVA EXCD=NYS -> rt_cd=0, last='27.9500'

So a hardcoded `exchange="NASDAQ"` did not fail loudly -- it silently
produced an empty price for every NYSE and AMEX candidate, which the
caller then reported as a generic "price unavailable". Any default,
fallback, or "try NAS first" behaviour reintroduces exactly that bug, so
an unresolved exchange is a hard block here, never a guess.
"""

from enum import Enum


class UnsupportedExchangeError(Exception):
    """Raised when an exchange is missing, blank, or not one this system
    is allowed to trade. Callers must treat it as a hard block: no
    transport, no fallback, no default venue."""


class USExchange(str, Enum):
    """The venues this system supports. A symbol whose venue is not one of
    these cannot be priced or traded -- deliberately: ARCA, BATS and OTC
    names are excluded until each is verified against a real KIS
    response, rather than assumed to behave like one of these three."""

    NASDAQ = "NASDAQ"
    NYSE = "NYSE"
    NYSE_AMERICAN = "NYSE_AMERICAN"


# Aliases seen in universe.csv, scanner output and broker responses. The
# KEY is compared case-insensitively after stripping; the VALUE is the
# canonical member. Anything absent is unsupported, not defaulted.
_ALIASES = {
    "NASDAQ": USExchange.NASDAQ,
    "NASD": USExchange.NASDAQ,
    "NAS": USExchange.NASDAQ,
    "XNAS": USExchange.NASDAQ,
    "NASDAQ GLOBAL SELECT": USExchange.NASDAQ,
    "NASDAQ GLOBAL MARKET": USExchange.NASDAQ,
    "NASDAQ CAPITAL MARKET": USExchange.NASDAQ,
    "NYSE": USExchange.NYSE,
    "NYS": USExchange.NYSE,
    "XNYS": USExchange.NYSE,
    "NEW YORK STOCK EXCHANGE": USExchange.NYSE,
    "AMEX": USExchange.NYSE_AMERICAN,
    "AMS": USExchange.NYSE_AMERICAN,
    "NYSE AMERICAN": USExchange.NYSE_AMERICAN,
    "NYSE_AMERICAN": USExchange.NYSE_AMERICAN,
    "XASE": USExchange.NYSE_AMERICAN,
}

# KIS quotation `EXCD` parameter (price / quote endpoints).
_KIS_EXCD = {
    USExchange.NASDAQ: "NAS",
    USExchange.NYSE: "NYS",
    USExchange.NYSE_AMERICAN: "AMS",
}

# KIS order `OVRS_EXCG_CD` parameter (order / revise-cancel endpoints).
# These are a DIFFERENT vocabulary from the quotation codes above -- the
# reason both live here, so no caller can pair a quote code with an order
# request or vice versa.
_KIS_ORDER_EXCG = {
    USExchange.NASDAQ: "NASD",
    USExchange.NYSE: "NYSE",
    USExchange.NYSE_AMERICAN: "AMEX",
}


def normalize_exchange(value) -> USExchange:
    """Maps whatever an upstream source called the venue onto a canonical
    member. Blank, None and unknown all raise -- there is no default."""
    if isinstance(value, USExchange):
        return value
    if value is None:
        raise UnsupportedExchangeError("exchange is missing (None)")
    text = str(value).strip()
    if not text:
        raise UnsupportedExchangeError("exchange is blank")
    try:
        return _ALIASES[text.upper()]
    except KeyError:
        raise UnsupportedExchangeError(f"unsupported US exchange: {text!r}") from None


def is_supported(value) -> bool:
    """True when normalize_exchange() would succeed. For callers that need
    to classify without raising -- it must not be used to pick a default."""
    try:
        normalize_exchange(value)
    except UnsupportedExchangeError:
        return False
    return True


def to_kis_exchange_code(exchange) -> str:
    """Canonical exchange -> KIS quotation EXCD."""
    canonical = normalize_exchange(exchange)
    try:
        return _KIS_EXCD[canonical]
    except KeyError:  # pragma: no cover -- enum and table are kept in step
        raise UnsupportedExchangeError(
            f"no KIS quotation code for exchange: {canonical!r}"
        ) from None


def to_kis_order_exchange_code(exchange) -> str:
    """Canonical exchange -> KIS order OVRS_EXCG_CD."""
    canonical = normalize_exchange(exchange)
    try:
        return _KIS_ORDER_EXCG[canonical]
    except KeyError:  # pragma: no cover
        raise UnsupportedExchangeError(
            f"no KIS order-exchange code for exchange: {canonical!r}"
        ) from None


def supported_exchanges():
    return tuple(USExchange)
