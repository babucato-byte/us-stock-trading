# Independent review package: S6 all-session ORB5 live + ORB15 shadow + entry quality

Branch: `feature/trading-core-next`, uncommitted working tree on top of `72421c24c0c5`
(the deployed release). Nothing is deployed. No order was placed during this work.

The reviewer is asked to VERIFY, not to trust, each claim below. Every item names
the file, the test, and the command that demonstrates it.

## A. ORB5 is the live primary in EVERY S6 session

- `scanners/orb/config.json` -> `params.orb_minutes_by_session` names all four
  sessions (`OVERNIGHT_DAYTIME`, `PREMARKET`, `REGULAR`, `AFTER_HOURS`) at 5.
  The global `orb_minutes` stays 15 and is now what an unscoped call resolves
  to; no live session reads it any more.
- `config/s6_sessions.py::orb_minutes_for / scanner_variant_for /
  shadow_orb_minutes_for` is the single resolver, used by the scanner, the
  precision watch, the fast-watch source and session readiness. Every session
  answers `5 / S6_ORB5 / 15`.
- `scanners/orb/scanner.py::orb_minutes(session)` resolves the override and
  validates against `supported_orb_minutes`. REGULAR keeps the original v1.0
  CODE path; only the window length changes.
- Tests: `tests/test_s6_orb5_entry_quality.py::TestSessionRouting`,
  `tests/test_s6_active_watch_runtime.py::TestAllSessionFastWatch`.

Verify: `venv/bin/python -c "from config import s6_sessions as s; print([(x, s.orb_minutes_for(x), s.shadow_orb_minutes_for(x)) for x in sorted(s.SCAN_SESSIONS)])"`.

## B. ORB15 is shadow-only

- Runs in every S6 session, from the same engine as the live range.
- `s6_live/range_shadow.py`: reads the collector store, builds a frozen 15-minute
  `SessionFeatures`, calls `precision_watch.evaluate(features=...)`, appends JSONL to
  `<SCANNER_DATA_ROOT>/range_shadow/<day>.jsonl` with `scanner_variant=S6_ORB15_SHADOW`,
  `shadow=true`, `order_capable=false`.
- Invoked only from `scripts/run_live_buy_entry.py::_record_shadow_signals` AFTER the
  cycle, in its own try/except, and it never writes `WatchedCandidateSource.evaluations`
  or the candidate directory.
- Tests: `TestOrb15Shadow` — AST proves the module imports nothing from `execution`/`brokers`
  and calls no `publish/submit_*/record_submission`; a live/shadow pair from the same
  store yields two frozen objects with different ranges; a recorded cycle sends nothing
  to any webhook; a session whose live range is already 15 records nothing.

## C. Late-entry safeguards are enforced (when configured)

- `s6_live/entry_quality.py::compute` measures, from bars up to the decision instant only:
  first breakout timestamp, breakout age (minutes/bars), post-range high and its age,
  recent volume 5/10/15/30m, RVOL vs the opening range's per-minute pace, time-bucket
  RVOL vs prior sessions (UNAVAILABLE under 3 usable days), 5m/15m and 5m/30m ratios,
  volume slope, decay flag, 5/10/15m returns, extension, dollar volume, provenance
  (provider, source timestamp, age, bar interval).
- `assess()` applies the session's `entry_quality` thresholds in a fixed order; the
  first failing dimension names the reason (`S6_BREAKOUT_STALE`, `S6_SESSION_HIGH_STALE`,
  `S6_RECENT_VOLUME_WEAK`, `S6_VOLUME_DECAY`, `S6_MOMENTUM_WEAKENING`,
  `S6_PREMARKET_LIQUIDITY_WEAK`); a configured threshold with no measurement is
  UNAVAILABLE (`S6_QUALITY_UNAVAILABLE`), never PASS.
- The gate is condition `ENTRY_QUALITY` in `precision_watch.CONDITION_ORDER`; it runs in
  `WatchedCandidateSource.symbols()` at the top of the entry cycle, before qualification,
  sizing, the execution lock and `execution_engine.submit_buy_order`. A stale breakout is
  terminal (INVALIDATED); the others keep WATCHING.
- Every evaluation (READY / WATCHING / INVALIDATED / SUBMITTED) is persisted with the
  snapshot in the shadow signal log (`scanner_variant`, `range_minutes`, `evaluated_at`,
  `entry_quality`, `entry_quality_reason`, `watch_state`); an ordered entry also stores
  it in `s6_positions.entry_quality_json` (migration 26).
