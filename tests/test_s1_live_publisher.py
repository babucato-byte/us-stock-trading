"""S1 Limited Live publisher and dynamic allow-list (PHASE 3).

The property under test throughout is fail-closed: every way a candidate
set can be wrong must produce an EMPTY allow-list, because an empty
allow-list is already how this codebase spells "reject every symbol".
There is no partial-credit path -- a file we cannot vouch for yields no
candidates at all, not the rows that happened to parse.

The second property is that a candidate is not a buy. Nothing here
approves an order; the tests assert the publisher and the source stay on
their own side of that line.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import scanner_live_mode  # noqa: E402
from s1_live import candidate_source, publisher, store  # noqa: E402
from scanners.base import result_store, run_context  # noqa: E402
from scanners.base.models import ScannerSignal  # noqa: E402

DAY = "2026-08-17"
RUN_ID = "20260817_DAILY_aa11bb"
S1 = "hma_early_trend"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANNER_ANALYTICS_DIR", str(tmp_path / "analytics"))
    monkeypatch.setenv(store.S1_CANDIDATE_DIR_ENV, str(tmp_path / "shared_state"))
    monkeypatch.delenv(candidate_source.S1_SOURCE_ENABLED_ENV, raising=False)
    return tmp_path


def signal(symbol, scanner=S1, score=80.0, *, day=DAY, run_id=RUN_ID, price=100.0):
    return ScannerSignal(
        timestamp=f"{day}T20:10:00+00:00", trading_day=day, symbol=symbol,
        scanner_name=scanner, scanner_version=f"{scanner}_v1.0",
        scanner_score=score, signal_price=price, scanner_run_id=run_id,
        market_data_provider="yfinance")


def write_signals(signals, day=DAY):
    result_store.write_signals(signals, trading_day=day)


def write_run(day=DAY, run_id=RUN_ID, scanners=(S1,), status=run_context.SUCCESS,
              scanner_status=run_context.SUCCESS, failed=False, provider="yfinance"):
    result_store.write_run_manifest({
        "run_id": run_id, "profile": "daily", "trading_day": day,
        "run_status": status, "provider": provider, "market_data_provider": provider,
        "scanners": [{"scanner_name": name, "scanner_version": f"{name}_v1.0",
                      "config_fingerprint": "abc123def456", "status": scanner_status,
                      "failed": failed} for name in scanners],
    }, trading_day=day)


def publish_default(**kw):
    write_run()
    return publisher.publish(DAY, **kw)


# ---------------------------------------------------------------- routing

class TestOnlyS1Publishes:
    def test_s1_signal_produces_a_candidate(self):
        write_signals([signal("NVDA")])
        result = publish_default()
        assert [row["symbol"] for row in result["rows"]] == ["NVDA"]

    @pytest.mark.parametrize("scanner", [
        "accumulation", "breakout_ready", "premarket_momentum",
        "gap_pullback", "orb"])
    def test_every_discovery_only_scanner_produces_nothing(self, scanner):
        write_signals([signal("NOPE", scanner=scanner, score=99.0)])
        result = publish_default()
        assert result["rows"] == []
        assert result["manifest"]["candidate_count"] == 0

    def test_mixed_signals_yield_only_s1(self):
        write_signals([
            signal("S1SYM", S1, 70.0),
            signal("S2SYM", "accumulation", 99.0),
            signal("S3SYM", "breakout_ready", 99.0),
            signal("S4SYM", "premarket_momentum", 99.0),
            signal("S5SYM", "gap_pullback", 99.0),
            signal("S6SYM", "orb", 99.0),
        ])
        assert [r["symbol"] for r in publish_default()["rows"]] == ["S1SYM"]

    def test_the_manifest_names_s1_as_the_source(self):
        write_signals([signal("NVDA")])
        assert publish_default()["manifest"]["source_scanner"] == S1


class TestLiveModeConfiguration:
    def test_s1_and_s2_are_live_and_the_other_four_are_not(self):
        """The posture after S2's approved promotion. S1's publisher now
        asks for S1 BY NAME rather than for "the only live scanner" --
        the two were the same thing until a second strategy was
        promoted, and inferring identity from being alone would have
        broken the moment that stopped being true."""
        assert scanner_live_mode.is_limited_live(S1) is True
        assert scanner_live_mode.is_limited_live("accumulation") is True
        assert len(scanner_live_mode.discovery_only_scanners()) == 4
        assert scanner_live_mode.require_limited_live(S1) == S1

    def test_s1s_publisher_refuses_when_s1_itself_is_not_live(self):
        """The failure that still matters. A second strategy going live
        is no longer an error -- S1 being switched OFF while its
        publisher runs is, because an empty candidate file from a
        stood-down strategy is indistinguishable from a quiet day."""
        modes = dict(scanner_live_mode.SCANNER_LIVE_MODE)
        modes[S1] = scanner_live_mode.MODE_DISCOVERY_ONLY
        with pytest.raises(scanner_live_mode.ScannerLiveModeError,
                           match="not LIMITED_LIVE"):
            scanner_live_mode.require_limited_live(S1, modes)
        write_signals([signal("NVDA")])
        write_run()
        with pytest.raises(publisher.S1PublishRefused):
            publisher.publish(DAY, modes=modes)

    def test_a_second_live_strategy_does_not_disturb_s1(self):
        """S2 is live now. S1's publisher must be unaffected by that --
        `is_limited_live` used to delegate to a helper that raises unless
        exactly one scanner is live, so promoting S2 would have made S1
        read as not-live too, silently stopping the checks that decide
        whether to place an order."""
        modes = dict(scanner_live_mode.SCANNER_LIVE_MODE,
                     orb=scanner_live_mode.MODE_LIMITED_LIVE)
        assert scanner_live_mode.is_limited_live(S1, modes) is True
        assert scanner_live_mode.require_limited_live(S1, modes) == S1

    def test_zero_limited_live_scanners_fail_closed(self):
        modes = {name: scanner_live_mode.MODE_DISCOVERY_ONLY
                 for name in scanner_live_mode.SCANNER_LIVE_MODE}
        with pytest.raises(scanner_live_mode.ScannerLiveModeError, match="exactly one"):
            scanner_live_mode.limited_live_scanner(modes)
        with pytest.raises(scanner_live_mode.ScannerLiveModeError):
            scanner_live_mode.require_limited_live(S1, modes)

    def test_an_unknown_mode_value_fails_closed(self):
        modes = dict(scanner_live_mode.SCANNER_LIVE_MODE, orb="SORT_OF_LIVE")
        with pytest.raises(scanner_live_mode.ScannerLiveModeError, match="unknown live mode"):
            scanner_live_mode.limited_live_scanner(modes)

    def test_is_limited_live_is_false_on_a_malformed_table(self):
        """A table with an unknown mode value cannot be trusted about any
        scanner in it, so the answer is False rather than a guess."""
        broken = dict(scanner_live_mode.SCANNER_LIVE_MODE, orb="SORT_OF_LIVE")
        assert scanner_live_mode.is_limited_live(S1, broken) is False


# ---------------------------------------------------------------- provenance

class TestProvenance:
    def test_no_successful_run_refuses_publication(self):
        """Signals with no corresponding successful run have no provenance."""
        write_signals([signal("NVDA")])
        with pytest.raises(publisher.S1PublishRefused, match="no successful"):
            publisher.publish(DAY)

    def test_a_failed_s1_run_refuses_publication(self):
        write_signals([signal("NVDA")])
        write_run(scanner_status="FAILED", failed=True)
        with pytest.raises(publisher.S1PublishRefused):
            publisher.publish(DAY)

    def test_signals_from_a_superseded_run_are_excluded(self):
        """A re-run gets a new id; mixing the two would publish a set no
        single data snapshot ever produced."""
        write_signals([signal("OLD", run_id="20260817_DAILY_old000"),
                       signal("NEW", run_id=RUN_ID)])
        result = publish_default()
        assert [r["symbol"] for r in result["rows"]] == ["NEW"]

    def test_the_manifest_carries_the_run_provenance(self):
        write_signals([signal("NVDA")])
        manifest = publish_default()["manifest"]
        assert manifest["scanner_run_id"] == RUN_ID
        assert manifest["config_fingerprint"] == "abc123def456"
        assert manifest["market_data_provider"] == "yfinance"
        assert manifest["scanner_version"] == f"{S1}_v1.0"
        for key in store.REQUIRED_MANIFEST_KEYS:
            assert manifest.get(key) not in (None, ""), key


# ---------------------------------------------------------------- ordering

class TestOrderingAndCap:
    def test_ranked_by_scanner_score_descending(self):
        write_signals([signal("LOW", score=10.0), signal("HIGH", score=90.0),
                       signal("MID", score=50.0)])
        assert [r["symbol"] for r in publish_default()["rows"]] == ["HIGH", "MID", "LOW"]

    def test_ties_break_on_symbol_ascending_and_are_deterministic(self):
        write_signals([signal("ZZZ", score=50.0), signal("AAA", score=50.0),
                       signal("MMM", score=50.0)])
        first = [r["symbol"] for r in publish_default()["rows"]]
        second = [r["symbol"] for r in publisher.publish(DAY)["rows"]]
        assert first == ["AAA", "MMM", "ZZZ"] == second

    def test_default_cap_is_ten(self):
        assert publisher.MAX_S1_LIVE_CANDIDATES == 10
        write_signals([signal(f"S{i:03d}", score=float(100 - i)) for i in range(25)])
        result = publish_default()
        assert len(result["rows"]) == 10
        assert result["truncated"] == 15
        assert result["manifest"]["candidate_count"] == 10

    def test_the_cap_is_configurable(self):
        write_signals([signal(f"S{i:03d}", score=float(100 - i)) for i in range(25)])
        write_run()
        assert len(publisher.publish(DAY, limit=3)["rows"]) == 3

    def test_rank_is_one_based_and_contiguous(self):
        write_signals([signal(f"S{i}", score=float(90 - i)) for i in range(5)])
        assert [r["rank"] for r in publish_default()["rows"]] == [1, 2, 3, 4, 5]


# ---------------------------------------------------------------- the store

class TestStoreValidation:
    def _published(self):
        write_signals([signal("NVDA", score=88.0), signal("AMD", score=70.0)])
        return publish_default()

    def test_a_valid_set_loads(self):
        self._published()
        result = store.load(expected_trading_day=DAY, expected_scanner=S1)
        assert result is not None
        assert result.symbols == {"NVDA", "AMD"}

    def test_missing_candidate_file_returns_none(self):
        self._published()
        store.candidate_path().unlink()
        assert store.load(expected_trading_day=DAY, expected_scanner=S1) is None

    def test_missing_manifest_returns_none(self):
        self._published()
        store.manifest_path().unlink()
        assert store.load(expected_trading_day=DAY, expected_scanner=S1) is None

    def test_payload_hash_mismatch_returns_none(self):
        """A hand-edited CSV, a stale CSV, or a half-written one."""
        self._published()
        path = store.candidate_path()
        path.write_text(path.read_text().replace("NVDA", "TSLA"))
        assert store.load(expected_trading_day=DAY, expected_scanner=S1) is None

    def test_stale_trading_day_returns_none(self):
        """Yesterday's candidate set must not be reused today."""
        self._published()
        assert store.load(expected_trading_day="2026-08-18", expected_scanner=S1) is None

    def test_scanner_run_id_mismatch_returns_none(self):
        self._published()
        assert store.load(expected_trading_day=DAY, expected_scanner=S1,
                          expected_run_id="some_other_run") is None

    def test_source_scanner_mismatch_returns_none(self):
        self._published()
        assert store.load(expected_trading_day=DAY, expected_scanner="orb") is None

    def test_provider_provenance_mismatch_returns_none(self):
        self._published()
        assert store.load(expected_trading_day=DAY, expected_scanner=S1,
                          expected_provider="alpaca") is None

    def test_malformed_manifest_returns_none(self):
        self._published()
        store.manifest_path().write_text("{not json")
        assert store.load(expected_trading_day=DAY, expected_scanner=S1) is None

    @pytest.mark.parametrize("key", list(store.REQUIRED_MANIFEST_KEYS))
    def test_each_missing_manifest_key_returns_none(self, key):
        self._published()
        payload = json.loads(store.manifest_path().read_text())
        payload.pop(key)
        store.manifest_path().write_text(json.dumps(payload))
        assert store.load(expected_trading_day=DAY, expected_scanner=S1) is None

    def test_wrong_schema_version_returns_none(self):
        self._published()
        payload = json.loads(store.manifest_path().read_text())
        payload["schema_version"] = "s1_live_candidates_v99"
        # re-hash so it is ONLY the schema that fails
        store.manifest_path().write_text(json.dumps(payload))
        assert store.load(expected_trading_day=DAY, expected_scanner=S1) is None

    def test_a_malformed_row_rejects_the_whole_file(self):
        """No partial credit: rows we cannot vouch for poison the set."""
        self._published()
        text = store.candidate_path().read_text().replace(",88.0,", ",notanumber,")
        payload = text.encode("utf-8")
        store.candidate_path().write_bytes(payload)
        manifest = json.loads(store.manifest_path().read_text())
        import hashlib
        manifest["payload_sha256"] = hashlib.sha256(payload).hexdigest()
        store.manifest_path().write_text(json.dumps(manifest))
        assert store.load(expected_trading_day=DAY, expected_scanner=S1) is None

    def test_a_changed_header_rejects_the_file(self):
        self._published()
        import hashlib
        text = store.candidate_path().read_text().replace("scanner_score", "score")
        payload = text.encode("utf-8")
        store.candidate_path().write_bytes(payload)
        manifest = json.loads(store.manifest_path().read_text())
        manifest["payload_sha256"] = hashlib.sha256(payload).hexdigest()
        store.manifest_path().write_text(json.dumps(manifest))
        assert store.load(expected_trading_day=DAY, expected_scanner=S1) is None

    def test_publish_leaves_no_temp_files(self):
        self._published()
        leftovers = [p.name for p in store.candidate_dir().iterdir()
                     if p.name.startswith(".")]
        assert leftovers == []

    def test_the_store_env_var_is_not_the_trading_one(self):
        assert store.S1_CANDIDATE_DIR_ENV == "S1_LIVE_CANDIDATE_DIR"
        assert store.S1_CANDIDATE_DIR_ENV != "KIS_CANDIDATE_DIR"
        assert store.CANDIDATE_FILE != "order_candidates.csv"
        assert store.MANIFEST_FILE != "order_candidates.manifest.json"


