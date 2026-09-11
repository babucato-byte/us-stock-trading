import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prune_releases", ROOT / "scripts/prune_releases.py")
prune = importlib.util.module_from_spec(spec); spec.loader.exec_module(prune)


def _setup(tmp_path, count=12):
    root = tmp_path / "releases"; root.mkdir(); shas = [f"{n:040x}" for n in range(count)]
    for n, sha in enumerate(shas):
        p = root / sha; p.mkdir(); (p / "data").write_bytes(b"x" * 32); p.touch()
        import os; os.utime(p, (100+n, 100+n))
    env = tmp_path / "env"; env.write_text(f"DEPLOYED_COMMIT={shas[-1]}\nVALIDATED_COMMIT={shas[-1]}\nTRADING_PROJECT_ROOT={root/shas[-1]}\n")
    return root, env, shas


def test_protects_latest_rollback_and_active_and_prunes_oldest(tmp_path, monkeypatch):
    root, env, shas = _setup(tmp_path)
    monkeypatch.setattr(prune, "disk_pct", lambda _p: 90)
    report = prune.plan(releases_root=root, env_file=env, proc_paths={str(root/shas[-2])})
    assert report["status"] == "DRY_RUN" and shas[-1] in report["protected_releases"]
    assert shas[-2] in report["protected_releases"] and report["candidates"][0].endswith(shas[0])
    before = set(root.iterdir()); result = prune.execute(report)
    assert result["deleted_releases"] and not (root/shas[0]).exists()
    assert root/shas[-1] in before and (root/shas[-1]).exists()


def test_noop_below_threshold_and_abort_when_metadata_unknown(tmp_path, monkeypatch):
    root, env, _ = _setup(tmp_path, count=5)
    monkeypatch.setattr(prune, "disk_pct", lambda _p: 70)
    assert prune.plan(releases_root=root, env_file=env, proc_paths=set())["trigger_reason"] == []
    env.write_text("DEPLOYED_COMMIT=bad\n")
    assert prune.plan(releases_root=root, env_file=env, proc_paths=set())["status"] == "RETENTION_SAFETY_ABORT"


def test_dry_run_never_deletes_shared_or_bundle_age_filter(tmp_path, monkeypatch):
    root, env, shas = _setup(tmp_path)
    shared = root / "shared"; shared.mkdir(); (shared / "TRADING_STATE.db").write_text("keep")
    monkeypatch.setattr(prune, "disk_pct", lambda _p: 90)
    report = prune.plan(releases_root=root, env_file=env, proc_paths=set())
    assert (shared / "TRADING_STATE.db").exists() and all("shared" not in p for p in report["candidates"])
    bundle = tmp_path / "old.bundle"; bundle.write_text("x")
    import os, time; os.utime(bundle, (time.time()-90000, time.time()-90000))
    assert prune.old_bundles(tmp_path) == [bundle]
    assert prune.old_bundles(tmp_path, active_paths={str(bundle)}) == []


def test_bounded_log_and_cron_flock_present(tmp_path):
    log = tmp_path / "retention.log"; log.write_bytes(b"x" * (prune.MAX_LOG_BYTES + 1))
    prune.bounded_log(log, {"status": "DRY_RUN"})
    assert log.stat().st_size < 1000 and log.with_suffix(".log.1").exists()
    assert "flock -n" in (ROOT / "deploy/cron/release_retention.sh").read_text()


def test_alert_only_abnormal(monkeypatch):
    calls = []
    monkeypatch.setattr("slack_utils.send_kis_live_alert", lambda text: calls.append(text) or True)
    assert not prune.alert_if_abnormal({"status": "DRY_RUN", "disk_before_pct": 70})
    assert prune.alert_if_abnormal({"status": "RETENTION_SAFETY_ABORT", "errors": ["x"]})
    assert len(calls) == 1
