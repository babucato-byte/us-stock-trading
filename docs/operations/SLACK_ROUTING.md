# Slack Routing

Traced from production code on 2026-09-02. Channels were renamed in place
on 2026-09-01; IDs, webhooks, members and history were preserved. No send
path resolves a channel by name -- every producer holds a webhook URL --
so the rename could not and did not affect delivery.

No webhook URLs, tokens or secrets appear in this file.

| Channel | Config key |
|---|---|
| stock-live-trading | `KIS_LIVE_SLACK_WEBHOOK_URL` |
| stock-live-alerts | `KIS_LIVE_SLACK_ALERT_WEBHOOK_URL` |
| stock-scanner | `SCANNER_MONITOR_SLACK_WEBHOOK_URL` |
| stock-system-health | `SLACK_ALERT_WEBHOOK_URL` |
| stock-trading-report | `SLACK_WEBHOOK_URL` |

## stock-live-trading

`operations/live_notifications.py::notify()` → `_webhook_for(event)` →
`slack_utils.send_kis_live_message` → `KIS_LIVE_SLACK_WEBHOOK_URL`.

Receives every lifecycle event NOT in `URGENT_EVENTS`:

`MARKET_START`, `BUY_CANDIDATE_SELECTED`, `LIVE_ORDER_PREPARED`,
`ORDER_SUBMITTED`, `ORDER_ACCEPTED`, `ORDER_PENDING`, `PARTIAL_FILL`,
`FILL_COMPLETED`, `EXIT_TRIGGERED`, `SELL_SUBMITTED`, `SELL_FILLED`,
`CANCEL_REQUESTED`, `CANCEL_COMPLETED`, `DAILY_SUMMARY`.

So: BUY intent, submitted, accepted, filled, position opened, SELL
intent, submitted, filled, position closed, realised result, routine
live status. Every message is prefixed `[KIS LIVE]`.

Sender: the S6 entry cron and the S6 exit monitor, both
release-authoritative. Cadence: per event.

There is deliberately NO fallback to `SLACK_WEBHOOK_URL` /
`SLACK_ALERT_WEBHOOK_URL`. Those carry Alpaca paper fills and scanner
output; a real-money order landing there is an order nobody notices. An
unset live webhook makes `kis_live_notifications_configured()` false and
blocks readiness at `KIS_LIVE_NOTIFICATION_NOT_CONFIGURED` rather than
silently rerouting.

## stock-live-alerts

Same caller; `_webhook_for` returns `send_kis_live_alert` when the event
is in `URGENT_EVENTS` → `KIS_LIVE_SLACK_ALERT_WEBHOOK_URL`.

`URGENT_EVENTS` = `ORDER_UNKNOWN`, `ORDER_REJECTED`, `CANCEL_FAILED`,
`RECONCILIATION_MISMATCH`, `POSITION_MISMATCH`, `KIS_API_FAILURE`,
`DB_FAILURE`, `HALT_ACTIVATED`, `KILL_SWITCH_ACTIVATED`.

Prefixed `[KIS LIVE][CRITICAL]`, so the distinction survives even if the
two channels are later merged by someone else.

Severity rule: membership in `URGENT_EVENTS`, nothing else. An event is
routine or urgent, never both -- the two live channels are mutually
exclusive for any single event.

`ORDER_UNKNOWN` additionally always carries `RETRY=BLOCKED` and
`RECONCILIATION_REQUIRED=true`: an UNKNOWN order may be live at the
broker, and the one thing that must never be inferred from the message
is that retrying is acceptable.

## stock-scanner

`scanners/notify/monitor.py`, `WEBHOOK_ENV =
"SCANNER_MONITOR_SLACK_WEBHOOK_URL"`. Unset means silence, never a
reroute.

Two distinct producers:

1. `notify_scan()` / `format_scan()` -- per-scan summary: universe,
   scanned, candidates, watching, ready, data errors. Sent by each S6
   discovery cycle.
2. `notify_tagged()` -- a COPY of selected live lifecycle events, called
   from `live_notifications.py:217`. Tags: `LIVE FILL`, `LIVE SELL`,
   `RECONCILIATION`, and submit/accept tagged by SIDE rather than event
   name, because `ORDER_SUBMITTED` carries both entries and exits and
   filing a sell under `[LIVE BUY]` would make the channel lie about the
   direction of a real order.

The mirror map is deliberately partial: `MARKET_START`,
`BUY_CANDIDATE_SELECTED`, `LIVE_ORDER_PREPARED`, `ORDER_PENDING` and the
`CANCEL_*` pair stay off it. An unmapped event mirrors nowhere rather
than defaulting into a catch-all tag.

Scan messages are de-duplicated on message CONTENT, in-process only.
That covers cronie's dual firing (no `CRON_TZ`, so every ET-guarded entry
fires from two UTC hours) and in-run retries. It deliberately does not
persist across processes: a crashed run's state could otherwise silence
the re-run that replaces it. Lifecycle messages are never de-duplicated,
because two identical-looking fills can be two real fills.

## The 전체 후보 / TOP 5 message does NOT come from here

Exact-string search places it in **`daily_candidate_scanner.py`**
(`전체 후보`, `수급 강한 후보`, `거래량 2배`, `신규 등장`, `반복 등장`,
`수급 리더`, `조건을 만족한 후보가 없습니다`), formatted around line 713
and sent by `scan()` at line 846 via **`send_slack_alert`**.

`send_slack_alert` → `SLACK_ALERT_WEBHOOK_URL` → **stock-system-health**.