# ---------------------------------------------------------------- allow-list

class TestDynamicAllowlist:
    class Rollout:
        def __init__(self, allowed=frozenset()):
            self.allowed_symbols = frozenset(allowed)

    def test_a_validated_set_becomes_the_allowlist(self):
        write_signals([signal("NVDA", score=88.0), signal("AMD", score=70.0)])
        publish_default()
        source = candidate_source.S1CandidateSource(
            trading_day=DAY, rollout=self.Rollout())
        assert source.allowed_symbols() == {"NVDA", "AMD"}
        assert source.symbols() == ["NVDA", "AMD"], "ranked order preserved"

    @pytest.mark.parametrize("break_it", [
        "no_csv", "no_manifest", "bad_hash", "wrong_day", "bad_manifest"])
    def test_every_failure_yields_an_empty_allowlist(self, break_it):
        write_signals([signal("NVDA")])
        publish_default()
        day = DAY
        if break_it == "no_csv":
            store.candidate_path().unlink()
        elif break_it == "no_manifest":
            store.manifest_path().unlink()
        elif break_it == "bad_hash":
            store.candidate_path().write_text("rank,symbol\n1,HACKED\n")
        elif break_it == "wrong_day":
            day = "2026-08-18"
        elif break_it == "bad_manifest":
            store.manifest_path().write_text("{}")
        source = candidate_source.S1CandidateSource(
            trading_day=day, rollout=self.Rollout())
        assert source.allowed_symbols() == frozenset()
        assert source.symbols() == []

    def test_s1_stood_down_yields_an_empty_allowlist(self):
        """The refusal that matters for the allowlist: S1 itself not
        live. Another strategy being live is now normal."""
        write_signals([signal("NVDA")])
        publish_default()
        modes = dict(scanner_live_mode.SCANNER_LIVE_MODE)
        modes[S1] = scanner_live_mode.MODE_DISCOVERY_ONLY
        source = candidate_source.S1CandidateSource(
            trading_day=DAY, rollout=self.Rollout(), modes=modes)
        assert source.allowed_symbols() == frozenset()

    def test_a_second_live_strategy_leaves_s1s_allowlist_intact(self):
        write_signals([signal("NVDA")])
        publish_default()
        modes = dict(scanner_live_mode.SCANNER_LIVE_MODE,
                     orb=scanner_live_mode.MODE_LIMITED_LIVE)
        source = candidate_source.S1CandidateSource(
            trading_day=DAY, rollout=self.Rollout(), modes=modes)
        assert source.allowed_symbols() == {"NVDA"}

    def test_an_operator_list_tightens_and_never_loosens(self):
        write_signals([signal("NVDA", score=88.0), signal("AMD", score=70.0)])
        publish_default()
        source = candidate_source.S1CandidateSource(
            trading_day=DAY, rollout=self.Rollout({"NVDA", "TSLA"}))
        assert source.allowed_symbols() == {"NVDA"}, "intersection, not union"
        assert "TSLA" not in source.allowed_symbols()

    def test_a_disjoint_operator_list_yields_nothing(self):
        write_signals([signal("NVDA")])
        publish_default()
        source = candidate_source.S1CandidateSource(
            trading_day=DAY, rollout=self.Rollout({"TSLA"}))
        assert source.allowed_symbols() == frozenset()

    def test_describe_reports_the_refusal(self):
        source = candidate_source.S1CandidateSource(
            trading_day=DAY, rollout=self.Rollout())
        described = source.describe()
        assert described["validated"] is False
        assert described["allowed_symbol_count"] == 0


class TestSourceResolution:
    class Rollout:
        allowed_symbols = frozenset({"AAPL"})

    def test_legacy_is_the_default(self):
        source = candidate_source.resolve(self.Rollout(), trading_day=DAY, env={})
        assert source.name == candidate_source.SOURCE_LEGACY
        assert source.allowed_symbols() == {"AAPL"}

    def test_s1_requires_an_explicit_opt_in(self):
        source = candidate_source.resolve(
            self.Rollout(), trading_day=DAY,
            env={candidate_source.S1_SOURCE_ENABLED_ENV: "true"})
        assert source.name == candidate_source.SOURCE_S1

    def test_s1_without_a_trading_day_falls_back_to_legacy(self):
        """A guessed date would defeat the staleness check it exists for."""
        source = candidate_source.resolve(
            self.Rollout(), trading_day=None,
            env={candidate_source.S1_SOURCE_ENABLED_ENV: "true"})
        assert source.name == candidate_source.SOURCE_LEGACY

    def test_enabling_the_source_does_not_enable_orders(self):
        from config.live_rollout_config import LiveRolloutConfig

        assert LiveRolloutConfig.from_env(
            {candidate_source.S1_SOURCE_ENABLED_ENV: "true"}).enabled is False
