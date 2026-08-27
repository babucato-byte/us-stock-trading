"""Universe construction.

Two outputs, deliberately separate (T8):

- `universe.csv` -- the FULL tradable-asset listing, unchanged in shape
  and semantics. It is also `market_data/exchange_registry.py`'s exchange
  metadata feed for the KIS order path (sells included), so it must never
  be narrowed by an entry-side affordability rule. See
  `universe_filter.py`'s module docstring for the full reasoning.
- `universe_tradable.csv` -- the entry-side candidate pool: symbols the
  account can actually buy at least one whole share of right now, that
  also clear the scanner's own price/liquidity floors, ranked by
  liquidity so a downstream `scan_limit` truncates the least liquid names
  rather than an arbitrary CSV suffix.
"""

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from broker import AlpacaBroker
from config.paths import get_project_root
from scalping_watchlist.atomic_io import atomic_write_csv
from universe_filter import (
    REASON_INCLUDED,
    filter_universe,
    format_summary_lines,
    load_scanner_thresholds,
    summarize,
)

# Kept relative for backward compatibility: build_universe() has always
# written it relative to the process cwd, and universe_daily_runner runs
# that step with cwd=BASE_DIR.
UNIVERSE_OUTPUT_PATH = "universe.csv"
# Absolute path for READERS -- systemd/cron invoke the runner from an
# arbitrary cwd, where a relative "universe.csv" would silently miss.
def universe_listing_path():
    """Where the full tradable listing lives, read and written.

    Deferred to `scanners.universe.universe_path()` so ONE variable --
    SCANNER_UNIVERSE_FILE -- governs both the producer here and the
    scanners that consume it. They used to resolve it independently:
    this module wrote to the project root while the scanner read
    whatever its own resolution produced, which is the same
    producer/consumer split that once had scanners publishing candidates
    into a directory no consumer read.

    It also matters that this is a FUNCTION. Evaluated at import, an
    env-dependent path freezes to whatever the variable was when the
    module first loaded, which is exactly the kind of staleness this
    file is being changed to avoid.
    """
    from scanners.universe import universe_path

    return universe_path()


#: Kept as a module attribute for callers that read it directly. Its
#: value is resolved at import; prefer `universe_listing_path()`.
UNIVERSE_LISTING_PATH = universe_listing_path()
TRADABLE_UNIVERSE_OUTPUT_PATH = get_project_root() / "universe_tradable.csv"
FILTER_REPORT_PATH = get_project_root() / "logs" / "universe_filter_report.json"
DECISIONS_LOG_PATH = get_project_root() / "logs" / "universe_decisions.csv"

TRADABLE_COLUMNS = [
    "symbol",
    "name",
    "exchange",
    "tradable",
    "shortable",
    "price_usd",
    "avg_dollar_volume_usd",
    "price_ceiling_usd",
    "max_affordable_shares",
]

DECISION_COLUMNS = [
    "symbol",
    "included",
    "reason",
    "detail",
    "exchange",
    "price_usd",
    "avg_dollar_volume_usd",
    "price_ceiling_usd",
    "max_affordable_shares",
]


class UniverseBuildError(Exception):
    """Raised when a filtered universe cannot be produced safely. The
    previous `universe_tradable.csv` is left in place rather than
    replaced by a universe derived from an unusable budget."""


def fetch_active_us_equity_rows(broker=None):
    """Fetch tradable US equity assets via the broker's safety-gated GET.

    Replaces a previous version that built the Alpaca base URL directly
    from ALPACA_PAPER_BASE_URL/ALPACA_BASE_URL and called requests.get()
    with no endpoint validation (CODEX-009) — AlpacaBroker.get_assets()
    goes through the same validate_order_allowed() gate as every other
    broker call, so a misconfigured or malicious endpoint is rejected
    before any network access, exactly like account/position/order calls.
    """
    broker = broker or AlpacaBroker()
    assets = broker.get_assets()
    rows = []
    for asset in assets:
        if (
            asset.get("status") == "active"
            and asset.get("tradable") is True
            and asset.get("class") == "us_equity"
        ):
            rows.append({
                "symbol": asset.get("symbol"),
                "name": asset.get("name"),
                "exchange": asset.get("exchange"),
                "tradable": asset.get("tradable"),
                "shortable": asset.get("shortable"),
            })
    return rows


def build_universe(broker=None, output_path=UNIVERSE_OUTPUT_PATH):
    rows = fetch_active_us_equity_rows(broker)
    df = pd.DataFrame(rows)
    df = df.dropna(subset=["symbol"])
    df = df.drop_duplicates(subset=["symbol"])
    df.to_csv(output_path, index=False)
    print(f"거래 가능 종목 저장 완료: {len(df)}개")
    print(f"파일: {output_path}")
    return df


