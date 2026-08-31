#!/usr/bin/env python3
"""Alert when an S1 position is held and nothing is evaluating it.

This exists because the failure happened. On 2026-08-18 the executor cron
was paused to free a contended rate limiter, and a TX position filled
BEFORE the pause -- so for about an hour a real holding had no exit
evaluation running at all. Nothing noticed. A stop that cannot be
evaluated is not a stop.

What it checks
--------------
Only one thing, and only when the answer means something: if an OPEN S1
position exists AND the market is in a session where ticks are expected,
then the newest recorded tick must be recent.

Outside those sessions no tick is due, so silence is correct and this says
nothing. That distinction is the whole design: a watchdog that cries every
evening gets muted, and a muted watchdog is worse than none.

What it does about it
---------------------
Escalates to `kill_switch_state.ENTRY_DISABLED`, which blocks NEW ENTRIES
and leaves exits permitted -- verified on this account. It deliberately
does not halt trading outright: the position needs the exit path more than
it needs everything stopped, and ALL_TRADING_DISABLED would take that away.

Exit codes: 0 healthy or not applicable, 1 stale (alert raised), 2 the
check itself could not run.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = os.environ.get("TRADING_PROJECT_ROOT") or str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

logger = logging.getLogger("s1_position_watchdog")

#: The executor runs every 15 minutes. Two missed intervals plus a margin
#: for a slow tick -- a single tick has been observed taking ~11.5 minutes
#: when it walks ten candidates, so one interval is not enough headroom.
DEFAULT_MAX_SILENCE_MINUTES = 40

STATUS_HEALTHY = "WATCHDOG_HEALTHY"
STATUS_NO_POSITION = "WATCHDOG_NOT_APPLICABLE_NO_POSITION"
STATUS_SESSION_IDLE = "WATCHDOG_NOT_APPLICABLE_SESSION_IDLE"
STATUS_STALE = "S1_POSITION_UNMANAGED"
#: S1 is not a live strategy, so a stale S1 cycle is not an account-wide
#: emergency. Reported, never escalated.
STATUS_NOT_LIVE = "WATCHDOG_NOT_APPLICABLE_STRATEGY_NOT_LIVE"
STATUS_UNKNOWN = "WATCHDOG_CHECK_FAILED"


def log_dir() -> Path:
    configured = os.environ.get("S1_LIVE_LOG_DIR")
    return Path(configured) if configured else Path(ROOT) / "logs" / "s1_live"


def newest_tick_at(trading_day):
    """The most recent tick timestamp for `trading_day`, or None.

    Reads the structured cycle log rather than inventing a second
    heartbeat: that file already records every tick including the ones
    where nothing happened, which is exactly what "is it still running"
    needs.
    """
    path = log_dir() / f"cycles-{trading_day}.jsonl"
    if not path.exists():
        return None
    newest = None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            stamp = json.loads(line).get("started_at")
            parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if newest is None or parsed > newest:
            newest = parsed
    return newest


def ticks_expected_now() -> bool:
    """Is the executor supposed to be running right now?

    Mirrors the executor's own session rule rather than restating a
    schedule: if it would refuse to order, it is not due to tick.
    """
    try:
        import market_hours

        return market_hours.get_market_state() == market_hours.REGULAR
    except Exception:
        # Cannot tell -- say no, so an unreadable clock produces silence
        # rather than a false alarm every minute.
        return False


def check(*, max_silence_minutes=DEFAULT_MAX_SILENCE_MINUTES, now=None):
    from scanners.base.trading_calendar import us_trading_day
    from s1_live import position_store as ps
    from state_store import db as state_db

    current = now or datetime.now(timezone.utc)
    conn = state_db.open_db()
    try:
        live = ps.load_live(conn)
    finally:
        conn.close()

    result = {
        "checked_at": current.isoformat(),
        "open_positions": [
            {"position_id": pid, "symbol": state.symbol,
             "status": row["status"], "exit_submitted": bool(row["exit_submitted"])}
            for pid, state, row in live
        ],
        "max_silence_minutes": max_silence_minutes,
    }

    if not live:
        result["status"] = STATUS_NO_POSITION
        return result
    if not ticks_expected_now():
        # A held position outside the session is not unmanaged, it is
        # simply not tradeable yet.
        result["status"] = STATUS_SESSION_IDLE
        return result

    trading_day = us_trading_day()
    newest = newest_tick_at(trading_day)
    result["trading_day"] = trading_day
    result["newest_tick_at"] = newest.isoformat() if newest else None
    if newest is None:
        result["status"] = STATUS_STALE
        result["detail"] = f"no tick recorded for {trading_day} while holding a position"
        return result

    silence = current - newest
    result["silence_minutes"] = round(silence.total_seconds() / 60.0, 1)
    if silence > timedelta(minutes=max_silence_minutes):
        result["status"] = STATUS_STALE
        result["detail"] = (
            f"newest tick is {result['silence_minutes']:.1f} min old "
            f"(limit {max_silence_minutes}) while holding "
            f"{[p['symbol'] for p in result['open_positions']]}")
        return result

    result["status"] = STATUS_HEALTHY
    return result


def s1_is_live() -> bool:
    """May S1 reach a real order at all?

    Fails CLOSED -- an unreadable live-mode table is treated as S1 being
    live, so a config that cannot be read keeps the old escalating
    behaviour rather than silently disarming the watchdog.
    """
    try:
        from config import scanner_live_mode

        return scanner_live_mode.is_limited_live(
            scanner_live_mode.S1_SCANNER_NAME)
    except Exception:  # noqa: BLE001
        logger.warning("could not read the live-mode table; treating S1 as "
                       "live so the watchdog keeps its escalation",
                       exc_info=True)
        return True


def escalate(result) -> bool:
    """Block new entries, leave exits alone. Returns True if it changed.

    Only for a LIVE strategy. This escalation is account-wide: it stops
    S6's entries too, and on 2026-08-31 it did exactly that for forty
    minutes over an S1 reading that was false. Once S1 is DISCOVERY_ONLY
    it holds no real position and cannot place a real order, so an S1
    cycle going quiet is a paper-side problem -- worth reporting, never
    worth disabling the one strategy that is actually trading.

    The stale status is still returned and still announced; what changes
    is that it no longer reaches for the kill switch.
    """
    import kill_switch_state as kss

    if not s1_is_live():
        logger.error(
            "%s but S1 is not a live strategy -- reporting without "
            "escalating; disabling entries account-wide would stop S6 for "
            "a strategy that cannot place an order", STATUS_STALE)
        return False
    if kss.get_state() != "ACTIVE":
        logger.info("kill switch already %s -- no escalation needed", kss.get_state())
        return False
    reason = f"{STATUS_STALE}: {result.get('detail', '')}"[:300]
    kss.activate(kss.ENTRY_DISABLED, reason=reason, activated_by="s1_position_watchdog")
    logger.error("escalated to ENTRY_DISABLED -- new entries blocked, exits still "
                 "permitted: %s", reason)
    return True


def notify_monitor(result, *, escalated: bool) -> bool:
    """Announce a stale watchdog on #scanner-monitor.

    Only the STALE case. A watchdog that reports HEALTHY every ten minutes
    into a channel is how a channel stops being read, and the escalation
    it performs -- ENTRY_DISABLED -- is precisely the thing an operator
    needs to learn about without going to look for it.

    Whether the kill switch actually CHANGED is stated rather than
    implied: "already ENTRY_DISABLED" and "just disabled entries now" are
    different situations, and a message that reads the same for both
    would hide a watchdog firing repeatedly.
    """
    try:
        from scanners.notify import monitor

        body = "\n".join([
            f"상태: {result.get('status')}",
            f"내용: {result.get('detail', '-')}",
            f"종목: {result.get('symbol', '-')}",
            f"무응답 시간: {result.get('silent_minutes', '-')}분",
            f"킬 스위치: "
            f"{'ENTRY_DISABLED (지금 차단됨)' if escalated else '변경 없음'}",
            "매도 경로는 계속 유지됩니다.",
        ])
        return monitor.notify_tagged(monitor.TAG_WATCHDOG, body)
    except Exception:  # noqa: BLE001 - the watchdog's job is the kill switch
        logger.warning("watchdog could not send its monitor message", exc_info=True)
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-silence-minutes", type=int,
                        default=DEFAULT_MAX_SILENCE_MINUTES)
    parser.add_argument("--no-escalate", action="store_true",
                        help="report only; do not touch the kill switch")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    try:
        result = check(max_silence_minutes=args.max_silence_minutes)
    except Exception as exc:  # noqa: BLE001 - a failed check must be visible
        logger.error("watchdog check failed: %s", exc, exc_info=True)
        print(json.dumps({"status": STATUS_UNKNOWN, "error": str(exc)}, default=str))
        return 2

    print(json.dumps(result, default=str))
    if result["status"] != STATUS_STALE:
        logger.info("%s", result["status"])
        return 0

    logger.error("%s: %s", result["status"], result.get("detail"))
    escalated = False
    if not args.no_escalate:
        escalated = escalate(result)
    notify_monitor(result, escalated=escalated)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