- Tests: `TestTheGate`, `TestPrecisionWatchIntegration`, `TestReportingAndPersistence`.

## D. No live entry-quality threshold is armed, and the data says so

- Shipped thresholds in `scanners/orb/config.json` -> `entry_quality.PREMARKET`:
  **every key is null**. `eq.assess()` therefore returns PASS for every
  snapshot, `ENTRY_QUALITY` is PASS in every evaluation, and no
  `S6_*` reason code can be produced. Nothing in this release can block an
  order on a quality measurement. Verify:
  `venv/bin/python -c "from scanners.base import config; from s6_live import entry_quality as eq; print(eq.thresholds_for(config.load_config('orb', scanner_name='orb'), 'PREMARKET'))"`.
- `min_rvol_5m = 0.5` is SHADOW_ONLY, per the previous review. It is recorded
  on every evaluation and gates nothing.
- The metric names now separate the two different questions that were both
  called "RVOL": `recent_vs_opening_pace_5m/10m/15m/30m` (recent volume
  against THIS session's opening-range pace; the legacy `rvol_*` names remain
  as aliases for stored-record compatibility) and `rvol_tb_5m/15m` (the same
  premarket time bucket on prior sessions, with `rvol_tb_status` and
  `rvol_tb_baseline_days`).

### Historical review (S6 PREMARKET) -- regenerated under production parity

The replay in the previous package was produced before the semantics were
corrected. It has been regenerated with the production rules: the official
04:00 ET origin, closed one-minute bars only, the scanner's
`volume_expansion >= 1.2` and the precision-watch volume condition, entry at
the next bar's open, and no submit-minute lookahead. Methodology is recorded
inside the artefact under `methodology`.

Data: 5 PREMARKET days (2026-09-01, 09-02, 09-03, 09-04, 09-08), 125
symbol-days of KIS one-minute bars, all 13 real PREMARKET positions.
Artefact: `docs/review/s6_premarket_replay_2026-09-01_to_09-08.json`.
Regenerate with `scripts/backfill_s6_premarket_bars.py` then
`scripts/replay_s6_premarket.py`.

The parity rules remove 62% of the opportunities the earlier run reported
(118 -> 45 for ORB5, 91 -> 44 for ORB15). Every number below replaces the
corresponding number in the previous package.

| set | signals | W | L | flat | ret30 | MFE30 | MAE30 |
|---|---|---|---|---|---|---|---|
| ORB5 | 45 | 20 | 21 | 4 | +0.04% | +0.45% | -0.43% |
| ORB15 | 44 | 15 | 25 | 4 | -0.04% | +0.56% | -0.50% |

Real fills, unchanged and not re-simulated: 9 filled of 13 submissions,
0 wins, 8 losses, 1 flat, realized sum -4.60%, mean -0.51%. Breakout age at
submission, measured from the official origin: 22 to 162 minutes, median 72.

On the 40 opportunities both ranges produced, ORB5 signalled earlier by a
median of 0 minutes; 5 ORB5 signals had no ORB15 counterpart. ORB5's
advantage on this sample is count and marginally better MAE, not lead time.

Counterfactual filters on the 45 ORB5 opportunities (losses blocked /
winners blocked / kept / mean 30-minute return before->after / mean MAE30
before->after):

| filter | threshold | L blocked | W blocked | kept | ret30 | MAE30 |
|---|---|---|---|---|---|---|
| max_breakout_age_minutes | 10 | 12 | 16 | 7 | +0.04 -> -0.34 | -0.43 -> -0.92 |
| max_breakout_age_minutes | 20 | 11 | 14 | 11 | +0.04 -> -0.14 | -0.43 -> -0.67 |
| max_breakout_age_minutes | 45 | 10 | 11 | 18 | +0.04 -> +0.05 | -0.43 -> -0.52 |
| max_minutes_since_session_high | 10 | 4 | 8 | 27 | +0.04 -> -0.01 | -0.43 -> -0.58 |
| max_minutes_since_session_high | 20 | 3 | 4 | 35 | +0.04 -> +0.02 | -0.43 -> -0.47 |
| min_rvol_5m | 0.5 | 1 | 0 | 44 | +0.04 -> +0.05 | -0.43 -> -0.43 |
| min_rvol_5m | 1.5 | 2 | 0 | 42 | +0.04 -> +0.09 | -0.43 -> -0.38 |
| min_volume_ratio_5m_15m | 0.6-1.0 | 0 | 0 | 45 | unchanged | unchanged |
| min_return_5m_pct | 0.0 | 3 | 8 | 28 | +0.04 -> -0.07 | -0.43 -> -0.58 |

