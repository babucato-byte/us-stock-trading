"""Plain-text CLI renderer for a StatusSnapshot.

Usage: `venv/bin/python -m ops_dashboard.cli`. Reads only local files and
env-derived config (see snapshot.py) -- safe to run at any time, never
touches the real Alpaca or Slack APIs, never modifies any operational file.
"""

from ops_dashboard.snapshot import build_snapshot


def _section_line(title, section):
    if not section.ok:
        return f"[{title}] UNAVAILABLE ({section.error})"
    return f"[{title}] {section.data}"


def render_text(snapshot):
    lines = [
        f"=== Operations Status @ {snapshot.generated_at} ===",
        _section_line("Mode", snapshot.mode),
        _section_line("Active Strategy", snapshot.active_strategy),
        _section_line("Market State", snapshot.market_state),
        _section_line("Watchlist", snapshot.watchlist),
        _section_line("Orders Today", snapshot.orders_today),
        _section_line("Positions", snapshot.positions),
        _section_line("PnL", snapshot.pnl),
        _section_line("Kill Switch", snapshot.kill_switch),
        _section_line("Slack", snapshot.slack),
        _section_line("Broker", snapshot.broker),
        _section_line("Reconciliation", snapshot.reconciliation),
        _section_line("Last Successful Run", snapshot.last_successful_run),
    ]
    return "\n".join(lines)


def main():
    snapshot = build_snapshot()
    print(render_text(snapshot))


if __name__ == "__main__":
    main()
