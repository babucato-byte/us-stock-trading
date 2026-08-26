"""Post-exit research: what the price did after each strategy sold.

Strictly a research path. Nothing in this package is consulted by an
execution gate, an exit decision, a threshold or a score, and every
entry point swallows its own failures -- a price feed that is down must
never turn into a trading fault. The one execution-side rule that grew
out of this work, the same-day re-entry block, lives in
`execution/reentry_policy.py` and is derived from position history, not
from anything here.
"""
