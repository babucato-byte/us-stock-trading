"""Derived S6 discovery-to-fill latency, without inventing missing stages."""

from datetime import datetime, timezone


def _time(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _seconds(row, start, end):
    left, right = _time(row.get(start)), _time(row.get(end))
    return (right - left).total_seconds() if left is not None and right is not None else None


def derive(row):
    """Return only measurements supported by both endpoint timestamps."""
    data = dict(row or {})
    return {
        "discovery_latency_seconds": _seconds(
            data, "full_scan_started_at", "candidate_discovered_at"),
        "publication_latency_seconds": _seconds(
            data, "candidate_discovered_at", "candidate_published_at"),
        "consumer_latency_seconds": _seconds(
            data, "candidate_published_at", "source_consumed_at"),
        "precision_watch_latency_seconds": _seconds(
            data, "precision_watch_started_at", "ready_at"),
        "execution_latency_seconds": _seconds(
            data, "execution_gate_at", "broker_submit_at"),
        "total_breakout_to_submit_seconds": _seconds(
            data, "candidate_discovered_at", "broker_submit_at"),
        "broker_fill_latency_seconds": _seconds(
            data, "broker_submit_at", "fill_at"),
    }
