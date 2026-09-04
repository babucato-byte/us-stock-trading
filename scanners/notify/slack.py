"""Failure-only Slack for the scanner runs (spec section 14, track B-1).

What gets sent, and what deliberately does not
----------------------------------------------
Only a run that could not do its job:

    FAILED / FAILED_PROVIDER / FAILED_NO_UNIVERSE / FAILED_NO_SCANNER
    PARTIAL, but only when a circuit breaker actually tripped
    a report/performance CLI that exited non-zero

Everything else is silence. In particular SUCCESS with zero candidates
is silence: a quiet market is the single most common outcome of a
scanning day, and an alert for it would train the reader to ignore the
channel, which is the only failure mode that makes every other alert
here worthless. `SKIPPED_MARKET_CLOSED` is silence for the same reason
-- a holiday is a correct no-op, not an incident.

A plain PARTIAL is also silence. One scanner of six failing on a
handful of symbols is visible in the manifest and in the weekly report;
the circuit breaker is the signal that a scanner is broken rather than
merely unlucky, and that is the line worth waking someone for.

No candidate data
-----------------
The message carries counts, statuses and reason codes. It never carries
a symbol or a score. Section 12's DO_NOT_ADD applies to the alert
channel too: a channel that occasionally prints tickers becomes a
channel people trade from, and month 1 is an observation period.

Best-effort, structurally
-------------------------
`notify_run()` and `notify_cli_failure()` cannot raise. Every call is
wrapped, `slack_utils` is imported lazily inside the sender (so a
missing `requests` cannot break `import scanners.runner`), and the
return value is a bool nobody is required to check. The scanner's exit
code is computed from the run, before this is ever called.

De-duplication
--------------
A run is identified by `(kind, profile, trading_day)`. A retried cron
job, a manual re-run after a failure, or two UTC firings of the same
ET-guarded entry must produce one alert, not three. State is a small
JSON file per trading day under the analytics store, so it rotates with
the data it describes and needs no cleanup job.

The de-dup state is advisory: if it cannot be read or written, the
alert is still sent. Losing a duplicate suppression is a nuisance;
losing the alert entirely because a state file was unwritable is the
failure this module exists to prevent.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from scanners.base import result_store, run_context

logger = logging.getLogger(__name__)

#: Master off switch for every scanner-originated Slack message. Unset
#: means enabled. Set it to a false-y value to silence the scanner
#: notifications WITHOUT touching any existing trading alert path.
ENABLED_ENV = "SCANNER_SLACK_ENABLED"

NOTIFY_SUBDIR = "notify"

_FALSE_VALUES = {"0", "false", "no", "n", "off"}

KIND_RUN = "run"
KIND_CLI = "cli"


def is_enabled(env=None) -> bool:
    mapping = os.environ if env is None else env
    raw = mapping.get(ENABLED_ENV)
    if raw is None:
        return True
    return str(raw).strip().lower() not in _FALSE_VALUES


def _state_path(trading_day: str) -> Path:
    directory = result_store.analytics_dir() / NOTIFY_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{trading_day}.json"


def _already_sent(key: str, trading_day: str) -> bool:
    """True only when we are CERTAIN this alert already went out.

    An unreadable or malformed state file returns False -- see the
    module docstring on why suppression fails open.
    """
    try:
        path = _state_path(trading_day)
        if not path.exists():
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
        return key in set(payload.get("sent") or [])
    except Exception:  # noqa: BLE001 - advisory state, never fatal
        logger.debug("scanner notify: could not read de-dup state", exc_info=True)
        return False


def _mark_sent(key: str, trading_day: str) -> None:
    try:
        path = _state_path(trading_day)
        sent = []
        if path.exists():
            try:
                sent = list(json.loads(path.read_text(encoding="utf-8")).get("sent") or [])
            except ValueError:
                sent = []
        if key not in sent:
            sent.append(key)
        path.write_text(
            json.dumps({"trading_day": trading_day, "sent": sent}, indent=2,
                       sort_keys=True),
            encoding="utf-8")
    except Exception:  # noqa: BLE001
        logger.debug("scanner notify: could not record de-dup state", exc_info=True)


def _send(message: str) -> bool:
    """The only outbound call. Imported lazily and never allowed to raise."""
    try:
        from slack_utils import send_scanner_monitor_message

        return bool(send_scanner_monitor_message(message))
    except Exception:  # noqa: BLE001 - a Slack outage is not a scan failure
        logger.warning("scanner notify: Slack send failed", exc_info=True)
        return False


def should_alert(report) -> bool:
    """The alerting predicate, separated so it is testable without Slack."""
    status = getattr(report, "status", None)
    if run_context.is_failure(status):
        return True
    if status == run_context.PARTIAL:
        return bool(getattr(report, "circuit_breaker_triggered", False))
    return False


def _failed_scanner_names(report):
    names = []
    for outcome in getattr(report, "outcomes", None) or []:
        if getattr(outcome, "failed", False) or getattr(
                outcome, "circuit_breaker_triggered", False):
            names.append(str(getattr(outcome, "scanner_name", "?")))
    for name in (getattr(report, "construction_failures", None) or {}):
        names.append(f"{name} (not built)")
    return names


def format_run_alert(report) -> str:
    """Counts, statuses and reason codes. No symbol, no score."""
    profile = getattr(report, "profile", None) or "(explicit scanner list)"
    lines = [
        f"*Scanner 실행 실패* — {report.status}",
        f"profile: {profile}",
        f"trading day: {getattr(report, 'trading_day', '?')}",
        f"run id: {getattr(report, 'run_id', '?')}",
        f"provider: {getattr(report, 'provider', '?')}",
        f"universe: {getattr(report, 'universe_size', '?')} symbols",
        f"provider errors: {getattr(report, 'fetch_failures', '?')}",
    ]
    if getattr(report, "circuit_breaker_triggered", False):
        peak = getattr(report, "consecutive_error_peak", "?")
        lines.append(f"circuit breaker: TRIGGERED (연속 실패 최대 {peak})")
    failed = _failed_scanner_names(report)
    if failed:
        lines.append(f"failed scanners: {', '.join(failed)}")
    if getattr(report, "skipped_reason", None):
        lines.append(f"skipped reason: {report.skipped_reason}")
    lines.append("주문 경로 영향 없음 · Candidate Decision: disabled")
    return "\n".join(lines)


def format_cli_alert(command: str, exit_code: int, trading_day: str,
                     detail: Optional[str] = None) -> str:
    lines = [
        f"*Scanner CLI 실패* — exit {exit_code}",
        f"command: {command}",
        f"trading day: {trading_day}",
    ]
    if exit_code == 2:
        lines.append("exit 2 = 잘못된 호출(날짜 형식/인자). cron 정의를 확인.")
    if detail:
        lines.append(f"detail: {detail}")
    lines.append("주문 경로 영향 없음 · Candidate Decision: disabled")
    return "\n".join(lines)


def notify_run(report, *, sender=None, env=None) -> bool:
    """Alert if this run failed. Returns whether a message was sent.

    Never raises. The caller's exit code has already been decided by the
    time this runs, and nothing here can change it.
    """
    try:
        if not is_enabled(env):
            return False
        if not should_alert(report):
            return False
        trading_day = str(getattr(report, "trading_day", "") or "unknown")
        profile = str(getattr(report, "profile", "") or "adhoc")
        key = f"{KIND_RUN}:{profile}:{trading_day}:{report.status}"
        if _already_sent(key, trading_day):
            logger.info("scanner notify: already alerted for %s", key)
            return False
        send = sender or _send
        if not send(format_run_alert(report)):
            return False
        _mark_sent(key, trading_day)
        return True
    except Exception:  # noqa: BLE001 - notification must never fail a run
        logger.warning("scanner notify: run alert failed", exc_info=True)
        return False


def notify_cli_failure(command: str, exit_code: int, *, trading_day=None,
                       detail=None, sender=None, env=None) -> bool:
    """Alert for a report/performance CLI that exited 1 or 2."""
    try:
        if not is_enabled(env):
            return False
        if not exit_code:
            return False
        day = str(trading_day or datetime.now(timezone.utc).date().isoformat())
        key = f"{KIND_CLI}:{command}:{day}:{exit_code}"
        if _already_sent(key, day):
            logger.info("scanner notify: already alerted for %s", key)
            return False
        send = sender or _send
        if not send(format_cli_alert(command, exit_code, day, detail)):
            return False
        _mark_sent(key, day)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("scanner notify: CLI alert failed", exc_info=True)
        return False


def send_report(message: str, *, sender=None, env=None) -> bool:
    """A scheduled, non-failure summary -> the REPORT channel.

    Separate from the alert path on purpose: a weekly summary landing in
    the incident channel would dilute it, and an incident landing in the
    report channel would be missed. Also best-effort.
    """
    try:
        if not is_enabled(env):
            return False
        if sender is not None:
            return bool(sender(message))
        from slack_utils import send_slack_message

        return bool(send_slack_message(message))
    except Exception:  # noqa: BLE001
        logger.warning("scanner notify: report send failed", exc_info=True)
        return False


def state_snapshot(trading_day: str) -> Dict[str, Any]:
    """What has already been alerted for a day. For tests and operators."""
    try:
        path = _state_path(trading_day)
        if not path.exists():
            return {"trading_day": trading_day, "sent": []}
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"trading_day": trading_day, "sent": []}
