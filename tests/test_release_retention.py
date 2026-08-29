"""What each release on disk is, and whether anything still needs it.

76 releases at ~311MB each on a disk 83% full. The tempting fix is to
delete the old ones; the reason to inventory first is that a release
directory holds the venv the running crons resolve through
TRADING_PROJECT_ROOT and is the only artifact a rollback can point at.
Deleting the wrong one breaks production silently -- the next cron
resolves a root that no longer exists and refuses to run, which reads as
a scanner fault.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "release_retention_inventory",
    REPO_ROOT / "scripts" / "release_retention_inventory.py")
retention = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(retention)

SHAS = [f"{i:040x}" for i in range(1, 11)]


@pytest.fixture
def releases(tmp_path, monkeypatch):
    root = tmp_path / "releases"
    root.mkdir()
    for index, sha in enumerate(SHAS):
        directory = root / sha
        directory.mkdir()
        (directory / "marker").write_text("x")
        # Oldest first, so SHAS[-1] is the newest.
        import os
        os.utime(directory, (1000 + index * 100, 1000 + index * 100))
    env = tmp_path / "env"
    env.write_text(f"DEPLOYED_COMMIT={SHAS[-1]}\nVALIDATED_COMMIT={SHAS[-1]}\n")
    monkeypatch.setattr(retention, "ENV_FILE", env)
    return root


class TestNothingIsDeleted:
    def test_the_tool_has_no_removal_call(self):
        source = (REPO_ROOT / "scripts"
                  / "release_retention_inventory.py").read_text()
        for forbidden in ("rmtree", "os.remove", "unlink", "rm -rf",
                          "shutil.rm"):
            assert forbidden not in source, forbidden

    def test_the_inventory_leaves_every_directory_in_place(self, releases):
        retention.inventory(releases_dir=releases, with_sizes=False)
        assert len(list(releases.iterdir())) == len(SHAS)


class TestWhatIsProtected:
    def test_the_deployed_release_is_protected(self, releases):
        report = retention.inventory(releases_dir=releases, with_sizes=False)
        row = next(r for r in report["rows"] if r["sha"] == SHAS[-1])
        assert retention.PROTECTED_DEPLOYED in row["roles"]
        assert row["disposition"] == "PROTECTED"

    def test_the_validated_release_is_protected(self, releases):
        report = retention.inventory(releases_dir=releases, with_sizes=False)
        row = next(r for r in report["rows"] if r["sha"] == SHAS[-1])
        assert retention.PROTECTED_VALIDATED in row["roles"]

    def test_a_rollback_target_is_named_explicitly(self, releases):
        """The newest release that is neither deployed nor validated is
        where a rollback goes; it must never be a candidate by
        accident."""
        report = retention.inventory(releases_dir=releases, with_sizes=False)
        rollback = [r for r in report["rows"]
                    if retention.PROTECTED_ROLLBACK in r["roles"]]
        assert len(rollback) == 1
        assert rollback[0]["sha"] == SHAS[-2]
        assert rollback[0]["disposition"] == "PROTECTED"

    def test_the_most_recent_n_are_protected(self, releases):
        report = retention.inventory(releases_dir=releases, keep_recent=4,
                                     with_sizes=False)
        recent = [r["sha"] for r in report["rows"]
                  if retention.PROTECTED_RECENT in r["roles"]]
        assert set(recent) == set(SHAS[-4:])

    def test_older_releases_become_candidates(self, releases):
        report = retention.inventory(releases_dir=releases, keep_recent=3,
                                     with_sizes=False)
        candidates = [r["sha"] for r in report["rows"]
                      if r["disposition"] == retention.PRUNE_CANDIDATE]
        assert SHAS[0] in candidates
        assert SHAS[-1] not in candidates

    def test_candidate_means_eligible_for_review_not_scheduled(self, releases):
        report = retention.inventory(releases_dir=releases, keep_recent=3,
                                     with_sizes=False)
        assert report["prune_candidates"] > 0
        # Still all present afterwards.
        assert len(list(releases.iterdir())) == len(SHAS)


class TestTheReportIsUsable:
    def test_it_counts_protected_and_candidates(self, releases):
        report = retention.inventory(releases_dir=releases, keep_recent=3,
                                     with_sizes=False)
        assert report["protected"] + report["prune_candidates"] \
            == report["releases"] == len(SHAS)

    def test_a_missing_directory_is_not_an_error(self, tmp_path):
        report = retention.inventory(releases_dir=tmp_path / "nope",
                                     with_sizes=False)
        assert report["releases"] == 0

    def test_non_sha_directories_are_ignored(self, releases):
        (releases / "shared").mkdir()
        (releases / "current").mkdir()
        report = retention.inventory(releases_dir=releases, with_sizes=False)
        assert report["releases"] == len(SHAS)

    def test_an_unreadable_env_leaves_nothing_deployed(self, releases,
                                                       monkeypatch):
        """And then nothing claims the deployed role -- but the recent
        and rollback protections still apply, so a broken env file
        cannot turn the live release into a candidate."""
        monkeypatch.setattr(retention, "ENV_FILE", releases / "absent")
        report = retention.inventory(releases_dir=releases, keep_recent=5,
                                     with_sizes=False)
        newest = next(r for r in report["rows"] if r["sha"] == SHAS[-1])
        assert newest["disposition"] == "PROTECTED"
