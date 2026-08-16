"""KIS live Slack is a separate pair of webhooks with no fallback.

The hazard being closed has two halves.

Volume: `SLACK_WEBHOOK_URL` / `SLACK_ALERT_WEBHOOK_URL` carry Alpaca
paper fills and scanner output. A real-money ORDER_UNKNOWN arriving in
that stream is an alert nobody reads.

Honesty: if the KIS live webhooks were unset and the code fell back to
the Alpaca pair, a first live order would place, notify "successfully",
and leave the operator watching the wrong channel. So an unset webhook
is a readiness BLOCKER (KIS_LIVE_NOTIFICATION_NOT_CONFIGURED) and, if
reached anyway, `slack_utils` refuses to send rather than rerouting.

What must NOT change is the Alpaca side: `send_slack_message` and
`send_slack_alert` keep reading exactly the variables they always did.
TestAlpacaRoutingIsUntouched is the regression guard for that.
"""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import slack_utils  # noqa: E402
from operations import live_notifications  # noqa: E402

KIS_GENERAL = "https://hooks.slack.test/kis-live-general"
KIS_ALERT = "https://hooks.slack.test/kis-live-alert"
ALPACA_GENERAL = "https://hooks.slack.test/alpaca-general"
ALPACA_ALERT = "https://hooks.slack.test/alpaca-alert"


@pytest.fixture
def captured(monkeypatch):
    """Replace the single transport `_send` so nothing leaves the process
    and the URL each sender chose is observable."""
    sent = []
    monkeypatch.setattr(slack_utils, "_send",
                        lambda url, message: sent.append((url, message)) or True)
    return sent


@pytest.fixture
def kis_configured(monkeypatch):
    monkeypatch.setenv("KIS_LIVE_SLACK_WEBHOOK_URL", KIS_GENERAL)
    monkeypatch.setenv("KIS_LIVE_SLACK_ALERT_WEBHOOK_URL", KIS_ALERT)


class TestKISLiveRouting:
    def test_a_routine_event_goes_to_the_kis_live_general_webhook(
            self, captured, kis_configured):
        live_notifications.notify(live_notifications.ORDER_SUBMITTED, {"symbol": "AAPL"})
        assert [url for url, _ in captured] == [KIS_GENERAL]

    def test_an_urgent_event_goes_to_the_kis_live_alert_webhook(
            self, captured, kis_configured):
        live_notifications.notify(live_notifications.ORDER_UNKNOWN, {"symbol": "AAPL"})
        assert [url for url, _ in captured] == [KIS_ALERT]

    def test_the_two_streams_stay_separate(self, captured, kis_configured):
        live_notifications.notify(live_notifications.ORDER_SUBMITTED, {"symbol": "AAPL"})
        live_notifications.notify(live_notifications.HALT_ACTIVATED, {"reason": "x"})
        assert [url for url, _ in captured] == [KIS_GENERAL, KIS_ALERT]


