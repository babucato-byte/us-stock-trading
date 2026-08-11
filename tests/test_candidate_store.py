"""Candidates are published once, to a shared path, atomically.

The defect: the scanner wrote `order_candidates.csv` beside its own
source and the consumer read `order_candidates.csv` beside ITS source.
Same relative path, never the same absolute one -- the scanner runs from
the legacy working copy, trading runs from a detached release. A fresh
release saw zero candidates, and the first LIMITED LIVE bootstrap needed
a hand-run `cp` to proceed.

Two properties are pinned here.

Shared: producer and consumer resolve the same absolute path regardless
of which release either is running from.

Atomic: `to_csv` truncates in place, so a reader can catch a partial
file -- and a partial candidate file does not look like an error, it
looks like fewer candidates. TestPublicationIsAtomic is the test that
matters, because that failure is silent.

Freshness is checked against the manifest's recorded trading day, not
mtime: an mtime survives a copy or a rollout that touched the file
without regenerating it.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_data import candidate_store as store  # noqa: E402

NOW = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)
DAY = "2026-08-10"
CSV = b"symbol,price,score\nOMDA,24.5,100\nCART,50.1,100\n"


@pytest.fixture(autouse=True)
def shared_dir(tmp_path, monkeypatch):
    target = tmp_path / "shared" / "state"
    monkeypatch.setenv(store.CANDIDATE_DIR_ENV, str(target))
    return target


class TestTheStoreIsSharedNotPerRelease:
    def test_two_different_releases_resolve_the_same_path(self, tmp_path, monkeypatch):
        """The whole point: release A publishes, release B reads."""
        shared = tmp_path / "releases" / "shared" / "state"
        shared.mkdir(parents=True)
        monkeypatch.delenv(store.CANDIDATE_DIR_ENV, raising=False)

        monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path / "releases" / "aaaa"))
        from_a = store.candidate_path()
        monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path / "releases" / "bbbb"))
        from_b = store.candidate_path()

        assert from_a == from_b
        assert "aaaa" not in str(from_a) and "bbbb" not in str(from_a)

    def test_the_default_sits_with_the_other_shared_state(self, tmp_path, monkeypatch):
        shared = tmp_path / "releases" / "shared" / "state"
        shared.mkdir(parents=True)
        monkeypatch.delenv(store.CANDIDATE_DIR_ENV, raising=False)
        monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path / "releases" / "sha"))
        assert store.candidate_dir() == shared

    def test_publication_then_read_round_trips(self, shared_dir):
        store.publish(CSV, trading_day=DAY, generated_at=NOW)
        assert store.symbols() == ["OMDA", "CART"]
        assert store.read_manifest()["trading_day"] == DAY

    def test_the_consumer_prefers_the_shared_store(self):
        source = (REPO_ROOT / "paper_strategy_order.py").read_text(encoding="utf-8")
        body = source.split("def load_watchlist", 1)[1].split("\ndef ", 1)[0]
        assert "candidate_store.symbols()" in body
        assert body.index("candidate_store.symbols()") < body.index("ORDER_CANDIDATES_FILE")

    def test_the_producer_publishes_to_the_shared_store(self):
        source = (REPO_ROOT / "daily_candidate_scanner.py").read_text(encoding="utf-8")
        body = source.split("def save_candidate_files", 1)[1].split("\ndef classify", 1)[0]
        assert "_publish_to_shared_store" in body


class TestPublicationIsAtomic:
    def test_a_reader_never_sees_a_partial_file(self, shared_dir):
        """Replace is a rename: the destination is either the whole old
        file or the whole new one, never a truncated prefix."""
        store.publish(CSV, trading_day=DAY, generated_at=NOW)
        first = store.candidate_path().read_bytes()

        bigger = CSV + b"".join(b"SYM%d,1.0,70\n" % i for i in range(500))
        store.publish(bigger, trading_day=DAY, generated_at=NOW)
        second = store.candidate_path().read_bytes()

        assert first == CSV
        assert second == bigger
        assert len(second) > len(first)

    def test_it_uses_replace_and_fsync_not_a_plain_write(self):
        source = (REPO_ROOT / "market_data" / "candidate_store.py").read_text(encoding="utf-8")
        body = source.split("def _atomic_write_bytes", 1)[1].split("\ndef ", 1)[0]
        for required in ("mkstemp", "os.fsync", "os.replace"):
            assert required in body, required
        assert body.index("os.fsync") < body.index("os.replace")

    def test_the_temp_file_is_removed_on_failure(self, shared_dir, monkeypatch):
        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            store.publish(CSV, trading_day=DAY, generated_at=NOW)
        leftovers = [p for p in shared_dir.iterdir() if p.name.startswith(".")]
        assert leftovers == [], leftovers

    def test_the_temp_file_lands_in_the_destination_directory(self):
        """A rename across filesystems is not atomic, so the temp file
        must be created beside its destination."""
        source = (REPO_ROOT / "market_data" / "candidate_store.py").read_text(encoding="utf-8")
        body = source.split("def _atomic_write_bytes", 1)[1].split("\ndef ", 1)[0]
        assert "dir=str(directory)" in body

    def test_the_csv_is_published_before_the_manifest(self):
        """The intermediate state must be new-file + old-manifest, which
        reads as stale and is refused. The reverse would be a fresh
        manifest describing an old file, which would be believed."""
        source = (REPO_ROOT / "market_data" / "candidate_store.py").read_text(encoding="utf-8")
        body = source.split("def publish(", 1)[1].split("\ndef ", 1)[0]
        assert body.index("candidate_path()") < body.index("manifest_path()")


class TestFreshness:
    def _publish(self, *, day=DAY, at=NOW):
        store.publish(CSV, trading_day=day, generated_at=at)

    def test_a_fresh_candidate_set_for_today_is_accepted(self):
        self._publish()
        rows, manifest = store.load_verified(trading_day=DAY, now=NOW)
        assert [r["symbol"] for r in rows] == ["OMDA", "CART"]
        assert manifest["trading_day"] == DAY

    def test_a_different_trading_day_is_stale(self):
        self._publish(day="2026-08-07")
        with pytest.raises(store.CandidatesStale) as caught:
            store.load_verified(trading_day=DAY, now=NOW)
        assert "2026-08-07" in str(caught.value)

    def test_an_old_candidate_set_is_stale(self):
        self._publish(at=NOW - timedelta(hours=9))
        with pytest.raises(store.CandidatesStale):
            store.load_verified(trading_day=DAY, now=NOW)

    def test_a_set_published_within_the_window_is_accepted(self):
        self._publish(at=NOW - timedelta(hours=5))
        assert store.load_verified(trading_day=DAY, now=NOW)

    def test_a_future_dated_set_is_stale(self):
        """A clock is wrong; guessing which is not this module's job."""
        self._publish(at=NOW + timedelta(hours=2))
        with pytest.raises(store.CandidatesStale):
            store.load_verified(trading_day=DAY, now=NOW)

    def test_freshness_comes_from_the_manifest_not_the_mtime(self):
        """An mtime survives a copy or a rollout that touched the file
        without regenerating it; the recorded trading day does not."""
        self._publish(day="2026-08-07")
        os.utime(store.candidate_path(), None)  # mtime is now "now"
        with pytest.raises(store.CandidatesStale):
            store.load_verified(trading_day=DAY, now=NOW)

    def test_no_manifest_is_unavailable_not_usable(self):
        store.publish(CSV, trading_day=DAY, generated_at=NOW)
        store.manifest_path().unlink()
        with pytest.raises(store.CandidatesUnavailable):
            store.load_verified(trading_day=DAY, now=NOW)

    def test_no_file_at_all_is_unavailable(self):
        with pytest.raises(store.CandidatesUnavailable):
            store.load_verified(trading_day=DAY, now=NOW)

    def test_an_empty_candidate_set_is_unavailable(self):
        store.publish(b"symbol,price,score\n", trading_day=DAY, generated_at=NOW)
        with pytest.raises(store.CandidatesUnavailable):
            store.load_verified(trading_day=DAY, now=NOW)

    def test_a_corrupt_manifest_is_unavailable(self):
        store.publish(CSV, trading_day=DAY, generated_at=NOW)
        store.manifest_path().write_text("{not json", encoding="utf-8")
        with pytest.raises(store.CandidatesUnavailable):
            store.load_verified(trading_day=DAY, now=NOW)

    def test_symbols_never_raises_for_an_absent_store(self):
        """The ordinary watchlist path must degrade to 'no candidates',
        not explode."""
        assert store.symbols() == []

    def test_the_reason_codes_are_stable(self):
        assert store.CandidatesUnavailable.reason_code == "NO_CANDIDATE"
        assert store.CandidatesStale.reason_code == "STALE_CANDIDATE"


