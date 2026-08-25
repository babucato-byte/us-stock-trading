"""Why a symbol produced no bar -- counted, not guessed.

"The provider refused us" and "this symbol does not trade" are opposite
findings that arrive looking identical: an empty row either way. A scan
that reports 47% coverage without separating them cannot tell whether it
is being throttled or whether half its universe is delisted, and those
have opposite fixes -- fetch less versus fetch different.

The provider does distinguish them; it just does so in its own log
rather than in what it returns. `yf.download` swallows a rate limit,
writes a line, and hands back an empty frame. So the counts come from
attaching a handler to that logger for the duration of the pass, which
is the only place the distinction actually exists.

Categories are chosen by what an operator would DO about each:

  RATE_LIMIT              fetch fewer symbols, or slower
  DATA_UNAVAILABLE        nothing -- delisted, halted, never traded
  NETWORK_RESOURCE_ERROR  concurrency or DNS; bound the workers
  LOCAL_DB_ERROR          the provider's own cache is unwritable
  PROVIDER_INTERNAL_ERROR the provider broke; retry later
  UNCLASSIFIED            add a pattern rather than assume

UNCLASSIFIED is a real answer for the same reason it is in
`reject_reasons`: a message this table does not recognise must be
reported as unrecognised, because a wrong bucket is worse than a missing
one when the point is counting.
"""

import logging
import re
from typing import Dict

RATE_LIMIT = "RATE_LIMIT"
DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
NETWORK_RESOURCE_ERROR = "NETWORK_RESOURCE_ERROR"
LOCAL_DB_ERROR = "LOCAL_DB_ERROR"
PROVIDER_INTERNAL_ERROR = "PROVIDER_INTERNAL_ERROR"
UNCLASSIFIED = "UNCLASSIFIED"

#: Matched against the provider's own message. First match wins, so the
#: more specific pattern is listed first.
_PATTERNS = (
    (RATE_LIMIT, re.compile(r"rate limit|too many requests|yfratelimit",
                            re.IGNORECASE)),
    (LOCAL_DB_ERROR, re.compile(r"unable to open database|database is locked|"
                                r"operationalerror", re.IGNORECASE)),
    (NETWORK_RESOURCE_ERROR, re.compile(
        r"getaddrinfo|can't start new thread|thread failed|timed out|"
        r"connection (reset|refused|aborted)|max retries", re.IGNORECASE)),
    (DATA_UNAVAILABLE, re.compile(
        r"possibly delisted|no price data found|no data found|not found|"
        r"quote not found|404", re.IGNORECASE)),
    (PROVIDER_INTERNAL_ERROR, re.compile(
        r"nonetype|typeerror|keyerror|attributeerror|500|502|503",
        re.IGNORECASE)),
)


def classify(message) -> str:
    text = "" if message is None else str(message)
    if not text.strip():
        return UNCLASSIFIED
    for category, pattern in _PATTERNS:
        if pattern.search(text):
            return category
    return UNCLASSIFIED


class ProviderFailureCounter(logging.Handler):
    """Tallies the provider's own log lines by category.

    Installed on the `yfinance` logger for the length of a pass and
    removed afterwards. It counts; it never suppresses -- a handler that
    swallowed the provider's errors would trade one blindness for
    another.
    """

    def __init__(self, level=logging.WARNING):
        super().__init__(level=level)
        self.counts: Dict[str, int] = {}
        self.samples: Dict[str, str] = {}

    def emit(self, record) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a counter must not raise into
            return                                      # the thing it counts
        category = classify(message)
        self.counts[category] = self.counts.get(category, 0) + 1
        self.samples.setdefault(category, message[:160])

    def summary(self) -> Dict[str, int]:
        return dict(sorted(self.counts.items(), key=lambda kv: -kv[1]))


class capture:
    """Context manager: `with capture() as failures: ...`."""

    def __init__(self, logger_name="yfinance"):
        self._logger = logging.getLogger(logger_name)
        self.counter = ProviderFailureCounter()

    def __enter__(self) -> ProviderFailureCounter:
        self._logger.addHandler(self.counter)
        return self.counter

    def __exit__(self, *exc) -> bool:
        self._logger.removeHandler(self.counter)
        return False


def use_project_cache(directory) -> bool:
    """Point the provider's cache at a writable, project-local path.

    Two scans running at once share the provider's default cache and
    have been observed to produce `unable to open database file`. That
    is a local-resource fault dressed as a data fault -- the symbols are
    fine and nothing was fetched.

    Returns whether the redirect took. The cache is NOT disabled on
    failure: turning it off would hide rate limiting behind extra
    requests, which is the opposite of what these counts are for.
    """
    try:
        from pathlib import Path

        import yfinance as yf

        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        if hasattr(yf, "set_tz_cache_location"):
            yf.set_tz_cache_location(str(target))
            return True
    except Exception:  # noqa: BLE001 - the default cache still works
        logging.getLogger(__name__).warning(
            "could not redirect the provider cache to %s", directory,
            exc_info=True)
    return False
