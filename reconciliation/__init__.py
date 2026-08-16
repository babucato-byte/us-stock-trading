"""KIS-is-authoritative reconciliation (spec §16). Every reconciler here
answers ONE question -- "does our internal record match what KIS itself
reports?" -- and on any mismatch returns a blocking, non-corrective
result: no auto-fix, no auto-reversal, new buys blocked until an
operator resolves it. Never a network call inside these functions --
callers fetch both sides (internal state + a fresh KIS read via
KISBroker) and pass them in, keeping this package pure and testable
without a broker.
"""