class TestTheBootstrapRefusesUnusableCandidates:
    """Freshness is enforced where it matters -- one step before a real
    order is priced."""

    def _select(self, symbol="OMDA", now=NOW):
        from live_pilot import bootstrap
        rollout = type("R", (), {"allowed_symbols": frozenset({symbol})})()
        return bootstrap.select_candidate(
            broker=object(), rollout=rollout, deployed_commit="abc", now=now)

    def test_no_published_candidates_blocks(self):
        from live_pilot import bootstrap
        with pytest.raises(bootstrap.BootstrapBlocked) as caught:
            self._select()
        assert bootstrap.NO_CANDIDATE in caught.value.reason_codes

    def test_stale_candidates_block(self, monkeypatch):
        from live_pilot import bootstrap
        store.publish(CSV, trading_day="2026-08-07", generated_at=NOW)
        monkeypatch.setattr(bootstrap, "us_trading_day", lambda *a: DAY)
        with pytest.raises(bootstrap.BootstrapBlocked) as caught:
            self._select()
        assert bootstrap.STALE_CANDIDATE in caught.value.reason_codes

    def test_an_allowlisted_symbol_the_scanner_did_not_nominate_blocks(self, monkeypatch):
        """A symbol can score well on a day the scanner never nominated
        it, so the live re-score alone would not catch this."""
        from live_pilot import bootstrap
        store.publish(CSV, trading_day=DAY, generated_at=NOW)
        monkeypatch.setattr(bootstrap, "us_trading_day", lambda *a: DAY)
        with pytest.raises(bootstrap.BootstrapBlocked) as caught:
            self._select(symbol="TSLA")
        assert bootstrap.CANDIDATE_SYMBOL_NOT_PUBLISHED in caught.value.reason_codes

    def test_the_check_happens_before_any_broker_read(self, monkeypatch):
        """broker=object() above has no methods: if any broker call were
        made before the candidate gate, these tests would AttributeError
        instead of raising BootstrapBlocked."""
        from live_pilot import bootstrap
        source = (REPO_ROOT / "live_pilot" / "bootstrap.py").read_text(encoding="utf-8")
        body = source.split("def select_candidate", 1)[1].split("\ndef ", 1)[0]
        assert body.index("load_verified") < body.index("analyze_stock")
        assert body.index("load_verified") < body.index("get_orderable_usd")
        assert bootstrap.NO_CANDIDATE == "NO_CANDIDATE"


