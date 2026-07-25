"""Stage 3 strategy platform.

This package provides the strategy plugin interface (`interface.py`), the
strategy lifecycle status constants (`status.py`), the strategy registry
that structurally enforces "at most one ACTIVE strategy" and "status must
be ACTIVE to generate real orders" (`registry.py`), and concrete strategy
plugins under `strategy/plugins/`.

Scope (see docs/autonomous/SCALPING_V1_ROADMAP.md, Phase 4 and the "유튜브
전략 정보 연결" track): this is Stage 3 of the build. Position lifecycle
(fills, stops, partial exits, time-based exit) is explicitly out of scope
here and is deferred to Stage 4 (Phase 5 in the roadmap) — see the
`manage_position`/`invalidate` stubs in `strategy/interface.py`.
"""
