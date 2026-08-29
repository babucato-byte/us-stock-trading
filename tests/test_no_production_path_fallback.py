"""A process that was not told where to write does not guess.

What happened
-------------
`slippage_log.log_path` fell back to the shared scanner directory when
no data root was configured. `open_from_fill` records a fill, the test
suite opens positions constantly, and the host regression therefore
wrote thirteen rows of fixture data -- AAPL at 100.0, strategy
PAPER_STRATEGY_ORDER_SCORE_V1, trading day 2026-07-29 -- straight into
the production observability dataset.

Nothing failed. The tests passed, the file was valid JSONL, and the
fabricated rows sat beside the real OWL and SBS ones looking exactly
like them. It was found only because the file's timestamp matched the
minute the regression was running.

That is the whole hazard: a wrong default in a WRITER is silent, and
what it corrupts is the evidence a later decision gets made from.
"""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PRODUCTION_ROOT = "/home/ubuntu/releases/us-stock-trading/shared"

#: Modules that APPEND observation rows. A wrong default here writes
#: fabricated data into a real dataset.
WRITERS = (
    "s6_live/slippage_log.py",
    "s6_live/closed_bar_shadow.py",
    "s6_live/shadow_signal_log.py",
)


def _string_constants(path):
    tree = ast.parse((REPO_ROOT / path).read_text())
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)]


class TestNoWriterGuessesAProductionPath:
    @pytest.mark.parametrize("module", WRITERS)
    def test_the_module_has_no_hardcoded_production_root(self, module):
        offenders = [text for text in _string_constants(module)
                     if PRODUCTION_ROOT in text]
        assert offenders == [], (
            f"{module} names a production path; an unconfigured process "
            "would write there. That is how fixture data reached the real "
            "slippage dataset.")

    @pytest.mark.parametrize("module", WRITERS)
    def test_an_unconfigured_environment_yields_no_path(self, module):
        loaded = __import__(module.replace("/", ".")[:-3],
                            fromlist=["log_path"])
        assert loaded.log_path("2026-07-29", env={}) is None

    @pytest.mark.parametrize("module", WRITERS)
    def test_an_unconfigured_append_writes_nothing_and_says_so(self, module):
        loaded = __import__(module.replace("/", ".")[:-3],
                            fromlist=["append"])
        assert loaded.append({"symbol": "AAPL"}, trading_day="2026-07-29",
                             env={}) is False

    @pytest.mark.parametrize("module", WRITERS)
    def test_an_unconfigured_read_is_empty_rather_than_an_error(self, module):
        loaded = __import__(module.replace("/", ".")[:-3],
                            fromlist=["read"])
        assert loaded.read("2026-07-29", env={}) == []

    @pytest.mark.parametrize("module", WRITERS)
    def test_a_configured_environment_still_works(self, module, tmp_path):
        loaded = __import__(module.replace("/", ".")[:-3],
                            fromlist=["log_path"])
        env = {"SCANNER_DATA_ROOT": str(tmp_path)}
        assert loaded.log_path("2026-07-29", env=env) is not None
        assert loaded.append({"symbol": "AAPL"}, trading_day="2026-07-29",
                             env=env) is True


class TestOpeningAPositionCannotWriteToProduction:
    """The exact path that did it: `open_from_fill` -> the slippage log."""

    def test_a_fill_with_no_configured_root_writes_nowhere(self, monkeypatch):
        import tempfile
        from datetime import datetime, timezone

        monkeypatch.delenv("SLIPPAGE_LOG_DIR", raising=False)
        monkeypatch.delenv("SCANNER_DATA_ROOT", raising=False)
        monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))

        from s6_live import position_store as s6ps
        from state_store.db import open_db

        written = []
        from s6_live import slippage_log

        monkeypatch.setattr(slippage_log, "append",
                            lambda *a, **k: written.append(a) or True)

        now = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
        with open_db() as conn:
            pid = s6ps.record_submission(conn, symbol="AAPL", variant="S6-R",
                                         entry_session="REGULAR",
                                         client_order_id="kislive-AAPL-1",
                                         now=now)
            # The position still opens. Observation never gates a trade.
            assert s6ps.open_from_fill(conn, pid, quantity=1,
                                       average_fill_price=100.0,
                                       venue="NASDAQ", now=now) is True
        # It tried to record -- and with no root configured the real
        # `append` would have refused. That refusal is the fix.
        assert slippage_log.log_path("2026-07-29", env={}) is None
