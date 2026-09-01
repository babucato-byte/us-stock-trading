# S6 production architecture and operating rules

Current as of 2026-09-01. Describes what production actually does, not
what it was once intended to do. Where the two differ, the difference is
recorded rather than smoothed over.

## What is live

`S6_ORB_BREAKOUT_V1` (scanner `orb`) is the **sole LIVE strategy**.
`accumulation`, `breakout_ready`, `gap_pullback`, `hma_early_trend` and
`premarket_momentum` are all `DISCOVERY_ONLY`. S1 is not live.

One strategy across all four sessions -- OVERNIGHT_DAYTIME, PREMARKET,
REGULAR, AFTER_HOURS. Session differences live strictly below the
strategy: data adapter, KIS route/TR, execution capability, market hours.
There is no per-session strategy fork.

## The path a trade takes

    ~590 common universe
      -> S6 discovery scan          (cron :02/:17/:32/:47, ~6 min in REGULAR)
      -> candidate published        (shared/state/candidates, append-only)
      -> candidate-specific validation, in strategy rank order
      -> WATCHING / READY / INVALIDATED
      -> sizing against real orderable cash, whole shares
      -> Common Execution Engine
      -> KIS  (TTTT1002U buy / TTTT1006U sell in REGULAR)
      -> ACCEPTED -> FILLED
      -> canonical position OPEN
      -> 60-second position monitor
      -> exit rule fires
      -> SELL -> reconciliation -> CLOSED

Verified end to end in production on 2026-09-01 across six trades.

## Pre-trade validation

Candidates are validated against their own freshly fetched data, in the
order the STRATEGY ranked them, within a per-tick budget. A candidate the
budget did not reach is `WAITING_FOR_DATA` -- **not** a rejection. The
strategy was never asked about it, and reporting those two the same way
is how an infrastructure limit starts looking like a market with no
setups.

Realtime subscription membership does **not** decide tradeability. It was
doing so until 2026-09-01: the collector streams at most 41 symbols
chosen before the session opened, and in PREMARKET/AFTER_HOURS/
OVERNIGHT_DAYTIME the entry path read that stream alone. Measured on
2026-09-01 in PREMARKET: of 32 candidates, 2 were subscribed and
evaluated normally; the other 30 sat at zero open gates without ever
being judged. Realtime is a data delivery mechanism; it does not select
stocks.

## Held positions

The 60-second monitor is independent of discovery. Discovery takes
`s6_scan.lock`; position management takes `s6_exec.lock`. A 37-minute
scan cannot delay a position check, and a position check cannot make a
scan stand down. Pinned by `tests/test_kis_lock_fairness.py`.

Entry and exit DO share `s6_exec.lock`, deliberately, so two processes
cannot both submit. Priority is by patience, not by isolation: the entry
wrapper sets `KIS_LOCK_ACQUIRE_TIMEOUT_SECONDS=1` and the exit does not,
so the entry yields to the exit and never the reverse.

Held-symbol data is fetched per position through the session's adapter --
only held symbols, one call per held row, never a universe. When nothing
can be read at all the tick reports `POSITION_DATA_UNAVAILABLE`, which is
distinct from a rule that read the data and said no.

Seven exit families, unchanged: `EMERGENCY`, `HARD_RISK_CAP`,
`RANGE_REENTRY`, `VWAP_FAILURE`, `EMA_STRUCTURE_FAILURE`,
`VOLUME_DECAY_PRICE_WEAKNESS`, `SESSION_EXIT`.

## Order price normalization

KIS refuses more than two decimals at $1 and above (`APTR0057`). The
limit price is normalized at the single wire point in `kis_broker`, so
BUY, SELL and any requote share one function.

  * `>= $1`  -> two decimals. BUY floors, SELL takes the ceiling.
  * `<  $1`  -> untouched. The broker stated the rule for $1 and above
    and nothing else; guessing the sub-dollar tick would be the same
    class of mistake as the one being fixed.

Direction is never against the strategy's intent -- a BUY must not pay
more than authorised, a SELL must not accept less. The move is at most
one cent. `Decimal`, not `round()`: `round(1.005, 2)` is `1.0` here.

The strategy's decision price is never mutated. Both values are logged as
`ORDER_PRICE_NORMALIZED strategy_price=... broker_order_price=...`.

Production-proven both ways on 2026-09-01: `14.795 -> 14.79` (buy, filled)
and `14.6901 -> 14.70` (sell, filled at exactly 14.70).

## Reconciliation: expected eventual consistency

Reconciliation runs `*/5`. Between a broker fill and the canonical write
there is a short window where the two disagree. Observed repeatedly on
2026-09-01 (MTCH, VALE, NU, PEGA), always bounded, always self-clearing.

