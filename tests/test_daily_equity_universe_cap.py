"""A symbol whose one share costs more than the account is dropped offline.

This is a COST filter on discovery, the same kind as
`discovery/eligible_universe.py`, and it inherits that module's
asymmetry: wrongly including an unaffordable name costs one provider
call and is caught by every gate downstream, while wrongly excluding an
affordable one removes it from discovery entirely and nothing recovers
it. So the tests that matter most here are the ones proving it excludes
NOTHING when it is unsure.

It is not an order gate. `get_orderable_usd()` remains the sole
authority on whether an order can be paid for, re-read per symbol and
price immediately before submission; this value is a day-old upper bound
used only to decide what to look at.
"""

from datetime import datetime, timezone

import pytest

from operations import daily_equity
from scanners.base import universe_selection as usel
from scanners.base.activity import ActivityRecord, ActivityStore

NOW = datetime(2026, 9, 17, 20, 17, tzinfo=timezone.utc)
DAY = "2026-09-17"


class _Position:
    def __init__(self, symbol, quantity, average_fill_price, unrealized_pnl=0.0):
        self.symbol = symbol
        self.quantity = quantity
        self.average_fill_price = average_fill_price
        self.unrealized_pnl = unrealized_pnl


class _Broker:
    def __init__(self, cash=1000.0, positions=(), cash_exc=None, pos_exc=None):
        self._cash = cash
        self._positions = list(positions)
        self._cash_exc = cash_exc
        self._pos_exc = pos_exc
        self.cash_calls = 0
        self.position_calls = 0

    def get_account_cash_usd(self):
        self.cash_calls += 1
        if self._cash_exc:
            raise self._cash_exc
        return self._cash

    def get_positions(self):
        self.position_calls += 1
        if self._pos_exc:
            raise self._pos_exc
        return list(self._positions)


@pytest.fixture
def snapshot_file(tmp_path, monkeypatch):
    target = tmp_path / "DAILY_ACCOUNT_EQUITY.json"
    monkeypatch.setenv(daily_equity.SNAPSHOT_ENV, str(target))
    return target


def _store(prices, day=DAY):
    store = ActivityStore("test")
    for symbol, price in prices.items():
        store._records[symbol] = ActivityRecord(
            symbol=symbol, trading_day=day, price=price,
            avg_volume=1_000_000.0, dollar_volume=(price or 1.0) * 1_000_000.0)
    return store


class _Eligibility:
    def __init__(self, skip=()):
        self._skip = set(skip)

    def should_skip(self, symbol, today=None):
        return symbol in self._skip


# -- the two cases the brief names ---------------------------------------

class TestTheExclusionRule:

    def test_equity_3000_price_3500_is_excluded(self, snapshot_file):
        daily_equity.write({"schema_version": 1, "trading_date": DAY,
                            "equity_usd": 3000.0, "captured_at": NOW.isoformat()})
        cap = daily_equity.equity_for(DAY)
        selection = usel.eligible_top(
            _store({"CHEAP": 2500.0, "DEAR": 3500.0}), _Eligibility(),
            limit=10, today=NOW.date(), max_share_price=cap)
        assert "DEAR" not in selection.symbols
        assert selection.skipped_unaffordable == 1

    def test_equity_3000_price_2500_is_retained(self, snapshot_file):
        daily_equity.write({"schema_version": 1, "trading_date": DAY,
                            "equity_usd": 3000.0, "captured_at": NOW.isoformat()})
        cap = daily_equity.equity_for(DAY)
        selection = usel.eligible_top(
            _store({"CHEAP": 2500.0}), _Eligibility(),
            limit=10, today=NOW.date(), max_share_price=cap)
        assert "CHEAP" in selection.symbols
        assert selection.skipped_unaffordable == 0

    def test_a_share_priced_exactly_at_equity_is_retained(self):
        """The account can buy it; the real orderable-cash check decides
        whether it actually may."""
        selection = usel.eligible_top(
            _store({"EXACT": 3000.0}), _Eligibility(),
            limit=10, today=NOW.date(), max_share_price=3000.0)
        assert "EXACT" in selection.symbols


# -- fail-closed: never empty the universe --------------------------------

