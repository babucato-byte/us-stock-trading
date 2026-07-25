"""Strategy lifecycle status constants.

Mirrors the flow documented in docs/autonomous/SCALPING_V1_ROADMAP.md's
"유튜브 전략 정보 연결" track and PROJECT_CONSTITUTION.md's "적용 전략 범위"
section:

    COLLECTED -> STRUCTURED -> REVIEWED -> BACKTESTED -> PAPER_APPROVED
        -> LIMITED_LIVE_APPROVED -> ACTIVE

PAUSED and REJECTED are terminal/side states a strategy can move into from
anywhere in that chain (a paused strategy can resume; a rejected one is
retired). This module intentionally only defines the *set* of valid
statuses and the ACTIVE-only order-generation rule -- it does not encode
the allowed state-transition graph, which is out of scope for Stage 3 and
left to whatever manages a strategy's lifecycle over time (a human
reviewer today; possibly automation later).

Hard rule (constitution: "ACTIVE 이전 전략은 주문 엔진에 연결하지 않는다" /
roadmap: "초기에는 최대 1개"): a strategy whose status is not exactly
ACTIVE must never be usable to generate a real order, no matter how far
along the chain it is (PAPER_APPROVED and LIMITED_LIVE_APPROVED are both
still blocked). See `strategy.registry.require_active`.
"""

COLLECTED = "COLLECTED"
STRUCTURED = "STRUCTURED"
REVIEWED = "REVIEWED"
BACKTESTED = "BACKTESTED"
PAPER_APPROVED = "PAPER_APPROVED"
LIMITED_LIVE_APPROVED = "LIMITED_LIVE_APPROVED"
ACTIVE = "ACTIVE"
PAUSED = "PAUSED"
REJECTED = "REJECTED"

VALID_STATUSES = {
    COLLECTED,
    STRUCTURED,
    REVIEWED,
    BACKTESTED,
    PAPER_APPROVED,
    LIMITED_LIVE_APPROVED,
    ACTIVE,
    PAUSED,
    REJECTED,
}

# Only this set of statuses may generate real orders. Everything else --
# including PAPER_APPROVED/LIMITED_LIVE_APPROVED, which sound close to
# "ready" -- is blocked. Kept as its own constant (rather than an
# `== ACTIVE` check sprinkled around) so the rule has exactly one
# definition site.
ORDER_GENERATING_STATUSES = {ACTIVE}


def is_valid_status(value) -> bool:
    return isinstance(value, str) and value in VALID_STATUSES


def can_generate_orders(status) -> bool:
    return status in ORDER_GENERATING_STATUSES
