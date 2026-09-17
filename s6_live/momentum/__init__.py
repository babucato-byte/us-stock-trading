"""S6's auxiliary momentum component.

The whole surface a caller needs:

    from s6_live.momentum import (
        evaluate_entry_momentum, evaluate_position_momentum, MomentumResult,
    )

Nothing outside this package should import `indicators` or know that the
pair is HMA20 and an HMA-MACD. Replacing them is a change to
`momentum/indicators.py` and to nothing else.
"""

from s6_live.momentum.evaluator import (  # noqa: F401
    evaluate_entry_momentum, evaluate_position_momentum,
)
from s6_live.momentum.model import (  # noqa: F401
    DOWN, FLAT, MomentumResult, UNKNOWN, UP,
)
from s6_live.momentum.state import (  # noqa: F401
    BarContext, ENTRY_STABILIZATION_SECONDS, PRECEDING_SESSION,
)

__all__ = [
    "evaluate_entry_momentum", "evaluate_position_momentum", "MomentumResult",
    "BarContext", "ENTRY_STABILIZATION_SECONDS", "PRECEDING_SESSION",
    "UP", "DOWN", "FLAT", "UNKNOWN",
]
