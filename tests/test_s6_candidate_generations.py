"""Only the newest scan's candidate rows reach the entry cycle.

The published candidate file is append-only for the whole session. A
scan every fifteen minutes writes its complete set, so by mid-afternoon
one REGULAR session held seventeen generations and 229 rows -- and the
consumer filtered on trading day and variant only, so it kept every one
of them.

The entry cycle then saw the same symbol repeatedly: STE three times,
ROP three times, SM ten. Each repeat cost a full KIS round trip, which
turned a two-minute cycle into a seven-minute one inside a window the
scanner already occupied most of.

The repeats were also WRONG, which is the part that matters. A candidate
row carries the ORB range, breakout price, rank and score computed at
scan time; judging a symbol against a two-hour-old row measures it
against a range the market has since left. That is the same failure that
let DT be re-offered every fifteen minutes on market data which had not
moved since the morning.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s6_live.candidate_source import _latest_generation  # noqa: E402


def _row(symbol, generated_at, **extra):
    row = {"symbol": symbol, "generated_at": generated_at}
    row.update(extra)
    return row


class TestOnlyTheNewestGenerationSurvives:
    def test_the_older_copies_of_a_symbol_are_dropped(self):
        rows = [_row("SM", "2026-08-27T14:02:00Z", rank=3),
                _row("SM", "2026-08-27T14:17:00Z", rank=1)]
        kept = _latest_generation(rows)
        assert [r["rank"] for r in kept] == [1]

    def test_the_whole_generation_is_the_unit_not_the_symbol(self):
        """A scan publishes a COMPLETE set, so a symbol the newest scan
        did not publish is no longer a candidate. Keeping its last
        appearance would quietly resurrect it."""
        rows = [_row("STE", "2026-08-27T14:02:00Z"),
                _row("ROP", "2026-08-27T14:02:00Z"),
                _row("ROP", "2026-08-27T14:17:00Z")]
        kept = {r["symbol"] for r in _latest_generation(rows)}
        assert kept == {"ROP"}, "STE dropped out of the newest scan"

    def test_a_single_generation_is_returned_unchanged(self):
        rows = [_row("A", "2026-08-27T14:17:00Z"),
                _row("B", "2026-08-27T14:17:00Z")]
        assert len(_latest_generation(rows)) == 2

    def test_the_real_shape_collapses_to_one_row_per_symbol(self):
        """229 rows over 17 generations was the production case."""
        rows = []
        for gen in range(17):
            stamp = f"2026-08-27T{13 + gen // 4:02d}:{(gen * 15) % 60:02d}:00Z"
            for symbol in ("SM", "GIS", "ROP"):
                rows.append(_row(symbol, stamp))
        kept = _latest_generation(rows)
        assert len(kept) == 3
        assert len({r["symbol"] for r in kept}) == 3


class TestAnUnstampedFileStillTrades:
    def test_rows_without_a_timestamp_are_deduplicated_not_discarded(self):
        """Losing the ordering must not silently empty the candidate
        set -- that would look exactly like "no candidates today"."""
        rows = [{"symbol": "SM", "rank": 9}, {"symbol": "SM", "rank": 1},
                {"symbol": "GIS", "rank": 2}]
        kept = _latest_generation(rows)
        assert {r["symbol"] for r in kept} == {"SM", "GIS"}
        # Last occurrence wins: newest in an append-only file.
        assert [r["rank"] for r in kept if r["symbol"] == "SM"] == [1]

    def test_a_mixed_file_prefers_the_stamped_rows(self):
        rows = [{"symbol": "OLD"},
                _row("NEW", "2026-08-27T14:17:00Z")]
        assert [r["symbol"] for r in _latest_generation(rows)] == ["NEW"]

    def test_an_empty_set_stays_empty(self):
        assert _latest_generation([]) == []