class TestUnavailableEquityChangesNothing:
    """The most important half. An equity this cannot obtain must exclude
    NOTHING -- excluding everything would silently end discovery."""

    def test_no_snapshot_means_no_cap(self, snapshot_file):
        assert daily_equity.equity_for(DAY) is None

    def test_a_snapshot_with_a_reason_instead_of_a_number_means_no_cap(
            self, snapshot_file):
        daily_equity.write({"schema_version": 1, "trading_date": DAY,
                            "equity_usd": None,
                            "unavailable_reason": daily_equity.UNAVAILABLE_CASH_READ_FAILED})
        assert daily_equity.equity_for(DAY) is None

    def test_yesterdays_snapshot_is_refused(self, snapshot_file):
        """Capturing daily is the point; applying yesterday's figure is
        the one way this could drop a name the account can now afford."""
        daily_equity.write({"schema_version": 1, "trading_date": "2026-09-16",
                            "equity_usd": 10.0})
        assert daily_equity.equity_for(DAY) is None

    def test_zero_or_negative_equity_is_not_a_cap(self, snapshot_file):
        for value in (0.0, -5.0):
            daily_equity.write({"schema_version": 1, "trading_date": DAY,
                                "equity_usd": value})
            assert daily_equity.equity_for(DAY) is None

    def test_none_cap_retains_every_symbol(self):
        prices = {"A": 1.0, "B": 50_000.0, "C": 10.0}
        selection = usel.eligible_top(
            _store(prices), _Eligibility(), limit=10, today=NOW.date(),
            max_share_price=None)
        assert set(selection.symbols) == set(prices)
        assert selection.skipped_unaffordable == 0

    def test_a_symbol_with_no_recorded_price_is_never_excluded(self):
        """Unknown is not unaffordable."""
        selection = usel.eligible_top(
            _store({"NOPRICE": None, "OK": 5.0}), _Eligibility(),
            limit=10, today=NOW.date(), max_share_price=1.0)
        assert "NOPRICE" in selection.symbols

    def test_a_store_without_price_lookup_disables_the_filter(self):
        class _Old:
            def active_symbols(self, **kwargs):
                return ["A", "B"]

        selection = usel.eligible_top(_Old(), _Eligibility(), limit=10,
                                      max_share_price=1.0)
        assert selection.symbols == ["A", "B"]
        assert selection.skipped_unaffordable == 0

    def test_an_unreadable_snapshot_file_means_no_cap(self, snapshot_file):
        snapshot_file.write_text("{not json", encoding="utf-8")
        assert daily_equity.equity_for(DAY) is None


# -- capture ---------------------------------------------------------------

