"""S-14: recovery messages for daily-health transitions.

S-02 (reconciliation CLEAN -> DIRTY) and S-03 (a held position's exit
evaluation failing repeatedly) each own their OWN recovery pairing,
right where they observe the condition -- see
`scripts/run_reconciliation.py` and `s6_live/exit_runtime.py`.

This module is for the other half of S-14: facts `trading_health_check`
already recomputes every run (collector heartbeat, the kill switch,
reconciliation's own daily snapshot) but never remembered between runs,
so a WARN/FAIL that cleared produced no message at all -- an operator
who saw the alert had no way to learn, from Slack, that it was over.

Never fatal. A tracking failure must not stop the health report itself,
and never re-raises past `notify_recoveries`.
"""

import json
import logging
import os
from pathlib import Path
from typing import Callable, Iterable, List, Mapping, Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "HEALTH_RECOVERY_STATE.json"

#: check name -> (Korean label, recovery sentence). Only checks a bad
#: verdict is worth telling an operator ENDED for, not every row in the
#: report.
TRACKED_CHECKS = {
    "collector:heartbeat": ("Collector 하트비트", "Collector 하트비트가 다시 정상적으로 갱신되고 있습니다."),
    "collector:connected": ("Collector 연결", "Collector가 KIS 실시간 피드에 다시 연결되었습니다."),
    "kill_switch": ("킬 스위치", "킬 스위치/HALT가 해제되어 신규 진입이 다시 허용됩니다."),
    "reconciliation": ("계좌 대조", "계좌 대조 상태가 다시 CLEAN으로 회복되었습니다."),
}

_BAD_VERDICTS = ("FAIL", "WARN")


def _resolve_state_path():
    override = os.environ.get("HEALTH_RECOVERY_STATE_FILE")
    return Path(override) if override else STATE_FILE


def _load(path=None) -> Mapping[str, str]:
    target = path or _resolve_state_path()
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 -- a corrupt state file is not a
        # reason to stop reporting; it is treated as "nothing tracked yet".
        return {}


def _save(state, path=None) -> None:
    target = path or _resolve_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def notify_recoveries(checks: Iterable[Mapping[str, str]], *,
                      send_fn: Optional[Callable[[str], object]] = None,
                      state_path=None) -> List[str]:
    """Compare `checks` (the health report's own `checks` list) against
    the previous run's verdicts and announce every TRACKED_CHECKS name
    that went bad -> OK. Returns the check names announced. Never raises.
    """
    try:
        previous = _load(state_path)
        current = dict(previous)
        recovered: List[str] = []
        for check in checks or ():
            name = check.get("name")
            if name not in TRACKED_CHECKS:
                continue
            verdict = check.get("verdict")
            was_bad = previous.get(name) in _BAD_VERDICTS
            current[name] = verdict
            if was_bad and verdict == "OK":
                recovered.append(name)
        _save(current, state_path)
    except Exception:  # noqa: BLE001 -- tracking must never break the report
        logger.warning("health recovery tracking failed", exc_info=True)
        return []

    if recovered and send_fn is None:
        try:
            import slack_utils

            send_fn = slack_utils.send_system_health_message
        except Exception:  # noqa: BLE001
            send_fn = None

    for name in recovered:
        label, sentence = TRACKED_CHECKS[name]
        message = f"✅ [시스템 회복] {label}\n\n{sentence}"
        if send_fn is None:
            logger.info("health recovery (unsent, no Slack sender): %s", name)
            continue
        try:
            send_fn(message)
        except Exception:  # noqa: BLE001 -- a Slack outage must not break reporting
            logger.warning("could not send health recovery message for %s", name,
                           exc_info=True)
    return recovered
