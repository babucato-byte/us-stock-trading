"""Bars built from the raw tape, with the odd lots left in.

The bars endpoint drops odd-lot prints and KIS's tick stream does not,
which is what made TMO uncomputable from Alpaca and computable from KIS
(measured 2026-09-16: 93 premarket trades, 93 of them under 100 shares,
1 bar from the bars endpoint against 47 from this aggregation).

So the assertions that matter here are about what is KEPT. An
aggregation that quietly filtered odd lots would pass every OHLC test
below and still reproduce the exact defect it exists to remove.
"""

from datetime import datetime, timedelta, timezone

import pytest

from market_data import alpaca_trade_tape as tape

UTC = timezone.utc
T0 = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)


def _trade(offset_seconds, price, size, conditions=("T",)):
    return {"at": T0 + timedelta(seconds=offset_seconds), "price": price,
            "size": float(size), "conditions": tuple(conditions),
            "exchange": "V"}


def _bars(trades, **kwargs):
    return tape.to_minute_bars(trades, symbol="X", session="PREMARKET",
                               **kwargs)


class TestOddLotsAreKept:
    """The entire point."""

    def test_an_odd_lot_only_symbol_still_produces_bars(self):
        trades = [_trade(i * 30, 641.0 + i, 7, ("T", "I")) for i in range(6)]
        result = _bars(trades)
        assert len(result["bars"]) == 3, "three minutes, three bars"
        assert result["odd_lot_trades"] == 6
        assert result["trades_used"] == 6

    def test_odd_lot_volume_is_counted_not_discarded(self):
        trades = [_trade(0, 100.0, 5, ("T", "I")),
                  _trade(10, 100.0, 7, ("T", "I"))]
        bar = _bars(trades)["bars"][0]
        assert bar["volume"] == 12.0

    def test_an_odd_lot_can_set_the_high_and_the_low(self):
        trades = [_trade(0, 100.0, 200), _trade(10, 110.0, 3, ("T", "I")),
                  _trade(20, 90.0, 3, ("T", "I"))]
        bar = _bars(trades)["bars"][0]
        assert bar["high"] == 110.0
        assert bar["low"] == 90.0

    def test_the_exclusion_set_is_empty_by_default(self):
        """If this ever contains `I`, the odd-lot gap is back."""
        assert tape.EXCLUDED_CONDITIONS == frozenset()
        assert "I" not in tape.EXCLUDED_CONDITIONS

    def test_excluding_I_reproduces_the_gap_it_exists_to_close(self):
        """The regression this module was written to prevent, shown."""
        trades = [_trade(i * 30, 641.0, 7, ("T", "I")) for i in range(6)]
        trades.append(_trade(200, 641.0, 500, ("T",)))
        kept = _bars(trades)
        dropped = _bars(trades, excluded_conditions=frozenset({"I"}))
        assert len(kept["bars"]) == 4
        assert len(dropped["bars"]) == 1, (
            "filtering odd lots collapses the session to the round lots -- "
            "exactly the 47-bars-to-1 collapse measured on TMO")


class TestOHLCAggregation:
    def test_open_is_the_first_print_and_close_the_last(self):
        trades = [_trade(0, 10.0, 100), _trade(20, 12.0, 100),
                  _trade(40, 11.0, 100)]
        bar = _bars(trades)["bars"][0]
        assert bar["open"] == 10.0
        assert bar["close"] == 11.0
        assert bar["high"] == 12.0
        assert bar["low"] == 10.0

    def test_volume_and_trade_count_accumulate(self):
        trades = [_trade(0, 10.0, 100), _trade(20, 10.0, 250)]
        bar = _bars(trades)["bars"][0]
        assert bar["volume"] == 350.0
        assert bar["trade_count"] == 2

    def test_price_volume_is_size_weighted(self):
        trades = [_trade(0, 10.0, 100), _trade(20, 20.0, 100)]
        bar = _bars(trades)["bars"][0]
        assert bar["price_volume"] == pytest.approx(3000.0)

    def test_bars_are_oldest_first(self):
        trades = [_trade(300, 10.0, 100), _trade(0, 10.0, 100),
                  _trade(120, 10.0, 100)]
        minutes = [b["minute"] for b in _bars(trades)["bars"]]
        assert minutes == sorted(minutes)

    def test_a_minute_with_no_print_produces_no_bar(self):
        trades = [_trade(0, 10.0, 100), _trade(600, 10.0, 100)]
        bars = _bars(trades)["bars"]
        assert len(bars) == 2, "sparse stays sparse -- no zero-volume filler"
        assert (bars[1]["minute"] - bars[0]["minute"]) == timedelta(minutes=10)

    def test_first_and_last_trade_times_are_carried(self):
        trades = [_trade(5, 10.0, 100), _trade(50, 10.0, 100)]
        bar = _bars(trades)["bars"][0]
        assert bar["first_trade_at"] == T0 + timedelta(seconds=5)
        assert bar["last_trade_at"] == T0 + timedelta(seconds=50)


