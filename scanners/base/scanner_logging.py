"""Per-scanner log files (spec section 26).

One file per scanner, because the six run in one process and an
interleaved log is unreadable when the question is "why did the
breakout scanner reject everything today". Handlers are attached to
`scanners.<name>` loggers and marked so repeated setup calls -- the
runner sets up, then each scanner does too -- do not stack duplicate
handlers and write every line six times.

Propagation is left ON. Whatever the calling process already does with
root logging (systemd journal, a cron redirect) keeps receiving these
records; the file is an addition, not a replacement.

Every line carries the fields section 26 asks for -- scanner, version,
symbol, result, reason, timestamp -- via `log_decision`, so the log is
greppable as data rather than prose.
"""

import logging
import os
from pathlib import Path
from typing import Optional

from config.paths import get_project_root

#: Redirects the whole scanner log tree. Tests point this at tmp_path;
#: an operator could point it at a volume with more room.
LOG_DIR_ENV = "SCANNER_LOG_DIR"

LOGGER_PREFIX = "scanners"

_HANDLER_TAG = "_scanner_file_handler"

FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def log_dir() -> Path:
    override = os.environ.get(LOG_DIR_ENV)
    if override and str(override).strip():
        return Path(override)
    return Path(get_project_root()) / "logs" / "scanners"


def logger_name(scanner_name: str) -> str:
    return f"{LOGGER_PREFIX}.{scanner_name}"


def get_scanner_logger(scanner_name: str, *, level: int = logging.INFO) -> logging.Logger:
    """The logger for one scanner, with its own file handler attached.

    A failure to open the log file is swallowed deliberately: losing the
    log is bad, but taking a scan down because a disk is full or a
    directory is read-only would be worse, and the records still reach
    whatever the root logger is doing.
    """
    log = logging.getLogger(logger_name(scanner_name))
    log.setLevel(level)
    if any(getattr(handler, _HANDLER_TAG, None) == scanner_name for handler in log.handlers):
        return log
    try:
        directory = log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(directory / f"{scanner_name}.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter(FORMAT))
        setattr(handler, _HANDLER_TAG, scanner_name)
        log.addHandler(handler)
    except OSError:
        log.debug("scanner log file unavailable for %s; console logging only", scanner_name)
    return log


def log_decision(
    log: logging.Logger,
    *,
    scanner: str,
    version: str,
    symbol: str,
    result: str,
    reason: str,
    level: Optional[int] = None,
) -> None:
    """One structured decision line.

    PASS lines are INFO; FAIL lines are DEBUG by default. An 800-name
    universe produces ~800 FAIL lines per scanner per run, and six
    scanners of that at INFO would bury the handful of passes that
    matter. The FAIL reasons are still written whenever the level is
    lowered, which is what calibration in month two needs.
    """
    if level is None:
        level = logging.INFO if result == "PASS" else logging.DEBUG
    log.log(level, "scanner=%s version=%s symbol=%s result=%s reason=%s",
            scanner, version, symbol, result, reason)
