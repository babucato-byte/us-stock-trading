"""Position lifecycle package (Stage 4, roadmap Phase 5).

See positions/states.py for the state machine, positions/store.py for the
atomic on-disk position record store, and positions/lifecycle.py for the
actual entry/fill/exit operations that call into paper_strategy_order.py's
existing safety-gated order-submission path.
"""
