"""Strategy plugin interface (Stage 3).

Defines the abstract contract every strategy plugin under `strategy/plugins/`
implements, plus the structured evaluation result shape required by
docs/autonomous/SCALPING_V1_ROADMAP.md's Phase 4 section:

    strategy_id, symbol, evaluated_at, state, signal, entry_reason,
    rejection_reasons, entry_price, stop_price, target_1, target_2,
    risk_per_share, confidence_score, input_snapshot

Sentinel convention: fields that cannot be computed (e.g. stop_price before
a setup exists) use the same UNKNOWN / NOT_AVAILABLE / NOT_EVALUATED
sentinels as `scalping_watchlist/models.py`, rather than a fabricated
number or None. This module reuses that exact convention (imports the
sentinels directly) instead of redefining an equivalent set, per the
project's established pattern of one source of truth per convention.

Stage 3 vs Stage 4 scope: `evaluate_setup`, `generate_entry`,
`calculate_stop`, and `calculate_targets` are real, testable logic in
Stage 3 -- they operate on a constructed bar DataFrame and produce a
structured result, with no live order submitted. `manage_position` and
`invalidate` are position-lifecycle concerns (tracking a *live* position
across fills, partial exits, time stops, and forced end-of-day
liquidation) that depend on Stage 4's position lifecycle state machine
(roadmap Phase 5), which does not exist yet. They are therefore left as
explicit NotImplementedError stubs here rather than half-implemented
against infrastructure that isn't built.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from scalping_watchlist.models import NOT_EVALUATED, UNKNOWN
from strategy.status import is_valid_status

# Evaluation states (the `state` field of EvaluationResult). Distinct from
# strategy lifecycle status (strategy/status.py) -- this is per-symbol,
# per-evaluation-call state, not the strategy's own approval status.
STATE_NO_SETUP = "NO_SETUP"          # required conditions not present
STATE_SETUP_FORMING = "SETUP_FORMING"  # rally/pullback present, breakout not yet confirmed
STATE_ENTRY_SIGNAL = "ENTRY_SIGNAL"    # all entry conditions satisfied this bar
STATE_REJECTED = "REJECTED"            # setup was present but explicitly disqualified
VALID_EVALUATION_STATES = {STATE_NO_SETUP, STATE_SETUP_FORMING, STATE_ENTRY_SIGNAL, STATE_REJECTED}


@dataclass
class EvaluationResult:
    """Structured output of `TradingStrategy.evaluate_setup()` /
    `generate_entry()`, matching the field list in SCALPING_V1_ROADMAP.md
    Phase 4. This is a plain data holder -- it carries no order-submission
    side effects itself.
    """

    strategy_id: str
    symbol: str
    evaluated_at: str  # ISO 8601 timestamp string
    state: str
    signal: bool
    entry_reason: str = ""
    rejection_reasons: str = ""  # semicolon-joined, mirrors WatchlistEntry convention
    entry_price: Any = UNKNOWN
    stop_price: Any = NOT_EVALUATED
    target_1: Any = NOT_EVALUATED
    target_2: Any = NOT_EVALUATED
    risk_per_share: Any = NOT_EVALUATED
    confidence_score: Any = NOT_EVALUATED
    input_snapshot: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.state not in VALID_EVALUATION_STATES:
            raise ValueError(
                f"Invalid evaluation state {self.state!r}; must be one of "
                f"{sorted(VALID_EVALUATION_STATES)}"
            )
        if self.signal and self.state != STATE_ENTRY_SIGNAL:
            raise ValueError("signal=True requires state=STATE_ENTRY_SIGNAL")


class TradingStrategy(ABC):
    """Abstract base class every strategy plugin implements.

    `strategy_id`, `version`, and `status` are validated at construction
    time (fail-closed on a malformed value) so a plugin can never be
    registered in a state the registry itself would have to re-discover
    was invalid -- see `strategy/registry.py`.
    """

    def __init__(self, strategy_id: str, version: str, status: str):
        if not isinstance(strategy_id, str) or not strategy_id.strip():
            raise ValueError(f"strategy_id must be a non-empty string, got {strategy_id!r}")
        if not isinstance(version, str) or not version.strip():
            raise ValueError(f"version must be a non-empty string, got {version!r}")
        if not is_valid_status(status):
            raise ValueError(f"Invalid strategy status: {status!r}")

        self.strategy_id = strategy_id
        self.version = version
        self.status = status

    # -- Stage 3: real, testable logic -----------------------------------

    @abstractmethod
    def evaluate_setup(self, bars, *, symbol: str, as_of: Optional[str] = None) -> EvaluationResult:
        """Evaluate `bars` (a pandas DataFrame of 1-minute OHLCV bars, most
        recent bar last, no look-ahead) for this strategy's setup
        conditions. Must not raise on a well-formed but setup-absent
        DataFrame -- it returns an EvaluationResult with
        state=STATE_NO_SETUP / signal=False instead.
        """
        raise NotImplementedError

    @abstractmethod
    def generate_entry(self, bars, *, symbol: str, as_of: Optional[str] = None) -> EvaluationResult:
        """Run evaluate_setup() and, if and only if it signals an entry,
        attach entry_price/stop_price/targets/risk_per_share computed from
        the same bar data. Returns the EvaluationResult either way (signal
        is False when there is no entry)."""
        raise NotImplementedError

    @abstractmethod
    def calculate_stop(self, bars, *, entry_price: float) -> float:
        """Return the stop price for a long entry at `entry_price`, given
        the same bar history used to detect the setup. Must return a stop
        strictly below `entry_price`."""
        raise NotImplementedError

    @abstractmethod
    def calculate_targets(self, *, entry_price: float, stop_price: float) -> Dict[str, float]:
        """Return a dict of at least {"target_1": ..., "target_2": ...,
        "risk_per_share": ...} derived from entry_price/stop_price."""
        raise NotImplementedError

    # -- Stage 4 scope: position lifecycle (roadmap Phase 5) -------------
    # Not implemented here on purpose. Stage 3 only defines the interface
    # shape; wiring these to the real fill/partial-exit/time-stop/EOD
    # forced-liquidation state machine is Stage 4's job, once that state
    # machine exists. Calling these today is a programming error, not a
    # runtime trading condition, hence NotImplementedError rather than a
    # NOT_EVALUATED-style return value.

    def manage_position(self, position_state, latest_bar):
        raise NotImplementedError(
            "manage_position() is Stage 4 scope (position lifecycle state "
            "machine, roadmap Phase 5) and is not implemented in Stage 3."
        )

    def invalidate(self, evaluation: EvaluationResult, reason: str):
        raise NotImplementedError(
            "invalidate() is Stage 4 scope (position lifecycle state "
            "machine, roadmap Phase 5) and is not implemented in Stage 3."
        )
