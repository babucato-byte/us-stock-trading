#!/usr/bin/env python3
"""Does KIS deliver extended-hours VOLUME? Measure it; do not assume.

Why this exists
---------------
S6's entry conditions are VWAP, EMA structure, ORB range and volume
expansion. Three of those need only price. The fourth needs volume, and
the daily-bar provider reports zero volume outside the regular session --
not "no data", but a number that reads as "nobody traded", which is the
one answer that must never be inferred.

So premarket and after-hours can be scanned and watched but cannot
produce a READY candidate, and that is the whole reason S6 is a
regular-session strategy in practice while being a four-session strategy
by design.

KIS publishes overseas real-time trades as HDFSCNT0 over a WebSocket.
This asks it directly, in a live session, and records what comes back.

What it will NOT do
-------------------
Infer. Every outcome below is a classification of an actual response, and
"we could not tell" is one of them. A probe that guessed would be worse
than no probe, because the guess would become the reason to enable
trading in a session whose data nobody had checked.

No order is placed. Nothing here can reach an execution path.
"""

import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from execution.secret_redaction import install_logging_redaction  # noqa: E402
from market_data.kis_realtime_ws import WebSocket, WebSocketError  # noqa: E402

logger = logging.getLogger("kis_realtime_probe")

#: The outcomes, named so a report cannot blur them together.
VOLUME_AVAILABLE = "KIS_EXTENDED_VOLUME_AVAILABLE"
VOLUME_UNAVAILABLE = "KIS_EXTENDED_VOLUME_UNAVAILABLE"
PERMISSION_REQUIRED = "KIS_REALTIME_PERMISSION_REQUIRED"
SUBSCRIPTION_REQUIRED = "MARKET_DATA_SUBSCRIPTION_REQUIRED"
NO_TRADES_OBSERVED = "NO_TRADES_OBSERVED_IN_WINDOW"
PROBE_FAILED = "PROBE_FAILED"

REALTIME_HOST = "ops.koreainvestment.com"
REALTIME_PORT_LIVE = 21000
REALTIME_PORT_PAPER = 31000

TR_TRADE = "HDFSCNT0"

#: HDFSCNT0's field order, from KIS's published layout. Named here so a
#: shifted or shortened record is visible as a mismatch rather than
#: silently read as different values.
HDFSCNT0_FIELDS = (
    "RSYM", "SYMB", "ZDIV", "TSYM", "XYMD", "XHMS", "KYMD", "KHMS",
    "OPEN", "HIGH", "LOW", "LAST", "SIGN", "DIFF", "RATE",
    "PBID", "PASK", "VBID", "VASK",
    "EVOL", "TVOL", "TAMT", "BIVL", "ASVL", "STRN", "MTYP",
)

#: The three that decide the question.
FIELD_TRADE_SIZE = "EVOL"        # this trade's size
FIELD_CUMULATIVE = "TVOL"        # cumulative session volume
FIELD_AMOUNT = "TAMT"            # cumulative traded amount

#: KIS addresses an overseas symbol by a FEED prefix plus the ticker,
#: and the prefix chooses which feed you get:
#:
#:   D...  delayed quotes  (included with the account)
#:   R...  real-time       (a separately purchased subscription)
#:
#: They are different products, not different spellings, and the
#: distinction is the whole question this probe exists to settle. A
#: delayed feed can answer SUBSCRIBE SUCCESS and then deliver nothing
#: outside regular hours, which looks exactly like "extended hours carry
#: no volume" while actually meaning "this feed does not cover them".
#: So both are asked, and what each one answers is reported separately.
DELAYED_PREFIX = {"NAS": "DNAS", "NASD": "DNAS", "NYS": "DNYS",
                  "NYSE": "DNYS", "AMS": "DAMS", "AMEX": "DAMS"}
REALTIME_PREFIX = {"NAS": "RBAQ", "NASD": "RBAQ", "NYS": "RBAY",
                   "NYSE": "RBAY", "AMS": "RBAA", "AMEX": "RBAA"}
FEED_DELAYED = "delayed"
FEED_REALTIME = "realtime"
EXCHANGE_PREFIX = DELAYED_PREFIX


def _approval_key(app_key, app_secret, base_url):
    """The WebSocket's own credential. Separate from the REST token."""
    import requests

    response = requests.post(
        f"{base_url}/oauth2/Approval",
        headers={"content-type": "application/json"},
        data=json.dumps({"grant_type": "client_credentials",
                         "appkey": app_key, "secretkey": app_secret}),
        timeout=20,
    )
    if response.status_code != 200:
        raise PermissionError(
            f"approval request refused with HTTP {response.status_code}: "
            f"{response.text[:300]}")
    key = (response.json() or {}).get("approval_key")
    if not key:
        raise PermissionError(
            f"approval response carried no approval_key: {response.text[:300]}")
    return key


