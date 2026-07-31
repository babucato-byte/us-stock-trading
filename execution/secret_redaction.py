"""CODEX-050: a full KIS account number was reaching order-gate error
messages and (via those messages becoming Shadow Mode's rejection_reason)
the durable Shadow Mode JSONL log. This module is the common redaction
layer everything that might surface a secret to a log/exception/Shadow
Mode record is expected to run through:

  - mask_account_number() -- for a value KNOWN to be an account number,
    shows only the last 4 digits (spec: "계좌번호 전체가 아니라 마지막
    4자리 또는 별칭/해시만 노출").
  - redact_value() -- recursively redacts dict/list structures by KEY
    name (case-insensitive, substring match, nested included) for
    appkey/appsecret/authorization/access_token/account_number/cano/
    token -- the exact key set spec §50 names.
  - redact_text() -- best-effort redaction of `key=value` / `"key":
    "value"` fragments embedded in free text (exception messages, raw
    HTTP response bodies) where there is no dict structure to walk by
    key -- never raises, returns non-strings unchanged.
"""

import re

REDACTED = "***REDACTED***"

_SENSITIVE_KEY_SUBSTRINGS = (
    "appkey", "app_key", "appsecret", "app_secret", "authorization",
    "accesstoken", "access_token", "accountnumber", "account_number", "cano", "token",
)


def _normalize_key(key):
    return str(key).strip().lower().replace("-", "").replace("_", "")


def _is_sensitive_key(key):
    normalized = _normalize_key(key)
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


def redact_value(value):
    """Recursively walks dicts/lists, replacing the VALUE of any key
    whose name matches a sensitive substring (case-insensitive) with
    REDACTED. Non-dict/list leaves are returned unchanged. Never
    raises -- an unredactable/unrecognized structure is simply
    returned as-is rather than blocking the caller."""
    if isinstance(value, dict):
        return {
            key: (REDACTED if _is_sensitive_key(key) else redact_value(val))
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value


_KV_PATTERN = re.compile(
    r'("?(?:app_?key|app_?secret|authorization|access_?token|account_?number|cano|token)"?\s*[:=]\s*)'
    r'("?)([^",}\s]+)("?)',
    re.IGNORECASE,
)


def redact_text(text):
    """Best-effort redaction of key=value / "key": "value" fragments
    embedded in free text -- exception messages, raw HTTP response
    bodies -- where there's no dict structure to walk by key. Returns
    non-string input unchanged; never raises."""
    if not isinstance(text, str):
        return text
    return _KV_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}{m.group(4)}", text)
