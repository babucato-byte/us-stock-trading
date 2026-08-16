"""Where the KIS entry cycle gets its symbols and its allow-list.

The cycle used to answer both questions inline: symbols came from
`paper_strategy_order.load_watchlist()` and the allow-list was
`rollout.allowed_symbols`. This module turns that pair into an interface
with two implementations, so a second candidate source can exist without
a second copy of the entry pipeline.

That constraint is the whole point. Everything that makes the KIS path
safe -- the Order Gate, entry limits, idempotency, the kill switch,
reconciliation, the price re-check -- lives in ONE cycle. Copying that
cycle to give S1 its own would create two places whose idea of safety
could drift apart, and the drift would be discovered in production. So
the gates stay where they are and only the source is swapped.

The default is the legacy source
--------------------------------
`resolve()` returns the legacy source unless `S1_LIVE_SOURCE_ENABLED` is
explicitly on. With it off, `symbols()` and `allowed_symbols()` return
exactly what the inline code returned, so the existing path is
unchanged -- asserted by a test rather than asserted by inspection.

A candidate is not a buy
------------------------
`S1CandidateSource.allowed_symbols()` answering with a symbol means only
"this may be examined further today". Every downstream check still runs.
The set is empty on ANY validation failure, and an empty allow-list is
already how this codebase spells "reject everything" -- that behaviour
is untouched, which is why the fail-closed path needed no new gate.

Operator override tightens, never loosens
-----------------------------------------
When `LIVE_ROLLOUT_ALLOWED_SYMBOLS` is set, the S1 set is INTERSECTED
with it. An operator who wrote a list down meant it, and this codebase's
established convention (see order_gateway's `min()` on cash) is that a
trusted setting may only ever tighten a computed one. When the operator
list is empty -- the default -- the S1 set stands alone, which is the
only way a dynamic source can function at all.
"""

import logging
import os
from typing import FrozenSet, List, Optional

from config import scanner_live_mode
from s1_live import store

logger = logging.getLogger(__name__)

#: Off by default. Setting this does NOT enable live orders: it only
#: changes where the cycle's candidates come from. Orders remain gated
#: by KIS_LIVE_ORDER_ENABLED, LIVE_ROLLOUT_ENABLED and ENTRY_DISABLED.
S1_SOURCE_ENABLED_ENV = "S1_LIVE_SOURCE_ENABLED"

SOURCE_LEGACY = "legacy_watchlist"
SOURCE_S1 = "s1_live"

_TRUE = {"1", "true", "yes", "y", "on"}


def _env_bool(mapping, name, default=False) -> bool:
    value = mapping.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in _TRUE


class CandidateSource:
    """Two questions the entry cycle asks, and nothing else.

    `symbols()` is what to evaluate. `allowed_symbols()` is what the
    Order Gate is handed. They are separate because the legacy source
    genuinely has two different answers: it evaluates a whole watchlist
    and lets the gate reject most of it.
    """

    name = "abstract"

    def symbols(self) -> List[str]:
        raise NotImplementedError

    def allowed_symbols(self) -> FrozenSet[str]:
        raise NotImplementedError

    def qualify(self, symbol, *, analyze=None, score_threshold=None):
        """Is this setup worth an order? SOURCE-SPECIFIC (PHASE 4A §2).

        Only the strategy judgement is per-source. Instrument validation,
        the live price re-check, the Order Gate, entry limits, the kill
        switch, reconciliation and the Execution Engine are shared and
        run identically whatever answered this.
        """
        raise NotImplementedError

    def describe(self) -> dict:
        return {"candidate_source": self.name}


class LegacyWatchlistSource(CandidateSource):
    """Exactly what the cycle did before this module existed.

    `watchlist_module` must be the module object the CALLER already
    holds -- `kis_live_trading` passes its own `pso`. It is not a
    convenience parameter and it must not be replaced by a local
    `import paper_strategy_order` here.

    Why: `tests/test_ai_analysis.py` deliberately pops
    "paper_strategy_order" out of `sys.modules` and leaves it popped, to
    prove `ai_analysis` does not transitively import the order modules.
    A fresh import performed later therefore builds a NEW module object,
    while `kis_live_trading.pso` still references the original one. Any
    monkeypatch aimed at `klt.pso.load_watchlist` lands on the original
    and would be invisible to the fresh import -- so the real
    `load_watchlist()` would run, read a nonexistent candidate CSV,
    return no symbols, and the cycle would silently evaluate nothing.

    That is not hypothetical: it is what happened when this class first
    did its own import. Eighteen existing tests failed, all of them
    passing in isolation and only failing once `test_ai_analysis` had
    run earlier in the same session. `tests/test_kis_live_trading.py`
    already carries a comment warning about exactly this hazard.
    """

    name = SOURCE_LEGACY

    def __init__(self, rollout, watchlist_module=None, load_watchlist=None):
        self._rollout = rollout
        self._watchlist_module = watchlist_module
        self._load_watchlist = load_watchlist

    def symbols(self) -> List[str]:
        if self._load_watchlist is not None:
            return list(self._load_watchlist())
        if self._watchlist_module is not None:
            # Attribute lookup at CALL time on the caller's own module
            # object -- the same expression the inline code used.
            return list(self._watchlist_module.load_watchlist())
        import paper_strategy_order as pso

        return list(pso.load_watchlist())

    def allowed_symbols(self) -> FrozenSet[str]:
        return self._rollout.allowed_symbols

    def qualify(self, symbol, *, analyze, score_threshold):
        """Unchanged legacy qualification: analyze_stock + SCORE_THRESHOLD."""
        from s1_live import qualification

        return qualification.qualify_legacy(
            symbol, analyze=analyze, score_threshold=score_threshold)

    def describe(self) -> dict:
        return {"candidate_source": self.name,
                "allowed_symbol_count": len(self._rollout.allowed_symbols)}