So a scanner candidate summary is being delivered to the system-health
channel. Under the intended role model it belongs in stock-scanner. It
is recorded here as a finding; nothing was rerouted.

Caller chain: `premarket_scan_runner.py:41` spawns
`daily_candidate_scanner.py` as a subprocess. `live_pilot/runner.py:151`
also imports and calls `scan()`. `daily_pipeline.py` lists it as a
pipeline step.

Schedule: `daily_candidate_scanner` has no cron entry of its own. It runs
via `premarket_scan_runner.py`, scheduled `*/15 * * * *` from
**`/home/ubuntu/trading/`** -- a mutable checkout, not the release. Also
via `run_premarket.py`, hourly, from the same checkout.

## Routing matrix

| Event / message | Source file | Function | Channel | Config key | Severity | S6 LIVE impact |
|---|---|---|---|---|---|---|
| BUY submitted/accepted/filled | operations/live_notifications.py | notify | stock-live-trading | KIS_LIVE_SLACK_WEBHOOK_URL | routine | none |
| Position opened | operations/live_notifications.py | notify | stock-live-trading | KIS_LIVE_SLACK_WEBHOOK_URL | routine | none |
| EXIT / SELL submitted/filled | operations/live_notifications.py | notify | stock-live-trading | KIS_LIVE_SLACK_WEBHOOK_URL | routine | none |
| DAILY_SUMMARY | operations/live_notifications.py | notify | stock-live-trading | KIS_LIVE_SLACK_WEBHOOK_URL | routine | none |
| ORDER_REJECTED / UNKNOWN | operations/live_notifications.py | notify | stock-live-alerts | KIS_LIVE_SLACK_ALERT_WEBHOOK_URL | urgent | none |
| RECONCILIATION / POSITION_MISMATCH | operations/live_notifications.py | notify | stock-live-alerts | KIS_LIVE_SLACK_ALERT_WEBHOOK_URL | urgent | none |
| KIS_API_FAILURE / DB_FAILURE | operations/live_notifications.py | notify | stock-live-alerts | KIS_LIVE_SLACK_ALERT_WEBHOOK_URL | urgent | none |
| HALT / KILL_SWITCH_ACTIVATED | operations/live_notifications.py | notify | stock-live-alerts | KIS_LIVE_SLACK_ALERT_WEBHOOK_URL | urgent | none |
| Per-scan summary | scanners/notify/monitor.py | notify_scan | stock-scanner | SCANNER_MONITOR_SLACK_WEBHOOK_URL | info | none |
| Mirrored LIVE FILL / SELL / RECONCILIATION | scanners/notify/monitor.py | notify_tagged | stock-scanner | SCANNER_MONITOR_SLACK_WEBHOOK_URL | info (copy) | none |
| 전체 후보 / TOP 5 / 신규 등장 | daily_candidate_scanner.py | scan → send_slack_alert | **stock-system-health** | SLACK_ALERT_WEBHOOK_URL | info | none |
| Alpaca paper fills | paper_strategy_order.py | send_slack_message | stock-trading-report | SLACK_WEBHOOK_URL | info | none |
| Scanner failure alerts | scanners/notify/slack.py | — | stock-system-health | SLACK_ALERT_WEBHOOK_URL | warn | none |
| Ops dashboard / health | ops_dashboard/snapshot.py, operations/alerts.py | — | stock-system-health | SLACK_ALERT_WEBHOOK_URL | warn | none |

## S6 dependency

**CONNECTED_TO_S6: PARTIAL, one direction only.**

stock-scanner receives S6 output two ways: the per-scan summary, and a
copy of selected live lifecycle events. Both are reporting.

Nothing flows back. No S6 decision reads a Slack result, and no Slack
outcome can influence candidate validity, WATCHING, READY, BUY, SELL or
reconciliation.

**Slack → live trading dependency: NO.**

## Failure behaviour

Slack is observability. A send that RAISES is the dangerous shape, not a
send that fails, because an exception propagates into whatever was being
done at the time.

`slack_utils._send` deliberately does not catch `requests` errors: a
transport that hides its own failures cannot be monitored. Containment is
at the caller -- `notify()` catches everything including bugs in its own
formatting, returns a bool callers are expected to ignore, and is never
used in a condition, a retry loop or an `except` that could alter
trading. `monitor.notify_tagged` is wrapped in its own `except` at
`live_notifications.py:218` with the note that a monitor must never reach
the order path.

Pinned by `tests/test_slack_failure_isolation.py`: raising webhooks never
propagate, `notify()` is never a condition, and the order path imports no
Slack module.

## Known gaps

1. The 전체 후보 / TOP 5 summary lands in stock-system-health rather than
   stock-scanner. Scanner content in the infrastructure channel.
2. Its sender chain runs from the mutable checkout
   `/home/ubuntu/trading/` (`premarket_scan_runner.py` `*/15`,
   `run_premarket.py` hourly), not from the release. Same class as the
   two drift incidents on 2026-08-31 and 2026-09-01, though this path is
   non-LIVE.
3. `DAILY_SUMMARY` goes to stock-live-trading; the role model would put
   periodic performance reporting in stock-trading-report. Defensible
   where it is -- live-trading's role includes realised results.
4. stock-trading-report mixes daily reports with Alpaca PAPER fills.
5. `trading_health_check.py`, `order_monitor.py`, `daily_pipeline.py` and
   `slack_report.py` all contain Slack senders with no execution
   evidence. Classified UNKNOWN; none deleted or disabled.

None of these affect S6 live trading.
