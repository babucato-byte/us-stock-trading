"""S2 volume-accumulation live policy: entry confirmation and exit.

Decisions only. Neither module in this package places an order, holds an
account, or reads a broker -- they answer questions, and the execution
layer acts on the answers. S2 is DISCOVERY_ONLY today, so nothing calls
them against real money yet.
"""