KIS's own endpoints can disagree during that window -- its order list may
still show an order open while its position list already reflects the
fill, or the reverse:

    stays ACCEPTED: KIS still lists '0030436155' as an open order
    mismatch: position mismatch for NU: internal=8 KIS=0
    settled ACCEPTED -> FILLED (matched KIS fill history: 8.0 of 8.0)

The system refuses to infer a fill from a position appearing or
disappearing and waits for authoritative fill history. Fill-to-canonical
CLOSED measured at ~7 seconds; the longer wall-clock gap is the broker
making up its mind, not reconciliation lagging.

A mismatch that persists beyond the `*/5` cadence, or that repeatedly
blocks valid entries outside that window, is a different thing and should
be treated as a defect.

## Release-authoritative cron paths

Every S6 runtime cron resolves the release through the shared env:

    ROOT=$(grep -m1 "^TRADING_PROJECT_ROOT=" \
      /home/ubuntu/releases/us-stock-trading/shared/env/kis-readonly.env \
      | cut -d= -f2-) && "$ROOT/deploy/cron/<wrapper>.sh"

This is not decoration. Two production incidents came from a cron
resolving a mutable checkout instead:

  * `s6_scan.sh` ran a 2026-08-27 checkout copy with no credential block
    for days. Every KIS chart request failed authentication, all 591
    symbols became DATA_ERROR, `fetch_failures` stayed 0, and the run
    reported SUCCESS. The tell was duration: 150s for a pass that takes
    ~37 minutes when the fetches actually happen.
  * `s6_exit_monitor.sh` ran a home-directory copy that predated the
    tested one.

Invariant after any deploy: HEAD = DEPLOYED = VALIDATED = SCANNER =
ENTRY = MONITOR. SCANNER and COLLECTOR lag by one cycle because a run
already in flight keeps the release it started on.

## Slack channels

Five webhooks, no API token. Roles are carried by the webhook, not by the
channel name.

| Env key | Role | Producers |
|---|---|---|
| `KIS_LIVE_SLACK_WEBHOOK_URL` | LIVE_TRADING (routine) | `operations/live_notifications.py` |
| `KIS_LIVE_SLACK_ALERT_WEBHOOK_URL` | LIVE_TRADING (urgent) | `operations/live_notifications.py` |
| `SCANNER_MONITOR_SLACK_WEBHOOK_URL` | SCANNER | `scanners/notify/monitor.py` |
| `SLACK_ALERT_WEBHOOK_URL` | mixed; target SYSTEM_HEALTH | `ops_dashboard/snapshot.py`, `final_pre_live_check.sh` |
| `SLACK_WEBHOOK_URL` | mixed; target PAPER_RESEARCH | `backtest_report_slack.py`, `run_scanner_report.py` |

The KIS live pair never falls back to the other two. An unset live
webhook makes `kis_live_notifications_configured()` false and blocks
readiness, rather than quietly routing a real order into the paper
stream.

Slack is observability only. A webhook that raises never propagates:
`notify()` catches everything and is never used in a condition. Pinned by
`tests/test_slack_failure_isolation.py`.

## Operating rules

**Do not mutate the repository while the full suite runs.** No `git add`,
`commit`, `stash`, `checkout`, `merge` or `rebase`. On 2026-09-01 a suite
run overlapping a commit and a stash pop produced five failures in
`TestStrictSchemaBlocksActivation`; three clean runs of the same code
passed 8519/0. A red run taken at face value there would have looked like
a reconciliation defect.

**Tests must not touch the production candidate store.** `candidate_dir()`
resolves from the environment, and on a release host that is the LIVE
store. A test that reaches `runner.main` without isolating it can read a
refused overlap as its own answer -- or, worse, win the race, take the
live S6 cycle lock and make a real scan stand down, because `flock -n`
does not queue. Enforced by `tests/test_no_production_cycle_lock.py`.

**Host gate greenness has a shelf life.** Date-pinned fixtures expire at
UTC midnight; a gate result older than a day is not current evidence.

## Known divergence: the position cap

`config/position_limits` states `S6_ORB_BREAKOUT_V1: 1` with
`ACTIVE = True`. That limit is **not in force**:

  * the account-scoped caps (`LIVE_ROLLOUT_MAX_POSITIONS`,
    `..._PER_STRATEGY`, `..._MAX_DAILY_ENTRIES`) are documented as
    OPTIONAL and are unset in the deployed environment, so they do not
    run;
  * `config/position_limits.check_entry` is only called by
    `s2_live/executor.py`; no S6 module consults it;
  * `s6_live.entry_timeout.entry_is_blocked` would refuse a second
    position but has no call site.

S6 held MTCH and PEGA concurrently on 2026-09-01, then MTCH and VALE.
What still holds is the always-on layer: the per-symbol position lock and
the same-day re-entry block. Exposure remains bounded by orderable cash,
whole shares, no leverage.

Recorded here because the config asserts a ceiling the runtime does not
enforce. Resolving it is a decision, not a cleanup.
