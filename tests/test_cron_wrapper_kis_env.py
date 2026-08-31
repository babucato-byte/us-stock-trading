"""Every wrapper that talks to KIS must load the KIS environment.

What happened
-------------
`s6_scan.sh` sourced `shared_env.sh` and nothing else. `shared_env.sh`
resolves the release root and the data directories; it deliberately
carries no credentials. So the scan cron ran with

    KIS_APP_KEY ABSENT   KIS_APP_SECRET ABSENT
    KIS_ACCOUNT_NO ABSENT   KIS_ENV ABSENT

and the failure was invisible. `KISBroker()` still CONSTRUCTS without
credentials, so the report printed `provider : kis` and `status:
SUCCESS`; only the per-symbol chart requests failed, at authentication,
and each one became a DATA_ERROR. Four premarket symbols, four
DATA_ERRORs, and it read as a market with no setups.

Measured on 2026-08-31, same command, same minute:

    without the env   signals=0 candidates=0 DATA_ERROR=4   2.1s
    with the env      signals=1 candidates=1 DATA_ERROR=1  14.4s

The duration was the tell: four KIS fetches cannot finish in 2.1s
because they never happened.

No real secret is needed to test any of this -- the question is whether
the wrapper LOADS the authority, not what the authority contains.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CRON_DIR = REPO_ROOT / "deploy" / "cron"

#: Wrappers that make authenticated KIS calls. Each must load the same
#: environment authority, in the same way.
KIS_WRAPPERS = (
    "s6_scan.sh",
    "s6_buy_entry.sh",
    "s6_realtime_collector.sh",
    "reconciliation.sh",
)

ENV_PATH = "/home/ubuntu/releases/us-stock-trading/shared/env/kis-readonly.env"


def _source(name):
    return (CRON_DIR / name).read_text()


class TestEveryKISWrapperLoadsTheEnvironment:
    @pytest.mark.parametrize("wrapper", KIS_WRAPPERS)
    def test_it_names_the_shared_env_file(self, wrapper):
        assert ENV_PATH in _source(wrapper), (
            f"{wrapper} does not load the KIS environment; its KIS calls "
            "will fail at authentication and every symbol will look like a "
            "data error")

    @pytest.mark.parametrize("wrapper", KIS_WRAPPERS)
    def test_it_exports_rather_than_merely_reading(self, wrapper):
        """`set -a` is what makes the values reach the child process. A
        plain `. file` would define them in the wrapper and leave the
        python process without them."""
        source = _source(wrapper)
        assert re.search(r"set -a;\s*\.\s*\"\$ENV_FILE\"", source), wrapper

    @pytest.mark.parametrize("wrapper", KIS_WRAPPERS)
    def test_an_unreadable_env_stops_the_wrapper(self, wrapper):
        """Running without credentials is the failure this file exists
        for; refusing to start is visible, and running is not."""
        assert '[ -r "$ENV_FILE" ] || exit 1' in _source(wrapper), wrapper

    @pytest.mark.parametrize("wrapper", KIS_WRAPPERS)
    def test_the_env_is_loaded_before_shared_env(self, wrapper):
        """Same order everywhere, so one wrapper cannot drift into
        resolving a root before it has the credentials that root needs."""
        source = _source(wrapper)
        assert source.index("ENV_FILE=") < source.index("shared_env.sh\"")

    @pytest.mark.parametrize("wrapper", KIS_WRAPPERS)
    def test_it_is_valid_shell(self, wrapper):
        done = subprocess.run(["bash", "-n", str(CRON_DIR / wrapper)])
        assert done.returncode == 0


class TestNoSecretIsEmbeddedOrPrinted:
    @pytest.mark.parametrize("wrapper", KIS_WRAPPERS)
    def test_no_credential_value_is_hardcoded(self, wrapper):
        source = _source(wrapper)
        for name in ("KIS_APP_KEY=", "KIS_APP_SECRET=", "KIS_ACCOUNT_NO="):
            # Naming the variable is fine; assigning a literal is not.
            for line in source.splitlines():
                if line.strip().startswith(name):
                    pytest.fail(f"{wrapper} assigns {name} literally")

    @pytest.mark.parametrize("wrapper", KIS_WRAPPERS)
    def test_no_credential_is_echoed(self, wrapper):
        source = _source(wrapper)
        for line in source.splitlines():
            if line.strip().startswith(("echo", "printf")):
                for name in ("APP_KEY", "APP_SECRET", "ACCOUNT_NO", "TOKEN"):
                    assert name not in line, f"{wrapper}: {line.strip()[:60]}"


class TestSharedEnvIsNotACredentialSource:
    """The mistake was assuming `shared_env.sh` covered this. It does
    not, on purpose: it resolves the release root and data dirs."""

    def test_shared_env_carries_no_credentials(self):
        source = (CRON_DIR / "shared_env.sh").read_text()
        for name in ("KIS_APP_KEY", "KIS_APP_SECRET"):
            assert name not in source

    def test_sourcing_shared_env_alone_provides_nothing(self, tmp_path):
        """Reproduces the broken wrapper's environment exactly."""
        script = tmp_path / "probe.sh"
        script.write_text(
            f'set -u\n. "{CRON_DIR}/shared_env.sh" >/dev/null 2>&1\n'
            'for v in KIS_APP_KEY KIS_APP_SECRET KIS_ACCOUNT_NO KIS_ENV; do\n'
            '  if [ -n "${!v:-}" ]; then echo "$v PRESENT"; '
            'else echo "$v ABSENT"; fi\ndone\n')
        done = subprocess.run(["bash", str(script)], capture_output=True,
                              text=True, env={"PATH": "/usr/bin:/bin",
                                              "HOME": str(tmp_path)})
        assert "KIS_APP_KEY ABSENT" in done.stdout
        assert "KIS_APP_SECRET ABSENT" in done.stdout