**No threshold is justified by this data, and that is why none is armed.**
Two findings the reviewer should check specifically:

1. `min_rvol_5m` is now nearly inert: 1 of 45 signals falls below 0.5
   (median 29.9, minimum 0.05). Under the official 04:00 origin the ORB5
   denominator is the 04:00-04:05 window, which in premarket is close to
   empty, so "recent volume vs opening pace" is a large and unstable
   ratio. The apparent power the earlier run attributed to 0.5 (16 losers
   vs 9 winners blocked) was an artefact of anchoring the range at the
   first bar that happened to exist, which the production scanner does not
   do. This is an independent reason for the same conclusion the previous
   review reached.
2. Every filter with real bite on this sample -- breakout age above all --
   blocks more winners than losers and makes the mean adverse excursion
   worse, not better. Late entry remains the observed defect in the real
   fills, but a first-signal age cutoff is not the demonstrated remedy on
   five days of data.

Caveats: five days, one week of market; the sample is small enough that no
cell above should be read as a stable estimate; `rvol_tb_*` has no baseline
in this window (`NO_BASELINE_SOURCE`) and is not gated; costs and slippage
are excluded from every theoretical entry.

## E. S1–S5 unchanged

- `git diff --stat -- scanners/hma_early_trend scanners/accumulation scanners/breakout_ready scanners/premarket_momentum scanners/gap_pullback s1_live s2_live` is empty.
- `TestOtherScannersUntouched` asserts none of the five scanners or their configs
  reference `entry_quality` or `orb_minutes`, and that `entry_quality` is reachable only
  through S6 modules.

## F. Risk / sizing / reconciliation / kill switch unchanged

- `git diff --stat -- risk execution/order_gate.py execution/risk_gate.py reconciliation kill_switch_state.py operations/kill_switch.py s6_live/exit_policy.py config/s6_exit_v0.py` is empty.
- `kis_live_trading.py` diff is limited to passing `watch=_s6_watch(source, symbol)` into
  `record_entry_submission` (lineage only) and the `_s6_watch` helper.

## G. The Slack refactor did not regress

- `tests/test_slack_presentation.py`, `tests/test_live_notification_lifecycle.py`,
  `tests/test_kis_live_slack_separation.py`, `tests/test_scanner_monitor.py` all green.
- New S6 codes added to `operations/slack_presentation.REASON_LABELS`; the block message
  gains `ORB: N분` and four compact quality lines; the fill message gains `ORB: N분` and an
  optional `돌파 후 경과` / `최근 거래량` line. No new webhook path: S6 blocks go through
  `live_notifications.notify(ORDER_BLOCKED, ..., dedupe_conn=...)`.

## H. No Slack output controls trading

- `tests/test_slack_presentation.py::TestDurableBehaviourUnchanged::test_notify_results_never_steer_control_flow`
  (AST) and `tests/test_slack_failure_isolation.py`; `_announce_quality_blocks` swallows a
  raising notifier (`TestSlackForS6Blocks::test_the_entry_runner_announces_only_quality_blocks_and_never_raises`).

## I. No hidden order path from ORB15

- The only orderable symbol set is `WatchedCandidateSource.symbols()` → `run_live_buy_entry_cycle`;
  the shadow writes a file and returns. `candidate_source` filters rows by session variant
  `S6-P`; shadow rows are never written to the candidate directory (AST test on `publish`).

## J. Replay is not contaminated by future data

- `entry_quality.compute` drops bars after `now`; `replay_s6_premarket.first_signal` builds
  features from `bars[:index+1]` and takes the entry at `bars[index+1].open`; outcomes use
  `bars[index+1:]` only. Candidate `generated_at` is never used as a decision clock.
- Test: `TestOpeningRangeAndBreakout::test_future_bars_are_never_used`.

## K. The active-watch runtime (all S6 sessions)

