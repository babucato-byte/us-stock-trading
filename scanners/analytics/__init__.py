"""Scanner performance analysis (spec sections 12-18 and 22).

Read-only with respect to the trading system. These modules read the
analytics store and write reports and exports; nothing here can change a
scanner's configuration, publish a candidate, or reach an order path.
"""