class TestTheManualCopyIsGone:
    def test_no_source_file_copies_a_candidate_csv_into_a_release(self):
        for path in REPO_ROOT.rglob("*.py"):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel.startswith(("tests/", "venv/")):
                continue
            text = path.read_text(encoding="utf-8")
            code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
            assert "cp /home/ubuntu/trading" not in code, rel

    def test_the_bridge_command_exists_and_compiles(self):
        import py_compile
        py_compile.compile(str(REPO_ROOT / "scripts" / "publish_candidates.py"), doraise=True)

    def test_the_bridge_refuses_a_stale_source(self):
        source = (REPO_ROOT / "scripts" / "publish_candidates.py").read_text(encoding="utf-8")
        assert "max-source-age-seconds" in source
        assert "REFUSED" in source

    def test_the_bridge_refuses_to_publish_an_empty_set(self):
        """An empty publication asserts 'the scanner found nothing',
        which only the scanner may assert."""
        source = (REPO_ROOT / "scripts" / "publish_candidates.py").read_text(encoding="utf-8")
        assert "if not data_rows:" in source

    def test_the_bridge_adds_no_strategy_logic(self):
        source = (REPO_ROOT / "scripts" / "publish_candidates.py").read_text(encoding="utf-8")
        code = "\n".join(l for l in source.splitlines() if not l.strip().startswith("#"))
        body = code.split('"""', 2)[-1]
        for forbidden in ("score", "threshold", "rsi", "vwap", "ema", "sort", "rank"):
            assert forbidden not in body.lower(), forbidden


