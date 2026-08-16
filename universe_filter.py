"""T8: account-budget + liquidity filter over the raw asset universe.

Why this is a SEPARATE output file and not an edit to `universe.csv`
-------------------------------------------------------------------
`universe.csv` is not only the scanner's candidate feed -- it is also the
authoritative exchange-metadata source for the KIS order path:
`market_data/exchange_registry.py::ExchangeRegistry._load_universe()`
reads it, and `resolve()` returns `EXCHANGE_UNKNOWN` for any symbol that
is not in it. That resolution runs on SELLS too, not only buys. Shrinking
`universe.csv` down to "what the account can currently afford to buy"
would therefore make a held position whose price has since risen above
the budget ceiling unresolvable, i.e. it could block an EXIT. That is a
safety regression, so `universe.csv` keeps its existing full-listing
shape and this module writes a second, entry-side-only file
(`universe_tradable.csv`).

What the filter actually does
-----------------------------
Per-symbol decision with an explicit reason code for BOTH outcomes (the
report has to explain inclusions as well as exclusions -- see
`summarize()`); no symbol is ever silently dropped.

    price_ceiling_usd = available_cash_usd
                        * (cash_usage_percent / 100)   # trusted operator ceiling
                        * MAX_POSITION_RATE            # risk_config position ratio

`cash_usage_percent` is read only from
`live_readiness/trusted_operator_config.py` (PROJECT_CONSTITUTION
"계층 분리 원칙"), never from a caller. It is applied on top of, not
instead of, `risk_config.MAX_POSITION_RATE`, so the ceiling is strictly
tighter than the position ratio alone -- fail-safe direction.

Whole shares only: `max_affordable_shares = floor(ceiling / price)` and a
symbol is included only when that is >= 1. There is no fractional path
here at all (project principle: 소수점 매수 금지), which is why this
module does not reuse `live_readiness/watchlist_affordability.py` --
that module's whole point is the KRW/fractional/reservation-aware
per-candidate decision at order time, and it can return
AFFORDABLE_FRACTIONAL, which is not a legal outcome for this universe.

Liquidity floor is not invented here either: it is read from the
scanner's own `config/scanner_rules.json` filters (`avg_dollar_volume`,
`price`), so the universe cannot admit a symbol the scanner would reject
one step later.
"""

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from config.paths import get_project_root
from domain.exchange import UnsupportedExchangeError, normalize_exchange
from live_readiness.trusted_operator_config import get_cash_usage_percent
from risk_config import MAX_POSITION_RATE

# Decision reason codes. Exactly one is attached to every evaluated symbol.
REASON_INCLUDED = "INCLUDED"
REASON_UNSUPPORTED_EXCHANGE = "EXCLUDED_UNSUPPORTED_EXCHANGE"
REASON_NO_PRICE_DATA = "EXCLUDED_NO_PRICE_DATA"
REASON_NO_LIQUIDITY_DATA = "EXCLUDED_NO_LIQUIDITY_DATA"
REASON_PRICE_BELOW_FLOOR = "EXCLUDED_PRICE_BELOW_FLOOR"
REASON_PRICE_ABOVE_BUDGET = "EXCLUDED_PRICE_ABOVE_BUDGET"
REASON_ILLIQUID = "EXCLUDED_ILLIQUID"

ALL_REASONS = (
    REASON_INCLUDED,
    REASON_UNSUPPORTED_EXCHANGE,
    REASON_NO_PRICE_DATA,
    REASON_NO_LIQUIDITY_DATA,
    REASON_PRICE_BELOW_FLOOR,
    REASON_PRICE_ABOVE_BUDGET,
    REASON_ILLIQUID,
)

# Fallbacks used only when config/scanner_rules.json is missing or its
# filter list does not carry the field. They match
# daily_candidate_scanner.DEFAULT_FILTERS exactly -- duplicated as plain
# constants rather than imported because importing that module pulls in
# yfinance, dotenv and the Slack client just to read two numbers.
DEFAULT_MIN_PRICE_USD = 5.0
DEFAULT_MIN_AVG_DOLLAR_VOLUME_USD = 20_000_000.0

SCANNER_RULES_FILE = get_project_root() / "config" / "scanner_rules.json"