def _subscribe_frame(approval_key, tr_key, *, tr_id=TR_TRADE, subscribe=True):
    return json.dumps({
        "header": {
            "approval_key": approval_key,
            "custtype": "P",
            "tr_type": "1" if subscribe else "2",
            "content-type": "utf-8",
        },
        "body": {"input": {"tr_id": tr_id, "tr_key": tr_key}},
    })


def _tr_key(symbol, exchange, feed=FEED_DELAYED):
    table = REALTIME_PREFIX if feed == FEED_REALTIME else DELAYED_PREFIX
    prefix = table.get(str(exchange or "").upper())
    if not prefix:
        raise ValueError(
            f"no KIS {feed} prefix for exchange {exchange!r}")
    return f"{prefix}{str(symbol).upper()}"


#: KIS returns a per-stream AES key and IV on every SUBSCRIBE SUCCESS.
#: They are ephemeral and unused while `encrypt` is "N", but they are
#: still a key on a wire, and this probe writes its output to a file and
#: into a report. Nothing that looks like a key gets stored.
_SECRET_CONTROL_FIELDS = ("key", "iv")


def _scrub_control(message):
    """Blank the stream key/IV, keeping everything a reader needs."""
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


def _is_pingpong(message):
    """KIS's application-level keep-alive."""
    if "PINGPONG" not in (message or ""):
        return False
    try:
        return (json.loads(message).get("header") or {}).get("tr_id") == "PINGPONG"
    except (ValueError, TypeError):
        return False


