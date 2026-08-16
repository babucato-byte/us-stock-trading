"""Where a watchlist file lives, and the only place this package writes.

One directory, `logs/watchlist/`, overridable with
`MANUAL_WATCHLIST_DIR` for tests. Two files per trading day:

    <day>.tomorrow.json   built the evening before, no Slack
    <day>.today.json      built in the morning, the one that is posted
    <day>.today.md        the same content, readable without a JSON tool

Both are keyed by the day the list is FOR, not the day it was built. A
Tomorrow Watchlist produced on Monday evening for Tuesday is filed under
Tuesday, so the morning pass reads `<today>.tomorrow.json` without
having to work out which prior session produced it -- and so the pair of
files for one trading day sit next to each other.

Writes are atomic (temp -> fsync -> os.replace). A reader that opens the
file while it is being rewritten must see the whole previous list or the
whole new one; a truncated watchlist does not look like an error, it
looks like a shorter list, which is the failure mode worth paying a few
lines to prevent.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.paths import get_project_root
from watchlist import config


class WatchlistStoreError(Exception):
    """A watchlist read or write failed."""


def watchlist_dir() -> Path:
    override = os.environ.get(config.WATCHLIST_DIR_ENV)
    if override and str(override).strip():
        return Path(override)
    return Path(get_project_root()).joinpath(*config.WATCHLIST_SUBDIR)


def _ensure_dir() -> Path:
    path = watchlist_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def path_for(trading_day: str, stage: str, suffix: str = "json") -> Path:
    return _ensure_dir() / f"{trading_day}.{stage}.{suffix}"


def _atomic_write(path: Path, text: str) -> None:
    directory = path.parent
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(directory), prefix=f".{path.name}.", delete=False)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except OSError as exc:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise WatchlistStoreError(f"cannot write {path}: {exc}") from exc


def write_json(payload: Dict[str, Any], *, trading_day: str, stage: str) -> str:
    path = path_for(trading_day, stage, "json")
    _atomic_write(path, json.dumps(payload, indent=2, sort_keys=True,
                                   ensure_ascii=False, default=str) + "\n")
    return str(path)


def write_text(body: str, *, trading_day: str, stage: str) -> str:
    path = path_for(trading_day, stage, "md")
    _atomic_write(path, body if body.endswith("\n") else body + "\n")
    return str(path)


def read_json(trading_day: str, stage: str) -> Optional[Dict[str, Any]]:
    """The stored watchlist, or None when there is not one.

    None rather than an exception for a missing file: the morning pass
    legitimately runs on a day with no Tomorrow Watchlist (the first day
    of the month, a day after a holiday, a day the evening scan failed),
    and that is a normal branch rather than an error.
    """
    path = path_for(trading_day, stage, "json")
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WatchlistStoreError(f"cannot read {path}: {exc}") from exc


def available_days(stage: str) -> List[str]:
    directory = watchlist_dir()
    if not directory.is_dir():
        return []
    return sorted(path.name.split(".")[0]
                  for path in directory.glob(f"*.{stage}.json"))
