"""CODEX-050: the ONE place this codebase decides what a secret looks
like and how it is masked. Everything that can surface a value to a log,
an exception message, a durable Shadow Mode record, an order-state event
payload or an HTTP error goes through here.

Three layers, applied independently, so a miss in one is caught by
another (the "세 단계 방어" the directive requires):

  1. call-site redaction -- `redact_value()` / `redact_text()` /
     `mask_account_number()` wherever a value is built into a message;
  2. logging-boundary redaction -- `install_logging_redaction()` adds a
     `RedactingFilter` to the root logger, so even an un-redacted call
     site cannot emit a raw secret through `logging`;
  3. persistence redaction -- shadow_mode.py, shadow_audit.py and
     execution/order_repository.py all re-run `redact_value()` on
     whatever they are about to write to disk.

What Codex found still leaking, and what changed:

  - `Authorization: Bearer <token>` masked only the word "Bearer" and
    left the token. Bearer/Basic/Token schemes are now matched
    explicitly and BEFORE the generic key=value pass, so the credential
    itself is what gets masked.
  - A Python dict repr (`{'CANO': '12345678'}`) matched nothing, because
    the pattern only understood `key=value` and JSON's `"key": "value"`.
    Single-quoted keys/values are now handled too.
  - A raw KIS response dict/row interpolated into an exception
    (`f"... {output!r}"`) carried whatever KIS put in it. Callers now use
    `safe_repr()`, which runs the structure through `redact_value()`
    before formatting it.
  - Bare account numbers appearing in free text with no key at all are
    masked by `redact_text()` via `mask_known_account_numbers()`, which
    is seeded from the configured account numbers.

Account identity inside the application uses an ALIAS or a keyed
FINGERPRINT, never the raw number: `account_fingerprint()` is
HMAC-SHA256 keyed with a local secret, not a bare SHA-256, because the
account-number space is small enough to brute-force an unkeyed digest.
"""

import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import asdict, is_dataclass

REDACTED = "***REDACTED***"

_SENSITIVE_KEY_SUBSTRINGS = (
    "appkey", "app_key", "appsecret", "app_secret", "authorization",
    "accesstoken", "access_token", "accountnumber", "account_number", "cano",
    "acct_no", "account_no", "token", "secret", "password", "credential",
)

# Keys that merely CONTAIN a sensitive substring but are themselves safe
# and load-bearing for debugging. Without this, e.g. "account_alias" (a
# deliberately non-secret identifier) would be masked into uselessness.
_SAFE_KEY_EXACTS = frozenset({
    "accountalias", "accountfingerprint", "accountlast4", "accountidmasked",
    "tokenexpiresat", "tokentype",
    # The OBSERVE gate's live-authorization VERDICT ("WOULD_APPROVE" /
    # "LIVE_BLOCKED:SYMBOL"). It contains the word "authorization" and no
    # credential; masking it turned the audit trail's most load-bearing
    # field into ***REDACTED***. Exact match only -- a key actually named
    # "authorization" is still masked.
    "liveauthorizationresult",
})


def _normalize_key(key):
    return str(key).strip().lower().replace("-", "").replace("_", "")


def _is_sensitive_key(key):
    normalized = _normalize_key(key)
    if normalized in _SAFE_KEY_EXACTS:
        return False
    return any(_normalize_key(s) in normalized for s in _SENSITIVE_KEY_SUBSTRINGS)


def mask_account_number(account_no):
    """Shows only the last 4 characters -- everything before that is
    masked. A None/empty input passes through unchanged (nothing to
    mask); anything 4 characters or shorter is masked entirely (no safe
    "last 4" to reveal)."""
    if not account_no:
        return account_no
    text = str(account_no)
    if len(text) <= 4:
        return "*" * len(text)
    return "*" * (len(text) - 4) + text[-4:]


def account_last4(account_no):
    if not account_no:
        return ""
    return str(account_no)[-4:]


