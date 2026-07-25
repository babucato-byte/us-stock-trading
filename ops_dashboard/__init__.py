"""Stage 9 (사용자 지시서): local operations monitoring.

A read-only aggregation of every safety-relevant piece of state this
project already persists -- mode, active strategy, market state,
watchlist, today's orders, open positions (with stop/target prices and
PnL), kill switch, broker config, reconciliation, and last-activity time
-- assembled entirely from local files and env-derived config. It never
calls the real Alpaca or Slack APIs: "checkable locally even if Slack is
down" is satisfied structurally, not by a fallback path, because nothing
here ever depends on Slack (or any other network service) succeeding.

Each section is independently fault-tolerant: a corrupted or missing
data source for one section (e.g. a hand-edited order_history.csv) never
prevents the rest of the snapshot from rendering -- see snapshot.py's
SectionResult, which records ok=False + the error message for that
section alone rather than raising out of build_snapshot() entirely. An
operator with everything else healthy should never lose the whole
dashboard because one file is temporarily broken.

Modules:
  snapshot.py -- build_snapshot(): gathers every section.
  cli.py      -- render_text(snapshot): a plain-text report for terminal/
                 cron use (`python -m ops_dashboard.cli`).
"""
