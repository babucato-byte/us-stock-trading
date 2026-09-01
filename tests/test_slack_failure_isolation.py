"""A Slack outage must not reach the order path.

Slack is observability. It cannot block a scan, refuse a READY, delay a
BUY or a SELL, move a position, trip the kill switch, or change what
reconciliation concluded. The dangerous shape is not a failed send -- it
is a send that RAISES, because an exception propagates into whatever was
being done at the time.

`slack_utils._send` deliberately does not catch `requests` errors: a
transport that hides its own failures cannot be monitored. The
containment is at the caller, and these tests pin it there.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import slack_utils  # noqa: E402
from operations import live_notifications  # noqa: E402


class _Exploding:
    """Every failure mode a webhook can present."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise self.exc


FAILURES = [
    ConnectionError("slack unreachable"),
    TimeoutError("slack timed out"),
    ValueError("malformed payload"),
    RuntimeError("unexpected"),
]


@pytest.mark.parametrize("failure", FAILURES, ids=lambda e: type(e).__name__)
def test_a_raising_webhook_never_propagates(failure, monkeypatch):
    # Patched on the slack_utils MODULE: live_notifications imports it
    # lazily inside the function, so a module-attribute patch would miss.
    boom = _Exploding(failure)
    monkeypatch.setattr(slack_utils, "send_kis_live_message", boom)
    monkeypatch.setattr(slack_utils, "send_kis_live_alert", boom)

    # Must not raise. The return value is deliberately unexamined: callers
    # use notify() for its side effect only.
    live_notifications.notify("BUY_SUBMITTED", {"symbol": "TEST", "qty": 1})


def test_a_broken_formatter_does_not_reach_trading(monkeypatch):
    """A bug in the message, not the transport."""
    class _Unformattable:
        def __repr__(self):
            raise RuntimeError("cannot render")

    live_notifications.notify("BUY_SUBMITTED", {"symbol": _Unformattable()})


def test_notify_is_never_used_as_a_condition():
    """If a caller branched on it, a Slack outage would change behaviour."""
    source = (REPO_ROOT / "operations" / "live_notifications.py").read_text()
    for forbidden in ("if notify(", "while notify(", "assert notify(",
                      "return notify("):
        assert forbidden not in source


def test_the_order_path_does_not_import_slack():
    """Execution must not be able to reach a webhook.

    Checked on IMPORTS, not on the word: `execution_engine` mentions
    Slack twice in comments -- "a Slack outage must not stop, delay or
    ..." -- which documents this very property. Matching the substring
    would have failed on its own documentation.
    """
    import ast

    for module in ("execution/execution_engine.py", "brokers/kis_broker.py"):
        tree = ast.parse((REPO_ROOT / module).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "slack" not in alias.name.lower(), module
            elif isinstance(node, ast.ImportFrom):
                assert "slack" not in (node.module or "").lower(), module


def test_the_transport_reports_failure_rather_than_hiding_it():
    """`_send` returning False on a non-200 is what makes an outage
    visible; swallowing inside the transport would make Slack look
    healthy while delivering nothing."""
    source = (REPO_ROOT / "slack_utils.py").read_text()
    assert "return False" in source
    assert "except Exception" not in source
