#!/usr/bin/env python3
"""The daily Slack health report for the LIVE KIS auto-trading system.

What it used to certify
-----------------------
"정상 운영 중 / 이상 없음" was produced by exactly one test: `git status`
was empty. The four scanner counters read CSVs from the script's own
directory, where the release keeps none, so every one printed 0 on
every day; and the performance block read an Alpaca PAPER file that the
release does not carry, so it said "페이퍼 트레이딩 성과 / 거래 기록 없음"
about a system that had closed 33 real KIS trades. The 2026-09-08 audit
listed eleven failure modes the message would have called normal.

What it certifies now
---------------------
Read-only facts, each with a verdict, and an overall that is NORMAL
only when nothing failed:

    Trading      LIVE from the env (KIS_ENV, EXECUTION_BROKER, the two
                 live flags); anything else is named, never assumed
    Release      HEAD == VALIDATED_COMMIT == DEPLOYED_COMMIT, clean tree
    Cron         the S6 scan, entry, exit, reconciliation, collector and
                 post-exit jobs present in the crontab
    Scanner      last run and last SUCCESS per profile and S6 session,
                 from the shared analytics manifests (the tree the release
                 writes), judged fresh against the last completed trading
                 day rather than the calendar day
    Daytime      which routes have live evidence (BUY / SELL / CANCEL)
    KIS          token cache validity; account allow-list match
    Reconciliation  clean, unknown_count, age
    Collector    running, on the deployed SHA, CONNECTED, subscriptions
    Kill switch  operations HALT, ENTRY_OFF, ENTRY_DISABLED
    Entry runner last tick age
    Errors       ERROR lines in the release cron logs in the last day
    Performance  LIVE, from TRADING_STATE.db: closed S6 trades, wins,
                 losses, realised P&L, open positions, BUY_NEVER_FILLED

Nothing here writes, refreshes market data, or calls an order endpoint.
The only network use is none: the token check reads the cache file.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

KST = ZoneInfo("Asia/Seoul")

OK = "OK"
FAIL = "FAIL"
WARN = "WARN"
INFO = "INFO"

#: The jobs the schedule must carry, by the script name in the crontab.
REQUIRED_CRON_JOBS = ("s6_scan.sh", "s6_buy_entry.sh", "s6_exit_monitor.sh",
                      "reconciliation.sh", "s6_realtime_collector.sh",
                      "post_exit_observations.sh")

RECONCILIATION_MAX_AGE_SECONDS = 15 * 60
ENTRY_TICK_MAX_AGE_SECONDS = 10 * 60
TOKEN_MIN_REMAINING_SECONDS = 300


class Check:
    def __init__(self, name: str, verdict: str, detail: str = ""):
        self.name, self.verdict, self.detail = name, verdict, detail

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "verdict": self.verdict, "detail": self.detail}


def _now(now=None) -> datetime:
    return now or datetime.now(timezone.utc)


def _parse(stamp) -> Optional[datetime]:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _env_true(env, name) -> bool:
    return str(env.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


def _run(cmd: List[str], cwd=None) -> str:
    try:
        return subprocess.check_output(cmd, cwd=cwd, text=True,
                                       stderr=subprocess.DEVNULL, timeout=20).strip()
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------
# facts
# ---------------------------------------------------------------------
def trading_mode(env) -> Check:
    live = (env.get("KIS_ENV") == "live" and env.get("EXECUTION_BROKER") == "kis"
            and _env_true(env, "KIS_LIVE_ORDER_ENABLED")
            and _env_true(env, "LIVE_ROLLOUT_ENABLED"))
    detail = (f"KIS_ENV={env.get('KIS_ENV')} broker={env.get('EXECUTION_BROKER')} "
              f"live_order={env.get('KIS_LIVE_ORDER_ENABLED')} "
              f"rollout={env.get('LIVE_ROLLOUT_ENABLED')}")
    return Check("trading_mode", OK if live else FAIL,
                 ("LIVE" if live else "NOT LIVE") + " (" + detail + ")")


def release_identity(env, base_dir=BASE_DIR) -> Check:
    head = _run(["git", "rev-parse", "HEAD"], cwd=str(base_dir)) or "unknown"
    deployed = str(env.get("DEPLOYED_COMMIT", "") or "")
    validated = str(env.get("VALIDATED_COMMIT", "") or "")
    dirty = _run(["git", "status", "--porcelain"], cwd=str(base_dir))
    if head != "unknown" and head == deployed == validated and not dirty:
        return Check("release", OK, f"{head[:12]} == VALIDATED == DEPLOYED, clean")
    return Check("release", FAIL,
                 f"HEAD={head[:12]} VALIDATED={validated[:12]} DEPLOYED={deployed[:12]}"
                 + (f", dirty {len(dirty.splitlines())}" if dirty else ""))


def cron_jobs(crontab_text: Optional[str] = None) -> Check:
    text = crontab_text if crontab_text is not None else _run(["crontab", "-l"])
    active = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    missing = [job for job in REQUIRED_CRON_JOBS if not any(job in line for line in active)]
    if missing:
        return Check("cron", FAIL, "missing: " + ", ".join(missing))
    return Check("cron", OK, f"{len(REQUIRED_CRON_JOBS)} required jobs present")


def _analytics_dir(env) -> Path:
    override = env.get("SCANNER_ANALYTICS_DIR")
    if override:
        return Path(override)
    root = env.get("SCANNER_DATA_ROOT") or "/home/ubuntu/releases/us-stock-trading/shared/scanner"
    return Path(root) / "logs" / "scanners"


def _last_trading_day(now: datetime) -> Optional[str]:
    try:
        from scanners.base.trading_calendar import previous_trading_day, us_trading_day

        today = us_trading_day(now)
        try:
            from market_guard import is_us_trading_day

            if is_us_trading_day(now):
                return str(today)
        except Exception:  # noqa: BLE001
            pass
        return str(previous_trading_day(today))
    except Exception:  # noqa: BLE001
        return None


def scanner_runs(env, now: datetime, analytics_dir: Optional[Path] = None) -> List[Check]:
    """Last run and last SUCCESS from the manifests, per profile and per
    S6 session, on the last completed trading day."""
    directory = analytics_dir or _analytics_dir(env)
    day = _last_trading_day(now)
    checks: List[Check] = []
    if not day:
        return [Check("scanner", FAIL, "trading day could not be resolved")]
    path = directory / "runs" / f"{day}.jsonl"
    if not path.exists():
        return [Check("scanner", FAIL, f"no run manifests for {day} at {path}")]
    runs = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            runs.append(json.loads(line))
        except ValueError:
            continue
    by_key: Dict[str, Dict[str, Any]] = {}
    for run in runs:
        key = run.get("profile") or ("S6 " + str(run.get("session") or
                                            _session_from_run_id(run.get("run_id"))))
        bucket = by_key.setdefault(key, {"runs": 0, "success": 0, "last": None,
                                         "last_success": None, "statuses": {}})
        bucket["runs"] += 1
        status = str(run.get("run_status") or "UNKNOWN")
        bucket["statuses"][status] = bucket["statuses"].get(status, 0) + 1
        stamp = run.get("started_at") or run.get("recorded_at")
        if stamp and (bucket["last"] is None or str(stamp) > str(bucket["last"])):
            bucket["last"] = stamp
        if status == "SUCCESS":
            bucket["success"] += 1
            if bucket["last_success"] is None or str(stamp) > str(bucket["last_success"]):
                bucket["last_success"] = stamp
    if not by_key:
        return [Check("scanner", FAIL, f"manifests for {day} are empty")]
    for key in sorted(by_key):
        bucket = by_key[key]
        verdict = OK if bucket["success"] else FAIL
        checks.append(Check(f"scanner:{key}", verdict,
                            f"{day}: {bucket['runs']} run(s), {bucket['success']} SUCCESS, "
                            f"last {str(bucket['last'])[:16]}, "
                            f"last success {str(bucket['last_success'])[:16]}, "
                            f"statuses {bucket['statuses']}"))
    total_signals = _count_lines(directory / "signals" / f"{day}.jsonl")
    checks.append(Check("scanner:signals", OK if total_signals is not None else WARN,
                        f"{day}: {total_signals} stored signals"))
    return checks


def _session_from_run_id(run_id) -> str:
    text = str(run_id or "")
    for name in ("OVERNIGHT_DAYTIME", "PREMARKET", "AFTER_HOURS", "REGULAR"):
        if name in text:
            return name
    return "adhoc"


def _count_lines(path: Path) -> Optional[int]:
    if not path.exists():
        return None
    return sum(1 for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
               if line.strip())


def daytime_routes() -> List[Check]:
    checks: List[Check] = []
    try:
        from brokers import kis_broker as kb
        from brokers import route_evidence
        from config import session_capability as sc

        pending = set(route_evidence.pending_items_after_live_evidence(kb.REQUIRED_FOR_DAYTIME))
        buy = "daytime_order_tr_id_live_buy" not in pending
        sell = "daytime_order_tr_id_live_sell" not in pending
        cancel = not ({"daytime_cancel_path", "daytime_cancel_tr_id_live"} & pending)
        checks.append(Check("daytime:buy", OK if buy else WARN,
                            "VERIFIED" if buy else "LIVE_RESPONSE_PENDING"))
        checks.append(Check("daytime:sell", OK if sell else WARN,
                            "VERIFIED" if sell else "LIVE_RESPONSE_PENDING"))
        checks.append(Check("daytime:cancel", OK if cancel else WARN,
                            "VERIFIED" if cancel else "LIVE_RESPONSE_PENDING"))
        for session in ("PREMARKET", "REGULAR", "AFTER_HOURS"):
            awaiting = sc.route_awaiting_live_evidence(session)
            checks.append(Check(f"route:{session}", FAIL if awaiting else OK,
                                "ROUTE_UNVERIFIED" if awaiting else "VERIFIED"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("daytime", FAIL, f"route state unreadable: {type(exc).__name__}"))
    return checks


def kis_token(env, now: datetime) -> Check:
    path = env.get("KIS_TOKEN_CACHE_FILE")
    if not path or not Path(path).exists():
        return Check("kis_token", WARN, "no token cache (the broker issues one on first use)")
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        remaining = float(payload.get("expires_at")) - now.timestamp()
    except Exception as exc:  # noqa: BLE001
        return Check("kis_token", WARN, f"token cache unreadable: {type(exc).__name__}")
    if remaining >= TOKEN_MIN_REMAINING_SECONDS:
        return Check("kis_token", OK, f"valid, {remaining / 60:.0f} min remaining")
    return Check("kis_token", WARN, f"expiring ({remaining:.0f}s); re-issued on first use")


def account_match(env) -> Check:
    acct = str(env.get("KIS_ACCOUNT_NO", "") or "")
    allowed = str(env.get("KIS_ALLOWED_ACCOUNT_NO", "") or "")
    if acct and acct == allowed:
        return Check("kis_account", OK, "KIS_ACCOUNT_NO == KIS_ALLOWED_ACCOUNT_NO")
    return Check("kis_account", FAIL, "account does not match the allow-list")


def reconciliation(env, now: datetime) -> Check:
    path = env.get("RECONCILIATION_STATE_FILE")
    if not path or not Path(path).exists():
        return Check("reconciliation", FAIL, "no reconciliation state file")
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return Check("reconciliation", FAIL, f"state unreadable: {type(exc).__name__}")
    checked = _parse(payload.get("checked_at"))
    age = (now - checked).total_seconds() if checked else None
    unknown = int(payload.get("unknown_count") or 0)
    if not payload.get("clean") or unknown or payload.get("halt"):
        return Check("reconciliation", FAIL,
                     f"clean={payload.get('clean')} unknown={unknown} "
                     f"mismatch={payload.get('mismatch_count')} halt={payload.get('halt')}")
    if age is None or age > RECONCILIATION_MAX_AGE_SECONDS:
        return Check("reconciliation", WARN, f"clean but stale ({age})")
    return Check("reconciliation", OK, f"CLEAN, unknown_count=0, {age:.0f}s old")


def collector(env, deployed_sha: str, ps_text: Optional[str] = None) -> List[Check]:
    checks: List[Check] = []
    root = env.get("SCANNER_DATA_ROOT") or "/home/ubuntu/releases/us-stock-trading/shared/scanner"
    status_path = Path(root) / "realtime_bars" / "collector_status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
        connected = status.get("connection_state") == "CONNECTED"
        req, got = status.get("subscription_requested"), status.get("subscription_count")
        checks.append(Check("collector:connected", OK if connected else FAIL,
                            f"{status.get('state')} subscriptions {got}/{req}"))
        if req and got != req:
            checks.append(Check("collector:subscriptions", FAIL, f"{got}/{req}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("collector:connected", FAIL, f"status unreadable: {type(exc).__name__}"))
    text = ps_text if ps_text is not None else _run(["ps", "-eo", "args"])
    procs = [line for line in text.splitlines() if "run_realtime_bar_collector" in line]
    if len(procs) != 1:
        checks.append(Check("collector:process", FAIL, f"{len(procs)} collector process(es)"))
    elif deployed_sha and deployed_sha not in procs[0]:
        checks.append(Check("collector:process", WARN, "running an older release (restarts hourly)"))
    else:
        checks.append(Check("collector:process", OK, "one process on the deployed release"))
    return checks


def kill_switches(env) -> Check:
    problems = []
    if _env_true(env, "ENTRY_DISABLED"):
        problems.append("ENTRY_DISABLED=true")
    try:
        from operations import kill_switch as ops

        if ops.is_halted():
            problems.append("operations HALT")
        if not ops.is_entry_allowed():
            problems.append("ENTRY_OFF")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"kill switch unreadable: {type(exc).__name__}")
    return Check("kill_switch", FAIL if problems else OK,
                 ", ".join(problems) if problems else "OFF (entries permitted)")


def entry_runner(env, now: datetime) -> Check:
    root = env.get("SCANNER_DATA_ROOT") or "/home/ubuntu/releases/us-stock-trading/shared/scanner"
    path = Path(root) / "logs" / "cron" / "s6_buy_entry.log"
    if not path.exists():
        return Check("entry_runner", FAIL, "no entry log")
    stamp = _last_stamp(path)
    if stamp is None:
        return Check("entry_runner", FAIL, "no timestamped tick in the entry log")
    age = (now - stamp).total_seconds()
    weekday = now.astimezone(ZoneInfo("America/New_York")).weekday() < 5
    if age > ENTRY_TICK_MAX_AGE_SECONDS and weekday:
        return Check("entry_runner", FAIL, f"last tick {age / 60:.0f} min ago")
    return Check("entry_runner", OK, f"last tick {age / 60:.0f} min ago")


_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})")


def _last_stamp(path: Path) -> Optional[datetime]:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 65536))
            tail = handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        match = _STAMP.match(line)
        if match:
            return _parse(match.group(1).replace(" ", "T"))
    return None


def recent_errors(env, now: datetime) -> Check:
    root = env.get("SCANNER_DATA_ROOT") or "/home/ubuntu/releases/us-stock-trading/shared/scanner"
    directory = Path(root) / "logs" / "cron"
    cutoff = now - timedelta(days=1)
    count, samples = 0, []
    for path in sorted(directory.glob("*.log")) if directory.exists() else []:
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 512 * 1024))
                tail = handle.read().decode("utf-8", errors="ignore")
        except OSError:
            continue
        for line in tail.splitlines():
            if " ERROR " not in line and "Traceback" not in line:
                continue
            match = _STAMP.match(line)
            stamp = _parse(match.group(1).replace(" ", "T")) if match else None
            if stamp and stamp < cutoff:
                continue
            count += 1
            if len(samples) < 3:
                samples.append(f"{path.name}: {line[:100]}")
    verdict = OK if count == 0 else WARN
    return Check("recent_errors", verdict, f"{count} ERROR line(s) in the last day"
                 + ("; " + " | ".join(samples) if samples else ""))


def live_performance(db_path: Optional[str]) -> Dict[str, Any]:
    """LIVE figures from TRADING_STATE.db, read-only. Never estimates."""
    out: Dict[str, Any] = {"source": "TRADING_STATE.db", "available": False}
    if not db_path or not Path(db_path).exists():
        out["detail"] = "state database not found"
        return out
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT entry_price, exit_price, quantity FROM s6_positions "
            "WHERE status = 'CLOSED' AND exit_price IS NOT NULL AND entry_price IS NOT NULL "
            "AND quantity IS NOT NULL").fetchall()
        wins = losses = 0
        pnl = 0.0
        for row in rows:
            delta = (float(row["exit_price"]) - float(row["entry_price"])) * int(row["quantity"])
            pnl += delta
            if delta > 0:
                wins += 1
            elif delta < 0:
                losses += 1
        out.update({
            "available": True,
            "closed_trades": len(rows), "wins": wins, "losses": losses,
            "realized_pnl_usd": round(pnl, 2),
            "open_positions": conn.execute(
                "SELECT COUNT(*) FROM s6_positions WHERE status != 'CLOSED'").fetchone()[0],
            "buy_never_filled": conn.execute(
                "SELECT COUNT(*) FROM s6_positions WHERE exit_reason LIKE 'BUY_%'").fetchone()[0],
            "unconfirmed_exits": conn.execute(
                "SELECT COUNT(*) FROM s6_positions WHERE status = 'CLOSED' AND exit_price IS NULL "
                "AND exit_reason NOT LIKE 'BUY_%'").fetchone()[0],
        })
        conn.close()
    except Exception as exc:  # noqa: BLE001
        out["detail"] = f"{type(exc).__name__}: {exc}"
    return out


# ---------------------------------------------------------------------
# report
# ---------------------------------------------------------------------
def build_report(env=None, *, now=None, analytics_dir=None, crontab_text=None,
                 ps_text=None, base_dir=BASE_DIR) -> Dict[str, Any]:
    env = dict(os.environ if env is None else env)
    current = _now(now)
    deployed = str(env.get("DEPLOYED_COMMIT", "") or "")
    checks: List[Check] = [trading_mode(env), release_identity(env, base_dir),
                           cron_jobs(crontab_text)]
    checks += scanner_runs(env, current, analytics_dir)
    checks += daytime_routes()
    checks += [kis_token(env, current), account_match(env), reconciliation(env, current)]
    checks += collector(env, deployed, ps_text)
    checks += [kill_switches(env), entry_runner(env, current), recent_errors(env, current)]
    failed = [c.name for c in checks if c.verdict == FAIL]
    warned = [c.name for c in checks if c.verdict == WARN]
    try:
        from market_guard import is_us_trading_day

        market_day = bool(is_us_trading_day(current))
    except Exception:  # noqa: BLE001
        market_day = None
    return {
        "now_kst": current.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        "market_day": market_day,
        "checks": [c.as_dict() for c in checks],
        "failed": failed, "warned": warned,
        "overall": "NORMAL" if not failed else "ATTENTION",
        "performance": live_performance(env.get("STATE_STORE_DB_FILE") or env.get("TRADING_STATE_DB")),
    }


def _find(report, name) -> Dict[str, Any]:
    for check in report["checks"]:
        if check["name"] == name:
            return check
    return {"verdict": "n/a", "detail": ""}


#: Verdict codes stay as they are (OK/WARN/FAIL are what the logs say);
#: the word beside them is what the operator reads.
VERDICT_WORDS = {OK: "정상", WARN: "주의", FAIL: "실패", "n/a": "미측정"}

#: Check name -> Korean label for the status lines.
CHECK_LABELS = (("kis_token", "KIS 토큰"), ("kis_account", "KIS 계좌"),
                ("reconciliation", "계좌 대조"),
                ("collector:connected", "Collector 연결"),
                ("collector:subscriptions", "Collector 구독"),
                ("collector:process", "Collector 프로세스"),
                ("kill_switch", "킬 스위치"), ("entry_runner", "진입 러너"),
                ("cron", "예약 작업"), ("recent_errors", "최근 오류"))


def _verdict(item) -> str:
    verdict = item.get("verdict", "n/a")
    return f"{VERDICT_WORDS.get(verdict, verdict)} ({verdict})"


def format_message(report: Dict[str, Any]) -> str:
    market_day = report["market_day"]
    lines = ["📊 [시스템 상태 점검] 미국주식 자동매매", "", f"점검시각: {report['now_kst']}",
             f"미국 증시 개장일: {'예' if market_day else '아니오' if market_day is not None else '불명'}", ""]
    mode = _find(report, "trading_mode")
    lines += ["거래 모드:", f"  {mode['detail'].split(' (')[0]}", ""]
    rel = _find(report, "release")
    lines += ["배포 버전:", f"  {_verdict(rel)} {rel['detail']}", ""]
    lines.append("데이장 주문 경로:")
    for leg, word in (("buy", "매수"), ("sell", "매도"), ("cancel", "취소")):
        item = _find(report, f"daytime:{leg}")
        lines.append(f"  {word}: {item['detail']}")
    lines.append("")
    lines.append("스캐너:")
    for check in report["checks"]:
        if check["name"].startswith("scanner"):
            lines.append(f"  {_verdict(check)} {check['name'].split(':', 1)[-1]}: {check['detail']}")
    lines.append("")
    for name, label in CHECK_LABELS:
        item = _find(report, name)
        if item.get("verdict") == "n/a":
            continue
        lines.append(f"{label}: {_verdict(item)} {item['detail']}")
    lines.append("")
    perf = report.get("performance") or {}
    lines.append("📈 실거래 성과 (S6, 위치: TRADING_STATE.db)")
    if perf.get("available"):
        lines += [f"  청산 거래: {perf['closed_trades']}건 (승 {perf['wins']} / 패 {perf['losses']})",
                  f"  실현 손익: {perf['realized_pnl_usd']:.2f} USD",
                  f"  보유 포지션: {perf['open_positions']}개",
                  f"  미체결 취소 (BUY_NEVER_FILLED): {perf['buy_never_filled']}건",
                  f"  청산가 미확인: {perf['unconfirmed_exits']}건"]
    else:
        lines.append(f"  성과 조회 불가: {perf.get('detail')}")
    lines.append("")
    overall = report["overall"]
    lines.append(f"종합: {'정상' if overall == 'NORMAL' else '확인 필요'} ({overall})")
    if report["failed"]:
        lines.append("  실패 항목: " + ", ".join(report["failed"]))
    if report["warned"]:
        lines.append("  주의 항목: " + ", ".join(report["warned"]))
    return "\n".join(lines)


def main() -> int:
    from slack_utils import send_system_health_message

    report = build_report()
    message = format_message(report)
    print(message)
    if not send_system_health_message(message):
        print("SYSTEM_HEALTH_NOTIFICATION_UNCONFIGURED_OR_FAILED")
    return 0 if report["overall"] == "NORMAL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
