# Human Review Findings

## 2026-07-22: BrokerConfig env-var mode/flags are frozen at first import, not re-read per instantiation

**Context:** Task was to review `broker/broker_config.py`'s `BrokerConfig`
validation for paper/live confusion paths (missing env-var combinations,
typo'd values, default fallbacks, endpoint/mode mismatches) and add missing
test coverage to `tests/test_broker_safety.py` without modifying `broker/**`.
Most of the paths checked out safe and are now pinned by tests added this
pass: missing env vars fall back to paper/disabled/dry-run
(`test_missing_all_env_vars_falls_back_to_safe_paper_defaults`); typo'd
`TRADING_MODE` values (e.g. `"papre"`) match neither `is_paper_mode` nor
`is_live_mode` and `validate_order_allowed()` fails closed
(`test_env_trading_mode_typo_blocks_order_after_reload`); case/whitespace in
a legitimate value is normalized
(`test_env_trading_mode_case_and_whitespace_normalized`); a live mode with
only `TRADING_MODE` set and the two safety flags left unset still falls back
to `LIVE_DRY_RUN` and blocks orders
(`test_live_mode_env_with_missing_flags_falls_back_to_dry_run`); and a paper
mode whose `ALPACA_PAPER_BASE_URL` is mistakenly pointed at the live host is
blocked (`test_env_paper_base_url_set_to_live_host_blocks_orders`).

**Finding (not fixed — `broker/**` out of scope for this task):**
`BrokerConfig`'s fields (`trading_mode`, `enable_real_trading`,
`live_dry_run`, `paper_base_url`, `live_base_url`, `api_key`, `secret_key`)
are declared as plain dataclass defaults —
`os.getenv("TRADING_MODE", ...).strip().lower()` and similar — which Python
evaluates exactly **once**, when `broker/broker_config.py` is first
imported, not on every `BrokerConfig()` call. Concretely:

```python
import os
os.environ.pop("TRADING_MODE", None)
import broker.broker_config as bc      # first import: trading_mode defaults to "paper"
os.environ["TRADING_MODE"] = "live"
bc.BrokerConfig().trading_mode          # still "paper" — the env change is invisible
```

`dashboard/app.py` calls `BrokerConfig()` fresh on every request
(`dashboard/app.py:115,180,236`, a long-running Flask process) — this reads
as "always reflects current config" but is actually a no-op with respect to
environment changes made after the process started; only a full process
restart re-reads `TRADING_MODE`/`ENABLE_REAL_TRADING`/`LIVE_DRY_RUN`. This is
inconsistent with `kill_switch.is_trading_halted()` (`kill_switch.py:22`),
which deliberately re-reads `os.environ`/the sentinel file on every call so
an operator's emergency halt takes effect immediately without a restart. If
an operator ever expects flipping `TRADING_MODE` back to `paper` (or
`ENABLE_REAL_TRADING` back to `false`) in a running process's environment to
have the same immediate effect as the kill switch, it silently won't — the
process keeps trading under whatever config was in memory at first import
until it is restarted. (The reverse direction — flipping *into* a more
permissive mode without a restart — is not exploitable this way, since it
equally fails to take effect and the process just keeps running under its
original, presumably safer, config.)