class TestCapture:

    def test_equity_is_cash_plus_position_market_value(self):
        broker = _Broker(cash=1000.0, positions=[
            _Position("AAPL", 10, 200.0, unrealized_pnl=150.0),   # 2150
            _Position("MU", 5, 100.0, unrealized_pnl=-50.0),      #  450
        ])
        snap = daily_equity.capture(broker, trading_date=DAY, now=NOW)
        assert snap["cash_usd"] == 1000.0
        assert snap["positions_usd"] == pytest.approx(2600.0)
        assert snap["equity_usd"] == pytest.approx(3600.0)
        assert snap["position_count"] == 2
        assert snap["unavailable_reason"] is None

    def test_a_flat_account_is_just_cash(self):
        snap = daily_equity.capture(_Broker(cash=250.0, positions=[]),
                                    trading_date=DAY, now=NOW)
        assert snap["equity_usd"] == 250.0
        assert snap["position_count"] == 0

    def test_the_snapshot_records_what_it_was_built_from(self):
        snap = daily_equity.capture(_Broker(), trading_date=DAY, now=NOW)
        assert snap["trading_date"] == DAY
        assert snap["captured_at"] == NOW.isoformat()
        assert snap["cash_source"] == daily_equity.CASH_SOURCE
        assert snap["position_source"] == daily_equity.POSITION_SOURCE
        assert "CTRP6504R" in snap["cash_source"]
        assert "TTTS3012R" in snap["position_source"]

    def test_the_disproved_krw_field_is_never_referenced(self):
        """`tot_asst_amt` is a won total across every currency -- measured
        at x5,844.18 the USD orderable amount on 2026-08-16. Using it
        would put KRW and an implicit FX rate into this figure."""
        import inspect

        source = inspect.getsource(daily_equity)
        code = "\n".join(l for l in source.splitlines()
                         if not l.strip().startswith("#"))
        # It appears in the docstring explaining WHY it is not used; what
        # must not exist is a read of it.
        assert "tot_asst_amt" not in code.split('"""')[-1]
        assert "frcr_evlu_amt2" not in code.split('"""')[-1]

    @pytest.mark.parametrize("kwargs,reason", [
        ({"cash_exc": RuntimeError("boom")}, daily_equity.UNAVAILABLE_CASH_READ_FAILED),
        ({"pos_exc": RuntimeError("boom")}, daily_equity.UNAVAILABLE_POSITION_READ_FAILED),
    ])
    def test_a_failed_read_yields_a_reason_not_a_number(self, kwargs, reason):
        snap = daily_equity.capture(_Broker(**kwargs), trading_date=DAY, now=NOW)
        assert snap["equity_usd"] is None
        assert snap["unavailable_reason"] == reason

    def test_no_broker_yields_a_reason_not_a_number(self):
        snap = daily_equity.capture(None, trading_date=DAY, now=NOW)
        assert snap["equity_usd"] is None
        assert snap["unavailable_reason"] == daily_equity.UNAVAILABLE_NO_BROKER

    def test_an_unreadable_position_row_refuses_the_whole_figure(self):
        """A row that cannot be valued must not silently shrink the total:
        a smaller equity is a TIGHTER cap, which excludes names the
        account can actually afford."""
        broker = _Broker(cash=1000.0, positions=[
            _Position("AAPL", 10, 200.0),
            _Position("BAD", None, None),
        ])
        snap = daily_equity.capture(broker, trading_date=DAY, now=NOW)
        assert snap["equity_usd"] is None
        assert snap["unavailable_reason"] == daily_equity.UNAVAILABLE_POSITION_READ_FAILED


# -- daily lifecycle -------------------------------------------------------

class TestDailyLifecycle:

    def test_an_intraday_balance_change_does_not_move_the_captured_value(
            self, snapshot_file):
        """Captured once; the account may move all day and this does not."""
        broker = _Broker(cash=3000.0)
        daily_equity.write(daily_equity.capture(broker, trading_date=DAY, now=NOW))
        assert daily_equity.equity_for(DAY) == 3000.0

        broker._cash = 99.0          # the account moves intraday
        assert daily_equity.equity_for(DAY) == 3000.0, "no intraday re-read"
        assert broker.cash_calls == 1, "the broker is read exactly once"

    def test_the_next_daily_refresh_replaces_it(self, snapshot_file):
        daily_equity.write(daily_equity.capture(
            _Broker(cash=3000.0), trading_date=DAY, now=NOW))
        assert daily_equity.equity_for(DAY) == 3000.0

        tomorrow = "2026-09-18"
        daily_equity.write(daily_equity.capture(
            _Broker(cash=4200.0), trading_date=tomorrow, now=NOW))
        assert daily_equity.equity_for(tomorrow) == 4200.0
        assert daily_equity.equity_for(DAY) is None, "the old day is not reused"

    def test_the_snapshot_is_owner_only(self, snapshot_file):
        """It carries the account's cash and equity totals, and every
        other file in shared/state is 0600."""
        import stat

        daily_equity.write({"schema_version": 1, "trading_date": DAY,
                            "equity_usd": 1.0})
        mode = stat.S_IMODE(snapshot_file.stat().st_mode)
        assert mode == daily_equity.SNAPSHOT_MODE == 0o600, oct(mode)

    def test_the_mode_is_set_before_the_rename(self):
        """Never briefly world-readable under its real name."""
        import inspect

        body = inspect.getsource(daily_equity.write)
        assert body.index("os.chmod(temp") < body.index("os.replace(temp")

    def test_the_write_is_atomic(self, snapshot_file):
        daily_equity.write({"schema_version": 1, "trading_date": DAY,
                            "equity_usd": 1.0})
        leftovers = list(snapshot_file.parent.glob(".*tmp"))
        assert leftovers == [], "no temporary file survives the write"


# -- capture happens only on the daily refresh -----------------------------

