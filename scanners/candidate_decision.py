"""The Candidate Decision Layer (spec section 10). Disabled in v1.0.

The separation this enforces
----------------------------
Section 10 splits one arrow into two:

    Scanner Results -> Scanner Analytics Store        (always)
    Scanner Results -> Candidate Decision -> Trading Candidate Store

and states that not every scanner result may become an order candidate.
This module is the second arrow's first half. `select_candidates` is a
pure function: signals in, a ranked and filtered candidate list out. It
applies a policy from `candidate_decision.json` -- which scanners are
eligible at all, a score floor, a confirmation-count floor, an extension
ceiling, and a cap on how many candidates a day may produce.

Why it does not publish
-----------------------
It has no import of `market_data/candidate_store.py`, and that is
deliberate rather than unfinished.

Section 30 is unambiguous: adding these scanners is not, by itself, a
reason to let them trade, and an unvalidated scanner must not
automatically generate a live order. Section 11 says month 1 is for
collecting data with the parameters frozen. A publish path wired up now
would mean the act of deploying the scanners changed what the live
system can be handed -- exactly the coupling both sections forbid.

There is also a concrete hazard. `candidate_store.publish()` overwrites
the shared `order_candidates.csv` that the limited-live bootstrap reads.
A scanner process that could call it could replace the candidate set the
existing, validated `daily_candidate_scanner` produced -- silently, and
with symbols chosen by logic that has zero days of measured performance.
Not importing it makes that impossible to do by accident, rather than
merely against the rules.

So: the policy is implemented and tested, ships disabled, and returns
rows to its caller. Turning it on is two deliberate steps -- flipping
`enabled` and bumping the version, then a human wiring the returned rows
into publication -- taken after the month-1 review, by someone who has
read the numbers.

`enabled: false` means EMPTY, not "ignore the policy"
-----------------------------------------------------
With the shipped config, `select_candidates` returns an empty list. It
does not fall back to "all signals" or to a default policy. A disabled
decision layer that quietly passed everything through would be worse
than no decision layer at all.
"""

import logging
from typing import Any, Dict, Iterable, List, Optional

from scanners.base.config import ScannerConfig, ScannerConfigError, config_root
from scanners.base.models import ScannerSignal

logger = logging.getLogger(__name__)

POLICY_FILENAME = "candidate_decision.json"


class CandidateDecisionDisabled(Exception):
    """The decision layer is off. Callers must treat this as "no
    candidates", never as "proceed without a policy"."""


def load_policy() -> ScannerConfig:
    """Read `scanners/candidate_decision.json`.

    Uses the same loader and the same strictness as a scanner config: a
    missing or malformed policy raises rather than defaulting, because a
    default policy for what may reach the order path is precisely the
    thing that should never exist.
    """
    import json
    from pathlib import Path

    path = Path(config_root()) / POLICY_FILENAME
    if not path.exists():
        raise ScannerConfigError(f"no candidate decision policy at {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScannerConfigError(f"candidate decision policy unreadable at {path}: {exc}") from exc
    version = payload.get("version")
    params = payload.get("params")
    if not version or not isinstance(params, dict):
        raise ScannerConfigError(f"candidate decision policy at {path} needs 'version' and 'params'")
    return ScannerConfig(
        scanner_name="candidate_decision",
        version=str(version),
        params=params,
        source=str(path),
    )


def is_enabled(policy: Optional[ScannerConfig] = None) -> bool:
    try:
        policy = policy or load_policy()
    except ScannerConfigError:
        # No readable policy is the same operational state as "off".
        return False
    return bool(policy.get("enabled", False))


def select_candidates(
    signals: Iterable[ScannerSignal],
    *,
    policy: Optional[ScannerConfig] = None,
    confirmation_counts: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Apply the policy to a day's signals. Empty list when disabled.

    `confirmation_counts` maps symbol -> how many scanners flagged it
    that day (section 18). It is accepted so the structure section 18
    asks to be prepared exists, and the policy's
    `min_confirmation_count` ships at 1 -- i.e. no agreement is
    required -- because section 17 says intersection must not be an
    entry precondition until the data supports it.
    """
    policy = policy or load_policy()
    if not bool(policy.get("enabled", False)):
        logger.info("candidate decision layer is disabled (%s); no candidates selected",
                    policy.version)
        return []

    eligible = {str(name) for name in (policy.get("eligible_scanners") or [])}
    if not eligible:
        logger.warning("candidate decision layer is enabled but no scanner is eligible; "
                       "no candidates selected")
        return []

    minimum_score = float(policy.get("min_scanner_score", 100))
    minimum_confirmations = int(policy.get("min_confirmation_count", 1))
    maximum_extension = policy.get("max_extension_hma200_pct")
    maximum_candidates = int(policy.get("max_candidates", 0))
    rank_by = str(policy.get("rank_by", "scanner_score"))

    selected: List[Dict[str, Any]] = []
    for signal in signals:
        if signal.scanner_name not in eligible:
            continue
        if signal.scanner_score is None or signal.scanner_score < minimum_score:
            continue
        if signal.signal_price is None or signal.signal_price <= 0:
            # No usable price means nothing downstream could size it.
            continue
        confirmations = (confirmation_counts or {}).get(signal.symbol, 1)
        if confirmations < minimum_confirmations:
            continue
        if maximum_extension is not None and signal.extension_hma200_pct is not None:
            if signal.extension_hma200_pct > float(maximum_extension):
                continue
        selected.append({
            "symbol": signal.symbol,
            "price": signal.signal_price,
            "scanner_name": signal.scanner_name,
            "scanner_version": signal.scanner_version,
            "scanner_score": signal.scanner_score,
            "confirmation_count": confirmations,
            "trading_day": signal.trading_day,
            "signal_id": signal.signal_id,
            "reason": signal.reason,
            "decision_policy_version": policy.version,
        })

    selected.sort(key=lambda row: (row.get(rank_by) or 0), reverse=True)
    if maximum_candidates > 0:
        selected = selected[:maximum_candidates]
    logger.info("candidate decision layer selected %s candidates under policy %s",
                len(selected), policy.version)
    return selected


def publish(candidates: List[Dict[str, Any]], **_kwargs):
    """Not implemented in v1.0, on purpose.

    Publication would overwrite the shared `order_candidates.csv` that
    the limited-live bootstrap reads -- replacing the candidate set the
    existing, validated scanner produced with symbols chosen by logic
    that has no measured track record. Section 30 does not permit that
    as a consequence of installing these scanners.

    When a scanner has earned it, an operator wires `select_candidates`
    output into `market_data.candidate_store.publish_dataframe` from a
    script they wrote deliberately. Doing it here would mean the
    capability existed the moment this package was deployed.
    """
    raise CandidateDecisionDisabled(
        "Scanner candidates are not published to the trading candidate store in v1.0 "
        "(spec sections 10 and 30). Month 1 is data collection; no scanner here has a "
        "measured track record yet. Enabling publication is a separate, deliberate "
        "change made after the month-1 review -- not part of installing the scanners.")