class S1CandidateSource(CandidateSource):
    """Today's validated S1 candidate set, or nothing at all.

    `trading_day` is required and is compared against the manifest, so a
    yesterday's file cannot be reused today: that comparison is the
    staleness policy at THIS stage. How fresh a signal must be at the
    moment an order is actually placed is a separate decision that
    belongs to a later phase, and is deliberately not guessed here.
    """

    name = SOURCE_S1

    def __init__(self, *, trading_day, rollout=None, modes=None,
                 expected_provider=None):
        self._trading_day = str(trading_day)
        self._rollout = rollout
        self._modes = modes
        self._expected_provider = expected_provider
        self._result = None
        self._loaded = False
        self._refusal = None

    def _load(self):
        if self._loaded:
            return self._result
        self._loaded = True
        try:
            scanner_name = scanner_live_mode.limited_live_scanner(self._modes)
        except scanner_live_mode.ScannerLiveModeError as exc:
            self._refusal = f"live-mode configuration refused: {exc}"
            logger.warning("S1 candidate source empty: %s", self._refusal)
            return None
        result = store.load(
            expected_trading_day=self._trading_day,
            expected_scanner=scanner_name,
            expected_provider=self._expected_provider,
        )
        if result is None:
            # store.load() already logged which check failed.
            self._refusal = "candidate set failed validation"
            return None
        self._result = result
        return result

    def _validated_symbols(self) -> FrozenSet[str]:
        result = self._load()
        if result is None:
            return frozenset()
        symbols = result.symbols
        operator = getattr(self._rollout, "allowed_symbols", None) or frozenset()
        if operator:
            tightened = frozenset(symbols) & frozenset(s.upper() for s in operator)
            if tightened != symbols:
                logger.info(
                    "S1 candidate set tightened by LIVE_ROLLOUT_ALLOWED_SYMBOLS: "
                    "%s of %s symbols remain", len(tightened), len(symbols))
            return tightened
        return symbols

    def symbols(self) -> List[str]:
        """Ranked order, not set order -- rank 1 is examined first."""
        result = self._load()
        if result is None:
            return []
        allowed = self._validated_symbols()
        return [row["symbol"] for row in result.rows if row["symbol"] in allowed]

    def allowed_symbols(self) -> FrozenSet[str]:
        return self._validated_symbols()

    def candidate_row(self, symbol):
        """The validated row for `symbol`, or None."""
        result = self._load()
        if result is None:
            return None
        wanted = str(symbol or "").upper()
        for row in result.rows:
            if row["symbol"] == wanted:
                return row
        return None

    def qualify(self, symbol, *, analyze=None, score_threshold=None):
        """S1 qualification: the validated candidate row, and nothing else.

        `analyze`/`score_threshold` are accepted so the cycle can call
        every source the same way, and are deliberately IGNORED. Applying
        the legacy score to an S1 candidate would mean the thing that
        actually trades is "S1 AND legacy score" -- which is not the
        strategy month 1 measured, and not what any report describes.
        """
        from s1_live import qualification

        return qualification.qualify_s1(symbol, candidate_row=self.candidate_row(symbol))

    def describe(self) -> dict:
        result = self._load()
        return {
            "candidate_source": self.name,
            "trading_day": self._trading_day,
            "validated": result is not None,
            "refusal": self._refusal,
            "allowed_symbol_count": len(self.allowed_symbols()),
            "scanner_run_id": (result.manifest.get("scanner_run_id")
                               if result is not None else None),
        }


def is_s1_source_enabled(env=None) -> bool:
    return _env_bool(os.environ if env is None else env, S1_SOURCE_ENABLED_ENV, False)


def resolve(rollout, *, trading_day=None, env=None, modes=None,
            watchlist_module=None) -> CandidateSource:
    """The source this cycle should use. Legacy unless S1 is switched on.

    `watchlist_module` is threaded straight through to
    `LegacyWatchlistSource` -- see its docstring for why the caller's own
    module object has to be the one used.

    A missing `trading_day` with S1 requested falls back to the legacy
    source rather than guessing a date: the trading day is what makes a
    stale candidate set detectable, and a guessed one would defeat the
    check it exists for.
    """
    if not is_s1_source_enabled(env):
        return LegacyWatchlistSource(rollout, watchlist_module=watchlist_module)
    if not trading_day:
        logger.warning(
            "%s is set but no trading day was supplied; falling back to the "
            "legacy candidate source", S1_SOURCE_ENABLED_ENV)
        return LegacyWatchlistSource(rollout, watchlist_module=watchlist_module)
    return S1CandidateSource(trading_day=trading_day, rollout=rollout, modes=modes)