The latency this release targets is discovery-to-READY, which was 47-93
minutes: S6's full scan runs at `2,17,32,47` past the hour, so a breakout
found at minute 3 waited for the next scan and then for the next entry tick.

- Owner: `scripts/run_live_buy_entry.py::_s6_source`. For every session in
  `s6_sessions.SCAN_SESSIONS` it returns `s6_live.fast_watch.ActiveWatchSource`;
  a session S6 does not scan returns the published-candidate source unchanged.
  One engine, four sessions -- no session-specific runtime.
- Scope: a watchlist belongs to one (session start date, session). The scope
  key is the session's START date, not the trading day, because
  OVERNIGHT_DAYTIME opens at 20:00 ET and `us_trading_day` rolls underneath it
  at midnight. Expiry is each session's own close, from the canonical window
  table, and a wrapping session expires on the following date. There is exactly one owner and no new
  process: the existing `deploy/cron/s6_buy_entry.sh` already runs
  `* * * * 1-5`, with `flock -n` on `s6_entry.lock` so a slow tick is dropped
  rather than overlapped. **Cadence is therefore one minute**, and no cron
  line is added or changed by this release.
- Store: `s6_live/active_watch.py`, one JSON file per trading day and session
  under `SCANNER_DATA_ROOT/s6_active_watch/`, written under `fcntl.flock` via
  a temp file and `os.replace`. Restart-safe (`added_at` survives a merge),
  day-scoped (a file for another day reads `STALE_SCOPE`), and expiring
  (`EXPIRED` at 09:30 ET). A payload holding more than the cap reads
  `OVER_CAPACITY` and yields no symbols -- it never silently truncates on the
  read path.
- Capacity: `MAX_SYMBOLS = market_data.kis_hdfscnt0.MAX_SUBSCRIPTIONS` (41,
  the measured provider ceiling). `merge()` clamps any caller-supplied cap to
  that value, so a test may lower it and nothing can raise it. Admission is
  deterministic: completed S6 discovery first, then current collector
  members, in that order, and the overflow count is persisted as `dropped`.
- Provider load: subscribed names are local snapshot reads and cost no REST
  call. A discovered name outside the stream costs one measured chart read
  (`kis_minute_chart.MEASURED_SECONDS_PER_SYMBOL = 2.44 s`) and those are
  bounded by the existing 30-second pretrade budget, i.e. at most 12 per
  tick; `pretrade_validation.Budget` defers the remainder to the next minute
  rather than overrunning. Worst case is unchanged from today's per-tick
  budget, which is why no new limiter is introduced.
- No order path: `ActiveWatchSource` imports no broker or execution module.
  It offers READY symbols to the same shared cycle S1/S2 use; qualification,
  sizing, the execution lock, revalidation and the gate are untouched.

## L. Two defects found and fixed while verifying this package

Both come from the same mistake -- treating the gap between premarket bars as
the width of a bar. Premarket frames contain only minutes that traded, so on
the replay sample the median gap is 5.5-7 minutes and reaches 66.

1. `s6_live/fast_watch.py` stamped `provenance.signal_timestamp` as
   `bar_open + entry_quality.bar_interval_minutes`, intending the bar's
   close. On sparse data that is a timestamp minutes to an hour in the
   FUTURE, which corrupts lineage and the S1-S6 comparison analytics. The
   width is one minute by construction (the collector store and the
   closed-bar filter both work on a whole-minute grid), so the module now
   uses `BAR_WIDTH_MINUTES = 1.0` and never the observed cadence. This did
   not bypass the pipeline budget -- `execution/signal_validity` anchors that
   at acceptance on the wall clock, not at this timestamp.
2. `scanners/base/session_range.closed_bars()` inferred the bar width from
   the median row gap, so on sparse premarket frames it discarded bars that
   had closed long ago -- safe, but it delays exactly the decision this
   release is trying to make faster. It now accepts an explicit
   `interval_seconds`, and `s6_live/realtime_features` passes the resolution
   it actually asked the provider for. Inference remains the default, so the
   S6 scanner path is unchanged.

Tests: `tests/test_s6_active_watch_runtime.py::TestSparseBarsDoNotDistortTimestamps`.

## Commands

    venv/bin/python -m pytest -q tests/test_s6_orb5_entry_quality.py \
        tests/test_s6_active_watch_runtime.py tests/test_slack_presentation.py
    venv/bin/python -m pytest -q            # full suite
    git status --porcelain; git diff --stat