**Recommended direction (not implemented here):** re-read `os.environ`
inside a classmethod/property or `default_factory` evaluated per instance
(matching `kill_switch.py`'s pattern) instead of module-import-time
dataclass field defaults, so a fresh `BrokerConfig()` in a long-running
process actually reflects the current environment.

**Verification:** current (unfixed) behavior is pinned by
`tests/test_broker_safety.py::test_env_change_after_first_import_is_not_observed_without_reload`,
which sets `TRADING_MODE`/`ENABLE_REAL_TRADING`/`LIVE_DRY_RUN` via
`monkeypatch` after `broker_config` is already imported and asserts the
resulting `BrokerConfig()` still reflects the pre-existing (baseline) mode,
not the newly-set environment. The other new tests in that file reload
`broker.broker_config` via `importlib.reload()` after `monkeypatch` to
simulate a fresh process start, which is required to actually exercise the
`os.getenv(...)` fallback/typo/normalization logic at all.

## 2026-07-22: order_intent_ledger retry now propagates through order_history.csv's same-day duplicate gate

**Context:** `order_intent_ledger.py` is a restart-safe two-phase (`reserve`
-> `commit`/`abort`) record keyed by `(symbol, trade_date,
client_order_id)`, wired into `paper_strategy_order.py`'s
`try_reserve_order()` and `main()`. The crash window described in the
original task ("order submission succeeds, then the process dies before
order_history.csv is saved") does not exist as literally stated:
`try_reserve_order()` already writes a `PENDING_SUBMISSION` row to
`order_history.csv` *before* `submit_order()` is ever called, so a crash
after a successful submit can only leave a row already on disk, never lose
one. The genuinely open gap was the inverse case: a crash (or any process
restart) between writing that reservation and recording its outcome, which
`order_intent_ledger.py` closes with an explicit, durable RESERVED /
COMMITTED / ABORTED state independent of `order_history.csv`'s `status`
column.

**Finding (previously deferred, now fixed):** A prior pass of this task
proved the ledger itself permits a fresh reservation after an explicit
`abort()` (see
`tests/test_restart_duplicate_order.py::test_aborted_intent_after_submission_failure_allows_retry`),
but left `main()`'s pre-existing `is_duplicate_order(order_history, symbol,
today)` gate unchanged, which matched on `(symbol, order_date)` alone and
ignored `status`. A round-trip review caught that this meant the
`SUBMISSION_FAILED` row left by a real Timeout during `main()` still blocked
`try_reserve_order()` on every later run for the same trading day, so the
ledger's abort/retry guarantee never actually reached the real order path
(only direct `order_intent_ledger.reserve()` calls in a test could observe
it).

**Fix applied this pass (all within `paper_strategy_order.py`):**
- `is_duplicate_order()` now excludes rows with `status ==
  "SUBMISSION_FAILED"` from the match. Every other status (including
  `PENDING_SUBMISSION`, meaning the previous run's outcome is still unknown)
  continues to block, unchanged.
- `try_reserve_order()` replaces a stale `SUBMISSION_FAILED` row for
  `(symbol, order_date)` instead of appending a second row, preserving the
  "at most one row per `(symbol, order_date)`" invariant that
  `update_order_status()` and the reconciliation correlation depend on.
- `main()`'s `requests.exceptions.RequestException` handler now also marks
  that attempt's `order_reconciliation.csv` row `local_status =
  "SUBMISSION_FAILED"` (a new terminal status, added to
  `RECONCILIATION_TERMINAL_STATUSES`). Without this, `reconcile_pending_orders()`
  would re-check that never-reached-broker `client_order_id` on the very
  next run, find no match, and overwrite `order_history`'s
  `SUBMISSION_FAILED` status with `MANUAL_REVIEW` before the retry could
  ever reach `try_reserve_order()` -- silently reintroducing the same block
  through a different code path.

**Verification:**
`tests/test_restart_duplicate_order.py::test_submission_failure_retry_actually_resubmits_through_main`
drives two full `pso.main()` calls against a broker double that times out on
the first `submit_order()` call for a symbol and succeeds on the second,
and asserts `submit_order()` is actually invoked a second time, exactly one
`order_history.csv` row remains for that `(symbol, order_date)`, and the
ledger shows the first `client_order_id` `ABORTED` and the second
`COMMITTED`. This is in addition to (not a replacement for) the
ledger-level-only test from the prior pass, which still passes unchanged.
Existing tests that pin `SUBMISSION_FAILED` as the terminal status after a
single failed run (e.g.
`tests/test_paper_order_execution.py::test_broker_timeout_is_handled_safely_and_next_symbol_continues`)
are unaffected, since none of them issue a second `main()` call.