class TestStrategyConditionsAreUntouched:
    def test_the_scanner_publication_hook_is_purely_additive(self):
        source = (REPO_ROOT / "daily_candidate_scanner.py").read_text(encoding="utf-8")
        body = source.split("def _publish_to_shared_store", 1)[1].split("\ndef ", 1)[0]
        for forbidden in ("threshold", "score >", "score <", "filter", "drop", "query("):
            assert forbidden not in body, forbidden

    def test_a_publication_failure_never_breaks_the_scan(self):
        """Statements only -- the function's own docstring contains the
        word "raises" while promising not to, and a naive substring
        search over the whole body matches the promise."""
        import ast

        source = (REPO_ROOT / "daily_candidate_scanner.py").read_text(encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(source))
                  if isinstance(n, ast.FunctionDef) and n.name == "_publish_to_shared_store")
        handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
        assert handlers, "publication is not wrapped at all"
        assert any(h.type is not None and getattr(h.type, "id", "") == "Exception"
                   for h in handlers)
        raises = [n for n in ast.walk(fn) if isinstance(n, ast.Raise)]
        assert raises == [], "a publication failure can escape into the scan"

    def test_the_production_threshold_is_unchanged(self):
        import kis_live_trading
        from live_pilot import bootstrap
        assert bootstrap.SCORE_THRESHOLD == kis_live_trading.SCORE_THRESHOLD == 70


