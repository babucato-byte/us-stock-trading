"""Daily-loss, drawdown and consecutive-loss gates for new S1 entries.

Thresholds are REUSED, enforcement is NEW
-----------------------------------------
`risk_config.MAX_DAILY_LOSS_RATE` (-2%) and `MAX_TOTAL_DRAWDOWN` (-10%)
already exist. What did not exist is any code on the KIS path that reads
them: `account_risk.py` enforces the daily figure against the ALPACA
account endpoint and is not imported by `kis_live_trading`, and
`MAX_TOTAL_DRAWDOWN` was enforced nowhere at all. No new number is
invented here; the numbers are imported and the enforcement is written.

Unknown blocks. It does not pass.
---------------------------------
Each guard returns one of three verdicts:

    ALLOW    measured, and inside the limit
    BLOCK    measured, and at or beyond the limit
    UNKNOWN  could not be measured

UNKNOWN blocks new entries, for the same reason `execution/entry_limits.py`
refuses to fall back to a count: "a count of zero is the single most
dangerous wrong answer a limit checker can give". A daily-loss guard that
cannot see today's P&L and answers ALLOW is not a risk control, it is a
risk control that is off. So the honest answer is "I cannot assert you
are within the limit", and the honest response to that is not to enter.

Why UNKNOWN is the CURRENT state on KIS
---------------------------------------
`account_risk.check_daily_loss_limit()` computes
`(equity - last_equity) / last_equity` from Alpaca's account endpoint.
KIS's balance response (TTTS3012R) carries neither figure -- see
`domain/account_snapshot.py`, which documents that it returns nine
purchase/valuation/P&L fields and no deposit at all. So the existing
convention's denominator, yesterday's equity, has no KIS source, and the
same is true of the peak equity a drawdown needs. Both are therefore
supplied by the caller as an explicit basis, and both guards answer
UNKNOWN until something supplies one. That gap is reported rather than
papered over with a guessed baseline.

Exits are never gated
---------------------
Everything here answers one question: may a NEW entry be opened. No
function in this module is consulted on the sell side. A drawdown limit
that also blocked liquidation would trap the account in exactly the
position the limit exists to escape.
"""

import logging
import math
from dataclasses import dataclass
from typing import Optional

import risk_config

logger = logging.getLogger(__name__)

ALLOW = "ALLOW"
BLOCK = "BLOCK"
UNKNOWN = "UNKNOWN"

REASON_DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
REASON_DAILY_LOSS_UNKNOWN = "DAILY_LOSS_STATE_UNKNOWN"
REASON_DRAWDOWN_LIMIT = "DRAWDOWN_LIMIT"
REASON_DRAWDOWN_UNKNOWN = "DRAWDOWN_STATE_UNKNOWN"
REASON_CONSECUTIVE_LOSS_LIMIT = "CONSECUTIVE_LOSS_LIMIT"
REASON_CONSECUTIVE_LOSS_UNCONFIGURED = "CONSECUTIVE_LOSS_UNCONFIGURED"

#: P&L basis names. The existing convention (Alpaca equity vs
#: last_equity) is equity-based and therefore INCLUDES unrealized P&L,
#: which is also the more conservative of the two choices: a position
#: sitting at -5% counts against today's budget before it is closed.
BASIS_REALIZED_AND_UNREALIZED = "realized_and_unrealized"
BASIS_REALIZED_ONLY = "realized_only"


@dataclass(frozen=True)
class GuardResult:
    verdict: str
    reason_code: Optional[str] = None
    detail: str = ""
    measured: Optional[float] = None
    threshold: Optional[float] = None
    basis: Optional[str] = None

    @property
    def allows_entry(self) -> bool:
        return self.verdict == ALLOW

    def as_dict(self):
        return {
            "verdict": self.verdict, "reason_code": self.reason_code,
            "detail": self.detail, "measured": self.measured,
            "threshold": self.threshold, "basis": self.basis,
        }


def _finite(value) -> Optional[float]:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def check_daily_loss(*, pnl_today_usd, basis_equity_usd,
                     basis=BASIS_REALIZED_AND_UNREALIZED,
                     max_daily_loss_rate=None) -> GuardResult:
    """May a new entry be opened given today's P&L?

    `pnl_today_usd` is SIGNED: negative is a loss. `basis_equity_usd` is
    the denominator -- the equity the day started from, matching
    `account_risk.check_daily_loss_limit()`'s `last_equity`.

    Either being unmeasurable yields UNKNOWN, which blocks. A basis of
    zero or less also yields UNKNOWN rather than a division: an account
    with no measurable starting equity has no meaningful loss RATE, and
    inventing one would be worse than admitting it.
    """
    threshold = _finite(
        max_daily_loss_rate if max_daily_loss_rate is not None
        else risk_config.MAX_DAILY_LOSS_RATE)
    if threshold is None or threshold >= 0:
        return GuardResult(UNKNOWN, REASON_DAILY_LOSS_UNKNOWN, basis=basis,
                           detail=f"MAX_DAILY_LOSS_RATE {threshold!r} is not a negative rate")

    pnl = _finite(pnl_today_usd)
    equity = _finite(basis_equity_usd)
    if pnl is None:
        return GuardResult(UNKNOWN, REASON_DAILY_LOSS_UNKNOWN, basis=basis,
                           threshold=threshold,
                           detail="today's P&L could not be established")
    if equity is None or equity <= 0:
        return GuardResult(UNKNOWN, REASON_DAILY_LOSS_UNKNOWN, basis=basis,
                           threshold=threshold,
                           detail="the day's starting equity could not be established; "
                                  "KIS's balance response carries no equity figure")

    rate = pnl / equity
    if rate <= threshold:
        return GuardResult(BLOCK, REASON_DAILY_LOSS_LIMIT, basis=basis,
                           measured=round(rate, 6), threshold=threshold,
                           detail=f"today's return {rate:.2%} is at or beyond "
                                  f"{threshold:.2%}; new S1 entries are blocked")
    return GuardResult(ALLOW, basis=basis, measured=round(rate, 6), threshold=threshold)


