"""Stage 6 (사용자 지시서): user chart-analysis and YouTube-sourced strategy
material, structured before any of it becomes a real `strategy/plugins/`
implementation.

Purpose: a human (the user) or a YouTube video makes claims about how a
strategy should work -- "enter on VWAP reclaim", "risk 1R, take profit at
2R", etc. Some of these claims are explicitly stated by the source;
others are the collector's own inference filling a gap the source left
unspecified. Conflating the two is how a strategy plugin ends up
implementing a rule nobody actually said (see DECISION_LOG.md Stage 3
decision 4, where exactly this distinction mattered for
VWAPMicroPullbackV1.invalidate()). This package's `StrategyClaim.origin`
field makes that distinction structural instead of implicit-in-someone's-
head.

Modules:
  models.py      -- StrategyClaim / StrategySource dataclasses. A source's
                     `validation_status` is restricted to the *first four*
                     of strategy/status.py's lifecycle states (COLLECTED/
                     STRUCTURED/REVIEWED/REJECTED) -- BACKTESTED and
                     everything after it describes a *strategy
                     implementation's* progress, not raw source material's.
                     ACTIVE is not merely "not reached yet" here, it is
                     structurally unreachable: constructing a
                     StrategySource with any other status raises.
  repository.py   -- versioned, append-only JSON file storage (never
                     overwrites an existing version; a change requires a
                     new version number), default path docs/strategy/sources/.
  similarity.py   -- deterministic, rule-based (NOT LLM) claim-overlap
                     scoring between two sources, mirroring Stage 8's
                     explainable-only requirement for strategy selection.
  known_sources.py -- the 8 source strategies named in the user's Stage 6
                     instruction (VWAP, 1:2 R:R, 50% partial exit, Turtle,
                     multi-timeframe RSI, Bollinger pullback, CCI/RSI/ADX,
                     Ross Cameron micro pullback), structured and seeded
                     into docs/strategy/sources/ by seed_known_sources().
"""