class TestLoadingTheEnvActuallyExportsIt:
    def test_the_pattern_exports_to_a_child_process(self, tmp_path):
        """The wrapper's own mechanism, against a FAKE env file. Proves
        the `set -a` + source pattern reaches a child, without needing a
        real secret."""
        fake_env = tmp_path / "kis.env"
        fake_env.write_text(
            "KIS_APP_KEY=not-a-real-key\nKIS_APP_SECRET=not-a-real-secret\n"
            "KIS_ACCOUNT_NO=00000000\nKIS_ENV=paper\n")
        script = tmp_path / "wrapper.sh"
        script.write_text(
            f'set -u\nENV_FILE="{fake_env}"\n'
            '[ -r "$ENV_FILE" ] || exit 1\n'
            'set -a; . "$ENV_FILE"; set +a\n'
            'bash -c \'for v in KIS_APP_KEY KIS_APP_SECRET KIS_ACCOUNT_NO '
            'KIS_ENV; do if [ -n "${!v:-}" ]; then echo "$v PRESENT"; '
            'else echo "$v ABSENT"; fi; done\'\n')
        done = subprocess.run(["bash", str(script)], capture_output=True,
                              text=True)
        for name in ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO",
                     "KIS_ENV"):
            assert f"{name} PRESENT" in done.stdout
        # Presence only -- never the value.
        assert "not-a-real-key" not in done.stdout

    def test_a_missing_env_file_exits_nonzero(self, tmp_path):
        script = tmp_path / "wrapper.sh"
        script.write_text(
            f'set -u\nENV_FILE="{tmp_path}/absent.env"\n'
            '[ -r "$ENV_FILE" ] || exit 1\n'
            'set -a; . "$ENV_FILE"; set +a\necho REACHED\n')
        done = subprocess.run(["bash", str(script)], capture_output=True,
                              text=True)
        assert done.returncode == 1
        assert "REACHED" not in done.stdout


class TestTheScannerGainsNoTradingCapability:
    """Credentials let the scan READ market data. They must not turn the
    scanner package into something that can place an order."""

    def test_the_scanner_package_still_imports_no_broker(self):
        source = (REPO_ROOT / "scanners" / "runner.py").read_text()
        assert "brokers" not in source
        assert "KISBroker" not in source

    def test_provider_injection_is_still_outside_the_package(self):
        entry = (REPO_ROOT / "scripts" / "run_scanners.py").read_text()
        assert "session_provider" in entry
        assert "main(provider=session_provider())" in entry

    def test_the_scan_wrapper_places_no_order(self):
        source = _source("s6_scan.sh")
        for forbidden in ("run_live_buy_entry", "kis_live_trading",
                          "submit_order", "run_s6_runtime"):
            assert forbidden not in source

    def test_the_scan_still_runs_the_scanner_entrypoint(self):
        assert "scripts/run_scanners.py" in _source("s6_scan.sh")