class TestAnUnresolvedStoreRefusesInsteadOfFallingBack:
    """The resolver used to fall back to the release directory when
    neither environment variable was set. That silently reintroduced the
    split brain this store exists to remove: a process would publish
    into its own release, where no other release could see it, and
    report success.

    It happened for real -- a publisher invoked without
    TRADING_PROJECT_ROOT wrote a CSV and manifest into a deployed
    release and left the working tree dirty, which would have blocked
    the next bootstrap on WORKING_TREE_DIRTY.

    The fix is the refusal, not a .gitignore entry: hiding the dirty
    tree would have left the wrong-path write in place and merely
    stopped anyone noticing.
    """

    @pytest.fixture
    def unset(self, monkeypatch):
        monkeypatch.delenv(store.CANDIDATE_DIR_ENV, raising=False)
        monkeypatch.delenv("TRADING_PROJECT_ROOT", raising=False)

    def test_resolution_refuses_when_nothing_is_set(self, unset):
        with pytest.raises(store.CandidateStoreUnresolved):
            store.candidate_dir()

    def test_resolution_refuses_when_the_shared_dir_does_not_exist(
            self, monkeypatch, tmp_path):
        monkeypatch.delenv(store.CANDIDATE_DIR_ENV, raising=False)
        monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path / "releases" / "sha"))
        with pytest.raises(store.CandidateStoreUnresolved):
            store.candidate_dir()

    def test_a_blank_env_value_does_not_count_as_set(self, monkeypatch):
        monkeypatch.setenv(store.CANDIDATE_DIR_ENV, "   ")
        monkeypatch.setenv("TRADING_PROJECT_ROOT", "  ")
        with pytest.raises(store.CandidateStoreUnresolved):
            store.candidate_dir()

    def test_publish_refuses_and_writes_nothing(self, unset, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        before = set(REPO_ROOT.iterdir())
        with pytest.raises(store.CandidateStoreUnresolved):
            store.publish(CSV, trading_day=DAY, generated_at=NOW)
        assert set(REPO_ROOT.iterdir()) == before, "the repository was written to"
        assert list(tmp_path.iterdir()) == [], "the cwd was written to"

    def test_no_candidate_artifact_lands_in_the_repository(self, unset):
        """The concrete regression: these two files appeared in a
        deployed release and dirtied its working tree."""
        for name in (store.CANDIDATE_FILE, store.MANIFEST_FILE):
            assert not (REPO_ROOT / name).exists(), name

    def test_the_strict_read_refuses(self, unset):
        with pytest.raises(store.CandidatesUnavailable):
            store.load_verified(trading_day=DAY, now=NOW)

    def test_it_carries_its_own_reason_code(self):
        assert store.CandidateStoreUnresolved.reason_code == "CANDIDATE_STORE_UNRESOLVED"
        assert issubclass(store.CandidateStoreUnresolved, store.CandidatesUnavailable)

    def test_the_bootstrap_blocks_with_that_code(self, unset):
        from live_pilot import bootstrap

        class R:
            allowed_symbols = frozenset({"OMDA"})

        with pytest.raises(bootstrap.BootstrapBlocked) as caught:
            bootstrap.select_candidate(broker=object(), rollout=R(),
                                       deployed_commit="abc", now=NOW)
        assert bootstrap.CANDIDATE_STORE_UNRESOLVED in caught.value.reason_codes

    def test_there_is_no_release_local_fallback_left_in_the_resolver(self):
        """Statements only -- the docstring describes the fallback it
        removed, so a substring search over the whole function matches
        the explanation."""
        import ast

        source = (REPO_ROOT / "market_data" / "candidate_store.py").read_text(encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(source))
                  if isinstance(n, ast.FunctionDef) and n.name == "candidate_dir")
        returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
        # Every return must be an env-derived path; none may be __file__-derived.
        for node in returns:
            assert "__file__" not in ast.dump(node), "release-local fallback survives"
        raises = [n for n in ast.walk(fn) if isinstance(n, ast.Raise)]
        assert len(raises) >= 2, "both unresolved cases must refuse"

    def test_the_fix_is_not_a_gitignore_entry(self):
        """Explicitly forbidden: hiding the dirty tree instead of
        stopping the wrong-path write."""
        gitignore = REPO_ROOT / ".gitignore"
        if gitignore.exists():
            body = gitignore.read_text(encoding="utf-8")
            assert store.MANIFEST_FILE not in body, \
                "the manifest was gitignored instead of the bad write being blocked"


class TestTheLegacyWatchlistPathStillWorks:
    """Live/bootstrap fail closed; the dashboard, health check and paper
    paths must not be collateral damage."""

    def test_symbols_is_tolerant_of_an_unresolved_store(self, monkeypatch):
        monkeypatch.delenv(store.CANDIDATE_DIR_ENV, raising=False)
        monkeypatch.delenv("TRADING_PROJECT_ROOT", raising=False)
        assert store.symbols() == []

    def test_load_watchlist_falls_through_to_the_local_csv(self, monkeypatch, tmp_path):
        import paper_strategy_order as pso

        monkeypatch.delenv(store.CANDIDATE_DIR_ENV, raising=False)
        monkeypatch.delenv("TRADING_PROJECT_ROOT", raising=False)
        local = tmp_path / "order_candidates.csv"
        local.write_text("symbol,price\nLEGACY,1.0\n", encoding="utf-8")
        monkeypatch.setattr(pso, "ORDER_CANDIDATES_FILE", local)
        assert pso.load_watchlist() == ["LEGACY"]

    def test_the_two_reads_have_different_strengths_on_purpose(self):
        """symbols() tolerant, load_verified() strict -- and only the
        strict one can authorise an order."""
        import inspect

        tolerant = inspect.getsource(store.symbols)
        assert "return []" in tolerant
        strict = inspect.getsource(store.load_verified)
        assert "raise" in strict or "read_manifest()" in strict

    def test_the_bootstrap_uses_the_strict_read(self):
        source = (REPO_ROOT / "live_pilot" / "bootstrap.py").read_text(encoding="utf-8")
        body = source.split("def select_candidate", 1)[1].split("\ndef ", 1)[0]
        assert "load_verified" in body
        assert "candidate_store.symbols()" not in body


