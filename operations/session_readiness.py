"""One readiness verdict per enabled market session, for stock-live-trading.

Read-only. It gathers facts that already exist -- the health checks in
`trading_health_check`, the broker's own cash/positions/open-orders
reads, the collector status file, the universe and ranking files -- and
turns them into the one compact message an operator reads at the start
of 데이장 / 프리장 / 정규장 / 애프터장.

What "ready" means
------------------
Every HARD check passed: live mode, release identity, KIS token and
account, reconciliation clean, collector connected with all its
subscriptions, kill switch off, and the session itself orderable for
S6. Anything else is a warning printed under the verdict. A report with
a failed hard check is BLOCKED and the message never says 준비 완료 --
`slack_presentation.session_ready` enforces that from the `ready` flag.

Nothing here can order, and nothing here writes trading state. The only
write is the notification ledger claim that keeps the message to one
per (trading day, session).
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STRATEGY_ID = "S6_ORB_BREAKOUT_V1"

#: A ranking older than this is stale for the purpose of a session start.
RANKING_MAX_AGE_DAYS = 4

#: Health-check names whose FAIL blocks the session.
HARD_CHECKS = ("trading_mode", "release", "kis_token", "kis_account",
               "reconciliation", "collector:connected", "collector:subscriptions",
               "kill_switch")

#: Health-check FAIL -> reason code shown on a BLOCKED message.
CHECK_CODES = {
    "trading_mode": "LIVE_MODE_OFF",
    "release": "RELEASE_IDENTITY_MISMATCH",
    "kis_token": "KIS_TOKEN_PROBLEM",
    "kis_account": "ACCOUNT_ALLOWLIST_MISMATCH",
    "reconciliation": "RECONCILIATION_BLOCKED",
    "collector:connected": "COLLECTOR_DISCONNECTED",
    "collector:subscriptions": "COLLECTOR_SUBSCRIPTIONS_INCOMPLETE",
    "kill_switch": "KILL_SWITCH",
}


def _now(now=None):
    return now or datetime.now(timezone.utc)


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _data_root(env) -> Path:
    root = env.get("SCANNER_DATA_ROOT")
    if root:
        return Path(root)
    return Path("/home/ubuntu/releases/us-stock-trading/shared/scanner")


def _universe_size(env) -> Optional[int]:
    path = env.get("SCANNER_UNIVERSE_FILE") or str(_data_root(env) / "universe.csv")
    try:
        with open(path, encoding="utf-8") as handle:
            rows = sum(1 for _ in handle) - 1
        return max(rows, 0)
    except Exception:  # noqa: BLE001
        return None


def _ranking_fresh(now) -> Optional[bool]:
    """True/False from the ranking file's own date, None if unreadable."""
    try:
        from scanners.base import activity

        path = activity.store_path()
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        stamp = (payload.get("asof") or payload.get("generated_at")
                 or payload.get("trading_day"))
        if not stamp:
            return None
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (now - moment).days <= RANKING_MAX_AGE_DAYS
    except Exception:  # noqa: BLE001
        return None


def _subscriptions(env) -> str:
    try:
        path = _data_root(env) / "realtime_bars" / "collector_status.json"
        status = json.loads(path.read_text(encoding="utf-8"))
        got = status.get("subscription_count")
        want = status.get("subscription_requested") or got
        return f"{got}/{want}"
    except Exception:  # noqa: BLE001
        return "-"


def _session_capable(now, session) -> Optional[bool]:
    try:
        from config import session_capability as sc

        cap = sc.capability_at(now, strategy_id=STRATEGY_ID)
        if str(cap.session) != str(session):
            return None
        return str(cap.entry_reason) == "CAPABLE"
    except Exception:  # noqa: BLE001
        return None


def _broker_facts(broker) -> Dict[str, Any]:
    facts: Dict[str, Any] = {"kis_connected": None, "orderable_cash_usd": None,
                             "open_positions": None, "open_orders": None}
    if broker is None:
        return facts
    try:
        facts["orderable_cash_usd"] = float(broker.get_account_cash_usd())
        facts["kis_connected"] = True
    except Exception:  # noqa: BLE001
        logger.warning("session readiness: cash read failed", exc_info=True)
        facts["kis_connected"] = False
        return facts
    try:
        positions = [p for p in (broker.get_positions() or [])
                     if int(getattr(p, "quantity", 0) or 0) != 0]
        facts["open_positions"] = len(positions)
    except Exception:  # noqa: BLE001
        logger.warning("session readiness: positions read failed", exc_info=True)
    try:
        facts["open_orders"] = len(broker.get_open_orders() or [])
    except Exception:  # noqa: BLE001
        logger.warning("session readiness: open orders read failed", exc_info=True)
    return facts


