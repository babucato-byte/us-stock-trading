"""Fail-closed symbol allow-list check for the limited-live pilot.

An empty or unset allow-list permits NOTHING, never everything -- the
opposite of how an empty blocklist would behave. The pilot's actual
symbol list is a TBD_OPERATOR item (see docs/live_review/
LIMITED_LIVE_30K_KRW_PLAYBOOK.md); until an operator populates it, this
function is a hard no for every symbol, which is the correct default for
a brand-new live-money gate that has never been exercised before.
"""


def is_symbol_allowed(symbol, allow_list):
    if not allow_list:
        return False
    if not isinstance(symbol, str) or not symbol.strip():
        return False
    normalized_allow_list = {s.strip().upper() for s in allow_list}
    return symbol.strip().upper() in normalized_allow_list