def _fingerprint_key():
    """The HMAC key. An operator sets ACCOUNT_FINGERPRINT_SECRET; without
    it this falls back to a process-local random key, which still makes
    the fingerprint non-invertible -- it just isn't stable across
    restarts, which is the correct failure mode (a stable fingerprint
    derived from a guessable key would be worse than an unstable one)."""
    secret = os.environ.get("ACCOUNT_FINGERPRINT_SECRET")
    if secret:
        return secret.encode("utf-8")
    global _EPHEMERAL_KEY
    if _EPHEMERAL_KEY is None:
        _EPHEMERAL_KEY = os.urandom(32)
    return _EPHEMERAL_KEY


_EPHEMERAL_KEY = None


def account_fingerprint(account_no, *, length=16):
    """Keyed, non-invertible identifier for an account number, for use
    as an internal correlation id in logs/records. HMAC-SHA256 rather
    than a plain SHA-256: the account-number space is small enough that
    an unkeyed digest can simply be enumerated back to the original."""
    if not account_no:
        return ""
    digest = hmac.new(_fingerprint_key(), str(account_no).encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:length]


def account_alias():
    """Operator-configured, non-secret name for the trading account --
    what application code should log instead of any form of the real
    number."""
    return os.environ.get("KIS_ACCOUNT_ALIAS", "kis-primary")


def redact_value(value, _depth=0):
    """Recursively redacts dicts/lists/tuples/sets/dataclasses by KEY
    name (case-insensitive, substring match, nested included). Non-
    container leaves are passed through `redact_text()` so a secret
    embedded in a string value is still masked. Never raises -- an
    unrecognized structure is returned as-is rather than blocking the
    caller."""
    if _depth > 12:  # pathological nesting / cycles
        return value
    if isinstance(value, dict):
        return {
            key: (REDACTED if _is_sensitive_key(key) else redact_value(val, _depth + 1))
            for key, val in value.items()
        }
    if is_dataclass(value) and not isinstance(value, type):
        try:
            return redact_value(asdict(value), _depth + 1)
        except (TypeError, ValueError):
            return redact_text(repr(value))
    if isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[0], str) \
            and _is_sensitive_key(value[0]):
        # A bare (key, value) pair -- e.g. an items() tuple that escaped
        # its mapping. Without this it would fall through as two
        # unrelated strings and the value would survive.
        pair = [value[0], REDACTED]
        return tuple(pair) if isinstance(value, tuple) else pair
    if isinstance(value, (list, tuple, set, frozenset)):
        redacted = [redact_value(item, _depth + 1) for item in value]
        if isinstance(value, tuple):
            return tuple(redacted)
        if isinstance(value, (set, frozenset)):
            return type(value)(redacted)
        return redacted
    if isinstance(value, BaseException):
        return redact_text(f"{type(value).__name__}: {value}")
    if isinstance(value, str):
        return redact_text(value)
    return value


_KEY_ALTERNATION = (
    r"app_?key|app_?secret|authorization|access_?token|refresh_?token|account_?number|"
    r"account_?no|acct_?no|cano|token|secret|password|credential"
)

# `key=value`, `key: value`, `"key": "value"` and Python's own
# `'key': 'value'` dict repr -- the last of which the previous pattern
# could not match at all.
_KV_PATTERN = re.compile(
    r'([\'"]?(?:' + _KEY_ALTERNATION + r')[\'"]?\s*[:=]\s*)'
    r'([\'"]?)([^\'",}\s]+)([\'"]?)',
    re.IGNORECASE,
)

# `Authorization: Bearer <token>` / `Bearer <token>` -- the scheme is
# informative and kept, the credential after it is not.
_AUTH_SCHEME_PATTERN = re.compile(
    r'\b(Bearer|Basic|Token)\s+([A-Za-z0-9\-\._~\+/=]{4,})',
    re.IGNORECASE,
)

_KNOWN_ACCOUNT_ENV_VARS = ("KIS_ACCOUNT_NO", "KIS_ALLOWED_ACCOUNT_NO")

