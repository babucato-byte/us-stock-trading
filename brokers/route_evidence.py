"""Live route evidence: a wire value confirmed by a real KIS response.

Why a store, and why this narrow
--------------------------------
`brokers/kis_broker.VERIFICATION_MATRIX` is static. Every value in it
that a real response confirmed got there by a reviewed edit quoting the
order that produced it -- the daytime SELL leg cites odno 0000001014
from 2026-08-27. That is the right shape for evidence that already
exists in history. It is the wrong shape for evidence that a one-shot
produces at 22:00 ET on a weeknight: the response arrives, the route is
proven, and the gate should open on that fact rather than on someone
remembering to edit a Python file afterwards.

So the one-shot RECORDS what KIS answered, here, and the matrix READS
it. Nothing else writes this file. There is no CLI, no operator command
and no test fixture that produces a record the matrix will honour --
`record()` refuses anything that is not an ACCEPTED response carrying
the broker's own order number, and `confirms()` refuses a record whose
wire value, session or source does not match the matrix entry it is
asked about.

What cannot verify a route
--------------------------
    a rejected response (rt_cd != "0")        refused at record()
    a response with no ODNO                    refused at record()
    a record naming a different TR or path     ignored at confirms()
    a record for a different session           ignored at confirms()
    a record from anything but the runner      ignored at confirms()
    a hand-written file at some other path     never read: the path is
                                               derived from the state
                                               directory, or explicit
    no state directory configured              nothing is read at all

The store sits beside the state database (`shared/state/`), keyed by
the matrix entry name, one record per name, replaced atomically.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Explicit override; otherwise derived from the state database's directory.
EVIDENCE_FILE_ENV = "ROUTE_EVIDENCE_FILE"
FILENAME = "route_evidence.json"

#: The only writer the matrix honours.
SOURCE_RUNNER = "ROUTE_VERIFICATION_RUNNER"

#: The session whose routes this store may verify.
SESSION = "OVERNIGHT_DAYTIME"

SCHEMA = "route_evidence_v1"


class RouteEvidenceRefused(Exception):
    """The response offered as evidence does not prove anything."""


def evidence_path(explicit=None) -> Optional[Path]:
    """Where the store lives, or None when no state directory is known.

    None is deliberate: a process with no state configuration (a test, a
    laptop shell) reads NO evidence and every pending route stays
    pending. A default under the repository would let a stray file
    verify a route on a machine that never placed an order.
    """
    if explicit is not None:
        return Path(explicit)
    override = os.environ.get(EVIDENCE_FILE_ENV)
    if override and override.strip():
        return Path(override)
    state_db = os.environ.get("STATE_STORE_DB_FILE") or os.environ.get("TRADING_STATE_DB")
    if state_db and state_db.strip():
        return Path(state_db).parent / FILENAME
    return None


def load(path=None) -> Dict[str, Dict[str, Any]]:
    """Every record, keyed by matrix entry name. Unreadable is empty."""
    target = evidence_path(path)
    if target is None or not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("route evidence unreadable at %s: %s", target, exc)
        return {}
    records = payload.get("records") if isinstance(payload, dict) else None
    return dict(records) if isinstance(records, dict) else {}


def _write(records: Dict[str, Dict[str, Any]], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": SCHEMA, "records": records}
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(target.parent), delete=False,
        prefix=f".{target.name}.")
    try:
        handle.write(json.dumps(payload, sort_keys=True, indent=1))
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, target)


def record(name, *, wire_value, broker_order_id, rt_cd, status,
           session=SESSION, msg_cd=None, msg1=None, http_status=None,
           run_id=None, internal_order_id=None, now=None, path=None,
           source=SOURCE_RUNNER) -> Dict[str, Any]:
    """Persist one real response as evidence for one matrix entry.

    Refuses -- raising rather than writing -- unless the response is an
    acceptance (`rt_cd == "0"`, status ACCEPTED or CANCELLED) that
    carries the broker's own order number. A rejection is a real
    response too, but it proves the route refused us, not that it
    works.
    """
    if str(rt_cd) != "0":
        raise RouteEvidenceRefused(
            f"{name}: rt_cd={rt_cd!r} is not an acceptance; not evidence")
    if str(status or "").upper() not in ("ACCEPTED", "CANCELLED"):
        raise RouteEvidenceRefused(
            f"{name}: status={status!r} is not ACCEPTED/CANCELLED; not evidence")
    if not broker_order_id or not str(broker_order_id).strip():
        raise RouteEvidenceRefused(
            f"{name}: no broker order number on the response; not evidence")
    if not wire_value or not str(wire_value).strip():
        raise RouteEvidenceRefused(f"{name}: no wire value named; not evidence")
    target = evidence_path(path)
    if target is None:
        raise RouteEvidenceRefused(
            f"{name}: no evidence store is configured "
            f"({EVIDENCE_FILE_ENV} / STATE_STORE_DB_FILE unset)")
    moment = now or datetime.now(timezone.utc)
    entry = {
        "name": str(name),
        "wire_value": str(wire_value),
        "session": str(session),
        "broker_order_id": str(broker_order_id),
        "rt_cd": str(rt_cd),
        "status": str(status).upper(),
        "msg_cd": msg_cd,
        "msg1": msg1,
        "http_status": http_status,
        "run_id": run_id,
        "internal_order_id": internal_order_id,
        "recorded_at": moment.isoformat(),
        "source": source,
    }
    records = load(target)
    records[str(name)] = entry
    _write(records, target)
    logger.warning("ROUTE_EVIDENCE_RECORDED name=%s wire_value=%s odno=%s rt_cd=%s "
                   "status=%s recorded_at=%s", name, wire_value, broker_order_id,
                   rt_cd, entry["status"], entry["recorded_at"])
    return entry


def confirms(name, *, expected_value, session=SESSION, path=None) -> bool:
    """Does a recorded live response confirm THIS matrix entry?

    The record must name the same wire value the matrix carries, the
    same session, the runner as its source, an acceptance with a broker
    order number, and a parseable time. Anything less is not evidence
    for this entry, whatever else it may be.
    """
    entry = load(path).get(str(name))
    if not isinstance(entry, dict):
        return False
    if entry.get("source") != SOURCE_RUNNER:
        return False
    if str(entry.get("wire_value") or "") != str(expected_value or ""):
        return False
    if str(entry.get("session") or "") != str(session):
        return False
    if str(entry.get("rt_cd")) != "0":
        return False
    if str(entry.get("status") or "").upper() not in ("ACCEPTED", "CANCELLED"):
        return False
    if not str(entry.get("broker_order_id") or "").strip():
        return False
    try:
        datetime.fromisoformat(str(entry.get("recorded_at")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return True


def describe(name, *, path=None) -> str:
    """The evidence string the matrix shows for a confirmed entry."""
    entry = load(path).get(str(name)) or {}
    return (f"LIVE {str(entry.get('recorded_at'))[:19]} {entry.get('session')}: "
            f"{name} -> {entry.get('wire_value')} answered rt_cd=0 "
            f"({entry.get('status')}) odno {entry.get('broker_order_id')}"
            + (f" msg_cd {entry.get('msg_cd')}" if entry.get("msg_cd") else "")
            + (f" run {entry.get('run_id')}" if entry.get("run_id") else ""))


def pending_items_after_live_evidence(posture, *, path=None):
    """The posture's pending wire values, minus those a recorded live
    acceptance confirms.

    This is what the route gate asks. `kis_broker.pending_items_for`
    stays a pure projection of the static matrix; the evidence is
    applied here, on top, and only for entries whose recorded wire
    value matches the matrix's own.
    """
    from brokers import kis_broker as kb

    values = {entry.name: entry.value for entry in kb.matrix_entries_for(posture)}
    return tuple(name for name in kb.pending_items_for(posture)
                 if not confirms(name, expected_value=values.get(name), path=path))


def confirmed_by_live_evidence(posture, *, path=None):
    """(name, evidence) for every pending matrix entry a record confirms."""
    from brokers import kis_broker as kb

    values = {entry.name: entry.value for entry in kb.matrix_entries_for(posture)}
    return tuple((name, describe(name, path=path))
                 for name in kb.pending_items_for(posture)
                 if confirms(name, expected_value=values.get(name), path=path))