def build(*, session, env=None, now=None, broker=None, health_report=None,
          trading_day=None) -> Dict[str, Any]:
    """The readiness report for one session. Read-only."""
    env = dict(os.environ if env is None else env)
    current = _now(now)
    if health_report is None:
        import trading_health_check as thc

        health_report = thc.build_report(env, now=current)
    checks_by_name = {c["name"]: c for c in health_report.get("checks", [])}

    blocking: List[Dict[str, str]] = []
    warnings: List[str] = []
    for name in HARD_CHECKS:
        check = checks_by_name.get(name)
        if check is None:
            continue
        if check["verdict"] == "FAIL":
            blocking.append({"code": CHECK_CODES.get(name, name.upper()),
                             "detail": f"{name}: {check['detail']}"})
        elif check["verdict"] == "WARN":
            warnings.append(f"{name}: {check['detail']}")

    facts = _broker_facts(broker)
    if facts["kis_connected"] is False:
        blocking.append({"code": "KIS_API_FAILURE", "detail": "KIS 계좌 조회 실패"})

    capable = _session_capable(current, session)
    if capable is False:
        blocking.append({"code": "SESSION_NOT_ORDERABLE",
                         "detail": f"{session}: S6 주문 불가 세션"})
    elif capable is None:
        warnings.append(f"{session}: 세션 주문 가능 여부 확인 불가")

    live_enabled = _truthy(env.get("KIS_LIVE_ORDER_ENABLED"))
    if not live_enabled:
        blocking.append({"code": "LIVE_MODE_OFF", "detail": "KIS_LIVE_ORDER_ENABLED 미설정"})

    s6 = _s6_status(session)
    if s6.get("error"):
        blocking.append({"code": "S6_CONFIG_INVALID", "detail": s6["error"]})

    universe = _universe_size(env)
    if universe is None:
        warnings.append("universe 파일을 읽을 수 없음")
    ranking = _ranking_fresh(current)
    if ranking is False:
        warnings.append(f"ranking이 {RANKING_MAX_AGE_DAYS}일보다 오래됨")
    elif ranking is None:
        warnings.append("ranking 파일을 읽을 수 없음")

    if trading_day is None:
        try:
            from config import kis_market_schedule as sched

            trading_day = sched.describe(current).get("trading_day")
        except Exception:  # noqa: BLE001
            trading_day = current.date().isoformat()

    def _ok(name):
        check = checks_by_name.get(name)
        return None if check is None else check["verdict"] != "FAIL"

    return {
        "session": str(session),
        "trading_day": trading_day,
        "ready": not blocking,
        "blocking": blocking,
        "warnings": warnings,
        "checks": {
            "kis_connected": facts["kis_connected"] if facts["kis_connected"] is not None
            else _ok("kis_token"),
            "account_match": _ok("kis_account"),
            "collector": (_ok("collector:connected") is not False
                          and _ok("collector:subscriptions") is not False),
            "reconciliation": _ok("reconciliation"),
            "ranking": ranking,
        },
        "orderable_cash_usd": facts["orderable_cash_usd"],
        "open_positions": facts["open_positions"],
        "open_orders": facts["open_orders"],
        "subscriptions": _subscriptions(env),
        "universe_size": universe,
        "live_enabled": live_enabled,
        "release": (env.get("DEPLOYED_COMMIT") or "")[:8] or "-",
        "generated_at": current.isoformat(),
        "s6": s6,
    }


def _s6_status(session) -> Dict[str, Any]:
    """Which S6 range trades this session live, and which one shadows it.

    Read from the same config the scanner and the watch use. A config
    that cannot resolve a supported range is a BLOCKING fact: the
    session must not be called ready while S6's own requirements are
    unresolved.
    """
    try:
        from config import s6_sessions

        live = s6_sessions.orb_minutes_for(session)
        shadow = s6_sessions.shadow_orb_minutes_for(session)
        return {"live_orb_minutes": live,
                "live_variant": s6_sessions.scanner_variant_for(session),
                "fast_watch_active": str(session).upper() in s6_sessions.SCAN_SESSIONS,
                "shadow_orb_minutes": shadow,
                "shadow_variant": (s6_sessions.SHADOW_SCANNER_VARIANT
                                   if shadow is not None else None),
                "session_data": _session_data_state(session),
                "orders_allowed": bool(s6_sessions.orders_allowed(session))}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"S6 range config unresolved: {type(exc).__name__}: {exc}"}


#: The collector states that can still produce this session's opening
#: range. CONNECTED_NO_TRADES is one of them: at the open there is
#: legitimately nothing yet.
_DATA_READY_STATES = frozenset({"CONNECTED_ACTIVE", "CONNECTED_NO_TRADES", "LIVE"})


def _session_data_state(session) -> str:
    """Can this session's official opening range be collected at all?

    Asked at the session's start, so it deliberately does NOT require the
    range to exist yet -- five minutes of bars cannot have accumulated in
    the first minute. It asks the weaker, answerable question: is the
    collector delivering THIS session, and can its store be read. A
    session whose data source is absent must not be announced as ready.
    """
    try:
        from market_data import collector_status

        status = collector_status.describe()
        state = str(status.get("state") or "").upper()
        on_session = (str(status.get("market_session") or "").upper()
                      == str(session).upper())
        if state in _DATA_READY_STATES and on_session:
            return "OK"
        if not on_session:
            return f"COLLECTOR_ON_{status.get('market_session') or 'UNKNOWN'}"
        return state or "UNKNOWN"
    except Exception as exc:  # noqa: BLE001 - unknown is not OK
        return f"UNAVAILABLE_{type(exc).__name__}"


def announce(report, *, conn=None, send_fn=None) -> bool:
    """Send the readiness message once per (trading day, session)."""
    from operations import live_notifications as ln

    event = ln.MARKET_START if report.get("ready") else ln.SESSION_BLOCKED
    return ln.notify(event, dict(report), send_fn=send_fn, track_health=False,
                     dedupe_conn=conn, dedupe_subject=str(report.get("session")),
                     dedupe_version=f"{report.get('trading_day')}:{'READY' if report.get('ready') else 'BLOCKED'}")
