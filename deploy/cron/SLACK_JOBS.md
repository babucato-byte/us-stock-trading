# Slack presentation jobs (proposed crontab lines, NOT installed)

All lines use the release-root pattern already in the crontab:

    ROOT=$(grep -m1 "^TRADING_PROJECT_ROOT=" /home/ubuntu/releases/us-stock-trading/shared/env/kis-readonly.env | cut -d= -f2-)

and the same `set -a; . "$ENV"; set +a` environment load. They are one-shot
per (trading day, session): every script claims its message in the
notification ledger and exits silently when it was already sent.

## Session readiness -> stock-live-trading

Four minutes after each enabled session opens (ET guard because cron has
no CRON_TZ; two UTC hours cover DST):

    4 1 * * 1-5   [ "$(TZ=America/New_York date +\%H)" = "20" ] && ... "$ROOT/venv/bin/python" "$ROOT/scripts/send_session_ready.py" --session OVERNIGHT_DAYTIME
    4 8,9 * * 1-5 [ "$(TZ=America/New_York date +\%H)" = "04" ] && ... --session PREMARKET
    34 13,14 * * 1-5 [ "$(TZ=America/New_York date +\%H)" = "09" ] && ... --session REGULAR
    4 20,21 * * 1-5 [ "$(TZ=America/New_York date +\%H)" = "16" ] && ... --session AFTER_HOURS

(Daytime: the KIS daytime window opens 10:00 KST = 01:00 UTC; the ET hour
is 20 or 21 depending on DST -- use `[ "$(TZ=Asia/Seoul date +\%H)" = "10" ]`
for that one instead.)

## Daily scanner summary (S1-S5) -> stock-sanner

After AFTER_HOURS closes at 20:00 ET, use 20:20 ET so all four session
sections are complete and it does not collide with the 20:10 trading report:

    20 0,1 * * 2-6 [ "$(TZ=America/New_York date +\%H)" = "20" ] && ... "$ROOT/deploy/cron/scanner_daily_summary.sh"

## S6 entry outcomes (ORB5 fills vs ORB15 shadow) -> entry_outcomes/<day>.jsonl

After the premarket session and before the trading report, 10:00 ET:

    0 14,15 * * 1-5 [ "$(TZ=America/New_York date +\%H)" = "10" ] && ... "$ROOT/scripts/run_s6_entry_outcomes.py" --session PREMARKET

## Daily trading report -> sotck-trading-report

After the after-hours session closes (20:00 ET) -- 20:10 ET, a slot no
existing job uses (audited 2026-09-09):

    10 0,1 * * 2-6 [ "$(TZ=America/New_York date +\%H)" = "20" ] && ... "$ROOT/scripts/run_daily_trading_report.py"

Requires `TRADING_REPORT_SLACK_WEBHOOK_URL` in the shared env file. Until it
is set the report is printed to the log and not sent (no fallback).

## System health

* `trading_health_check.py` keeps its existing daily line (09:00 UTC).
* `deploy/cron/s6_realtime_collector.sh` now posts COLLECTOR_RESTART /
  COLLECTOR_UNHEALTHY_NO_RESTART through `scripts/notify_system_health.py`
  (deduplicated per hour).
* Scanner run failures (`scanners/notify/slack.py`) now post to the
  system-health webhook instead of the scanner channel.

## Channel roles -> webhook variables (slack_utils.ROLE_WEBHOOK_ENV)

| role           | channel               | env                              |
|----------------|-----------------------|----------------------------------|
| LIVE_TRADING   | stock-live-trading    | KIS_LIVE_SLACK_WEBHOOK_URL       |
| LIVE_ALERTS    | stock-live-alerts     | KIS_LIVE_SLACK_ALERT_WEBHOOK_URL |
| SCANNER        | stock-sanner          | SCANNER_MONITOR_SLACK_WEBHOOK_URL|
| SYSTEM_HEALTH  | stock-system-health   | SYSTEM_HEALTH_SLACK_WEBHOOK_URL  |
| TRADING_REPORT | sotck-trading-report  | TRADING_REPORT_SLACK_WEBHOOK_URL |