class TestThereIsNoFallback:
    def test_an_unset_kis_webhook_does_not_borrow_the_alpaca_one(
            self, captured, monkeypatch):
        monkeypatch.delenv("KIS_LIVE_SLACK_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("KIS_LIVE_SLACK_ALERT_WEBHOOK_URL", raising=False)
        monkeypatch.setattr(slack_utils, "SLACK_WEBHOOK_URL", ALPACA_GENERAL)
        monkeypatch.setattr(slack_utils, "SLACK_ALERT_WEBHOOK_URL", ALPACA_ALERT)

        live_notifications.notify(live_notifications.ORDER_SUBMITTED, {"symbol": "AAPL"})
        live_notifications.notify(live_notifications.ORDER_UNKNOWN, {"symbol": "AAPL"})

        chosen = [url for url, _ in captured]
        assert ALPACA_GENERAL not in chosen
        assert ALPACA_ALERT not in chosen
        assert all(not url for url in chosen), chosen

    def test_a_blank_webhook_counts_as_unset(self, monkeypatch):
        monkeypatch.setenv("KIS_LIVE_SLACK_WEBHOOK_URL", "   ")
        monkeypatch.setenv("KIS_LIVE_SLACK_ALERT_WEBHOOK_URL", KIS_ALERT)
        assert slack_utils.kis_live_notifications_configured() is False

    def test_one_of_two_is_not_configured(self, monkeypatch):
        """Both, not either: a half-configured deployment silently drops
        half the lifecycle."""
        monkeypatch.setenv("KIS_LIVE_SLACK_WEBHOOK_URL", KIS_GENERAL)
        monkeypatch.delenv("KIS_LIVE_SLACK_ALERT_WEBHOOK_URL", raising=False)
        assert slack_utils.kis_live_notifications_configured() is False

        monkeypatch.delenv("KIS_LIVE_SLACK_WEBHOOK_URL", raising=False)
        monkeypatch.setenv("KIS_LIVE_SLACK_ALERT_WEBHOOK_URL", KIS_ALERT)
        assert slack_utils.kis_live_notifications_configured() is False

    def test_both_set_is_configured(self, kis_configured):
        assert slack_utils.kis_live_notifications_configured() is True

    def test_the_senders_name_only_the_kis_variables(self):
        """Static proof: neither KIS sender REFERENCES an Alpaca variable,
        so no fallback can be added by accident.

        Identifiers only -- the docstrings of those functions say the
        words "never falls back to SLACK_WEBHOOK_URL", and a naive
        substring search over the dumped tree matches the promise instead
        of a violation of it."""
        source = Path(slack_utils.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden = {"SLACK_WEBHOOK_URL", "SLACK_ALERT_WEBHOOK_URL"}
        for name in ("send_kis_live_message", "send_kis_live_alert"):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            referenced = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
            referenced |= {n.value for n in ast.walk(fn)
                           if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            assert not (referenced & forbidden), f"{name} references {referenced & forbidden}"


class TestAlpacaRoutingIsUntouched:
    def test_the_general_sender_still_uses_the_original_variable(
            self, captured, monkeypatch, kis_configured):
        monkeypatch.setattr(slack_utils, "SLACK_WEBHOOK_URL", ALPACA_GENERAL)
        slack_utils.send_slack_message("scanner output")
        assert captured == [(ALPACA_GENERAL, "scanner output")]

    def test_the_alert_sender_still_uses_the_original_variable(
            self, captured, monkeypatch, kis_configured):
        monkeypatch.setattr(slack_utils, "SLACK_ALERT_WEBHOOK_URL", ALPACA_ALERT)
        slack_utils.send_slack_alert("paper alert")
        assert captured == [(ALPACA_ALERT, "paper alert")]

    def test_operations_alerts_still_delegates_to_the_alpaca_alert_channel(
            self, captured, monkeypatch):
        from operations import alerts

        monkeypatch.setattr(slack_utils, "SLACK_ALERT_WEBHOOK_URL", ALPACA_ALERT)
        alerts.send_alert("something")
        assert [url for url, _ in captured] == [ALPACA_ALERT]

    def test_configuring_kis_live_changes_nothing_for_alpaca(
            self, captured, monkeypatch, kis_configured):
        monkeypatch.setattr(slack_utils, "SLACK_WEBHOOK_URL", ALPACA_GENERAL)
        slack_utils.send_slack_message("still alpaca")
        assert [url for url, _ in captured] == [ALPACA_GENERAL]


class TestEveryMessageIsPrefixed:
    def test_routine_messages_carry_the_kis_live_prefix(self):
        message = live_notifications._format(
            live_notifications.ORDER_SUBMITTED, {"symbol": "AAPL"})
        assert message.startswith("[KIS LIVE] ")
        assert "[CRITICAL]" not in message

    def test_urgent_messages_carry_the_critical_prefix(self):
        message = live_notifications._format(
            live_notifications.ORDER_UNKNOWN, {"symbol": "AAPL"})
        assert message.startswith("[KIS LIVE][CRITICAL] ")

    @pytest.mark.parametrize("event", sorted(live_notifications.EVENTS))
    def test_no_event_is_unprefixed(self, event):
        assert live_notifications._format(event, {}).startswith("[KIS LIVE]")

    def test_the_test_marker_still_comes_first(self):
        """An operator scanning for real traffic must see [TEST] before
        anything else."""
        message = live_notifications._format(
            live_notifications.HALT_ACTIVATED, {}, test=True)
        assert message.startswith("[TEST][KIS LIVE][CRITICAL]")

    def test_the_unknown_contract_lines_survive_the_prefix_change(self):
        message = live_notifications._format(
            live_notifications.ORDER_UNKNOWN, {"symbol": "AAPL"})
        assert live_notifications.UNKNOWN_RETRY_LINE in message
        assert live_notifications.UNKNOWN_RECONCILIATION_LINE in message


class TestNoSecretIsPrinted:
    def test_the_webhook_url_never_appears_in_a_message(self, captured, kis_configured):
        live_notifications.notify(live_notifications.ORDER_SUBMITTED, {"symbol": "AAPL"})
        for url, message in captured:
            assert url not in message

    def test_redaction_still_applies_to_payload_values(self, captured, kis_configured):
        live_notifications.notify(live_notifications.ORDER_SUBMITTED, {
            "symbol": "AAPL", "app_key": "PSxxxxxxxxxxxxxxxxxx",
            "authorization": "Bearer abcdef123456",
        })
        body = captured[0][1]
        assert "PSxxxxxxxxxxxxxxxxxx" not in body
        assert "abcdef123456" not in body

    def test_the_checker_reports_presence_without_the_value(self):
        source = (REPO_ROOT / "scripts" / "final_pre_live_check.sh").read_text(
            encoding="utf-8")
        block = source.split("KIS_LIVE_NOTIFICATION_NOT_CONFIGURED", 1)[0][-600:]
        # Presence is tested with -n; the value is never echoed or printf'd.
        assert '[ -n "${KIS_LIVE_SLACK_WEBHOOK_URL:-}" ]' in block
        for line in block.splitlines():
            if line.strip().startswith(("printf", "echo", "pass ", "fail ")):
                assert "${KIS_LIVE_SLACK_WEBHOOK_URL" not in line
                assert "${KIS_LIVE_SLACK_ALERT_WEBHOOK_URL" not in line


class TestNotificationFailureIsolationSurvives:
    """The rule that predates this change and must outlive it: a Slack
    failure can never alter what the trading system does."""

    def test_a_failing_kis_webhook_does_not_raise(self, monkeypatch, kis_configured):
        def _down(url, message):
            raise RuntimeError("slack is down")

        monkeypatch.setattr(slack_utils, "_send", _down)
        assert live_notifications.notify(
            live_notifications.ORDER_SUBMITTED, {"symbol": "AAPL"}) is False

    def test_an_unconfigured_kis_webhook_does_not_raise(self, monkeypatch):
        monkeypatch.delenv("KIS_LIVE_SLACK_WEBHOOK_URL", raising=False)
        assert live_notifications.notify(
            live_notifications.ORDER_SUBMITTED, {"symbol": "AAPL"}) is False

    def test_the_engine_never_branches_on_a_notify_result(self):
        """No transport may be re-run because Slack answered badly."""
        source = (REPO_ROOT / "execution" / "execution_engine.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.If, ast.While)):
                test_dump = ast.dump(node.test)
                assert "live_notifications" not in test_dump, \
                    f"line {node.lineno}: control flow depends on a notification"
            if isinstance(node, ast.Assign):
                value = ast.dump(node.value)
                if "attr='notify'" in value:
                    raise AssertionError(
                        f"line {node.lineno}: a notify() result is captured")
