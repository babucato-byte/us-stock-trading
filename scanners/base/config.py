"""Scanner parameters live in files, not in code (spec section 19).

Format: JSON, not YAML
----------------------
Section 19 illustrates the parameter files as YAML. They are shipped
here as JSON instead, for one reason: PyYAML is not installed in this
project's environment and is not in `requirements.txt`, so adopting YAML
would add a new third-party import to a system that places live orders.
A scanner that raises `ModuleNotFoundError` on a server where the
`pip install` step was missed is a worse outcome than a config file with
quotes and braces in it.

JSON is also what the repository already uses for exactly this purpose --
`config/scanner_rules.json` and `config/scanner_presets.json` hold the
existing scanner's parameters -- and section 25 is explicit that
compatibility with the current repository beats the suggested layout.
The parameter names, nesting and values are unchanged from the spec; only
the punctuation differs.

The version/fingerprint pact
----------------------------
Section 19 requires a version bump whenever a parameter changes, and
section 11 requires the parameters to stay frozen through month one.
Both are honour-system rules that nobody notices breaking until the
month-end analysis is already contaminated by a config edit nobody
recorded.

So every config carries a `fingerprint`: a hash of its own parameter
values, computed at load time and written onto every signal the scanner
emits. If someone edits `adx_min` without bumping `version`, month one's
data does not silently become a mixture of two experiments -- the
fingerprint changes mid-month and the analysis can see exactly which
signals came from which parameter set, and on which day it happened.
The fingerprint is descriptive, not enforcing: it never blocks a scan,
it just makes the change impossible to lose.

Lookups are strict
------------------
`ScannerConfig.require()` raises on a missing key rather than falling
back to a default baked into the scanner. A silent code-side default is
precisely the hard-coded parameter section 19 prohibits, and it is worse
than a hard-coded constant because it is invisible: the config file
looks authoritative while the value in force came from somewhere else.
"""

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

SCANNERS_DIR = Path(__file__).resolve().parents[1]

#: Points the loader at an alternative config tree. Intended for tests
#: and for an operator running a calibration set side by side with the
#: production one -- never for changing production values in place.
CONFIG_DIR_ENV = "SCANNER_CONFIG_DIR"

CONFIG_FILENAME = "config.json"


class ScannerConfigError(Exception):
    """A config file is missing, unparseable, or missing a required key.

    Fail-closed: a scanner that cannot read its parameters does not run
    with guessed ones.
    """


def config_root() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV)
    if override and str(override).strip():
        return Path(override)
    return SCANNERS_DIR


def config_path(scanner_dir: str) -> Path:
    return config_root() / scanner_dir / CONFIG_FILENAME


def fingerprint_params(params: Dict[str, Any]) -> str:
    """A stable hash of the parameter values.

    `sort_keys` so that reordering a file by hand is not mistaken for a
    parameter change, and `separators` so whitespace edits are not
    either. Only real value changes move the fingerprint.
    """
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class ScannerConfig:
    """One scanner's parameters, as loaded from disk."""

    scanner_name: str
    version: str
    params: Dict[str, Any] = field(default_factory=dict)
    source: Optional[str] = None

    @property
    def fingerprint(self) -> str:
        return fingerprint_params(self.params)

    def require(self, key: str) -> Any:
        """A parameter that must be present. Raises if it is not."""
        if key not in self.params:
            raise ScannerConfigError(
                f"{self.scanner_name}: required parameter {key!r} missing from "
                f"{self.source or 'config'}")
        return self.params[key]

    def require_float(self, key: str) -> float:
        value = self.require(key)
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ScannerConfigError(
                f"{self.scanner_name}: parameter {key!r} must be a number, got {value!r}"
            ) from exc

    def require_int(self, key: str) -> int:
        value = self.require(key)
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ScannerConfigError(
                f"{self.scanner_name}: parameter {key!r} must be an integer, got {value!r}"
            ) from exc

    def require_bool(self, key: str) -> bool:
        return bool(self.require(key))

    def get(self, key: str, default: Any = None) -> Any:
        """For genuinely optional parameters only.

        Use `require*` for anything that changes which symbols pass. A
        threshold reached through `get` with a default is a hard-coded
        threshold wearing a config file's clothes.
        """
        return self.params.get(key, default)

    def to_manifest(self) -> Dict[str, Any]:
        return {
            "scanner_name": self.scanner_name,
            "scanner_version": self.version,
            "config_fingerprint": self.fingerprint,
            "params": dict(self.params),
            "source": self.source,
        }


def load_config(scanner_dir: str, *, scanner_name: Optional[str] = None) -> ScannerConfig:
    """Read `scanners/<scanner_dir>/config.json`."""
    path = config_path(scanner_dir)
    if not path.exists():
        raise ScannerConfigError(f"no scanner config at {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScannerConfigError(f"scanner config unreadable at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ScannerConfigError(f"scanner config at {path} must be a JSON object")

    version = payload.get("version")
    if not version:
        raise ScannerConfigError(
            f"scanner config at {path} has no 'version'. Section 19 requires a version "
            "that changes whenever a parameter does; an unversioned config would make "
            "month-end results unattributable.")
    params = payload.get("params")
    if not isinstance(params, dict):
        raise ScannerConfigError(f"scanner config at {path} has no 'params' object")

    return ScannerConfig(
        scanner_name=scanner_name or payload.get("scanner_name") or scanner_dir,
        version=str(version),
        params=params,
        source=str(path),
    )
