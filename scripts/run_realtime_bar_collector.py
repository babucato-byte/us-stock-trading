#!/usr/bin/env python3
"""Collect KIS trades into one-minute bars for the current session.

Runs alongside the trading crons and deliberately touches nothing they
use. It holds a market-data WebSocket and writes a snapshot file; it
never takes the KIS rate-limit lock, never opens the order database and
never calls a broker REST endpoint. The 2026-08-27 starvation came from
a market-data-shaped workload competing for a trading resource, and this
is a market-data-shaped workload.

The snapshot is what makes a restart survivable. Without it, restarting
mid-session leaves the strategy reading volume=0 and VWAP=None, which is
indistinguishable from a session in which nothing traded -- and that is
the reading that must never be produced by our own process management.
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from execution.secret_redaction import install_logging_redaction  # noqa: E402
from market_data import kis_hdfscnt0 as wire  # noqa: E402
from market_data.kis_realtime_ws import WebSocket, WebSocketError  # noqa: E402
from market_data.realtime_bars import RealtimeBarStore  # noqa: E402

logger = logging.getLogger("realtime_bars")

#: One collector per account, enforced with a file lock held for the
#: process's life. Two collectors on one snapshot file would each write
#: their own view of the session and the last writer would win --
#: producing a volume that is neither of them and belongs to no
#: measurement anyone made.
SINGLETON_LOCK = "/home/ubuntu/logs/cron/s6_realtime_collector.lock"

REALTIME_HOST = "ops.koreainvestment.com"
PORT_LIVE = 21000
PORT_PAPER = 31000


class AlreadyRunning(Exception):
    """Another collector holds the singleton lock."""


def acquire_singleton(path=None):
    """Hold the collector lock for this process's lifetime, or raise.

    Returned handle must stay referenced: closing it releases the lock.
    """
    import fcntl

    target = Path(path or os.environ.get("COLLECTOR_LOCK") or SINGLETON_LOCK)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = open(target, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise AlreadyRunning(
            f"another collector already holds {target}") from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def snapshot_path(session, trading_day, *, env=None):
    env = env if env is not None else os.environ
    root = env.get("REALTIME_BAR_DIR") or env.get("SCANNER_DATA_ROOT") \
        or "/home/ubuntu/releases/us-stock-trading/shared/scanner"
    return Path(root) / "realtime_bars" / f"{trading_day}-{session}.json"


def _approval_key(app_key, app_secret, base_url):
    import requests

    response = requests.post(
        f"{base_url}/oauth2/Approval",
        headers={"content-type": "application/json"},
        data=json.dumps({"grant_type": "client_credentials",
                         "appkey": app_key, "secretkey": app_secret}),
        timeout=20)
    if response.status_code != 200:
        raise PermissionError(
            f"approval refused with HTTP {response.status_code}")
    key = (response.json() or {}).get("approval_key")
    if not key:
        raise PermissionError("approval response carried no approval_key")
    return key


def _load(path, *, stale_after_seconds=None):
    """Resume this session's bars, or start clean.

    A snapshot for a DIFFERENT session is not resumed: the file is keyed
    by session and day precisely so yesterday's regular-session volume
    cannot become this morning's premarket volume.
    """
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            store = RealtimeBarStore.restore(payload)
            logger.info("resumed %d symbols from %s", len(payload.get(
                "accumulators") or ()), path)
            return store
    except Exception:  # noqa: BLE001 - an unreadable snapshot starts a
        # fresh session rather than aborting the collector; the bars are
        # rebuildable and the alternative is no data at all.
        logger.warning("could not resume %s; starting clean", path,
                       exc_info=True)
    return RealtimeBarStore()


def _persist(store, path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(store.snapshot()), encoding="utf-8")
        temp.replace(path)
    except Exception:  # noqa: BLE001 - losing a snapshot costs a restart
        # its history, never the live collection.
        logger.warning("could not persist bars to %s", path, exc_info=True)


def collect(symbols, *, session, trading_day, seconds, env=None,
            persist_every=30.0, resume=None):
    env = env if env is not None else os.environ
    app_key = env.get("KIS_APP_KEY")
    app_secret = env.get("KIS_APP_SECRET")
    base_url = env.get("KIS_BASE_URL") or "https://openapi.koreainvestment.com:9443"
    if not app_key or not app_secret:
        raise PermissionError("KIS_APP_KEY / KIS_APP_SECRET are not set")

    path = snapshot_path(session, trading_day, env=env)
    store = resume if resume is not None else _load(path)
    approval = _approval_key(app_key, app_secret, base_url)
    live = str(env.get("KIS_ENV", "")).strip().lower() == "live"
    port = PORT_LIVE if live else PORT_PAPER

    started = time.time()
    last_persist = started
    try:
        with WebSocket(REALTIME_HOST, port, "/", timeout=15.0) as ws:
            store.mark_connected()
            for symbol, exchange in symbols:
                ws.send_text(wire.subscribe_frame(
                    approval, wire.tr_key(symbol, exchange,
                                          wire.FEED_REALTIME)))
            logger.info("subscribed %d symbols for %s", len(symbols), session)

            while time.time() - started < seconds:
                try:
                    message = ws.recv()
                except (TimeoutError, OSError):
                    continue
                if message is None:
                    continue
                if wire.is_pingpong(message):
                    ws.send_text(message)
                    continue
                trades = wire.parse_trades(message)
                if not trades:
                    logger.info("control: %s", wire.scrub_control(message)[:200])
                    continue
                for trade in trades:
                    store.add_trade(trade, session=session)
                if time.time() - last_persist >= persist_every:
                    _persist(store, path)
                    last_persist = time.time()
    except WebSocketError as exc:
        store.mark_disconnected()
        logger.warning("feed ended: %s", exc)
    finally:
        _persist(store, path)

    return store, path


def main(argv=None):
    parser = argparse.ArgumentParser(description="KIS one-minute bar collector")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--seconds", type=float, default=300.0)
    parser.add_argument("--session", default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(message)s")
    install_logging_redaction()

    from market_hours import us_trading_day
    from scanners.base import scan_session

    now = datetime.now(timezone.utc)
    session = args.session or scan_session.session_at()
    trading_day = us_trading_day(now)

    pairs = []
    for token in args.symbols.split(","):
        token = token.strip()
        if not token:
            continue
        symbol, _, exchange = token.partition(":")
        pairs.append((symbol.strip(), (exchange or "NAS").strip()))
    if not pairs:
        logger.error("no symbols given; nothing to collect")
        return 1

    try:
        lock = acquire_singleton()
    except AlreadyRunning as exc:
        logger.error("refusing to start: %s", exc)
        return 2

    # Reconnect rather than exit. A dropped socket is ordinary -- KIS
    # closes idle connections and networks blip -- and a collector that
    # died on the first one would leave the rest of the session with no
    # volume at all, which is exactly the state that stops S6 trading
    # premarket. Each reconnect re-subscribes and records its gap; the
    # gap is what makes the missing volume visible rather than silently
    # absent.
    deadline = time.time() + args.seconds
    reconnects = 0
    store = path = None
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        store, path = collect(pairs, session=session, trading_day=trading_day,
                              seconds=remaining, resume=store)
        if time.time() >= deadline:
            break
        reconnects += 1
        logger.warning("reconnecting (%d) after a dropped feed", reconnects)
        time.sleep(min(5.0 * reconnects, 30.0))
    lock.close()
    summary = {"session": session, "trading_day": trading_day,
               "snapshot": str(path), "reconnects": reconnects,
               "feed": store.describe()}
    for symbol, _exchange in pairs:
        accumulator = store.accumulator(symbol, session)
        if accumulator is None:
            summary[symbol] = {"bars": 0, "volume": 0, "vwap": None}
            continue
        summary[symbol] = {
            "bars": len(accumulator.bars),
            "volume": accumulator.volume,
            "trades": accumulator.trade_count,
            "vwap": accumulator.vwap,
            "vwap_kis_ratio": accumulator.vwap_from_kis_cumulative,
            "cross_check": accumulator.volume_cross_check(),
        }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
