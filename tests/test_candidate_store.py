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
