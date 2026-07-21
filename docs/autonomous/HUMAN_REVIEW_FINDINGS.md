# Human Review Findings

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
