"""S1 Limited Live: publishing a candidate set, and nothing more.

A candidate is not a buy
------------------------
Everything in this package produces one thing: a validated list of
symbols that MAY be examined further today. That is the entire meaning.
A symbol on this list has passed no freshness check against a live
price, no extension check, no cash check, no position or daily-entry
limit, no loss or drawdown gate, no re-entry cooldown, no kill switch
and no reconciliation check -- all of which live downstream and all of
which must still pass before an order exists.

Naming it a "candidate source" rather than a "candidate list" is
deliberate for the same reason: it is an input to a gate, not an output
of one.

Separation from the trading candidate store
-------------------------------------------
`market_data/candidate_store.py` owns `order_candidates.csv`, which the
limited-live bootstrap reads. Nothing here writes to it, reads from it,
or imports it. This package publishes its own files under its own
names, and a test asserts the import is absent rather than merely
unused.
"""
