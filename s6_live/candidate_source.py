"""S6's candidates, offered to the SHARED buy cycle.

The same shape S2 uses, for the same reason: only the SOURCE is
pluggable, and a second candidate source must never mean a second
pipeline. Everything below -- COMMON_STOCK, orderable cash,
reconciliation, duplicate orders, the kill switch, position limits, the
execution-price gate -- is the shared cycle's and is not touched here.

One thing S2 did not need: VARIANT
----------------------------------
S6 runs the same scanner in four sessions, and each forms its own range.
A candidate is therefore identified by (trading_day, session, variant),
not by day alone -- an S6-O row reaching an S6-R evaluation would be a
breakout of a level nobody in the regular session traded against.
The variant is re-checked here rather than trusted to the file path,
because a path is not a guarantee.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, List, Optional

from config import s6_sessions, scanner_live_mode


logger = logging.getLogger(__name__)

#: Continuity is bounded by the SCAN'S LIFE, not by a clock.
#:
#: This briefly used `realtime_features.DEFAULT_MAX_BAR_AGE_SECONDS`
#: (900s) as a generation TTL. That was wrong twice over.
#:
#: It did not work: a REGULAR scan takes ~6 minutes, so 900s covers it,
#: but an OVERNIGHT_DAYTIME scan takes ~61 (3619s, 3660s, 3772s measured
#: on 2026-09-02). The previous generation expired about fifteen minutes
#: into every hour-long scan and left a ~46 minute blackout -- the very
#: gap continuity exists to close.
#:
#: And it was the kind of number `scanners/publish/scan_cycle` refuses
#: on principle: "picking 'candidates older than N minutes are stale'
#: would be inventing a threshold nobody measured". 900s is the age of
#: the newest BAR in a features view. It is not the age at which a
#: completed scan's candidate LIST stops describing what the scanner
#: found, and borrowing it for that made it look measured when it was
#: not.
#:
#: The honest question is the one that module already answers exactly:
#: is the scan that supersedes this generation still alive? That is
#: kernel-truth -- a crashed or killed scan releases its flock and stops
#: being alive within microseconds -- so continuity needs no timeout to
#: end and cannot get stuck on a dead producer.

SOURCE_S6 = "s6_orb_breakout"
STRATEGY_ID = s6_sessions.STRATEGY_ID

#: S6's PIPELINE budget: how long a candidate this source has offered,
#: and the cycle has accepted, may take to reach the broker. Read by the
#: shared buy cycle through `signal_valid_seconds()` -- the one hook
#: `execution/signal_validity.py` asks a source for.
#:
#: This is not the strategy's freshness. How old the breakout may be is
#: the precision watch's and the scan cycle's question (`_cycle_ok`,
#: the market-data as-of bound, the generation record) and none of that
#: moves here. This covers only what happens AFTER qualification, per
#: candidate, on the way to submit:
#:
#:     KIS quote, account snapshot, orderable cash, open orders,
#:     positions, execution-price check           ~6 reads
#:     execution lock                              <= 2 s (acquire timeout;
#:                                                 SKIPPED, never queued)
#:     revalidation: open orders, orderable cash   2 reads
#:     engine: reconciliation snapshot             ~2 reads
#:     the order itself                            1 write
#:
#: Reads are paced at DEFAULT_READ_MIN_INTERVAL = 3.0 s by the shared
#: rate limiter, so eleven of them cannot take less than ~33 s and,
#: with ordinary latency, take ~45 s. A rate-limit backoff is 3 s
#: doubling to a 15 s cap, so two of them add up to 30 s. That bounds
#: the legitimate path at roughly 80 s. 180 s is that with a factor of
#: two, and still well inside the five-minute consume cadence and the
#: fifteen-minute scan, so a Signal cannot outlive the generation it was
#: built from. The precision watch, which dominated the 2-5 minute cycle
#: measured on 2026-09-02, runs BEFORE qualification and is not in this
#: budget -- which is why the 120 s default, started at the cycle's
#: beginning, expired 2 of 2 that day.
#:
#: Not 30 or 60 minutes. The ORB thesis may well stay valid that long,
#: but that is the strategy-age question above, and a pipeline budget
#: that long would let a candidate sit through a superseding scan.
SIGNAL_VALID_SECONDS = 180.0

#: The scan ran and no symbol broke out. Wait.
NO_CANDIDATE = "scan ran; no symbol met the S6 breakout conditions"
#: No scan ran at all. Waiting will not help.
NO_PRODUCER_RUN = ("no S6 scan ran for this session -- the candidate "
                   "producer is missing, not the candidates")



def _latest_generation(rows):
    """Only the newest scan's rows. Older generations are superseded.

    The published file is append-only for the whole session: a scan
    every fifteen minutes writes its complete candidate set, so by
    mid-afternoon one REGULAR session holds seventeen generations and
    229 rows. Filtering on trading day and variant alone keeps all of
    them, and the entry cycle then sees the same symbol several times --
    STE three times, ROP three times, SM ten -- once per generation it
    ever appeared in.

    Two things go wrong with that, and the cheap one is the cost: each
    repeat spends a full KIS round trip, and a cycle that should take
    two minutes takes seven, in a window the scanner is already
    occupying most of.

    The expensive one is that the older copies are WRONG. A candidate
    row carries the ORB range, breakout price, rank and score the scan
    computed at the time. Evaluating a symbol against a two-hour-old row
    judges it against a range the market has since left -- which is the
    exact failure that let DT be re-offered every fifteen minutes on
    market data that had not changed since morning.

    A scan publishes a COMPLETE set, so "newest generation" is the right
    unit rather than "newest row per symbol": a symbol the latest scan
    did not publish is not a candidate any more, and keeping its last
    appearance would quietly resurrect it.

    Rows with no `generated_at` cannot be ordered, so they are treated
    as one generation of their own and kept only if nothing else
    qualifies -- an unstamped file still trades rather than silently
    becoming empty.
    """
    stamped = [r for r in rows if str(r.get("generated_at") or "")]
    if not stamped:
        # Nothing to order by. Deduplicate on symbol keeping the LAST
        # occurrence, which is newest in an append-only file.
        seen = {}
        for row in rows:
            seen[str(row.get("symbol") or "").upper()] = row
        return list(seen.values())

    newest = max(str(r.get("generated_at")) for r in stamped)
    latest = [r for r in stamped if str(r.get("generated_at")) == newest]
    if len(latest) < len(rows):
        logger.info("S6 candidate set: %s of %s published rows are the "
                    "newest generation (%s); the rest are superseded",
                    len(latest), len(rows), newest)
    return latest

class S6CandidateSource:
    """This session's published S6 rows, or nothing at all."""

    name = SOURCE_S6

    def __init__(self, *, trading_day, session=None, rollout=None,
                 modes=None):
        self._trading_day = str(trading_day)
        self._session = session
        self._variant = s6_sessions.variant_for(session)
        #: Set when a previous completed generation is being served
        #: because a newer scan is still running.
        self._continuity = None
        #: The generation record the offered rows came from.
        self._generation = None
        self._rollout = rollout
        self._modes = modes
        self._rows: Optional[List[dict]] = None
        self._loaded = False
        self._refusal: Optional[str] = None
        # Stamped when the rows are actually READ, not when the source is
        # constructed. The age that matters is between publication and
        # use, and a constructor that ran early would understate it.
        self._consumed_at: Optional[datetime] = None
        #: What the scan-cycle check saw, kept so the audit record can
        #: state it rather than re-deriving it a second time.
        self._cycle_state: Optional[Dict[str, Any]] = None

    def _live_mode_ok(self) -> bool:
        try:
            scanner_live_mode.require_limited_live(
                s6_sessions.SCANNER_NAME, self._modes)
            return True
        except scanner_live_mode.ScannerLiveModeError as exc:
            self._refusal = f"S6 is not LIMITED_LIVE: {exc}"
            return False

    def _session_ok(self) -> bool:
        if s6_sessions.orders_allowed(self._session):
            return True
        # Scanning happens in every session; ordering does not. A shadow
        # session offering symbols to the buy cycle would erase that
        # distinction at the one point where it matters.
        self._refusal = (
            f"session {self._session!r} is {s6_sessions.mode_for(self._session)}; "
            f"orders are enabled only in {sorted(s6_sessions.LIVE_SESSIONS)}")
        return False

    def _cycle_ok(self) -> bool:
        """Is the file on disk this cycle's answer, or the last one's?

        S6 is SCHEDULED every fifteen minutes and consumes five minutes
        later, so a scan that runs long leaves a file whose trading day,
        session and variant are all CORRECT and whose contents are
        superseded.

        "Every fifteen minutes" is the schedule, not the observed
        cadence. A REGULAR scan takes about six minutes on the bulk
        provider; an OVERNIGHT_DAYTIME scan takes about SIXTY-ONE on the
        per-symbol KIS path (3619s, 3660s, 3772s measured on
        2026-09-02), so overlapping ticks are skipped by the cycle lock
        and that session effectively re-scans hourly. That gap is why
        the previous completed generation is now kept while a scan runs
        -- see `_previous_generation_ok`.
        Every other refusal in this class is keyed on one of those three,
        which is why none of them catches it.

        Two facts are enough, and neither is a threshold. A scan holding
        its cycle lock is producing the answer that replaces this file --
        so the file is the previous cycle's and is refused. A scan that
        ended FAILED produced no answer at all -- so whatever is on disk
        predates it and is refused too.

        Refusing is only ever about a new BUY. Nothing here is reachable
        from an exit: `s6_live.exit_runtime` reads no candidate file.
        """
        from scanners.publish import scan_cycle

        try:
            state = scan_cycle.state(self._trading_day, self._session,
                                     scanner=s6_sessions.SCANNER_NAME)
        except Exception as exc:  # noqa: BLE001 - the source must never
            # raise into the shared cycle; an unanswerable question is
            # refused, which is the same direction as every other check.
            self._refusal = f"scan state could not be established: {exc}"
            return False

        self._cycle_state = state.as_dict()
        if state.blocks_consumption:
            # A scan is running (or its state is unknowable). Its output
            # is NOT consumable -- that rule does not move. But refusing
            # outright discards the answer the PREVIOUS scan already
            # completed, and an OVERNIGHT_DAYTIME scan takes about an
            # hour, so the consumer spent most of every hour with no
            # candidates while a perfectly good generation sat on disk.
            #
            # So: keep the previous COMPLETED generation, for THIS day,
            # session and variant, while it is fresh. Anything else --
            # no record, a FAILED one, a stale one, another variant --
            # refuses exactly as before.
            if self._previous_generation_ok(state):
                return True
            self._refusal = state.refusal()
            return False

        try:
            ok, detail = scan_cycle.last_run_consumable(
                self._trading_day, self._session, strategy_id=STRATEGY_ID)
        except Exception as exc:  # noqa: BLE001
            self._refusal = f"last scan status could not be read: {exc}"
            return False
        if not ok:
            self._refusal = detail
            return False
        return True

    def _previous_generation_ok(self, state=None) -> bool:
        """May the last completed generation still be served?

        Bounded by the SCAN, not by a clock: the previous generation is
        served for exactly as long as the scan that supersedes it is
        alive and holding its cycle lock. When that process ends --
        completing, failing, crashing or being killed -- the kernel
        releases the lock, `state.running` goes false, and this branch
        stops being reached at all. Nothing here can outlive its producer.

        Serving a previous generation does NOT make it READY. Precision
        Watch revalidates price, as-of, VWAP, EMA, volume, breakout,
        extension and reentry against the CURRENT session on every tick,
        exactly as it does for a fresh generation. What continuity
        restores is the candidate LIST, never its market evidence.
        """
        from scanners.publish import generations

        if not (state is not None and state.running):
            # Not a live scan. Either nothing is running -- in which case
            # this branch should not have been reached -- or liveness
            # could not be ESTABLISHED (fcntl unavailable, unreadable
            # cycle file). "Cannot tell" is not "still going", so
            # continuity does not extend over it.
            return False

        record = generations.current(self._trading_day, self._session,
                                     strategy_id=STRATEGY_ID)
        if not generations.is_consumable(
                record, variant=self._variant, strategy_id=STRATEGY_ID,
                trading_day=self._trading_day, session=self._session):
            return False

        self._continuity = dict(record)
        self._continuity["age_seconds"] = generations.age_seconds(record)
        logger.info(
            "S6 serving the previous completed generation %s while scan %s "
            "(pid %s) is still running: %s candidate(s), completed %s",
            record.get("generation_id"), state.run_id, state.pid,
            record.get("candidate_count"), record.get("completed_at"))
        return True

    def _generation_rows(self, rows):
        """Rows belonging to the CURRENT declared generation.

        Declared, not inferred. `_latest_generation` picks the newest
        `generated_at` among the rows PRESENT, which cannot represent a
        completed generation that found nothing: an empty scan writes no
        rows, so the previous generation's stay newest and its candidates
        stay live. The record says how many there are, so zero is finally
        expressible.
        """
        from scanners.publish import generations

        record = self._continuity or generations.current(
            self._trading_day, self._session, strategy_id=STRATEGY_ID)
        if not generations.is_consumable(
                record, variant=self._variant, strategy_id=STRATEGY_ID,
                trading_day=self._trading_day, session=self._session):
            # No declared generation: pre-record store, or a producer
            # that never ran. Fall back to the historical inference so a
            # store written before generation records still trades.
            return _latest_generation(rows)

        self._generation = dict(record)
        generation_id = record.get("generation_id")
        if not generation_id:
            return _latest_generation(rows)
        declared = int(record.get("candidate_count") or 0)
        mine = [r for r in rows
                if str(r.get("scanner_run_id") or "") == str(generation_id)]
        if declared == 0:
            # Authoritative. The scan ran and nothing broke out; the
            # previous generation's candidates are superseded, not
            # inherited.
            if mine:
                logger.warning(
                    "S6 generation %s declares 0 candidates but %d rows "
                    "carry its id; trusting the declaration",
                    generation_id, len(mine))
            return []
        if len(mine) != declared:
            logger.warning(
                "S6 generation %s declares %d candidate(s) and %d row(s) "
                "carry its id", generation_id, declared, len(mine))
        return mine

    def _load(self):
        if self._loaded:
            return self._rows
        self._loaded = True

        if not self._live_mode_ok() or not self._session_ok():
            logger.info("S6 candidate source empty: %s", self._refusal)
            return None

        if not self._cycle_ok():
            logger.warning("S6 candidate source refused: %s", self._refusal)
            return None

        try:
            from scanners.publish import candidates as publisher

            rows = [r for r in publisher.read(self._trading_day, self._session)
                    if str(r.get("strategy_id")) == STRATEGY_ID]
            rows = self._generation_rows(rows)
        except Exception:  # noqa: BLE001 - an unreadable file is empty,
            # never an exception that could abort the shared cycle and
            # take S1's entries down with it.
            logger.warning("could not read S6 candidates for %s/%s",
                           self._trading_day, self._session, exc_info=True)
            self._refusal = "candidate file could not be read"
            return None

        if not rows:
            if publisher.scan_ran(self._trading_day, self._session):
                self._refusal = NO_CANDIDATE
            else:
                self._refusal = NO_PRODUCER_RUN
                logger.error("no S6 scan ran for %s/%s", self._trading_day,
                             self._session)
            return None

        fresh = [r for r in rows
                 if str(r.get("trading_day")) == self._trading_day
                 and str(r.get("variant") or "") == self._variant]
        if not fresh:
            self._refusal = (
                f"published S6 rows are not {self._variant} for "
                f"{self._trading_day}")
            logger.warning("S6 candidate source refused: %s", self._refusal)
            return None

        fresh = _latest_generation(fresh)

        self._consumed_at = datetime.now(timezone.utc)
        self._rows = sorted(fresh, key=lambda r: (int(r.get("rank") or 10**6),
                                                  str(r.get("symbol") or "")))
        return self._rows

    def _validated_symbols(self) -> FrozenSet[str]:
        rows = self._load()
        if not rows:
            return frozenset()
        symbols = frozenset(str(r.get("symbol") or "").upper() for r in rows)
        operator = getattr(self._rollout, "allowed_symbols", None) or frozenset()
        if operator:
            tightened = symbols & frozenset(s.upper() for s in operator)
            if tightened != symbols:
                logger.info("S6 candidate set tightened by the operator "
                            "allow-list: %s of %s remain", len(tightened),
                            len(symbols))
            return tightened
        return symbols

    def symbols(self) -> List[str]:
        rows = self._load()
        if not rows:
            return []
        allowed = self._validated_symbols()
        return [str(r["symbol"]).upper() for r in rows
                if str(r.get("symbol") or "").upper() in allowed]

    def allowed_symbols(self) -> FrozenSet[str]:
        return self._validated_symbols()

    def candidate_row(self, symbol) -> Optional[dict]:
        wanted = str(symbol or "").upper()
        for row in self._load() or []:
            if str(row.get("symbol") or "").upper() == wanted:
                return row
        return None

    def qualify(self, symbol, *, analyze=None, score_threshold=None):
        """The source-specific step. `analyze`/`score_threshold` ignored
        for the reason S1 and S2 ignore them: a second, unrelated score
        would make the thing that trades "S6 AND legacy score"."""
        from s6_live import qualification

        return qualification.qualify_s6(
            symbol, candidate_row=self.candidate_row(symbol))

    def signal_valid_seconds(self) -> float:
        """S6's pipeline budget for one accepted candidate. See the
        constant. Implementing this opts the source into the measured
        submit-time check in `execution/signal_validity.py`."""
        return SIGNAL_VALID_SECONDS

    def freshness(self) -> Dict[str, Any]:
        """When the rows were made and when they were used.

        Reported rather than enforced: the trading-day and session checks
        above are this stage's staleness policy, and how old a price may
        be at the moment an order is placed is the shared gate's
        question. Inventing a second age limit here would put two
        freshness policies in the codebase.
        """
        rows = self._rows or []
        generated = rows[0].get("generated_at") if rows else None
        age = None
        if generated and self._consumed_at is not None:
            try:
                made = datetime.fromisoformat(
                    str(generated).replace("Z", "+00:00"))
                if made.tzinfo is None:
                    made = made.replace(tzinfo=timezone.utc)
                age = (self._consumed_at - made).total_seconds()
            except Exception:  # noqa: BLE001 - a display value must not
                # be able to fail the source that carries it.
                age = None
        return {
            "candidate_generated_at": generated,
            "candidate_consumed_at": (self._consumed_at.isoformat()
                                      if self._consumed_at else None),
            "candidate_age_at_consume_seconds": age,
        }

    def describe(self) -> dict:
        rows = self._load()
        return {
            "source": self.name, "strategy_id": STRATEGY_ID,
            **self.freshness(),
            "variant": self._variant, "trading_day": self._trading_day,
            "session": self._session, "candidates": len(rows or []),
            "allowed": len(self._validated_symbols()),
            "mode": s6_sessions.mode_for(self._session),
            "scan_state": self._cycle_state,
            "refusal": self._refusal,
        }