_DOUBLE_REDACTION_PATTERN = re.compile(
    re.escape(REDACTED) + r"(?:\s+" + re.escape(REDACTED) + r")+"
)


def mask_known_account_numbers(text):
    """Masks any CONFIGURED account number appearing anywhere in free
    text, even with no key next to it -- the case no key-based pattern
    can catch."""
    if not isinstance(text, str):
        return text
    for env_var in _KNOWN_ACCOUNT_ENV_VARS:
        value = os.environ.get(env_var)
        if value and len(value) > 4 and value in text:
            text = text.replace(value, mask_account_number(value))
    return text


def redact_text(text):
    """Best-effort redaction of secrets embedded in free text --
    exception messages, raw HTTP response bodies, Python dict reprs.
    Returns non-string input unchanged; never raises."""
    if not isinstance(text, str):
        return text
    # Scheme-first: masking `Bearer <token>` before the generic key=value
    # pass is what stops the generic pass from "redacting" only the word
    # Bearer and leaving the credential behind (Codex's exact repro).
    redacted = _AUTH_SCHEME_PATTERN.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    redacted = _KV_PATTERN.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}{m.group(4)}", redacted,
    )
    # Both passes can legitimately fire on the same fragment
    # (`Authorization: Bearer x`); collapse the doubled marker.
    redacted = _DOUBLE_REDACTION_PATTERN.sub(REDACTED, redacted)
    return mask_known_account_numbers(redacted)


def safe_repr(value, *, limit=500):
    """The replacement for `f"... {raw_dict!r}"` in exception messages.
    Redacts the structure FIRST, then formats it, and truncates so a
    large response body cannot flood a log. Use this anywhere a raw
    broker response, row, or payload would otherwise be interpolated
    into an error."""
    try:
        redacted = redact_value(value)
        rendered = json.dumps(redacted, default=str, sort_keys=True)
    except (TypeError, ValueError):
        rendered = redact_text(repr(value))
    if len(rendered) > limit:
        rendered = rendered[:limit] + "...(truncated)"
    return rendered


class RedactingFilter(logging.Filter):
    """Logging-boundary defense: redacts the formatted message and every
    positional argument of each LogRecord. A call site that forgets to
    redact still cannot emit a raw secret through `logging`."""

    def filter(self, record):
        try:
            if record.args:
                # Redact the FORMATTED message and drop the args. Redacting
                # the format string instead would replace its own `%s`
                # placeholders and break `record.getMessage()` outright.
                record.msg = redact_text(record.getMessage())
                record.args = ()
            elif isinstance(record.msg, str):
                record.msg = redact_text(record.msg)
            elif record.msg is not None and not isinstance(record.msg, (int, float, bool)):
                record.msg = redact_value(record.msg)
            if record.exc_info and record.exc_info[1] is not None:
                # The traceback itself is rendered by the formatter from
                # the exception object; masking the exception's own args
                # is what keeps a secret out of that rendering.
                exc = record.exc_info[1]
                if exc.args and all(isinstance(a, str) for a in exc.args):
                    exc.args = tuple(redact_text(a) for a in exc.args)
        except Exception:  # pragma: no cover -- a logging filter must never raise
            return True
        return True


_INSTALLED_FILTER = None


def install_logging_redaction(logger=None):
    """Attaches RedactingFilter to `logger` (the root logger by default).
    Idempotent. Called by every operational entrypoint in scripts/ so a
    deployed service always has the logging-boundary layer active."""
    global _INSTALLED_FILTER
    target = logger if logger is not None else logging.getLogger()
    for existing in target.filters:
        if isinstance(existing, RedactingFilter):
            return existing
    if _INSTALLED_FILTER is None:
        _INSTALLED_FILTER = RedactingFilter()
    target.addFilter(_INSTALLED_FILTER)
    # A filter on a logger only applies to records logged THROUGH that
    # logger, not to records propagated from children -- so the handlers
    # get the filter too, which is what actually catches everything.
    for handler in target.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(_INSTALLED_FILTER)
    return _INSTALLED_FILTER
