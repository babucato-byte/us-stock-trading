"""What the momentum component answers, independent of how it measures.

The caller asks "is there momentum, and may I use it" and receives this.
It is deliberately free of order logic: nothing here knows about gates,
candidates, positions or sells. S6 decides what a direction means; this
only reports one.

Replaceability is the point of the shape. Every field below is either
generic (`combined_direction`, `available`, `entry_stabilized`) or lives
behind an explicitly indicator-named prefix (`hma_*`, `macd_*`). A future
pair swaps the prefixed fields and the generic ones keep their meaning,
so `evaluator.py` and the callers do not move.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

UP = "UP"
DOWN = "DOWN"
FLAT = "FLAT"
UNKNOWN = "UNKNOWN"

#: Why a result carries no usable direction. A reason, never a guess.
NO_BARS = "NO_BARS"
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
NOT_STABILIZED = "ENTRY_NOT_STABILIZED"
COMPUTE_FAILED = "COMPUTE_FAILED"


@dataclass(frozen=True)
class MomentumResult:
    """One symbol's momentum at one instant, for one purpose."""

    symbol: str
    session: Optional[str] = None
    observed_at: Optional[str] = None

    available: bool = False
    hma_available: bool = False
    macd_available: bool = False

    hma_value: Optional[float] = None
    hma_previous: Optional[float] = None
    hma_slope: Optional[float] = None
    hma_direction: str = UNKNOWN

    macd: Optional[float] = None
    signal: Optional[float] = None
    histogram: Optional[float] = None
    histogram_previous: Optional[float] = None
    histogram_delta: Optional[float] = None
    macd_direction: str = UNKNOWN

    #: The one field a caller should normally read. UP only when BOTH
    #: measurements agree, DOWN only when both disagree with the trend --
    #: never on one side alone, which is what "confirmation" means here.
    combined_direction: str = UNKNOWN

    session_started_at: Optional[str] = None
    current_session_bar_count: int = 0
    inherited_bar_count: int = 0
    #: True when the history behind these numbers reaches back past the
    #: session boundary. Context, never confirmation on its own.
    inherited_context: bool = False
    #: Entry only. A position is never gated on this -- see evaluator.
    entry_stabilized: bool = False
    session_elapsed_seconds: Optional[float] = None

    previous_session_last_price: Optional[float] = None
    new_session_first_price: Optional[float] = None
    boundary_gap_pct: Optional[float] = None

    reason: Optional[str] = None
    timings_ms: Dict[str, float] = field(default_factory=dict)

    # -- derived, so no caller re-derives them differently -------------
    @property
    def positive(self) -> bool:
        return self.available and self.combined_direction == UP

    @property
    def negative(self) -> bool:
        return self.available and self.combined_direction == DOWN

    @property
    def entry_eligible(self) -> bool:
        """Usable for an ENTRY decision: measured AND stabilized."""
        return bool(self.available and self.entry_stabilized)

    def as_record(self) -> Dict[str, Any]:
        row = asdict(self)
        row.update({"positive": self.positive, "negative": self.negative,
                    "entry_eligible": self.entry_eligible})
        return row