class TestThePublisherRefusesAnUnresolvedDestination:
    SCRIPT = REPO_ROOT / "scripts" / "publish_candidates.py"

    def test_it_resolves_the_destination_before_reading_the_source(self):
        source = self.SCRIPT.read_text(encoding="utf-8")
        body = source.split("args = parser.parse_args()", 1)[1]
        assert body.index("candidate_path()") < body.index("source.exists()")

    def test_it_exits_non_zero_without_writing(self, tmp_path, monkeypatch):
        import subprocess

        env = dict(os.environ)
        env.pop("KIS_CANDIDATE_DIR", None)
        env.pop("TRADING_PROJECT_ROOT", None)
        env["PYTHONPATH"] = str(REPO_ROOT)
        src = tmp_path / "order_candidates.csv"
        src.write_text("symbol,price\nOMDA,24.5\n", encoding="utf-8")

        before = set(REPO_ROOT.iterdir())
        result = subprocess.run(
            [sys.executable, str(self.SCRIPT), "--source", str(src)],
            capture_output=True, text=True, timeout=120, env=env, cwd=str(tmp_path))

        assert result.returncode == 1, result.stdout
        assert "REFUSED" in result.stdout
        assert set(REPO_ROOT.iterdir()) == before, "the repository was written to"
        assert [p.name for p in tmp_path.iterdir()] == ["order_candidates.csv"]

    def test_a_resolved_destination_still_publishes(self, tmp_path):
        import subprocess

        env = dict(os.environ)
        env.pop("TRADING_PROJECT_ROOT", None)
        env["KIS_CANDIDATE_DIR"] = str(tmp_path / "shared")
        env["PYTHONPATH"] = str(REPO_ROOT)
        src = tmp_path / "order_candidates.csv"
        src.write_text("symbol,price\nOMDA,24.5\n", encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(self.SCRIPT), "--source", str(src)],
            capture_output=True, text=True, timeout=120, env=env, cwd=str(tmp_path))

        assert result.returncode == 0, result.stdout
        assert (tmp_path / "shared" / store.CANDIDATE_FILE).exists()
        assert (tmp_path / "shared" / store.MANIFEST_FILE).exists()

    def test_the_cron_style_invocation_resolves(self, tmp_path):
        """What cron actually does: TRADING_PROJECT_ROOT exported, shared
        dir present beside the release."""
        import subprocess

        release = tmp_path / "releases" / "deadbeef"
        (tmp_path / "releases" / "shared" / "state").mkdir(parents=True)
        release.mkdir(parents=True)
        env = dict(os.environ)
        env.pop("KIS_CANDIDATE_DIR", None)
        env["TRADING_PROJECT_ROOT"] = str(release)
        env["PYTHONPATH"] = str(REPO_ROOT)
        src = tmp_path / "order_candidates.csv"
        src.write_text("symbol,price\nOMDA,24.5\n", encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(self.SCRIPT), "--source", str(src)],
            capture_output=True, text=True, timeout=120, env=env, cwd=str(tmp_path))

        assert result.returncode == 0, result.stdout
        assert (tmp_path / "releases" / "shared" / "state" / store.CANDIDATE_FILE).exists()
        assert not (release / store.CANDIDATE_FILE).exists(), "wrote into the release"