class TestTradeNormalization:
    def test_nanosecond_timestamps_are_accepted(self):
        """Alpaca stamps to nanoseconds; 3.9's fromisoformat takes micro."""
        moment = tape._parse_timestamp("2026-09-16T08:00:00.123456789Z")
        assert moment == datetime(2026, 9, 16, 8, 0, 0, 123456, tzinfo=UTC)

    def test_offset_timestamps_normalise_to_utc(self):
        moment = tape._parse_timestamp("2026-09-16T08:00:00.123456789-04:00")
        assert moment == datetime(2026, 9, 16, 12, 0, 0, 123456, tzinfo=UTC)

    def test_a_trade_without_a_usable_price_or_size_is_dropped(self):
        good = {"t": "2026-09-16T08:00:00Z", "p": 10.0, "s": 5, "c": ["T"]}
        assert tape._normalize_trade(good) is not None
        for bad in ({**good, "p": 0}, {**good, "s": 0}, {**good, "t": None},
                    {k: v for k, v in good.items() if k != "p"}):
            assert tape._normalize_trade(bad) is None

    def test_conditions_are_preserved_for_the_histogram(self):
        trade = tape._normalize_trade(
            {"t": "2026-09-16T08:00:00Z", "p": 10.0, "s": 5, "c": ["T", "I"]})
        assert trade["conditions"] == ("T", "I")

    def test_the_condition_histogram_counts_every_print(self):
        result = _bars([_trade(0, 10.0, 5, ("T", "I")),
                        _trade(10, 10.0, 5, ("T", "I")),
                        _trade(20, 10.0, 500, ("T",))])
        assert result["conditions"]["I"] == 2
        assert result["conditions"]["T"] == 3


class TestFetchMechanics:
    def _client(self, pages):
        class _Client(tape.TradeTapeClient):
            def __init__(self, pages):
                super().__init__(key="k", secret="s")
                self._pages = list(pages)
                self.sent = []

            def _get(self, params):
                self.sent.append(params)
                self.api_calls += 1
                return self._pages.pop(0)

        return _Client(pages)

    def test_pagination_follows_the_token_to_exhaustion(self):
        client = self._client([
            {"trades": {"X": [{"t": "2026-09-16T08:00:00Z", "p": 1.0,
                               "s": 5, "c": ["T"]}]},
             "next_page_token": "a"},
            {"trades": {"X": [{"t": "2026-09-16T08:01:00Z", "p": 1.0,
                               "s": 5, "c": ["T"]}]},
             "next_page_token": None},
        ])
        out = client.fetch_trades(["X"], start=T0)
        assert len(out["X"]) == 2
        assert client.page_count == 2
        assert client.sent[1]["page_token"] == "a"

    def test_the_feed_is_sip_and_is_not_a_parameter(self):
        import inspect

        assert tape.FEED == "sip"
        assert "feed" not in inspect.signature(
            tape.TradeTapeClient.fetch_trades).parameters

    def test_symbols_are_batched_without_changing_the_result(self):
        page = {"trades": {}, "next_page_token": None}
        client = self._client([page, page, page])
        client._batch_size = 2
        client.fetch_trades(["A", "B", "C", "D", "E"], start=T0)
        assert [p["symbols"] for p in client.sent] == ["A,B", "C,D", "E"]

    def test_duplicate_symbols_are_requested_once(self):
        client = self._client([{"trades": {}, "next_page_token": None}])
        client.fetch_trades(["A", "A", "A"], start=T0)
        assert client.sent[0]["symbols"] == "A"

    def test_counters_are_exposed_for_the_cost_report(self):
        client = self._client([{"trades": {"X": [
            {"t": "2026-09-16T08:00:00Z", "p": 1.0, "s": 5, "c": ["T"]}]},
            "next_page_token": None}])
        client.fetch_trades(["X"], start=T0)
        assert client.raw_trade_count == 1
        assert client.page_count == 1
        assert client.api_calls == 1
        assert client.fetch_seconds >= 0.0