def parse_trades(payload):
    """An HDFSCNT0 frame -> list of records.

    The wire format is `0|HDFSCNT0|<count>|<caret-delimited fields>`, and
    `count` is not decoration: KIS packs SEVERAL trades into one frame
    when they arrive together. The first run of this probe read the whole
    body as a single record and logged `fields=156` for one NVDA frame --
    six trades flattened into one, five of them silently dropped.

    For a probe answering "is there volume at all", losing five trades
    changes nothing. For the aggregation this is meant to feed, it would
    understate volume by whatever fraction of trades arrive in bursts --
    which is largest exactly when the market is busy, and volume
    expansion is what S6 is looking for.

    A frame whose length is not a whole number of records is returned as
    one flagged record rather than chopped: a changed layout must be
    visible, not mapped positionally onto the wrong names.
    """
    if not payload or payload[0] not in "01":
        return []
    parts = payload.split("|")
    if len(parts) < 4 or parts[1] != TR_TRADE:
        return []
    fields = parts[3].split("^")
    width = len(HDFSCNT0_FIELDS)
    try:
        declared = int(parts[2])
    except (TypeError, ValueError):
        declared = None

    if len(fields) < width or len(fields) % width:
        return [{"raw_field_count": len(fields), "declared_count": declared,
                 "layout_mismatch": True}]

    records = []
    for index in range(len(fields) // width):
        chunk = fields[index * width:(index + 1) * width]
        record = {"raw_field_count": len(fields), "declared_count": declared,
                  "records_in_frame": len(fields) // width,
                  "layout_mismatch": False}
        record.update(dict(zip(HDFSCNT0_FIELDS, chunk)))
        records.append(record)
    return records


def parse_trade(payload):
    """The first record of a frame, or None. Kept for callers that want
    one; `parse_trades` is what the collector uses."""
    found = parse_trades(payload)
    return found[0] if found else None


def _as_number(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def classify(records, *, control_messages):
    """What the responses actually establish. No inference."""
    joined = " ".join(control_messages).upper()
    for marker, verdict in (
            ("APPROVAL", PERMISSION_REQUIRED),
            ("NOT AUTHORIZED", PERMISSION_REQUIRED),
            ("권한", PERMISSION_REQUIRED),
            ("사용자권한", PERMISSION_REQUIRED),
            ("시세신청", SUBSCRIPTION_REQUIRED),
            ("신청", SUBSCRIPTION_REQUIRED)):
        if marker in joined:
            return verdict

    if not records:
        return NO_TRADES_OBSERVED

    sizes = [_as_number(r.get(FIELD_TRADE_SIZE)) for r in records]
    cumulative = [_as_number(r.get(FIELD_CUMULATIVE)) for r in records]
    if any(s for s in sizes if s) or any(c for c in cumulative if c):
        return VOLUME_AVAILABLE
    # Trades arrived and every volume field was zero or absent. That is
    # an answer, and a different one from "no trades".
    return VOLUME_UNAVAILABLE


def probe(symbols, *, seconds, env=None, port=None, feeds=(FEED_DELAYED,)):
    env = env if env is not None else os.environ
    app_key = env.get("KIS_APP_KEY")
    app_secret = env.get("KIS_APP_SECRET")
    base_url = env.get("KIS_BASE_URL") or "https://openapi.koreainvestment.com:9443"
    if not app_key or not app_secret:
        raise PermissionError("KIS_APP_KEY / KIS_APP_SECRET are not set")
    # Never the value: enough to tell two keys apart in a report, and
    # not enough to use one.
    logger.info("app key fingerprint: len=%d tail=%s", len(app_key),
                app_key[-4:] if len(app_key) > 8 else "****")

    approval = _approval_key(app_key, app_secret, base_url)
    logger.info("approval key obtained (len=%d)", len(approval))

    live = str(env.get("KIS_ENV", "")).strip().lower() == "live"
    ws_port = port or (REALTIME_PORT_LIVE if live else REALTIME_PORT_PAPER)
    logger.info("connecting to %s:%s (KIS_ENV=%s)", REALTIME_HOST, ws_port,
                env.get("KIS_ENV"))

    records = []
    control = []
    pongs = [0]
    per_symbol = defaultdict(list)
    started = time.time()
    with WebSocket(REALTIME_HOST, ws_port, "/", timeout=15.0) as ws:
        for symbol, exchange in symbols:
            for feed in feeds:
                key = _tr_key(symbol, exchange, feed)
                ws.send_text(_subscribe_frame(approval, key))
                logger.info("subscribed %s (%s) as %s [%s]", symbol,
                            exchange, key, feed)

        while time.time() - started < seconds:
            try:
                message = ws.recv()
            except socket_timeout_types():
                continue
            except WebSocketError as exc:
                logger.warning("websocket ended: %s", exc)
                break
            if message is None:
                continue
            if _is_pingpong(message):
                # KIS's keep-alive is an APPLICATION message, not a
                # protocol ping: it arrives as a text frame carrying
                # tr_id PINGPONG and must be echoed back verbatim.
                # Answering the RFC 6455 ping opcode is not enough --
                # the first run of this probe did exactly that and KIS
                # closed the connection after 100 seconds, which read as
                # "no trades" when it was really "we stopped listening".
                ws.send_text(message)
                pongs[0] += 1
                continue
            parsed_all = parse_trades(message)
            if not parsed_all:
                safe = _scrub_control(message)
                control.append(safe[:600])
                logger.info("control: %s", safe[:300])
                continue
            for parsed in parsed_all:
                records.append(parsed)
                per_symbol[parsed.get("SYMB") or parsed.get("RSYM")].append(parsed)
                if len(records) <= 12:
                    logger.info(
                        "TRADE %s local=%s/%s last=%s trade_size(%s)=%s "
                        "cumulative(%s)=%s amount(%s)=%s in_frame=%s%s",
                        parsed.get("SYMB"), parsed.get("XYMD"),
                        parsed.get("XHMS"), parsed.get("LAST"),
                        FIELD_TRADE_SIZE, parsed.get(FIELD_TRADE_SIZE),
                        FIELD_CUMULATIVE, parsed.get(FIELD_CUMULATIVE),
                        FIELD_AMOUNT, parsed.get(FIELD_AMOUNT),
                        parsed.get("records_in_frame"),
                        " LAYOUT_MISMATCH" if parsed["layout_mismatch"] else "")

    verdict = classify(records, control_messages=control)
    return {
        "verdict": verdict,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "window_seconds": seconds,
        "subscribed": [f"{s}:{e}" for s, e in symbols],
        "feeds": list(feeds),
        "trade_records": len(records),
        "keepalives_answered": pongs[0],
        "symbols_with_trades": {k: len(v) for k, v in per_symbol.items()},
        "control_messages": control[:10],
        "sample": records[:5],
    }


def socket_timeout_types():
    import socket as _socket

    return (_socket.timeout, TimeoutError)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Probe KIS HDFSCNT0 for extended-hours volume")
    parser.add_argument("--symbols", default="META:NAS,AAPL:NAS,NVDA:NAS",
                        help="comma-separated SYMBOL:EXCHANGE pairs")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--feeds", default=FEED_DELAYED,
                        help="comma-separated: delayed, realtime, or both")
    parser.add_argument("--out", default=None,
                        help="write the JSON result here as well")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(message)s")
    install_logging_redaction()

    pairs = []
    for token in args.symbols.split(","):
        token = token.strip()
        if not token:
            continue
        symbol, _, exchange = token.partition(":")
        pairs.append((symbol.strip(), (exchange or "NAS").strip()))

    try:
        feeds = tuple(f.strip() for f in args.feeds.split(",") if f.strip())
        result = probe(pairs, seconds=args.seconds, feeds=feeds)
    except PermissionError as exc:
        result = {"verdict": PERMISSION_REQUIRED, "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 -- a probe reports, never raises
        logger.exception("probe failed")
        result = {"verdict": PROBE_FAILED, "detail": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
