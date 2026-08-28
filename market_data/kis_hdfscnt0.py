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
DELAYED_PREFIX = {"NAS": "DNAS", "NASD": "DNAS", "NYS": "DNYS",
                  "NYSE": "DNYS", "AMS": "DAMS", "AMEX": "DAMS"}
REALTIME_PREFIX = {"NAS": "RBAQ", "NASD": "RBAQ", "NYS": "RBAY",
                   "NYSE": "RBAY", "AMS": "RBAA", "AMEX": "RBAA"}
FEED_DELAYED = "delayed"
FEED_REALTIME = "realtime"


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
                  "source": SOURCE}
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
