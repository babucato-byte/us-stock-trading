#!/bin/bash
# Daily immutable-release retention.  The Python tool is dry-run by default;
# --execute itself still no-ops unless count/disk thresholds require cleanup.
set -u
ENV_FILE=/home/ubuntu/releases/us-stock-trading/shared/env/kis-readonly.env
[ -r "$ENV_FILE" ] || exit 1
set -a; . "$ENV_FILE"; set +a
ROOT="${TRADING_PROJECT_ROOT:?}"
LOCK=/home/ubuntu/logs/cron/release_retention.lock
exec flock -n -E 99 "$LOCK" "$ROOT/venv/bin/python" "$ROOT/scripts/prune_releases.py" --execute
