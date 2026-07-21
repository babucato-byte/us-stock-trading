"""Stage D: repeat-detection tracking across scan cycles (Phase 2
instructions, section 7). No equivalent exists in the repo today — see
DECISION_LOG.md — daily_candidate_scanner.py only diffs against the
single previous cycle for a Slack message, it does not persist a
multi-cycle streak.

State is a small CSV, atomically written and lock-protected (the same
technique as order_history.csv, reimplemented independently — see
atomic_io.py). One call to update_repeat_tracker() represents one scan
cycle's full result, which is what lets it distinguish a genuine
consecutive streak from "dropped out, then reappeared later".
"""

from pathlib import Path

from .atomic_io import atomic_write_csv, file_lock, read_csv_or_empty

BASE_DIR = Path(__file__).resolve().parent.parent
REPEAT_STATE_FILE = BASE_DIR / "scalping_repeat_state.csv"
REPEAT_STATE_LOCK_FILE = BASE_DIR / "scalping_repeat_state.lock"
REPEAT_STATE_COLUMNS = [
    "symbol",
    "trading_date",
    "first_detected_at",
    "last_detected_at",
    "detect_count",
    "consecutive_streak",
    "was_missed_last_cycle",
    "reappeared_after_gap",
    "last_relative_volume",
    "last_price",
    "last_scalping_score",
]


def load_repeat_state():
    return read_csv_or_empty(REPEAT_STATE_FILE, REPEAT_STATE_COLUMNS)


def update_repeat_tracker(detections, trading_date, detected_at, lock_timeout=5.0):
    """`detections`: dict of symbol -> {"relative_volume", "price", "scalping_score"}
    for every symbol that passed Stage A-C this cycle (not just new ones).

    Returns dict[symbol, dict] with the fields a caller needs to populate
    WatchlistEntry.repeat_count etc. Any symbol from a PRIOR trading_date is
    reset (section 7: "동일 거래일 기준" / "다른 거래일이면 초기화") rather
    than carried forward.
    """
    with file_lock(REPEAT_STATE_LOCK_FILE, timeout=lock_timeout):
        state = load_repeat_state()
        rows_by_symbol = {row["symbol"]: dict(row) for _, row in state.iterrows()}

        # Symbols tracked for today but not seen this cycle: streak resets.
        for symbol, row in rows_by_symbol.items():
            if row.get("trading_date") == trading_date and symbol not in detections:
                row["was_missed_last_cycle"] = True
                row["consecutive_streak"] = 0

        results = {}
        for symbol, obs in detections.items():
            existing = rows_by_symbol.get(symbol)
            is_same_day = existing is not None and existing.get("trading_date") == trading_date
            if not is_same_day:
                row = {
                    "symbol": symbol,
                    "trading_date": trading_date,
                    "first_detected_at": detected_at,
                    "last_detected_at": detected_at,
                    "detect_count": 1,
                    "consecutive_streak": 1,
                    "was_missed_last_cycle": False,
                    "reappeared_after_gap": False,
                    "last_relative_volume": obs.get("relative_volume"),
                    "last_price": obs.get("price"),
                    "last_scalping_score": obs.get("scalping_score"),
                }
            else:
                was_missed = bool(existing.get("was_missed_last_cycle"))
                row = dict(existing)
                row["detect_count"] = int(existing.get("detect_count", 0)) + 1
                row["last_detected_at"] = detected_at
                row["consecutive_streak"] = 1 if was_missed else int(existing.get("consecutive_streak", 0)) + 1
                row["reappeared_after_gap"] = was_missed
                row["was_missed_last_cycle"] = False
                row["last_relative_volume"] = obs.get("relative_volume")
                row["last_price"] = obs.get("price")
                row["last_scalping_score"] = obs.get("scalping_score")
            rows_by_symbol[symbol] = row
            results[symbol] = row

        import pandas as pd

        new_state = pd.DataFrame(list(rows_by_symbol.values()), columns=REPEAT_STATE_COLUMNS)
        atomic_write_csv(REPEAT_STATE_FILE, new_state)
        return results
