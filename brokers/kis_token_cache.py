"""MEDIUM: one KIS access token, shared by every process on the box.

The token was cached in a KISBroker instance and nowhere else. Each
systemd unit -- reconciliation, health, Shadow entry, Shadow exit,
diagnostics -- is a separate PROCESS, so each one issued a fresh token.
KIS allows one issuance per minute and answers the second with:

    HTTP 403  EGW00133  "접근토큰 발급 잠시 후 다시 시도하세요(1분당 1회)"

which Oracle verification hit for real. Two units firing within a minute
of each other is enough to break one of them.

A KIS token is valid for hours, so the fix is to persist it: a small
JSON file, guarded by an flock, with a double-check inside the lock so a
thundering herd of ten processes issues exactly one token.

What is stored is deliberately minimal, and the App Secret is NEVER
among it. The App Key is stored only as a non-reversible fingerprint,
which exists purely so a cache written under one credential or
environment can never be replayed under another.
"""

import errno
import fcntl
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_REFRESH_SKEW_SECONDS = 300.0
# ORACLE-HIGH-04: a cache whose created_at is in the FUTURE was used
# verbatim. That trusts a stepped clock, a corrupted file, or a token
# copied in from somewhere else. Small skew is tolerated; more is not.
DEFAULT_MAX_CLOCK_SKEW_SECONDS = 5.0
# KIS tokens last 24h; anything claiming much more is not a KIS token.
DEFAULT_MAX_LIFETIME_SECONDS = 90000.0
_LOCK_TIMEOUT_SECONDS = 30.0

REASON_TOKEN_UNAVAILABLE = "KIS_TOKEN_UNAVAILABLE"


class TokenCacheError(Exception):
    """Raised only when the cache itself cannot be operated safely.
    A merely absent or invalid cache is not an error -- it is a miss."""


