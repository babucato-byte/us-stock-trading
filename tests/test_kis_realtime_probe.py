"""The extended-hours volume question, and refusing to guess at it.

S6's entry needs VWAP, EMA structure, ORB range and volume expansion.
The first three need only price; the fourth needs volume, and the
daily-bar provider reports ZERO volume outside the regular session --
not "unknown", but a number that reads as "nobody traded". That single
ambiguity is why S6 is a regular-session strategy in practice while
being a four-session strategy by design.

So the question gets asked of KIS directly, and every outcome here is a
classification of a real response. "We could not tell" is one of them,
and it is deliberately distinct from "there was no volume": a probe that
collapsed those two would become the reason to enable trading in a
session whose data nobody had actually checked.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_probe", REPO_ROOT / "scripts" / "probe_kis_realtime_volume.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def _trade(**fields):
    values = {name: "" for name in probe.HDFSCNT0_FIELDS}
    values.update(fields)
    body = "^".join(values[name] for name in probe.HDFSCNT0_FIELDS)
    return f"0|{probe.TR_TRADE}|001|{body}"


class TestTheOutcomesStayDistinct:
    def test_trades_with_volume_are_available(self):
        records = [probe.parse_trade(_trade(SYMB="AAPL", LAST="231.5",
                                            EVOL="100", TVOL="4210"))]
        assert probe.classify(records, control_messages=[]) == probe.VOLUME_AVAILABLE

    def test_trades_with_no_volume_field_are_unavailable(self):
        """Trades arrived and every volume field was zero. That is an
        answer, and a different one from "no trades"."""
        records = [probe.parse_trade(_trade(SYMB="AAPL", LAST="231.5",
                                            EVOL="0", TVOL="0"))]
        assert probe.classify(records, control_messages=[]) == probe.VOLUME_UNAVAILABLE

    def test_no_trades_is_not_reported_as_no_volume(self):
        """A quiet window says nothing about whether volume is carried,
        and reading it as "unavailable" would condemn a session on no
        evidence."""
        assert probe.classify([], control_messages=[]) == probe.NO_TRADES_OBSERVED

    def test_a_permission_message_outranks_the_trade_count(self):
        assert probe.classify(
            [], control_messages=['{"msg1":"NOT AUTHORIZED"}']
        ) == probe.PERMISSION_REQUIRED

    def test_a_subscription_message_is_its_own_outcome(self):
        assert probe.classify(
            [], control_messages=["시세신청이 필요합니다"]
        ) == probe.SUBSCRIPTION_REQUIRED


class TestTheWireFormatIsNotAssumed:
    def test_a_trade_record_maps_onto_the_published_layout(self):
        record = probe.parse_trade(_trade(SYMB="NVDA", LAST="180.1", EVOL="7"))
        assert record["SYMB"] == "NVDA"
        assert record["LAST"] == "180.1"
        assert record[probe.FIELD_TRADE_SIZE] == "7"
        assert record["layout_mismatch"] is False

    def test_a_short_record_is_flagged_not_silently_shifted(self):
        """Mapping a changed layout positionally would put the price in
        the volume column and the probe would answer confidently with
        the wrong number."""
        record = probe.parse_trade(f"0|{probe.TR_TRADE}|001|AAPL^231.5")
        assert record["layout_mismatch"] is True

    def test_a_message_for_another_tr_is_not_a_trade(self):
        assert probe.parse_trade("0|HDFSASP0|001|x^y") is None

    def test_a_control_json_is_not_a_trade(self):
        assert probe.parse_trade('{"header":{"tr_id":"HDFSCNT0"}}') is None


class TestTheKeepAlive:
    def test_a_pingpong_is_recognised(self):
        assert probe._is_pingpong(
            '{"header":{"tr_id":"PINGPONG","datetime":"20260828093112"}}')

    def test_a_subscribe_reply_is_not_a_pingpong(self):
        assert not probe._is_pingpong(
            '{"header":{"tr_id":"HDFSCNT0"},"body":{"msg1":"SUBSCRIBE SUCCESS"}}')

    def test_it_is_echoed_rather_than_ignored(self):
        """KIS's keep-alive is an application message, not the RFC 6455
        ping opcode. The first run of this probe answered only the
        protocol ping and KIS closed the connection after 100 seconds --
        which the result then reported as "no trades observed", when it
        really meant "we stopped listening"."""
        source = (REPO_ROOT / "scripts" / "probe_kis_realtime_volume.py").read_text(
            encoding="utf-8")
        block = source[source.index("if _is_pingpong(message):"):]
        assert "ws.send_text(message)" in block[:800]


class TestNothingSecretIsStored:
    def test_the_stream_key_and_iv_are_redacted(self):
        """KIS returns an AES key and IV on every SUBSCRIBE SUCCESS. They
        are unused while encrypt is "N", but this probe writes its output
        to a file and into a report."""
        message = json.dumps({
            "header": {"tr_id": "HDFSCNT0"},
            "body": {"msg1": "SUBSCRIBE SUCCESS",
                     "output": {"iv": "a65ac7a1c1396634",
                                "key": "kkjcjcoazvlppmhkagfuvwmaqsaevkkf"}}})
        scrubbed = probe._scrub_control(message)
        assert "a65ac7a1c1396634" not in scrubbed
        assert "kkjcjcoazvlppmhkagfuvwmaqsaevkkf" not in scrubbed
        assert "SUBSCRIBE SUCCESS" in scrubbed
        assert "redacted" in scrubbed

    def test_a_non_json_control_message_survives_unchanged(self):
        assert probe._scrub_control("not json") == "not json"

    def test_the_app_key_is_never_logged(self):
        source = (REPO_ROOT / "scripts" / "probe_kis_realtime_volume.py").read_text(
            encoding="utf-8")
        # Length and last four characters: enough to tell two keys apart
        # in a report, not enough to use one.
        assert "app key fingerprint" in source
        fingerprint = source[source.index("app key fingerprint"):]
        assert "len(app_key)" in fingerprint[:200]
        assert "app_key[-4:]" in fingerprint[:300]
        # The whole value never reaches a log call as its own argument.
        assert '%s", app_key' not in source
        assert "logger.info(app_key" not in source
        # ...and the secret is never an argument to a log call at all.
        for line in source.splitlines():
            if "logger." in line and "app_secret" in line:
                raise AssertionError(f"the app secret reaches a log call: {line}")


class TestItCannotPlaceAnOrder:
    def test_no_execution_path_is_reachable(self):
        source = (REPO_ROOT / "scripts" / "probe_kis_realtime_volume.py").read_text(
            encoding="utf-8")
        for forbidden in ("submit_buy_order", "submit_sell_order",
                          "submit_order", "execution_engine", "order_gate"):
            assert forbidden not in source, forbidden


class TestTheSymbolAddressing:
    def test_the_exchange_prefix_is_applied(self):
        assert probe._tr_key("AAPL", "NAS") == "DNASAAPL"
        assert probe._tr_key("BTG", "AMEX") == "DAMSBTG"

    def test_an_unknown_exchange_is_refused_not_guessed(self):
        import pytest

        with pytest.raises(ValueError):
            probe._tr_key("AAPL", "LSE")