class UniverseFilterError(Exception):
    """Raised when the filter cannot be run safely at all (e.g. an
    unusable budget). Never raised for a single bad symbol -- those are
    per-symbol exclusions."""


def _is_finite_positive(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


@dataclass(frozen=True)
class ScannerThresholds:
    """The scanner's own price/liquidity floors, reused verbatim."""

    min_price_usd: float
    min_avg_dollar_volume_usd: float
    source: str


def _numeric_filter_value(filters, field_name):
    """Returns the `>=` threshold the scanner applies to `field_name`, or
    None. Only `>=` is honoured: any other operator means the scanner is
    doing something this module has not been taught to mirror, and
    guessing would silently loosen the universe."""
    for entry in filters or []:
        if not isinstance(entry, dict) or entry.get("field") != field_name:
            continue
        if entry.get("operator") != ">=":
            continue
        value = entry.get("value")
        if _is_finite_positive(value):
            return float(value)
    return None


def load_scanner_thresholds(rules_file=None):
    """Reads the scanner's active rules file. A missing/corrupt file falls
    back to the documented defaults rather than raising -- the universe
    build must still run, and the fallbacks are the same numbers the
    scanner itself falls back to."""
    path = Path(rules_file) if rules_file is not None else SCANNER_RULES_FILE
    try:
        with open(path, "r", encoding="utf-8") as handle:
            rules = json.load(handle)
    except (OSError, ValueError):
        return ScannerThresholds(
            min_price_usd=DEFAULT_MIN_PRICE_USD,
            min_avg_dollar_volume_usd=DEFAULT_MIN_AVG_DOLLAR_VOLUME_USD,
            source="defaults",
        )
    if not isinstance(rules, dict):
        return ScannerThresholds(
            min_price_usd=DEFAULT_MIN_PRICE_USD,
            min_avg_dollar_volume_usd=DEFAULT_MIN_AVG_DOLLAR_VOLUME_USD,
            source="defaults",
        )
    filters = rules.get("filters")
    min_price = _numeric_filter_value(filters, "price")
    min_dollar_volume = _numeric_filter_value(filters, "avg_dollar_volume")
    return ScannerThresholds(
        min_price_usd=DEFAULT_MIN_PRICE_USD if min_price is None else min_price,
        min_avg_dollar_volume_usd=(
            DEFAULT_MIN_AVG_DOLLAR_VOLUME_USD if min_dollar_volume is None else min_dollar_volume
        ),
        source=str(path),
    )


@dataclass(frozen=True)
class SymbolMetrics:
    """Per-symbol market facts needed by the filter. `None` means "not
    available" and is never substituted with a default -- an unknown
    price excludes the symbol, it does not admit it."""

    symbol: str
    price_usd: Optional[float] = None
    avg_dollar_volume_usd: Optional[float] = None


@dataclass(frozen=True)
class UniverseBudget:
    """The account-side input, computed ONCE per build and shared by every
    symbol decision (same discipline as
    live_readiness/watchlist_affordability.AccountState)."""

    available_cash_usd: float
    as_of: str
    source: str
    position_rate: float = MAX_POSITION_RATE
    cash_usage_percent: Optional[float] = None

    def validation_error(self):
        if not (
            isinstance(self.available_cash_usd, (int, float))
            and not isinstance(self.available_cash_usd, bool)
            and math.isfinite(self.available_cash_usd)
            and self.available_cash_usd >= 0
        ):
            return f"available_cash_usd must be a non-negative finite number, got {self.available_cash_usd!r}"
        if not _is_finite_positive(self.position_rate) or self.position_rate > 1:
            return f"position_rate must be in (0, 1], got {self.position_rate!r}"
        if self.cash_usage_percent is not None:
            if not _is_finite_positive(self.cash_usage_percent) or self.cash_usage_percent > 100:
                return f"cash_usage_percent must be in (0, 100], got {self.cash_usage_percent!r}"
        if not isinstance(self.source, str) or not self.source.strip():
            return f"source must be a non-empty string, got {self.source!r}"
        return None

    @property
    def effective_cash_usage_percent(self):
        """Caller-supplied percent can only ever LOWER the trusted operator
        value (min()), matching order_gateway.py / account_engine.py."""
        trusted = get_cash_usage_percent()
        if self.cash_usage_percent is None:
            return trusted
        return min(self.cash_usage_percent, trusted)

    @property
    def price_ceiling_usd(self):
        """Most expensive single share the account may buy right now."""
        return (
            self.available_cash_usd
            * (self.effective_cash_usage_percent / 100.0)
            * self.position_rate
        )


@dataclass(frozen=True)
class UniverseDecision:
    symbol: str
    included: bool
    reason: str
    detail: str
    price_usd: Optional[float] = None
    avg_dollar_volume_usd: Optional[float] = None
    price_ceiling_usd: Optional[float] = None
    max_affordable_shares: int = 0
    exchange: Optional[str] = None


def max_affordable_whole_shares(price_ceiling_usd, price_usd):
    """Whole shares only -- floor(), never round(). Returns 0 for any
    unusable input rather than raising, so one bad price cannot abort a
    12,000-symbol build."""
    if not _is_finite_positive(price_usd) or not _is_finite_positive(price_ceiling_usd):
        return 0
    return int(math.floor(price_ceiling_usd / price_usd))


def evaluate_symbol(row, metrics, budget, thresholds):
    """Evaluates one asset row. Never raises.

    `row` is a mapping with at least `symbol` and `exchange` (the shape
    universe_builder.fetch_active_us_equity_rows() already produces).
    """
    symbol = str(row.get("symbol") or "").strip().upper()
    raw_exchange = row.get("exchange")
    ceiling = budget.price_ceiling_usd

    def _decide(included, reason, detail, exchange=None, shares=0):
        return UniverseDecision(
            symbol=symbol,
            included=included,
            reason=reason,
            detail=detail,
            price_usd=metrics.price_usd if metrics is not None else None,
            avg_dollar_volume_usd=metrics.avg_dollar_volume_usd if metrics is not None else None,
            price_ceiling_usd=ceiling,
            max_affordable_shares=shares,
            exchange=exchange,
        )

    try:
        exchange = normalize_exchange(raw_exchange).value
    except UnsupportedExchangeError:
        return _decide(
            False, REASON_UNSUPPORTED_EXCHANGE,
            f"exchange {raw_exchange!r} is not one this system trades",
        )

    if metrics is None or metrics.price_usd is None:
        return _decide(False, REASON_NO_PRICE_DATA, "no usable price", exchange)
    if not _is_finite_positive(metrics.price_usd):
        return _decide(
            False, REASON_NO_PRICE_DATA,
            f"price {metrics.price_usd!r} is not a positive finite number", exchange,
        )

    if metrics.avg_dollar_volume_usd is None:
        return _decide(False, REASON_NO_LIQUIDITY_DATA, "no usable average dollar volume", exchange)
    if not (
        isinstance(metrics.avg_dollar_volume_usd, (int, float))
        and not isinstance(metrics.avg_dollar_volume_usd, bool)
        and math.isfinite(metrics.avg_dollar_volume_usd)
        and metrics.avg_dollar_volume_usd >= 0
    ):
        return _decide(
            False, REASON_NO_LIQUIDITY_DATA,
            f"average dollar volume {metrics.avg_dollar_volume_usd!r} is not a usable number", exchange,
        )

    if metrics.price_usd < thresholds.min_price_usd:
        return _decide(
            False, REASON_PRICE_BELOW_FLOOR,
            f"price {metrics.price_usd:.4f} < scanner floor {thresholds.min_price_usd:.4f}", exchange,
        )

    if metrics.avg_dollar_volume_usd < thresholds.min_avg_dollar_volume_usd:
        return _decide(
            False, REASON_ILLIQUID,
            f"avg dollar volume {metrics.avg_dollar_volume_usd:.0f} < scanner floor "
            f"{thresholds.min_avg_dollar_volume_usd:.0f}", exchange,
        )

    shares = max_affordable_whole_shares(ceiling, metrics.price_usd)
    if shares < 1:
        return _decide(
            False, REASON_PRICE_ABOVE_BUDGET,
            f"1 whole share at {metrics.price_usd:.4f} exceeds per-position ceiling {ceiling:.2f} USD",
            exchange,
        )

    return _decide(
        True, REASON_INCLUDED,
        f"{shares} whole share(s) affordable at {metrics.price_usd:.4f} "
        f"(ceiling {ceiling:.2f} USD)",
        exchange, shares,
    )


def filter_universe(rows, metrics_by_symbol, budget, thresholds=None):
    """Evaluates every row against ONE budget snapshot. Returns all
    decisions (included and excluded) in input order.

    Raises UniverseFilterError only for an unusable budget -- a build with
    a bad account figure must fail closed rather than emit a universe
    derived from a fabricated ceiling.
    """
    error = budget.validation_error()
    if error is not None:
        raise UniverseFilterError(f"unusable universe budget: {error}")
    thresholds = thresholds or load_scanner_thresholds()
    decisions = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        decisions.append(
            evaluate_symbol(row, metrics_by_symbol.get(symbol), budget, thresholds)
        )
    return decisions


@dataclass
class FilterSummary:
    total: int
    included: int
    excluded: int
    reason_counts: Dict[str, int] = field(default_factory=dict)
    price_ceiling_usd: Optional[float] = None
    budget_source: Optional[str] = None
    budget_as_of: Optional[str] = None
    available_cash_usd: Optional[float] = None
    cash_usage_percent: Optional[float] = None
    position_rate: Optional[float] = None
    min_price_usd: Optional[float] = None
    min_avg_dollar_volume_usd: Optional[float] = None
    thresholds_source: Optional[str] = None

    def as_dict(self):
        return {
            "total": self.total,
            "included": self.included,
            "excluded": self.excluded,
            "reason_counts": dict(self.reason_counts),
            "price_ceiling_usd": self.price_ceiling_usd,
            "budget_source": self.budget_source,
            "budget_as_of": self.budget_as_of,
            "available_cash_usd": self.available_cash_usd,
            "cash_usage_percent": self.cash_usage_percent,
            "position_rate": self.position_rate,
            "min_price_usd": self.min_price_usd,
            "min_avg_dollar_volume_usd": self.min_avg_dollar_volume_usd,
            "thresholds_source": self.thresholds_source,
        }


def summarize(decisions, budget=None, thresholds=None) -> FilterSummary:
    """Counts by reason. Every reason in ALL_REASONS is present in the
    output even at zero, so a report reader can tell "no symbol hit this
    rule" apart from "this rule was not evaluated"."""
    counts = {reason: 0 for reason in ALL_REASONS}
    included = 0
    for decision in decisions:
        counts[decision.reason] = counts.get(decision.reason, 0) + 1
        if decision.included:
            included += 1
    total = len(decisions)
    summary = FilterSummary(
        total=total, included=included, excluded=total - included, reason_counts=counts,
    )
    if budget is not None:
        summary.price_ceiling_usd = budget.price_ceiling_usd
        summary.budget_source = budget.source
        summary.budget_as_of = budget.as_of
        summary.available_cash_usd = budget.available_cash_usd
        summary.cash_usage_percent = budget.effective_cash_usage_percent
        summary.position_rate = budget.position_rate
    if thresholds is not None:
        summary.min_price_usd = thresholds.min_price_usd
        summary.min_avg_dollar_volume_usd = thresholds.min_avg_dollar_volume_usd
        summary.thresholds_source = thresholds.source
    return summary


def format_summary_lines(summary: FilterSummary) -> List[str]:
    """Human-readable log block (the 로그 half of T8's
    "포함/제외 사유별 통계를 로그·리포트로 남긴다")."""
    lines = [
        f"[UNIVERSE FILTER] total={summary.total} included={summary.included} "
        f"excluded={summary.excluded}",
        f"[UNIVERSE FILTER] budget source={summary.budget_source} as_of={summary.budget_as_of} "
        f"cash={summary.available_cash_usd} usage%={summary.cash_usage_percent} "
        f"position_rate={summary.position_rate} ceiling={summary.price_ceiling_usd}",
        f"[UNIVERSE FILTER] thresholds min_price={summary.min_price_usd} "
        f"min_avg_dollar_volume={summary.min_avg_dollar_volume_usd} "
        f"source={summary.thresholds_source}",
    ]
    for reason in ALL_REASONS:
        lines.append(f"[UNIVERSE FILTER]   {reason}={summary.reason_counts.get(reason, 0)}")
    for reason, count in sorted(summary.reason_counts.items()):
        if reason not in ALL_REASONS:
            lines.append(f"[UNIVERSE FILTER]   {reason}={count}")
    return lines
