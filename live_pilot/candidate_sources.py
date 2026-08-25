"""Where a bootstrap's one candidate comes from.

Only the SOURCE is pluggable
----------------------------
`live_pilot/bootstrap.py` is the one-shot that establishes the five wire
values a real KIS response is the only way to confirm. Everything it
does after choosing a symbol -- the price re-check, the orderable-cash
read, whole-share sizing, the order intent, the capability mint, the
Order Gate, the single transport, the UNKNOWN contract, verification,
the at-most-one cancel -- is execution, and it is already verified. A
second bootstrap that re-implemented any of it would be verifying a
different path from the one production runs, which is the opposite of
what a bootstrap is for.

So the split is exactly one function wide:

    S1 bootstrap --> S1 candidate source --.
                                            >-- common live execution
    S6 bootstrap --> S6 candidate source --'

Each source answers one question -- "what does MY strategy say about
this symbol, at MY production threshold?" -- and returns the instrument,
the signal and the analysis. Nothing else differs.

Why S6 could not simply reuse S1's source
-----------------------------------------
S1's source reads `market_data.candidate_store` and re-scores with
`paper_strategy_order.analyze_stock` at S1's threshold. Pointing that at
an S6 symbol asks the wrong scanner the wrong question: an ORB breakout
is not an HMA early trend, and a name that broke out of its session
range has no reason to clear S1's score. The observable symptom was
`CANDIDATE_SYMBOL_NOT_PUBLISHED` -- S6's symbol is not in S1's published
set, and never will be. Putting S6's symbol into S1's store to get past
that would make the bootstrap trade on reasoning no strategy actually
produced.
"""

import logging

from config import s6_sessions, strategy_registry
from domain.instrument import InstrumentError
from domain.signal import SignalError, build_signal
from market_data.exchange_registry import (
    ExchangeResolutionError, build_kis_instrument,
)

logger = logging.getLogger(__name__)

#: Reason codes. Shared with bootstrap.py, which re-exports the ones it
#: raises, so an operator sees one vocabulary.
CANDIDATE_SYMBOL_NOT_PUBLISHED = "CANDIDATE_SYMBOL_NOT_PUBLISHED"
NO_QUALIFYING_CANDIDATE = "NO_QUALIFYING_CANDIDATE"
STALE_CANDIDATE = "STALE_CANDIDATE"
NO_CANDIDATE = "NO_CANDIDATE"
INSTRUMENT_INVALID = "INSTRUMENT_INVALID"
S6_SOURCE_REFUSED = "S6_SOURCE_REFUSED"


class CandidateSourceBlocked(Exception):
    """This source will not nominate the symbol, and why.

    Carries `reason_codes` so `bootstrap.BootstrapBlocked` can re-raise
    them unchanged -- the operator reads the same code whichever source
    refused.
    """

    def __init__(self, message, *, reason_codes=()):
        super().__init__(message)
        self.reason_codes = tuple(reason_codes)


class Selection:
    """One source's answer: the three things execution needs."""

    def __init__(self, *, instrument, signal, analysis):
        self.instrument = instrument
        self.signal = signal
        self.analysis = analysis


def _instrument_and_signal(symbol, *, strategy_id, strategy_version,
                           signal_price, score, entry_reason, deployed_commit,
                           now, valid_for_seconds):
    """The half of selection that is identical for every strategy.

    The venue is RESOLVED from the registry, never assumed -- KIS answers
    a wrong-exchange quote with rt_cd=0 and an empty price, so a
    hardcoded NASDAQ would make every NYSE/AMEX name silently
    unpriceable.
    """
    try:
        instrument, _record = build_kis_instrument(symbol)
        signal = build_signal(
            strategy_id=strategy_id, strategy_version=strategy_version,
            config_version="live_rollout_v1", code_commit=deployed_commit,
            symbol=symbol, exchange=instrument.exchange,
            signal_price=signal_price, score=score,
            entry_reason=entry_reason,
            valid_for_seconds=valid_for_seconds, now=now,
        )
    except (InstrumentError, SignalError, ExchangeResolutionError) as exc:
        raise CandidateSourceBlocked(
            f"signal/instrument construction failed: {exc}",
            reason_codes=(INSTRUMENT_INVALID,),
        ) from exc
    return instrument, signal


