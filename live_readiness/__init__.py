"""Stage 10 (사용자 지시서): 30,000 KRW limited-live-trading preparation.

Pure, isolated, testable calculations that a future limited-live rollout
needs -- micro-order quantity sizing (with a fractional-share check and a
minimum-order-amount check) and a fail-closed symbol allow-list check.

Deliberately NOT wired into the live order-submission path
(paper_strategy_order.py / positions/lifecycle.py) by this stage: those
modules are the safety-critical network boundary that has already been
through the CODEX-016~022 remediation arc and a Codex PASS_WITH_CONDITIONS
verdict. Splicing new logic into that path this late, without an
independent Codex review of the change, would be exactly the kind of
unreviewed live-path modification the project's governance docs warn
against. This package's functions are the building blocks; actually
calling them from the live path is recorded as an explicit residual
decision in docs/live_review/LIMITED_LIVE_30K_KRW_PLAYBOOK.md, to be
made when the limited-live review itself happens (with real account
data, not the placeholder TBD_OPERATOR values this stage necessarily
uses).

Modules:
  sizing.py    -- calculate_micro_order_quantity(): KRW budget -> whole-
                   share USD quantity, fail-closed on insufficient funds
                   or below-minimum order value.
  allowlist.py -- is_symbol_allowed(): fail-closed (empty/unset allow-list
                   permits nothing) symbol gate.
"""