class TestCaptureRunsOnlyDaily:

    @pytest.mark.parametrize("argv,expected", [
        (["--profile", "daily"], "daily"),
        (["--profile=daily"], "daily"),
        (["--profile", "premarket"], "premarket"),
        (["--scanners", "orb"], None),
        ([], None),
    ])
    def test_profile_is_read_from_the_command_line(self, argv, expected):
        from scripts.run_scanners import _profile_of

        assert _profile_of(argv) == expected

    @pytest.mark.parametrize("argv", [["--profile", "premarket"],
                                      ["--scanners", "orb"], []])
    def test_a_non_daily_run_captures_nothing(self, argv):
        from scripts.run_scanners import capture_daily_equity

        assert capture_daily_equity(argv) is None


# -- where the snapshot lives ---------------------------------------------

class TestSnapshotPathResolution:
    """The first version defaulted to `BASE_DIR / FILENAME`, which on the
    Oracle host resolves INSIDE the release directory -- so the snapshot
    did not survive a release switch and left an untracked file in the
    release worktree. Resolution now mirrors
    `brokers/route_evidence.evidence_path`.
    """

    PROD_DB = "/home/ubuntu/releases/us-stock-trading/shared/state/TRADING_STATE.db"
    PROD_SNAPSHOT = ("/home/ubuntu/releases/us-stock-trading/shared/state/"
                     "DAILY_ACCOUNT_EQUITY.json")

    def test_1_the_explicit_env_path_is_honoured(self, monkeypatch, tmp_path):
        target = tmp_path / "somewhere" / "EQUITY.json"
        monkeypatch.setenv(daily_equity.SNAPSHOT_ENV, str(target))
        assert daily_equity.snapshot_path() == target

    def test_1b_an_explicit_argument_outranks_the_environment(self, monkeypatch,
                                                              tmp_path):
        monkeypatch.setenv(daily_equity.SNAPSHOT_ENV, str(tmp_path / "env.json"))
        assert daily_equity.snapshot_path(tmp_path / "arg.json") == tmp_path / "arg.json"

    def test_2_the_production_path_is_outside_the_release_directory(
            self, monkeypatch):
        """The whole point of the fix, asserted against the real shape of
        the host's layout."""
        monkeypatch.delenv(daily_equity.SNAPSHOT_ENV, raising=False)
        monkeypatch.setenv(daily_equity.SNAPSHOT_ENV, self.PROD_SNAPSHOT)
        resolved = str(daily_equity.snapshot_path())
        assert resolved == self.PROD_SNAPSHOT
        assert "/shared/state/" in resolved
        # A release directory is keyed by a 40-character SHA; the snapshot
        # must not sit under one.
        import re

        assert not re.search(r"/[0-9a-f]{40}/", resolved), (
            "the snapshot must not live inside a release directory")

    def test_3_the_snapshot_survives_a_release_root_change(self, monkeypatch,
                                                          tmp_path):
        """Simulates a deploy: the release root moves, shared state does
        not. The value written under the old release must still be read
        under the new one."""
        shared = tmp_path / "shared" / "state"
        shared.mkdir(parents=True)
        monkeypatch.delenv(daily_equity.SNAPSHOT_ENV, raising=False)

        monkeypatch.setenv("STATE_STORE_DB_FILE", str(shared / "TRADING_STATE.db"))
        monkeypatch.chdir(tmp_path)
        daily_equity.write({"schema_version": 1, "trading_date": DAY,
                            "equity_usd": 4242.0})
        assert daily_equity.equity_for(DAY) == 4242.0

        # "Deploy": a brand new release directory, same shared state.
        new_release = tmp_path / "releases" / ("b" * 40)
        new_release.mkdir(parents=True)
        monkeypatch.chdir(new_release)
        assert daily_equity.equity_for(DAY) == 4242.0, (
            "the snapshot must survive a release switch")

    def test_4_without_its_own_env_it_follows_the_state_database(
            self, monkeypatch, tmp_path):
        """The canonical shared-state marker, not a second path system."""
        shared = tmp_path / "shared" / "state"
        shared.mkdir(parents=True)
        monkeypatch.delenv(daily_equity.SNAPSHOT_ENV, raising=False)
        monkeypatch.setenv("STATE_STORE_DB_FILE", str(shared / "TRADING_STATE.db"))
        assert daily_equity.snapshot_path() == shared / daily_equity.FILENAME

    def test_4b_the_legacy_db_variable_is_also_accepted(self, monkeypatch,
                                                        tmp_path):
        monkeypatch.delenv(daily_equity.SNAPSHOT_ENV, raising=False)
        monkeypatch.delenv("STATE_STORE_DB_FILE", raising=False)
        monkeypatch.setenv("TRADING_STATE_DB", str(tmp_path / "TRADING_STATE.db"))
        assert daily_equity.snapshot_path() == tmp_path / daily_equity.FILENAME

    def test_4c_no_state_configuration_resolves_to_nothing(self, monkeypatch):
        """Never a repository-relative default: a stray file in a checkout
        must not become a live account figure."""
        for name in (daily_equity.SNAPSHOT_ENV, "STATE_STORE_DB_FILE",
                     "TRADING_STATE_DB"):
            monkeypatch.delenv(name, raising=False)
        assert daily_equity.snapshot_path() is None
        assert daily_equity.read() is None
        assert daily_equity.equity_for(DAY) is None
        assert daily_equity.write({"equity_usd": 1.0}) is None

    def test_4d_a_blank_override_is_not_a_path(self, monkeypatch, tmp_path):
        monkeypatch.setenv(daily_equity.SNAPSHOT_ENV, "   ")
        monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "db.sqlite"))
        assert daily_equity.snapshot_path() == tmp_path / daily_equity.FILENAME

    def test_the_module_keeps_no_repository_relative_default(self):
        """The defect, asserted gone."""
        import inspect

        code = "\n".join(
            line for line in inspect.getsource(daily_equity).splitlines()
            if not line.lstrip().startswith("#"))
        body = code.split('"""')
        executable = "".join(body[i] for i in range(0, len(body), 2))
        assert "BASE_DIR" not in executable
        assert "Path(__file__)" not in executable


