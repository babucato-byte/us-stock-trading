"""Flat CSV/JSON export of the signal+performance dataset (spec section 22).

One row per signal, every measured variable as a column, forward returns
and excursions joined on. That shape is what makes section 22's
questions answerable by anything that reads a table:

    which variables separate the winners from the losers?
    did ADX actually contribute?
    which volume-multiple band was most stable?
    do high-extension names do worse?
    do multi-scanner symbols do better?

Read-only, and one-directional
------------------------------
Section 22 is explicit that an analysis must never write settings back
into the running system. This module has no write path into anything but
its own export file: it does not import a scanner, a config writer, the
candidate store, or the order path. An analysis that suggests a change
produces a suggestion, and the section 22 sequence -- propose, backtest,
verify, approve, new scanner version -- is a human one.

Nulls stay null
---------------
A missing 5-day return is written as an empty CSV cell, not 0. Anything
consuming this has to be able to tell "we have not measured it yet" from
"it went nowhere", and filling nulls at the export boundary would
destroy that distinction after every other layer took care to preserve
it.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from scanners.analytics.common import MAE_FIELDS, MFE_FIELDS, RETURN_FIELDS
from scanners.analytics.intersection_analysis import build_symbol_days
from scanners.base import result_store
from scanners.base.models import NUMERIC_FIELDS

logger = logging.getLogger(__name__)

#: Columns every export carries, whether or not any row happened to have
#: a value for them.
#:
#: Without this the CSV's schema is whatever the rows contained. A month
#: in which no signal ever reached its 5-day horizon would export with
#: NO `return_5d` column at all -- and a consumer would read that as "we
#: measured it and it is not here" rather than "nothing has matured
#: yet". Worse, two months' exports would have different columns and
#: could not be concatenated.
GUARANTEED_COLUMNS = (
    ("trading_day", "timestamp", "symbol", "scanner_name", "scanner_version",
     "signal_id", "reasons", "confirmation_count", "scanners_agreeing",
     "includes_signal_day_intraday", "sessions_available")
    + tuple(NUMERIC_FIELDS)
    + tuple(RETURN_FIELDS)
    + tuple(MFE_FIELDS)
    + tuple(MAE_FIELDS)
)

#: Columns pushed to the front of the CSV. Everything else follows in
#: sorted order, so a reader opening the file sees identity, price and
#: outcome before eighty indicator columns.
LEADING_COLUMNS = [
    "trading_day", "timestamp", "symbol", "scanner_name", "scanner_version",
    "scanner_score", "signal_price",
    "return_30m", "return_1h", "return_2h", "return_close",
    "return_1d", "return_3d", "return_5d",
    "mfe_1d", "mae_1d", "mfe_3d", "mae_3d", "mfe_5d", "mae_5d",
    "confirmation_count", "scanners_agreeing",
]


def build_dataset(
    start_day: str,
    end_day: str,
    *,
    include_confirmation: bool = True,
) -> pd.DataFrame:
    """The joined signal+performance table for a date range.

    `include_confirmation` adds the section 17/18 columns -- how many
    scanners flagged this symbol that day, and which ones. They are
    included by default because "were multi-scanner symbols better?" is
    one of section 22's listed questions and it is unanswerable from a
    table that does not carry the answer alongside each row.
    """
    rows = result_store.joined_rows(start_day, end_day)
    if include_confirmation and rows:
        lookup = {
            (record["trading_day"], record["symbol"]): record
            for record in build_symbol_days(rows)
        }
        for row in rows:
            record = lookup.get((str(row.get("trading_day")), str(row.get("symbol"))))
            if record:
                row["confirmation_count"] = record["confirmation_count"]
                row["scanners_agreeing"] = "|".join(record["scanners"])
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    for column in GUARANTEED_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    ordered = [name for name in LEADING_COLUMNS if name in frame.columns]
    remaining = sorted(name for name in frame.columns if name not in ordered)
    return frame[ordered + remaining]


def to_csv(start_day: str, end_day: str, path: Optional[Path] = None) -> Optional[str]:
    frame = build_dataset(start_day, end_day)
    if frame.empty:
        logger.warning("no signals between %s and %s; nothing exported", start_day, end_day)
        return None
    target = Path(path) if path else (
        result_store.exports_dir() / f"scanner_signals_{start_day}_{end_day}.csv")
    target.parent.mkdir(parents=True, exist_ok=True)
    # `na_rep=""` is the default; stated explicitly because it is the
    # behaviour this file depends on -- see the module docstring.
    frame.to_csv(target, index=False, na_rep="")
    logger.info("exported %s rows to %s", len(frame), target)
    return str(target)


def to_json(start_day: str, end_day: str, path: Optional[Path] = None) -> Optional[str]:
    rows = result_store.joined_rows(start_day, end_day)
    if not rows:
        logger.warning("no signals between %s and %s; nothing exported", start_day, end_day)
        return None
    target = Path(path) if path else (
        result_store.exports_dir() / f"scanner_signals_{start_day}_{end_day}.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "start_day": start_day,
        "end_day": end_day,
        "generated_at": datetime.now().astimezone().isoformat(),
        "row_count": len(rows),
        "note": ("Analysis export. Section 22: an analysis of this data may propose "
                 "a change, but must never write scanner settings back into the "
                 "running system. Changes go through backtest, verification, "
                 "approval, and a new scanner version."),
        "rows": rows,
    }
    target.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str),
                      encoding="utf-8")
    logger.info("exported %s rows to %s", len(rows), target)
    return str(target)
