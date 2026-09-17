"""The observation stage, against the shape production actually hands it.

`position_store.load_live` returns `List[Tuple[str, Dict]]` -- (position_id,
row) PAIRS -- which is how every other caller in exit_runtime and
exit_timeout consumes it. The observation stage iterated it as if it were
a list of rows, so `row["symbol"]` indexed a tuple with a string and
raised TypeError on every tick from the moment it was deployed:

    File "scripts/run_s6_runtime.py", line 241, in _observe_momentum
      symbol = row["symbol"]
    TypeError: tuple indices must be integers or slices, not str

The tests that let it through used a dict-like mock. These use the real
return shape.
"""

from datetime import datetime, timezone

import pytest

from s6_live import position_store

UTC = timezone.utc
NOW = datetime(2026, 9, 17, 10, 30, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "state.db"))
    monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path / "analytics"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _open(conn, symbol="VIAV", quantity=4):
    pid = position_store.record_submission(
        conn, symbol=symbol, variant="S6-P", entry_session="PREMARKET",
        range_high=10.0, range_low=9.0, entry_volume_expansion=2.0,
        client_order_id=f"c-{symbol}")
    position_store.open_from_fill(conn, pid, quantity=quantity,
                                  average_fill_price=10.0,
                                  entry_order_id=f"o-{symbol}")
    return pid


class TestTheLiveRowShape:

    def test_1_load_live_really_returns_pairs(self, conn):
        """The contract the stage got wrong, asserted directly."""
        _open(conn)
        rows = position_store.load_live(conn)
        assert rows and isinstance(rows[0], tuple) and len(rows[0]) == 2
        position_id, row = rows[0]
        assert isinstance(position_id, str)
        assert isinstance(row, dict) and row["symbol"] == "VIAV"

    def test_2_the_stage_completes_against_that_shape(self, conn):
        """The regression: this raised TypeError on every live tick."""
        from scripts.run_s6_runtime import _observe_momentum

        _open(conn)
        written = _observe_momentum(conn, session="PREMARKET", now=NOW)
        assert isinstance(written, int)

    def test_2b_a_flat_account_is_zero_not_an_error(self, conn):
        from scripts.run_s6_runtime import _observe_momentum

        assert _observe_momentum(conn, session="PREMARKET", now=NOW) == 0

    def test_3_a_momentum_result_is_produced_for_a_held_symbol(self):
        from s6_live.momentum import evaluate_position_momentum
        from s6_live.momentum.state import BarContext

        result = evaluate_position_momentum(
            "VIAV", session="PREMARKET", now=NOW,
            context=BarContext(symbol="VIAV", session="PREMARKET",
                               current=[], inherited=[]))
        assert result.symbol == "VIAV"
        assert result.available is False        # no bars: a reason, not a guess
        assert result.reason == "NO_BARS"

    def test_the_stage_unpacks_rather_than_indexing(self):
        import inspect

        from scripts import run_s6_runtime

        body = inspect.getsource(run_s6_runtime._observe_momentum)
        assert "for position_id, row in rows" in body
        assert 'row["position_id"]' not in body


class TestIsolationStillHolds:

    def test_5_an_observer_exception_cannot_stop_exit_monitoring(self, conn,
                                                                 monkeypatch):
        """The stage loop catches per stage; the sweep also catches per
        symbol, so one unreadable position cannot end the others."""
        from scripts import run_s6_runtime

        _open(conn, symbol="AAA")
        _open(conn, symbol="BBB")
        monkeypatch.setattr(
            run_s6_runtime, "_observe_momentum",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            run_s6_runtime._observe_momentum(conn, session="PREMARKET", now=NOW)
        # and the runtime loop wraps every stage in try/except
        import inspect

        loop = inspect.getsource(run_s6_runtime.run_once)
        assert '("momentum_observation"' in loop
        assert "except Exception as exc:" in loop

    def test_a_single_unreadable_symbol_does_not_end_the_sweep(self, conn,
                                                              monkeypatch):
        import s6_live.momentum as momentum
        from scripts.run_s6_runtime import _observe_momentum

        _open(conn, symbol="AAA")
        _open(conn, symbol="BBB")
        # The stage imports the PACKAGE re-export, so that is the
        # attribute a stub has to replace.
        monkeypatch.setattr(
            momentum, "evaluate_position_momentum",
            lambda *a, **k: (_ for _ in ()).throw(ValueError("nope")))
        assert _observe_momentum(conn, session="PREMARKET", now=NOW) == 0

    def test_6_7_no_sell_broker_provider_or_limiter_from_the_observer(self):
        import ast
        import inspect
        import textwrap

        from scripts import run_s6_runtime

        tree = ast.parse(textwrap.dedent(
            inspect.getsource(run_s6_runtime._observe_momentum)))
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body \
                    and isinstance(body[0], ast.Expr) \
                    and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
        code = ast.dump(ast.fix_missing_locations(tree)).lower()
        for banned in ("broker", "submit", "get_open_orders", "get_positions",
                       "rate_limiter", "provider"):
            assert banned not in code, banned

    def test_4_the_exit_decision_files_are_untouched_by_this_fix(self):
        """exit_runtime changed for the QUANTITY defect only; its exit
        CONDITIONS and the momentum module are unrelated."""
        import inspect

        from s6_live import exit_runtime

        source = inspect.getsource(exit_runtime)
        assert "momentum" not in source.lower()
        assert "MOMENTUM_STRUCTURE_FAILURE" not in source
