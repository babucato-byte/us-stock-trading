"""Independent scanners and their analytics (spec: Scanner Expansion v1.0).

Six scanners with different theories about what a good entry candidate
looks like, run over the same universe and the same bars, storing their
findings side by side so that a month of real data -- not an opinion --
decides which theory works.

Boundaries this package does not cross
--------------------------------------
Nothing here imports `broker/`, `execution/`, `live_pilot/`, or
`market_data/candidate_store.py`. A scanner discovers symbols; it does
not decide when to buy (Entry Engine), how much (Risk Engine), or
whether to send anything (Execution). Spec section 30 is explicit that
adding these scanners is not a live-trading change, and the absence of
those imports is what makes that structural rather than a promise.
"""
