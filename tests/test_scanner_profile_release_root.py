"""The scanner profiles must run from the immutable release.

What was wrong
--------------
The premarket / open / daily profiles ran as

    cd /home/ubuntu/trading && env TRADING_PROJECT_ROOT=/home/ubuntu/trading \\
      venv/bin/python scripts/run_scanners.py --profile daily

against a tree at ecb906b14 with four uncommitted modifications, while
the deployed release was ad7019c7d. The activity ranking every S6
session depends on was produced by unvalidated code from a mutable
checkout.

It also landed where the release could not read it: the ranking goes to
<root>/logs/scanners/activity unless SCANNER_ANALYTICS_DIR overrides,
the legacy tree set no override and the release sets one. So S6 reported
"no active universe available" while 2MB of ranking covering 10,564
symbols sat two paths away.
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "deploy" / "cron" / "scanner_profile.sh"


def _source():
    return WRAPPER.read_text()


class TestTheWrapperExistsAndIsRunnable:
    def test_it_is_executable(self):
        assert WRAPPER.exists()
        assert os.stat(WRAPPER).st_mode & stat.S_IXUSR

    def test_it_is_valid_shell(self):
        assert subprocess.run(["bash", "-n", str(WRAPPER)]).returncode == 0

    def test_it_refuses_an_unknown_profile(self):
        done = subprocess.run(["bash", str(WRAPPER), "bogus"],
                              capture_output=True, text=True)
        assert done.returncode == 2
        assert "unknown profile" in done.stderr

    def test_it_requires_a_profile(self):
        done = subprocess.run(["bash", str(WRAPPER)], capture_output=True,
                              text=True)
        assert done.returncode != 0

    @pytest.mark.parametrize("profile", ["premarket", "open", "daily"])
    def test_every_real_profile_is_accepted(self, profile):
        assert f"{profile}" in _source()


class TestItRunsTheDeployedRelease:
    def test_it_resolves_the_release_root_rather_than_hardcoding_one(self):
        """`resolve_release_root` is what enforces
        SCANNER == DEPLOYED == VALIDATED and refuses on drift."""
        assert "resolve_release_root" in _source()

    def test_it_never_points_at_the_legacy_tree(self):
        source = _source()
        run_lines = [line for line in source.splitlines()
                     if "TRADING_PROJECT_ROOT=" in line
                     and not line.strip().startswith("#")]
        assert run_lines
        for line in run_lines:
            assert "/home/ubuntu/trading" not in line

    def test_the_interpreter_comes_from_the_release(self):
        """One venv, the release's -- a profile quietly using a different
        interpreter is the same class of split as the code."""
        assert '"$SCANNER_RUNTIME_ROOT/venv/bin/python"' in _source()

    def test_it_records_the_sha_it_ran(self):
        assert "sha=$SCANNER_SHA" in _source()


class TestOutputsGoToSharedState:
    def test_the_analytics_dir_is_passed_explicitly(self):
        """So the ranking lands where every reader already looks, rather
        than in whichever tree the process happened to start in."""
        assert 'SCANNER_ANALYTICS_DIR="$SCANNER_ANALYTICS_DIR"' in _source()

    def test_the_log_lives_in_shared_state_not_the_release(self):
        """Running code must not dirty the release it runs from."""
        assert 'LOG="${SCANNER_DATA_ROOT}' in _source()

    def test_it_resolves_the_shared_data_dirs(self):
        assert "resolve_scanner_data_dirs" in _source()


class TestOverlapIsDistinguishableFromFailure:
    def test_each_profile_takes_its_own_lock(self):
        """The daily walk is long and must not start twice, but must not
        block the premarket refresh either."""
        assert 'scanner_${PROFILE}.lock' in _source()

    def test_a_skipped_overlap_exits_zero_and_says_so(self):
        source = _source()
        assert "-E 99" in source
        assert "OVERLAP_SKIPPED" in source

    def test_a_completed_pass_records_its_status(self):
        assert "PROFILE_COMPLETE" in _source()


class TestTheReaderFindsWhatTheProfileWrites:
    def test_discovery_searches_the_shared_analytics_dir(self):
        """The two halves have to agree on the location, or the split
        that caused this comes straight back."""
        from s6_live import session_discovery as sd

        paths = sd.activity_search_paths(env={
            "SCANNER_ANALYTICS_DIR": "/shared/logs/scanners",
            "SCANNER_DATA_ROOT": "/shared",
        })
        assert Path("/shared/logs/scanners/activity") in paths

    def test_shared_state_is_searched_before_the_legacy_bridge(self):
        from s6_live import session_discovery as sd

        paths = sd.activity_search_paths(env={
            "SCANNER_ANALYTICS_DIR": "/shared/logs/scanners",
            "SCANNER_LEGACY_ANALYTICS_DIR": "/legacy/logs/scanners",
        })
        assert paths.index(Path("/shared/logs/scanners/activity")) \
            < paths.index(Path("/legacy/logs/scanners/activity"))