def load_universe_rows(path=None):
    """Reads the full listing back as plain dicts.

    Uses csv.DictReader rather than pandas so that every value stays the
    string the file actually holds -- pandas would coerce a symbol like
    "NA" or "INF" into a float, which is exactly the class of bug the
    exchange registry avoids by reading this file the same way.
    """
    path = path if path is not None else universe_listing_path()
    file_path = Path(path)
    rows = []
    try:
        with open(file_path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "symbol" not in reader.fieldnames:
                return rows
            for row in reader:
                symbol = (row.get("symbol") or "").strip()
                if symbol:
                    rows.append(dict(row))
    except OSError as exc:
        raise UniverseBuildError(f"cannot read universe listing {file_path}: {exc}") from exc
    return rows


def _decision_records(decisions):
    for decision in decisions:
        yield {
            "symbol": decision.symbol,
            "included": decision.included,
            "reason": decision.reason,
            "detail": decision.detail,
            "exchange": decision.exchange,
            "price_usd": decision.price_usd,
            "avg_dollar_volume_usd": decision.avg_dollar_volume_usd,
            "price_ceiling_usd": decision.price_ceiling_usd,
            "max_affordable_shares": decision.max_affordable_shares,
        }


def _tradable_frame(rows, decisions):
    by_symbol = {str(r.get("symbol") or "").strip().upper(): r for r in rows}
    records = []
    for decision in decisions:
        if not decision.included:
            continue
        source = by_symbol.get(decision.symbol, {})
        records.append({
            "symbol": decision.symbol,
            "name": source.get("name"),
            "exchange": source.get("exchange"),
            "tradable": source.get("tradable"),
            "shortable": source.get("shortable"),
            "price_usd": decision.price_usd,
            "avg_dollar_volume_usd": decision.avg_dollar_volume_usd,
            "price_ceiling_usd": decision.price_ceiling_usd,
            "max_affordable_shares": decision.max_affordable_shares,
        })
    frame = pd.DataFrame(records, columns=TRADABLE_COLUMNS)
    if not frame.empty:
        # Most liquid first: a downstream scan_limit then truncates the
        # least liquid tail instead of an arbitrary alphabetical suffix.
        frame = frame.sort_values(
            by=["avg_dollar_volume_usd", "symbol"], ascending=[False, True], kind="mergesort",
        ).reset_index(drop=True)
    return frame


def _write_report(summary, path, *, generated_at, budget_stale, decisions_path, output_path):
    payload = dict(summary.as_dict())
    payload["generated_at"] = generated_at.isoformat()
    payload["budget_stale"] = bool(budget_stale)
    payload["decisions_file"] = str(decisions_path)
    payload["tradable_universe_file"] = str(output_path)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def build_tradable_universe(
    rows,
    metrics_provider,
    budget,
    *,
    thresholds=None,
    output_path=None,
    report_path=None,
    decisions_path=None,
    budget_stale=False,
    now=None,
    logger=print,
):
    """Filters `rows` down to the buyable pool and writes all three
    artifacts (tradable CSV, per-symbol decision CSV, JSON summary).

    Raises UniverseBuildError when `budget` is None -- there is no safe
    default cash figure, and writing an unfiltered file under the
    filtered file's name would be worse than writing nothing.
    """
    if budget is None:
        raise UniverseBuildError(
            "no account budget available (KIS balance never read and nothing persisted); "
            "refusing to write a filtered universe"
        )

    output_path = Path(output_path or TRADABLE_UNIVERSE_OUTPUT_PATH)
    report_path = Path(report_path or FILTER_REPORT_PATH)
    decisions_path = Path(decisions_path or DECISIONS_LOG_PATH)
    thresholds = thresholds or load_scanner_thresholds()
    generated_at = now or datetime.now(timezone.utc)

    symbols = [str(r.get("symbol") or "").strip().upper() for r in rows]
    symbols = [s for s in symbols if s]
    metrics_by_symbol = metrics_provider.get_metrics(symbols)

    decisions = filter_universe(rows, metrics_by_symbol, budget, thresholds)
    summary = summarize(decisions, budget=budget, thresholds=thresholds)

    # ORACLE-CASH-01: refuse to REPLACE the entry-side universe with an
    # empty one. This used to be a warning printed after the write, which
    # is how an unusable cash figure could erase the pool silently: an
    # unknown balance read as $0 gives a $0 price ceiling, every symbol
    # falls to EXCLUDED_ABOVE_BUDGET, and a zero-row file lands under the
    # name downstream scanning trusts.
    #
    # Keeping the previous file is the safe direction. universe_tradable
    # is an entry-side PRE-filter, never an exit input (`universe.csv`
    # remains the exchange-metadata source, so a held position stays
    # resolvable), and the per-candidate orderable-amount read at entry
    # time is the final authority on affordability -- a symbol this pool
    # keeps but the account cannot afford is still blocked there.
    if summary.reason_counts.get(REASON_INCLUDED, 0) == 0:
        raise UniverseBuildError(
            f"filter included 0 of {len(decisions)} symbols; refusing to replace "
            f"{output_path} with an empty universe (budget source={budget.source!r}, "
            f"price_ceiling_usd={budget.price_ceiling_usd}). The previous file is "
            "left in place; downstream entry is still gated per candidate."
        )

    frame = _tradable_frame(rows, decisions)
    if not atomic_write_csv(output_path, frame):
        raise UniverseBuildError(f"failed to write filtered universe to {output_path}")

    decisions_frame = pd.DataFrame(list(_decision_records(decisions)), columns=DECISION_COLUMNS)
    if not atomic_write_csv(decisions_path, decisions_frame):
        # The decision log is evidence, not a gate: losing it must not
        # invalidate a universe that was already written correctly.
        logger(f"[UNIVERSE FILTER] WARNING: could not write decision log {decisions_path}")

    _write_report(
        summary, report_path, generated_at=generated_at, budget_stale=budget_stale,
        decisions_path=decisions_path, output_path=output_path,
    )

    for line in format_summary_lines(summary):
        logger(line)
    if budget_stale:
        logger(
            "[UNIVERSE FILTER] WARNING: budget is a kept previous value "
            f"(as_of={budget.as_of}); today's KIS balance read did not succeed"
        )
    logger(f"[UNIVERSE FILTER] wrote {output_path} ({summary.included} symbols)")
    logger(f"[UNIVERSE FILTER] report {report_path}")

    return {
        "summary": summary,
        "decisions": decisions,
        "output_path": output_path,
        "report_path": report_path,
        "decisions_path": decisions_path,
    }


if __name__ == "__main__":
    build_universe()
