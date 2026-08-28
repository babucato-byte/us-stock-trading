"""KIS's overseas real-time trade stream (HDFSCNT0), on the wire.

Extracted from the probe that first measured it so the probe and the
production feed cannot drift apart. Two of the details here were each
found by losing real data, and both failed quietly rather than loudly:

  * the keep-alive is an APPLICATION message carrying `tr_id=PINGPONG`,
    not the RFC 6455 ping opcode. Answering only the opcode gets the
    connection closed after about a hundred seconds, which then reads as
    "this session has no trades".

  * `count` in `0|HDFSCNT0|<count>|...` is real. One observed frame
    carried nineteen trades. Reading a frame as one record undercounts
    volume by whatever fraction arrives in bursts, and bursts are
    largest when the market is busiest -- so the error is biased against
    exactly the volume expansion S6 is looking for.

Measured 2026-08-28 during OVERNIGHT_DAYTIME: EVOL, TVOL and TAMT all
carry real values outside regular hours. The daily-bar provider's zero
was a provider limitation, not a fact about the market.
"""

import json

#: How many symbols one appkey may stream at once. MEASURED, not read
#: from a document: on 2026-08-28 the 42nd subscription was refused with
#: `OPSP0008 MAX SUBSCRIBE OVER` after 41 were accepted.
#:
#: This number decides an architecture. A universe of six hundred cannot
#: be watched in real time, so discovery for premarket and after-hours
#: cannot work by streaming everything and filtering: the pool has to be
#: narrowed to at most this many BEFORE any realtime data exists, and
#: only then does the stream refine it.
MAX_SUBSCRIPTIONS = 41

#: One WebSocket per appkey, also measured: a second connection is
#: refused with `OPSP8996 ALREADY IN USE appkey`, and KIS holds the slot
#: for a while after a disconnect. So the collector must be a single
#: long-lived process rather than something restarted per tick, and any
#: diagnostic that opens a stream competes with production for the one
#: slot.
ONE_CONNECTION_PER_APPKEY = True

MSG_MAX_SUBSCRIBE_OVER = "OPSP0008"
MSG_APPKEY_IN_USE = "OPSP8996"

TR_TRADE = "HDFSCNT0"
SOURCE = "KIS_HDFSCNT0"

#: HDFSCNT0's published field order. Named so a changed or shortened
#: record is visible as a mismatch rather than silently mapped onto the
#: wrong names -- which would put the price in the volume column and let
#: everything downstream answer confidently with the wrong number.
FIELDS = (
    "RSYM", "SYMB", "ZDIV", "TSYM", "XYMD", "XHMS", "KYMD", "KHMS",
    "OPEN", "HIGH", "LOW", "LAST", "SIGN", "DIFF", "RATE",
    "PBID", "PASK", "VBID", "VASK",
    "EVOL", "TVOL", "TAMT", "BIVL", "ASVL", "STRN", "MTYP",
)

FIELD_PRICE = "LAST"
FIELD_TRADE_SIZE = "EVOL"     # this trade's size
FIELD_CUMULATIVE = "TVOL"     # cumulative volume, as KIS counts it
FIELD_AMOUNT = "TAMT"         # cumulative traded amount
FIELD_LOCAL_DATE = "XYMD"
FIELD_LOCAL_TIME = "XHMS"

#: A feed prefix selects a PRODUCT, not a spelling: `D...` is the
#: delayed quote included with the account, `R...` the separately
#: purchased real-time one. Both answered SUBSCRIBE SUCCESS here.
#: Keyed by every spelling the rest of the system actually produces.
#: `exchange_registry` answers "NASDAQ" where the KIS wire codes are
#: "NAS", and the first live run of the bootstrap died on exactly that --
#: one unmappable name, and the collector never started.
_NASDAQ = ("NAS", "NASD", "NASDAQ")
_NYSE = ("NYS", "NYSE", "NEW YORK STOCK EXCHANGE")
_AMEX = ("AMS", "AMEX", "NYSE AMERICAN", "NYSE MKT")

DELAYED_PREFIX = {name: prefix for names, prefix in (
    (_NASDAQ, "DNAS"), (_NYSE, "DNYS"), (_AMEX, "DAMS")) for name in names}
REALTIME_PREFIX = {name: prefix for names, prefix in (
    (_NASDAQ, "RBAQ"), (_NYSE, "RBAY"), (_AMEX, "RBAA")) for name in names}
FEED_DELAYED = "delayed"
FEED_REALTIME = "realtime"

#: The feed that actually carries data for this account.
#:
#: MEASURED on 2026-08-28 during REGULAR, one feed at a time:
#:   RBAQ (realtime) -> SUBSCRIBE SUCCESS, and 0 trades in 70 seconds
#:   DNAS (delayed)  -> 1124 trades in 70 seconds
#:
#: The realtime subscription is a FALSE POSITIVE: KIS accepts it and
#: sends nothing, so "SUBSCRIBE SUCCESS" is not evidence that data will
#: arrive. An earlier dual-feed probe subscribed both at once and I
#: credited the trades to realtime without proving it -- the collector
#: was then configured for realtime alone and collected nothing while
#: reporting 41 of 41 subscribed.
#:
#: Despite the name, this feed is effectively real time.
DEFAULT_FEED = FEED_DELAYED