# -- what must NOT have changed -------------------------------------------

class TestUnchangedBehaviour:

    def test_the_ranking_order_is_untouched(self):
        """Membership only. A retained symbol keeps the rank it had."""
        store = _store({"A": 1.0, "B": 2.0, "C": 3.0})
        without = usel.eligible_top(store, _Eligibility(), limit=10,
                                    today=NOW.date())
        with_cap = usel.eligible_top(store, _Eligibility(), limit=10,
                                     today=NOW.date(), max_share_price=10.0)
        assert with_cap.symbols == without.symbols
        for symbol in with_cap.symbols:
            assert with_cap.rank_of(symbol) == without.rank_of(symbol)

    def test_eligibility_skips_are_still_counted_separately(self):
        selection = usel.eligible_top(
            _store({"A": 1.0, "SKIP": 2.0, "DEAR": 9_999.0}),
            _Eligibility(skip={"SKIP"}), limit=10, today=NOW.date(),
            max_share_price=100.0)
        assert selection.skipped_ineligible == 1
        assert selection.skipped_unaffordable == 1
        assert "A" in selection.symbols

    def test_the_summary_reports_the_new_counter(self):
        selection = usel.eligible_top(
            _store({"DEAR": 9_999.0}), _Eligibility(), limit=10,
            today=NOW.date(), max_share_price=1.0)
        assert selection.summary()["skipped_unaffordable"] == 1

    def test_the_default_call_behaves_exactly_as_before(self):
        """Every existing caller passes no cap and must be unaffected."""
        store = _store({"A": 1.0, "B": 50_000.0})
        selection = usel.eligible_top(store, _Eligibility(), limit=10,
                                      today=NOW.date())
        assert set(selection.symbols) == {"A", "B"}
        assert selection.skipped_unaffordable == 0

    def test_the_equity_module_imports_no_broker(self):
        """It takes a broker as an ARGUMENT; importing one would give the
        scanner a capability tests/test_scanner_trading_isolation.py
        forbids, since the runner reads this module."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(daily_equity))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert not any(n.startswith(("broker", "execution", "kis_live"))
                       for n in imported), imported

    def test_the_orderable_cash_precheck_is_untouched(self):
        """The order path's authority must not have moved."""
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1]
                  / "brokers" / "kis_broker.py").read_text()
        assert "def get_orderable_usd" in source
        assert "TTTS3007R" in source
        assert "daily_equity" not in source, (
            "the order path must not consult the daily cap")