def check_drawdown(*, equity_usd, peak_equity_usd, max_total_drawdown=None) -> GuardResult:
    """May a new entry be opened given the drawdown from peak?

    `peak_equity_usd` is the high-water mark. This project has no live
    peak-equity tracker -- `backtest_multi.py` computes one with
    `equity_curve.cummax()` but that is a backtest, and nothing on the
    live path records it. Rather than invent a second definition, the
    peak is an input; absent it, the answer is UNKNOWN.
    """
    threshold = _finite(
        max_total_drawdown if max_total_drawdown is not None
        else risk_config.MAX_TOTAL_DRAWDOWN)
    if threshold is None or threshold >= 0:
        return GuardResult(UNKNOWN, REASON_DRAWDOWN_UNKNOWN,
                           detail=f"MAX_TOTAL_DRAWDOWN {threshold!r} is not a negative rate")

    equity = _finite(equity_usd)
    peak = _finite(peak_equity_usd)
    if equity is None or peak is None or peak <= 0:
        return GuardResult(UNKNOWN, REASON_DRAWDOWN_UNKNOWN, threshold=threshold,
                           detail="equity or peak equity could not be established; "
                                  "this project records no live high-water mark")

    drawdown = (equity - peak) / peak
    if drawdown <= threshold:
        return GuardResult(BLOCK, REASON_DRAWDOWN_LIMIT,
                           measured=round(drawdown, 6), threshold=threshold,
                           detail=f"drawdown {drawdown:.2%} is at or beyond "
                                  f"{threshold:.2%}; new S1 entries are blocked")
    return GuardResult(ALLOW, measured=round(drawdown, 6), threshold=threshold)


def check_consecutive_losses(*, consecutive_losses, limit=None) -> GuardResult:
    """Structure and state interface only -- no threshold is set.

    PHASE 4A §11: no validated consecutive-loss limit exists anywhere in
    this project, so none is invented. With `limit` unset the guard
    reports UNCONFIGURED and does NOT block: unlike the two guards above,
    there is no measurement being attempted and therefore nothing to be
    uncertain about. The count is still computed and recorded, so the
    threshold can be chosen from real S1 data later rather than guessed
    now.
    """
    if limit is None:
        return GuardResult(ALLOW, REASON_CONSECUTIVE_LOSS_UNCONFIGURED,
                           measured=_finite(consecutive_losses),
                           detail="no consecutive-loss limit is configured; "
                                  "counting only, pending S1 live data")
    bound = _finite(limit)
    count = _finite(consecutive_losses)
    if bound is None or bound < 1 or count is None:
        return GuardResult(UNKNOWN, REASON_CONSECUTIVE_LOSS_LIMIT,
                           measured=count, threshold=bound,
                           detail="the consecutive-loss state could not be established")
    if count >= bound:
        return GuardResult(BLOCK, REASON_CONSECUTIVE_LOSS_LIMIT,
                           measured=count, threshold=bound,
                           detail=f"{int(count)} consecutive losses reached the limit "
                                  f"of {int(bound)}; new S1 entries are blocked")
    return GuardResult(ALLOW, measured=count, threshold=bound)


def evaluate_all(*, pnl_today_usd=None, basis_equity_usd=None, equity_usd=None,
                 peak_equity_usd=None, consecutive_losses=0,
                 consecutive_loss_limit=None):
    """Every guard, in one call. Returns (allowed, [GuardResult, ...]).

    ALL must allow. The first non-allowing result is the operative one
    for reporting, but every result is returned so an operator can see
    which controls were measurable and which were not.
    """
    results = [
        check_daily_loss(pnl_today_usd=pnl_today_usd, basis_equity_usd=basis_equity_usd),
        check_drawdown(equity_usd=equity_usd, peak_equity_usd=peak_equity_usd),
        check_consecutive_losses(consecutive_losses=consecutive_losses,
                                 limit=consecutive_loss_limit),
    ]
    return all(item.allows_entry for item in results), results