#: How far behind it runs, measured PER TRADE at ingest:
#: median 0.57s, p95 1.20s, max 1.27s over 512 samples during REGULAR.
#:
#: An earlier reading here said 70 seconds and was wrong, in a way worth
#: keeping written down. It compared a probe's summary timestamp --
#: written when the run ENDED -- against trades captured when it began,
#: so it measured the length of the observation window and called it
#: lag. The number was plausible for something labelled "delayed", which
#: is exactly why nothing questioned it.
#:
#: This constant is documentation of one measurement. Nothing consults
#: it: `realtime_bars.FeedLag` observes the distribution continuously,
#: because a threshold built on a frozen number is the zero-volume
#: assumption in another costume.
OBSERVED_FEED_LAG_SECONDS = 0.57


def tr_key(symbol, exchange, feed=FEED_REALTIME):
    table = REALTIME_PREFIX if feed == FEED_REALTIME else DELAYED_PREFIX
    prefix = table.get(str(exchange or "").upper())
    if not prefix:
        raise ValueError(f"no KIS {feed} prefix for exchange {exchange!r}")
    return f"{prefix}{str(symbol).upper()}"


def subscribe_frame(approval_key, key, *, tr_id=TR_TRADE, subscribe=True):
    return json.dumps({
        "header": {"approval_key": approval_key, "custtype": "P",
                   "tr_type": "1" if subscribe else "2",
                   "content-type": "utf-8"},
        "body": {"input": {"tr_id": tr_id, "tr_key": key}},
    })


def is_pingpong(message):
    """KIS's application-level keep-alive, which must be echoed back."""
    if "PINGPONG" not in (message or ""):
        return False
    try:
        return (json.loads(message).get("header") or {}).get("tr_id") == "PINGPONG"
    except (ValueError, TypeError):
        return False


#: What a feed is actually doing, which is not what it agreed to do.
#:
#: The distinction that cost a session: KIS accepted an RBAQ
#: registration this account is not entitled to and then sent nothing.
#: "SUBSCRIBE SUCCESS" only ever establishes SUBSCRIPTION_ACCEPTED, and
#: readiness requires DATA_FLOWING -- a message actually received.
SUBSCRIPTION_ACCEPTED = "SUBSCRIPTION_ACCEPTED"
DATA_FLOWING = "DATA_FLOWING"
NO_DATA_FLOW = "NO_DATA_FLOW"
ENTITLEMENT_SUSPECTED = "ENTITLEMENT_SUSPECTED"
FEED_FAILED = "FAILED"


def feed_capability(*, accepted, messages, seconds_observed,
                    expect_data_after=45.0):
    """What this feed has PROVEN, not what it acknowledged.

    `expect_data_after` is the window past which silence on a liquid
    subscription stops being plausible. Below it the honest answer is
    still "accepted, nothing yet" -- an illiquid premarket name can go
    minutes without a print, and calling that an entitlement problem
    would be the same species of guess in the other direction.
    """
    if not accepted:
        return FEED_FAILED
    if messages > 0:
        return DATA_FLOWING
    if seconds_observed >= expect_data_after:
        # Accepted, observed long enough, and silent. Suspected, never
        # concluded: only KIS can say whether this is entitlement.
        return ENTITLEMENT_SUSPECTED
    return SUBSCRIPTION_ACCEPTED


def feed_of(tr_key):
    """Which feed a message's tr_key came from.

    Recorded per message so a dual subscription can never again have its
    data credited to the wrong feed -- which is exactly how the collector
    ended up configured for one that sends nothing.
    """
    text = str(tr_key or "").upper()
    if text[:1] == "D":
        return FEED_DELAYED
    if text[:1] == "R":
        return FEED_REALTIME
    return None


def parse_trades(payload):
    """An HDFSCNT0 frame -> list of trade records.

    A body that is not a whole number of records comes back as ONE
    record flagged `layout_mismatch` rather than being chopped: a
    changed layout has to be visible, and splitting it anyway would map
    fields positionally onto the wrong names.
    """
    if not payload or payload[0] not in "01":
        return []
    parts = payload.split("|")
    if len(parts) < 4 or parts[1] != TR_TRADE:
        return []
    fields = parts[3].split("^")
    width = len(FIELDS)
    try:
        declared = int(parts[2])
    except (TypeError, ValueError):
        declared = None

    if len(fields) < width or len(fields) % width:
        return [{"raw_field_count": len(fields), "declared_count": declared,
                 "layout_mismatch": True}]

    records = []
    total = len(fields) // width
    for index in range(total):
        chunk = fields[index * width:(index + 1) * width]
        record = {"raw_field_count": len(fields), "declared_count": declared,
                  "records_in_frame": total, "layout_mismatch": False,
                  "source": SOURCE, "tr_id": parts[1]}
        record.update(dict(zip(FIELDS, chunk)))
        records.append(record)
    return records


def as_number(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


#: KIS returns a per-stream AES key and IV on every SUBSCRIBE SUCCESS.
#: Unused while `encrypt` is "N", but still a key on a wire, and these
#: messages get written to files and into reports.
_SECRET_CONTROL_FIELDS = ("key", "iv")


def scrub_control(message):
    try:
        parsed = json.loads(message)
    except (ValueError, TypeError):
        return message
    output = (parsed.get("body") or {}).get("output")
    if isinstance(output, dict):
        for field in _SECRET_CONTROL_FIELDS:
            if output.get(field):
                output[field] = f"<redacted len={len(str(output[field]))}>"
    return json.dumps(parsed, ensure_ascii=False)
