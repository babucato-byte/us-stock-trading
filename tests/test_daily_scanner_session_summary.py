from datetime import datetime

from scanners.analytics import daily_scanner_summary as dss


def _signal(identifier, stamp, scanner="accumulation"):
    return {"signal_id": identifier, "timestamp": stamp, "scanner_name": scanner}


def test_signals_partition_into_the_four_canonical_sessions():
    signals = [
        _signal("p", "2026-03-09T08:00:00-04:00"),
        _signal("r", "2026-03-09T10:00:00-04:00"),
        _signal("a", "2026-03-09T17:00:00-04:00"),
        _signal("o", "2026-03-09T21:00:00-04:00"),
        _signal("bad", "not-a-timestamp"),
    ]
    all_sessions = dss.build_by_session("2026-03-09", signals=signals,
                                        performance={}, manifests=[], conn=None)
    assert [item["total_signals"] for item in all_sessions.values()] == [1, 1, 1, 1]


def test_manifest_explicit_session_wins_and_old_manifest_falls_back_to_started_at():
    explicit = {"session": "PREMARKET", "started_at": "2026-03-09T18:00:00+00:00"}
    legacy = {"started_at": "2026-03-09T18:00:00+00:00"}
    assert dss._session_for_manifest(explicit) == "PREMARKET"
    assert dss._session_for_manifest(legacy) == "REGULAR"


def test_session_s1_entries_require_matching_signal_and_do_not_replicate():
    class Conn:
        def execute(self, *_args):
            return type("Cursor", (), {"fetchall": lambda self: [
                ("pre", "x", None, None, None, None, 1),
                ("unmatched", "x", None, None, None, None, 1)]})()
    signals = [_signal("pre", "2026-03-09T08:00:00-04:00", "hma_early_trend")]
    summary = dss.build("2026-03-09", session="PREMARKET", signals=signals,
                        performance={}, manifests=[], conn=Conn())
    s1 = summary["rows"][0]
    assert s1["entries"] == 1 and summary["unattributed_live_entries"] == 1
    regular = dss.build("2026-03-09", session="REGULAR", signals=signals,
                        performance={}, manifests=[], conn=Conn())
    assert regular["rows"][0]["entries"] == 0


def test_run_status_failures_and_four_section_message():
    manifests = [{"session": "REGULAR", "run_status": "PARTIAL"}]
    sessions = dss.build_by_session("2026-03-09", signals=[], performance={},
                                    manifests=manifests, conn=None)
    assert sessions["REGULAR"]["failures"] == ["REGULAR: PARTIAL"]
    text = dss.format_message({"trading_day": "2026-03-09", "sessions": sessions})
    assert text.startswith("[스캐너 세션별 일일 성과]")
    assert all(text.count("\n" + name + "\n") == 1 for name in sessions)