class TestStreamingMatchesBatch:
    """Folding page-by-page must give byte-identical bars.

    Streaming exists because the production host has 956MB of RAM and
    already swaps; holding 743k trade dicts is what made aggregation
    measure 81us/trade there against 3.1us/trade unloaded. That is only
    a safe trade if the bars come out the same, so this compares the two
    paths on the same prints rather than trusting that they agree.
    """

    def _client(self, pages):
        class _Client(tape.TradeTapeClient):
            def __init__(self, pages):
                super().__init__(key="k", secret="s")
                self._pages = list(pages)

            def _get(self, params):
                self.api_calls += 1
                return self._pages.pop(0)

        return _Client(pages)

    def _raw(self, at, price, size, conditions=("T", "I")):
        return {"t": at, "p": price, "s": size, "c": list(conditions)}

    def test_streaming_equals_batch_across_page_boundaries(self):
        """The same minute split over two pages must not become two bars."""
        page_one = {"trades": {"X": [
            self._raw("2026-09-16T08:00:05Z", 10.0, 5),
            self._raw("2026-09-16T08:00:30Z", 12.0, 5)]},
            "next_page_token": "p2"}
        page_two = {"trades": {"X": [
            self._raw("2026-09-16T08:00:55Z", 9.0, 5),
            self._raw("2026-09-16T08:01:10Z", 11.0, 5)]},
            "next_page_token": None}

        streamed = self._client([page_one, page_two]).stream_minute_bars(
            ["X"], start=T0)["X"]["bars"]

        batched = self._client([page_one, page_two])
        raw = batched.fetch_trades(["X"], start=T0)
        expected = tape.to_minute_bars(raw["X"], symbol="X",
                                       session="PREMARKET")["bars"]

        assert len(streamed) == 2, "one bar per minute, not one per page"
        assert len(expected) == len(streamed)
        for a, b in zip(streamed, expected):
            for field in ("minute", "open", "high", "low", "close",
                          "volume", "trade_count"):
                assert a[field] == b[field], field

    def test_open_and_close_survive_the_fold(self):
        page = {"trades": {"X": [
            self._raw("2026-09-16T08:00:05Z", 10.0, 5),
            self._raw("2026-09-16T08:00:30Z", 14.0, 5),
            self._raw("2026-09-16T08:00:55Z", 11.0, 5)]},
            "next_page_token": None}
        bar = self._client([page]).stream_minute_bars(["X"], start=T0)["X"]["bars"][0]
        assert bar["open"] == 10.0
        assert bar["close"] == 11.0
        assert bar["high"] == 14.0
        assert bar["low"] == 10.0
        assert bar["volume"] == 15.0

    def test_streaming_keeps_the_odd_lot_counters(self):
        page = {"trades": {"X": [
            self._raw("2026-09-16T08:00:05Z", 10.0, 5),
            self._raw("2026-09-16T08:00:30Z", 10.0, 500, ("T",))]},
            "next_page_token": None}
        out = self._client([page]).stream_minute_bars(["X"], start=T0)["X"]
        assert out["odd_lot_trades"] == 1
        assert out["trades_used"] == 2
        assert out["conditions"]["I"] == 1

    def test_a_symbol_with_no_prints_returns_an_empty_entry(self):
        page = {"trades": {}, "next_page_token": None}
        out = self._client([page]).stream_minute_bars(["X"], start=T0)
        assert out["X"]["bars"] == []


class TestIsolation:
    def test_the_tape_module_never_reaches_kis(self):
        """This phase forbids a KIS read; an import that does not exist
        cannot be reached by a path nobody thought of."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(tape))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert not any("kis" in name.lower() for name in imported), imported

    def test_the_offline_harness_never_reaches_kis(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1]
                  / "scripts" / "run_trade_tape_parity.py").read_text()
        for banned in ("KISBroker", "kis_broker", "kis_minute_chart",
                       "kis_rate_limiter", "provider_for_session"):
            assert banned not in source

    def test_regular_and_daytime_are_not_referenced_by_the_tape(self):
        import inspect

        source = inspect.getsource(tape)
        for token in ("RBAQ", "RBAY", "RBAA", "OVERNIGHT_DAYTIME"):
            assert token not in source