class S1CandidateSource:
    """S1's published set, re-scored at S1's production threshold.

    Unchanged behaviour -- this is the path the existing bootstrap ran,
    moved behind the interface so a second strategy can have its own.
    """

    slot = strategy_registry.SLOT_S1

    def __init__(self, *, strategy_id, score_threshold, valid_for_seconds,
                 analyze=None):
        self._strategy_id = strategy_id
        self._score_threshold = score_threshold
        self._valid_for_seconds = valid_for_seconds
        # The analyser is INJECTED rather than imported inside `select`.
        # A local `import paper_strategy_order` resolves through
        # sys.modules at call time, so it can silently pick up a
        # different module object than the caller holds -- which made
        # this source ignore a patched analyser and run the real one.
        # The bootstrap passes its own reference; nothing has to guess.
        self._analyze = analyze

    def _analyzer(self):
        if self._analyze is not None:
            return self._analyze
        import paper_strategy_order as pso

        return pso.analyze_stock

    def select(self, symbol, *, deployed_commit, now) -> Selection:
        from market_data import candidate_store
        from market_hours import us_trading_day

        # The published candidate set must be TODAY's and must actually
        # contain this symbol. Without this, a stale file left in the
        # shared store from a previous session would let the bootstrap
        # trade on yesterday's reasoning -- and the live re-score below
        # would not catch it, because a symbol can still score well on a
        # day the scanner never nominated it.
        try:
            rows, _manifest = candidate_store.load_verified(
                trading_day=us_trading_day(now), now=now)
        except candidate_store.CandidatesStale as exc:
            raise CandidateSourceBlocked(
                f"published candidates are not usable: {exc}",
                reason_codes=(STALE_CANDIDATE,),
            ) from exc
        except candidate_store.CandidatesUnavailable as exc:
            raise CandidateSourceBlocked(
                f"no published candidates: {exc}",
                reason_codes=(getattr(exc, "reason_code", NO_CANDIDATE),),
            ) from exc

        if candidate_store.find(symbol, rows=rows) is None:
            raise CandidateSourceBlocked(
                f"{symbol} is allow-listed but today's scanner did not nominate it "
                f"(published: {[r.get('symbol') for r in rows]})",
                reason_codes=(CANDIDATE_SYMBOL_NOT_PUBLISHED,),
            )

        analysis = self._analyzer()(symbol)
        if analysis is None or analysis.get("score", 0) < self._score_threshold:
            score = None if analysis is None else analysis.get("score")
            raise CandidateSourceBlocked(
                f"{symbol} does not meet the production score threshold "
                f"({score} < {self._score_threshold}); the threshold is not lowered "
                "for the bootstrap",
                reason_codes=(NO_QUALIFYING_CANDIDATE,),
            )

        instrument, signal = _instrument_and_signal(
            symbol, strategy_id=self._strategy_id, strategy_version="v1",
            signal_price=analysis["price"], score=analysis["score"],
            entry_reason="score_threshold_breakout",
            deployed_commit=deployed_commit, now=now,
            valid_for_seconds=self._valid_for_seconds,
        )
        return Selection(instrument=instrument, signal=signal, analysis=analysis)


class S6CandidateSource:
    """S6's own published breakout row for THIS session and variant.

    The freshness policy is `s6_live.candidate_source.S6CandidateSource`'s
    and is not duplicated here: it already refuses a row from another
    trading day, another variant, a scan cycle that is still running, or
    a scan that ended FAILED. Re-deciding any of that would put two
    staleness policies in the codebase, and they would disagree.

    No second score is applied. The published row IS the qualified
    candidate -- it cleared the ORB scanner's production conditions when
    it was written. Re-scoring it with S1's analyser would make the thing
    that trades "S6 AND an unrelated score", which is a different
    strategy from the one that was evidenced.
    """

    slot = strategy_registry.SLOT_S6

    def __init__(self, *, trading_day, session, rollout, valid_for_seconds):
        self._trading_day = trading_day
        self._session = session
        self._rollout = rollout
        self._valid_for_seconds = valid_for_seconds

    def _source(self):
        from s6_live.candidate_source import S6CandidateSource as LiveSource

        return LiveSource(trading_day=self._trading_day, session=self._session,
                          rollout=self._rollout)

    def select(self, symbol, *, deployed_commit, now) -> Selection:
        source = self._source()
        row = source.candidate_row(symbol)
        if row is None:
            # `refusal` says WHY there is no row -- not LIMITED_LIVE, a
            # shadow session, a scan still running, an empty scan. That
            # distinction is the whole operator response, so it is
            # carried through rather than flattened to "not published".
            refusal = getattr(source, "_refusal", None)
            if refusal:
                raise CandidateSourceBlocked(
                    f"S6 offered no candidate for {symbol}: {refusal}",
                    reason_codes=(S6_SOURCE_REFUSED,),
                )
            raise CandidateSourceBlocked(
                f"{symbol} is allow-listed but is not in this session's S6 "
                f"{s6_sessions.variant_for(self._session)} candidate set "
                f"(published: {source.symbols()})",
                reason_codes=(CANDIDATE_SYMBOL_NOT_PUBLISHED,),
            )

        qualification = source.qualify(symbol)
        if not qualification.qualified:
            raise CandidateSourceBlocked(
                f"{symbol} is an S6 row but does not qualify: "
                f"{qualification.reason_code} -- {qualification.detail}",
                reason_codes=(qualification.reason_code or NO_QUALIFYING_CANDIDATE,),
            )

        # `analysis` mirrors S1's shape so `BootstrapCandidate.as_dict()`
        # reports the same fields for both. The range high is carried
        # because an S6 position that lost it could never detect the
        # re-entry its exit policy is built on.
        analysis = {
            "symbol": symbol,
            "price": qualification.price,
            "score": qualification.score,
            "range_high": row.get("range_high"),
            "range_low": row.get("range_low"),
            "variant": row.get("variant"),
            "rank": row.get("rank"),
            "source_signal_id": qualification.source_signal_id,
        }

        instrument, signal = _instrument_and_signal(
            symbol, strategy_id=qualification.strategy_id, strategy_version="v1",
            signal_price=qualification.price, score=qualification.score,
            entry_reason=qualification.entry_reason,
            deployed_commit=deployed_commit, now=now,
            valid_for_seconds=self._valid_for_seconds,
        )
        return Selection(instrument=instrument, signal=signal, analysis=analysis)
