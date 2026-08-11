#!/usr/bin/env python3
"""Publish a scanner's candidate CSV into the shared store.

Why a bridge exists at all
--------------------------
The scanner in THIS tree publishes to the shared store itself (see
daily_candidate_scanner.save_candidate_files). But the scanner that cron
actually runs today lives in the legacy working copy at
/home/ubuntu/trading, which is not deployed from git and which this
project does not edit in place. Until cron points at a release, that
scanner writes only its own local CSV.

This command closes that gap without a human `cp`: it reads the
producer's output, checks it is today's, and republishes it atomically
into the shared store with a manifest. Run it after the scanner -- one
cron line -- and no release ever needs a file carried into it by hand.

It is a transport, not a strategy. It does not filter, re-score, re-rank
or threshold anything; the rows it reads are the rows it publishes. If
the source is empty it publishes nothing and says so, because an empty
publication would look to a consumer like "the scanner found nothing
today", which is a claim only the scanner may make.
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_data import candidate_store  # noqa: E402
from market_hours import us_trading_day  # noqa: E402

DEFAULT_SOURCE = Path("/home/ubuntu/trading/order_candidates.csv")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(DEFAULT_SOURCE),
                        help="candidate CSV produced by the scanner")
    parser.add_argument("--max-source-age-seconds", type=int,
                        default=candidate_store.DEFAULT_MAX_AGE_SECONDS,
                        help="refuse to publish a source file older than this")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # Resolve the destination FIRST. An unresolved shared store must
    # refuse before anything is read or written -- the old behaviour was
    # to fall back to the release directory, which published where no
    # other release could see it while reporting success.
    try:
        destination = candidate_store.candidate_path()
    except candidate_store.CandidateStoreUnresolved as exc:
        print(f"REFUSED: {exc}")
        print("         set TRADING_PROJECT_ROOT (or KIS_CANDIDATE_DIR). "
              "Nothing was written.")
        return 1
    print(f"  destination  : {destination}")

    source = Path(args.source)
    if not source.exists():
        print(f"REFUSED: no source candidate file at {source}")
        return 1

    stat = source.stat()
    generated_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    age = (datetime.now(timezone.utc) - generated_at).total_seconds()
    trading_day = us_trading_day()

    print(f"  source       : {source}")
    print(f"  generated_at : {generated_at.isoformat()} ({age:.0f}s ago)")
    print(f"  trading_day  : {trading_day}")

    if age > args.max_source_age_seconds:
        print(f"REFUSED: source is {age:.0f}s old, limit {args.max_source_age_seconds}s. "
              f"Publishing it would present a stale scan as current.")
        return 1

    payload = source.read_bytes()
    rows = payload.decode("utf-8", errors="replace").splitlines()
    data_rows = [r for r in rows[1:] if r.strip()]
    print(f"  rows         : {len(data_rows)}")
    if not data_rows:
        print("REFUSED: source has a header but no candidates. Publishing an empty "
              "set would assert 'the scanner found nothing', which only the "
              "scanner may assert.")
        return 1

    if args.dry_run:
        print(f"\nDRY RUN: would publish to {destination}")
        return 0

    manifest = candidate_store.publish(
        payload, trading_day=trading_day, generated_at=generated_at,
        source=f"bridge:{source}")
    print(f"\nPUBLISHED to {destination}")
    print(f"  manifest: {manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