def cache_file():
    override = os.environ.get("KIS_TOKEN_CACHE_FILE", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return BASE_DIR / "KIS_TOKEN_CACHE.json"


def refresh_skew_seconds():
    raw = os.environ.get("KIS_TOKEN_REFRESH_SKEW_SECONDS", "").strip()
    if not raw:
        return DEFAULT_REFRESH_SKEW_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_REFRESH_SKEW_SECONDS
    return value if value >= 0 else DEFAULT_REFRESH_SKEW_SECONDS


def max_clock_skew_seconds():
    raw = os.environ.get("KIS_TOKEN_MAX_CLOCK_SKEW_SECONDS", "").strip()
    if not raw:
        return DEFAULT_MAX_CLOCK_SKEW_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MAX_CLOCK_SKEW_SECONDS
    return value if value >= 0 else DEFAULT_MAX_CLOCK_SKEW_SECONDS


def max_lifetime_seconds():
    raw = os.environ.get("KIS_TOKEN_MAX_LIFETIME_SECONDS", "").strip()
    if not raw:
        return DEFAULT_MAX_LIFETIME_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MAX_LIFETIME_SECONDS
    return value if value > 0 else DEFAULT_MAX_LIFETIME_SECONDS


def _finite_number(value):
    """A usable timestamp: a real number, not a bool, not NaN, not inf."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if value != value:            # NaN
        return False
    return value not in (float("inf"), float("-inf"))


def app_key_fingerprint(app_key):
    """A non-reversible identifier for the credential. The App Key itself
    is never written to disk -- this only has to distinguish one key from
    another, not reproduce it."""
    if not app_key:
        return ""
    digest = hashlib.sha256(str(app_key).encode("utf-8")).hexdigest()
    return digest[:16]


def _identity(config):
    """The tuple a cached token must match to be reusable at all."""
    return {
        "environment": getattr(config, "kis_env", "") or "",
        "base_url": getattr(config, "base_url", "") or "",
        "app_key_fingerprint": app_key_fingerprint(getattr(config, "app_key", "")),
    }


class KISTokenCache:
    """Process-shared token storage. `clock` is injectable for tests."""

    def __init__(self, *, path=None, clock=None, sleeper=None):
        self._path = Path(path) if path else None
        self._clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        self._sleeper = sleeper or time.sleep

    def _resolve_path(self):
        return self._path if self._path is not None else cache_file()

    def _lock_path(self, path):
        return path.with_name(path.name + ".lock")

    # -- reading ---------------------------------------------------------

    def _load(self, path, identity):
        """Returns a usable token or None. EVERY malformed shape is a
        miss, never a partially-trusted token."""
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("KIS token cache unreadable (%s) -- treating as a miss", exc)
            return None
        if not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            logger.warning("KIS token cache is corrupt -- treating as a miss")
            return None
        if not isinstance(data, dict):
            return None

        token = data.get("access_token")
        expires_at = data.get("expires_at")
        created_at = data.get("created_at")
        if not isinstance(token, str) or not token.strip():
            return None
        if not _finite_number(expires_at) or not _finite_number(created_at):
            # ORACLE-HIGH-04: created_at is REQUIRED and must be a real
            # number. It used not to be checked at all.
            logger.warning("KIS token cache has unusable time fields -- treating as a miss")
            return None

        now = self._clock()
        skew = max_clock_skew_seconds()
        if created_at > now + skew:
            # Issued in the future: a stepped clock, a corrupt file, or a
            # token copied from another host. Never reuse it.
            logger.warning(
                "KIS token cache was created %.1fs in the future -- refusing it",
                created_at - now,
            )
            self._alert_invalid("created_at is in the future")
            return None
        if expires_at <= created_at:
            logger.warning("KIS token cache expires before it was created -- refusing it")
            self._alert_invalid("expires_at <= created_at")
            return None
        if expires_at - created_at > max_lifetime_seconds():
            logger.warning("KIS token cache claims an implausible lifetime -- refusing it")
            self._alert_invalid("lifetime exceeds the maximum")
            return None
        for field, expected in identity.items():
            if str(data.get(field, "")) != str(expected):
                # A different credential or environment. Not an error --
                # just not ours, so it must not be reused.
                return None
        if now >= float(expires_at) - refresh_skew_seconds():
            return None
        return {
            "access_token": token,
            "token_type": data.get("token_type") or "Bearer",
            "expires_at": float(expires_at),
        }

    def _store(self, path, identity, token, token_type, expires_at):
        payload = dict(identity)
        payload.update({
            "access_token": token,
            "token_type": token_type or "Bearer",
            "expires_at": float(expires_at),
            "created_at": self._clock(),
        })
        tmp = path.with_name(path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            os.chmod(path, 0o600)
        except OSError as exc:
            logger.warning("could not persist the KIS token cache: %s", exc)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # -- the public entry point -----------------------------------------

    def get_or_issue(self, config, issue_fn):
        """Returns a valid access token, issuing one only if no usable
        cached token exists.

        `issue_fn()` must return (token, token_type, expires_in_seconds)
        and is called AT MOST ONCE per process per miss, inside the lock,
        after a second cache check -- so ten processes racing from cold
        produce a single issuance.
        """
        identity = _identity(config)
        path = self._resolve_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)
        except OSError:
            pass

        cached = self._load(path, identity)
        if cached is not None:
            return cached["access_token"]

        try:
            lock_handle = open(self._lock_path(path), "a+")
        except OSError as exc:
            # No lock means no cross-process guarantee. Issue anyway --
            # failing to authenticate would be worse than issuing twice --
            # but say so.
            logger.warning("KIS token lock unavailable (%s) -- issuing without sharing", exc)
            token, token_type, expires_in = issue_fn()
            return token

        try:
            os.chmod(self._lock_path(path), 0o600)
        except OSError:
            pass

        try:
            if not self._acquire(lock_handle):
                logger.warning("KIS token lock timed out -- issuing without sharing")
                token, token_type, expires_in = issue_fn()
                return token
            try:
                # Double-check: another process may have issued while we
                # were waiting. This is what collapses the herd to one.
                cached = self._load(path, identity)
                if cached is not None:
                    return cached["access_token"]
                token, token_type, expires_in = issue_fn()
                try:
                    expires_at = self._clock() + max(float(expires_in), 0.0)
                except (TypeError, ValueError):
                    expires_at = self._clock()
                self._store(path, identity, token, token_type, expires_at)
                return token
            finally:
                try:
                    fcntl.flock(lock_handle, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            lock_handle.close()

    def _acquire(self, handle):
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    return False
                if time.monotonic() >= deadline:
                    return False
                self._sleeper(0.05)

    def _alert_invalid(self, reason):
        """Operator-visible, and deliberately says nothing about the token
        itself -- only why the cache was rejected."""
        try:
            from operations import alerts

            alerts.send_alert(
                "*KIS token cache rejected*\n"
                f"- reason: {reason}\n"
                "- action: a new token will be issued; check the host clock if this repeats"
            )
        except Exception as exc:  # noqa: BLE001 -- alerting must not block auth
            logger.debug("could not alert on an invalid token cache: %s", exc)

    def invalidate(self):
        path = self._resolve_path()
        try:
            os.unlink(path)
        except OSError:
            pass


_CACHE = None


def get_cache():
    global _CACHE
    if _CACHE is None:
        _CACHE = KISTokenCache()
    return _CACHE


def reset_cache():
    """Test hook."""
    global _CACHE
    _CACHE = None
