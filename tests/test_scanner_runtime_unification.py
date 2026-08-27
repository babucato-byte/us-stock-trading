"""The scanner runs the release, or it does not run.

The split this closes
---------------------
`s6_scan.sh` derived the project root from its own location, so the
scanner ran /home/ubuntu/trading while the trading runtime ran an
immutable release. On 2026-08-27 that checkout was still pinned at
5326eac -- roughly twenty commits behind -- and every scanner change
deployed to the release had been inert in production the whole time.
Nothing errored. Both halves reported success about different code.

Falling back is the bug
-----------------------
A missing env file, an absent release, or a scanner SHA that does not
match the deployed one now stops the scan. There is deliberately no
fallback to a checkout: falling back is precisely what produced twenty
commits of silent drift, and a scan that does not run is visible in a
way that one running old code is not.

These drive the real shell script against fixture directories. No
network, no scan, no orders.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_ENV = REPO_ROOT / "deploy" / "cron" / "shared_env.sh"


def _run(script, *, env_file=None, data_root=None, extra=""):
    """Source shared_env.sh and call one function, reporting its exit."""
    body = f"""
set -u
export SCANNER_SHARED_ENV='{env_file or "/nonexistent/env"}'
export SCANNER_DATA_ROOT='{data_root or "/nonexistent/data"}'
{extra}
. '{SHARED_ENV}'
{script}
"""
    return subprocess.run(["bash", "-c", body], capture_output=True, text=True)


@pytest.fixture
def release(tmp_path):
    """A fixture release: a git repo with a venv stub and an env file."""
    root = tmp_path / "release"
    (root / "venv" / "bin").mkdir(parents=True)
    python = root / "venv" / "bin" / "python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    (root / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "x"], check=True)
    sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()

    candidates = tmp_path / "candidates"
    candidates.mkdir()
    data = tmp_path / "data"
    (data / "logs" / "scanners").mkdir(parents=True)
    (data / "universe.csv").write_text("symbol\nAAPL\n")

    env_file = tmp_path / "kis.env"
    env_file.write_text(
        f"TRADING_PROJECT_ROOT={root}\n"
        f"DEPLOYED_COMMIT={sha}\n"
        f"VALIDATED_COMMIT={sha}\n"
        f"SCANNER_CANDIDATE_DIR={candidates}\n")
    return {"root": root, "sha": sha, "env_file": env_file, "data": data,
            "tmp": tmp_path}


class TestItUsesTheReleaseRoot:
    def test_item1_the_root_comes_from_the_shared_env(self, release):
        out = _run("resolve_release_root && echo ROOT=$SCANNER_RUNTIME_ROOT",
                   env_file=release["env_file"], data_root=release["data"])
        assert out.returncode == 0, out.stderr
        assert f"ROOT={release['root']}" in out.stdout

    def test_trading_project_root_is_exported_too(self, release):
        out = _run("resolve_release_root && echo TPR=$TRADING_PROJECT_ROOT",
                   env_file=release["env_file"], data_root=release["data"])
        assert f"TPR={release['root']}" in out.stdout

    def test_item5_a_matching_sha_runs(self, release):
        out = _run("resolve_release_root && echo SHA=$SCANNER_SHA",
                   env_file=release["env_file"], data_root=release["data"])
        assert out.returncode == 0
        assert release["sha"] in out.stdout


class TestItFailsClosed:
    def test_item2_a_missing_shared_env_refuses(self, release):
        out = _run("resolve_release_root", env_file="/nonexistent/env",
                   data_root=release["data"])
        assert out.returncode != 0
        assert "SCANNER_RUNTIME_ROOT_INVALID" in out.stderr

    def test_item3_an_absent_release_refuses_without_falling_back(self, release):
        env_file = release["tmp"] / "bad.env"
        env_file.write_text("TRADING_PROJECT_ROOT=/nonexistent/release\n")
        out = _run("resolve_release_root", env_file=env_file,
                   data_root=release["data"])
        assert out.returncode != 0
        assert "SCANNER_RUNTIME_ROOT_INVALID" in out.stderr
        # The whole point: no checkout is substituted.
        assert "/home/ubuntu/trading" not in out.stdout

    def test_a_release_without_a_venv_refuses(self, release):
        (release["root"] / "venv" / "bin" / "python").unlink()
        out = _run("resolve_release_root", env_file=release["env_file"],
                   data_root=release["data"])
        assert out.returncode != 0
        assert "no venv" in out.stderr

    def test_item4_a_drifted_sha_blocks_the_scan(self, release):
        """The exact failure that went unnoticed for twenty commits."""
        env_file = release["tmp"] / "drift.env"
        env_file.write_text(
            f"TRADING_PROJECT_ROOT={release['root']}\n"
            "DEPLOYED_COMMIT=0000000000000000000000000000000000000000\n"
            "VALIDATED_COMMIT=0000000000000000000000000000000000000000\n")
        out = _run("resolve_release_root", env_file=env_file,
                   data_root=release["data"])
        assert out.returncode != 0
        assert "SCANNER_RELEASE_DRIFT" in out.stderr

    def test_deployed_and_validated_must_both_match(self, release):
        env_file = release["tmp"] / "half.env"
        env_file.write_text(
            f"TRADING_PROJECT_ROOT={release['root']}\n"
            f"DEPLOYED_COMMIT={release['sha']}\n"
            "VALIDATED_COMMIT=0000000000000000000000000000000000000000\n")
        out = _run("resolve_release_root", env_file=env_file,
                   data_root=release["data"])
        assert out.returncode != 0
        assert "SCANNER_RELEASE_DRIFT" in out.stderr


class TestCodeIsTheReleaseDataIsNot:
    """§6 -- running release code must not dirty the release."""

    def test_writes_are_redirected_outside_the_release(self, release):
        out = _run("resolve_release_root && resolve_scanner_data_dirs && "
                   "echo A=$SCANNER_ANALYTICS_DIR && echo U=$SCANNER_UNIVERSE_FILE",
                   env_file=release["env_file"], data_root=release["data"])
        assert out.returncode == 0, out.stderr
        assert str(release["root"]) not in out.stdout
        assert str(release["data"]) in out.stdout

    def test_the_universe_file_lives_in_shared_data(self, release):
        out = _run("resolve_release_root && resolve_scanner_data_dirs && "
                   "echo U=$SCANNER_UNIVERSE_FILE",
                   env_file=release["env_file"], data_root=release["data"])
        assert f"U={release['data']}/universe.csv" in out.stdout

    def test_a_missing_universe_refuses_rather_than_scanning_nothing(self, release):
        (release["data"] / "universe.csv").unlink()
        out = _run("resolve_release_root && resolve_scanner_data_dirs",
                   env_file=release["env_file"], data_root=release["data"])
        assert out.returncode != 0
        assert "SCANNER_UNIVERSE_MISSING" in out.stderr


class TestTheWrapperNoLongerInfersItsRoot:
    def test_s6_scan_calls_the_release_resolver(self):
        text = (REPO_ROOT / "deploy" / "cron" / "s6_scan.sh").read_text(
            encoding="utf-8")
        assert "resolve_release_root" in text
        assert "resolve_scanner_data_dirs" in text

    def test_it_does_not_hardcode_the_checkout(self):
        text = (REPO_ROOT / "deploy" / "cron" / "s6_scan.sh").read_text(
            encoding="utf-8")
        code = "\n".join(line for line in text.splitlines()
                         if not line.strip().startswith("#"))
        assert "/home/ubuntu/trading" not in code

    def test_the_log_is_not_written_into_the_release(self):
        text = (REPO_ROOT / "deploy" / "cron" / "s6_scan.sh").read_text(
            encoding="utf-8")
        assert 'LOG="${SCANNER_DATA_ROOT}/logs/cron/s6_scan.log"' in text

    def test_the_scanner_sha_is_logged(self):
        """So a drift is visible in the log, not only in a refusal."""
        text = (REPO_ROOT / "deploy" / "cron" / "s6_scan.sh").read_text(
            encoding="utf-8")
        assert "scanner_sha=$SCANNER_SHA" in text
