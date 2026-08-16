"""StrategyClaim / StrategySource data model (Stage 6).

Fail-closed validation at construction, matching the project's convention
(TradingStrategy.__init__, EvaluationResult.__post_init__): a malformed
record cannot be constructed at all, rather than being constructed and
discovered-invalid later by some other piece of code.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from strategy.status import COLLECTED, REJECTED, REVIEWED, STRUCTURED

# Claim categories: what part of a strategy this claim describes.
CATEGORY_ENTRY = "ENTRY"
CATEGORY_STOP = "STOP"
CATEGORY_TARGET = "TARGET"
CATEGORY_EXIT = "EXIT"
CATEGORY_FILTER = "FILTER"
CATEGORY_RISK = "RISK"
VALID_CATEGORIES = {
    CATEGORY_ENTRY, CATEGORY_STOP, CATEGORY_TARGET, CATEGORY_EXIT,
    CATEGORY_FILTER, CATEGORY_RISK,
}

# Origin: is this claim something the source actually said, something the
# collector inferred to fill a gap, or a known unknown the source never
# addressed at all?
ORIGIN_SOURCE = "SOURCE"          # the source material explicitly states this
ORIGIN_ASSUMPTION = "ASSUMPTION"  # collector's inference, not explicitly stated
ORIGIN_UNKNOWN = "UNKNOWN"        # source leaves this unspecified; flagged, not guessed
VALID_ORIGINS = {ORIGIN_SOURCE, ORIGIN_ASSUMPTION, ORIGIN_UNKNOWN}

# Source-material types (distinct from "how the strategy was later
# implemented", which is strategy/status.py's concern).
SOURCE_TYPE_USER_CHART_ANALYSIS = "USER_CHART_ANALYSIS"
SOURCE_TYPE_YOUTUBE = "YOUTUBE"
VALID_SOURCE_TYPES = {SOURCE_TYPE_USER_CHART_ANALYSIS, SOURCE_TYPE_YOUTUBE}

# Source material never reaches BACKTESTED/PAPER_APPROVED/
# LIMITED_LIVE_APPROVED/ACTIVE/PAUSED -- those describe an actual code
# strategy's progress (strategy/status.py), not raw source material.
# Reusing the same string constants (rather than redefining equivalent
# ones) keeps one source of truth for the status vocabulary; restricting
# the *allowed subset* here is what makes ACTIVE structurally unreachable
# for a StrategySource.
SOURCE_VALIDATION_STATUSES = {COLLECTED, STRUCTURED, REVIEWED, REJECTED}


class InvalidStrategyClaimError(Exception):
    pass


class InvalidStrategySourceError(Exception):
    pass


@dataclass
class StrategyClaim:
    category: str
    statement: str
    origin: str
    source_excerpt: Optional[str] = None
    confidence: Optional[float] = None  # 0.0-1.0, collector's subjective confidence; None if not assessed

    def __post_init__(self):
        if self.category not in VALID_CATEGORIES:
            raise InvalidStrategyClaimError(
                f"Invalid category {self.category!r}; must be one of {sorted(VALID_CATEGORIES)}"
            )
        if not isinstance(self.statement, str) or not self.statement.strip():
            raise InvalidStrategyClaimError("statement must be a non-empty string")
        if self.origin not in VALID_ORIGINS:
            raise InvalidStrategyClaimError(
                f"Invalid origin {self.origin!r}; must be one of {sorted(VALID_ORIGINS)}"
            )
        if self.origin == ORIGIN_SOURCE and not self.source_excerpt:
            raise InvalidStrategyClaimError(
                "origin=SOURCE claims must include source_excerpt (what the source actually said)"
            )
        if self.confidence is not None and not (0.0 <= self.confidence <= 1.0):
            raise InvalidStrategyClaimError(f"confidence must be in [0.0, 1.0], got {self.confidence!r}")


@dataclass
class StrategySource:
    source_id: str
    source_type: str
    title: str
    reference: str  # URL, file path, or free-text citation of where this came from
    collected_at: str  # ISO 8601
    version: int
    validation_status: str
    claims: List[StrategyClaim] = field(default_factory=list)
    derived_strategy_id: Optional[str] = None  # links to strategy/plugins/ once implemented, if ever
    similar_to: List[str] = field(default_factory=list)  # other source_ids flagged as similar
    notes: str = ""

    def __post_init__(self):
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise InvalidStrategySourceError("source_id must be a non-empty string")
        if self.source_type not in VALID_SOURCE_TYPES:
            raise InvalidStrategySourceError(
                f"Invalid source_type {self.source_type!r}; must be one of {sorted(VALID_SOURCE_TYPES)}"
            )
        if not isinstance(self.title, str) or not self.title.strip():
            raise InvalidStrategySourceError("title must be a non-empty string")
        if not isinstance(self.version, int) or self.version < 1:
            raise InvalidStrategySourceError(f"version must be a positive integer, got {self.version!r}")
        if self.validation_status not in SOURCE_VALIDATION_STATUSES:
            raise InvalidStrategySourceError(
                f"Invalid validation_status {self.validation_status!r} for source material; "
                f"must be one of {sorted(SOURCE_VALIDATION_STATUSES)} "
                "(BACKTESTED/PAPER_APPROVED/LIMITED_LIVE_APPROVED/ACTIVE/PAUSED describe an "
                "implemented strategy's progress, not raw source material)"
            )
        if not self.claims:
            raise InvalidStrategySourceError("claims must be non-empty -- a source with no claims carries no information")
        for claim in self.claims:
            if not isinstance(claim, StrategyClaim):
                raise InvalidStrategySourceError(f"claims must all be StrategyClaim instances, got {type(claim)!r}")

    def claims_by_origin(self, origin):
        return [c for c in self.claims if c.origin == origin]

    def to_dict(self):
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "title": self.title,
            "reference": self.reference,
            "collected_at": self.collected_at,
            "version": self.version,
            "validation_status": self.validation_status,
            "claims": [
                {
                    "category": c.category,
                    "statement": c.statement,
                    "origin": c.origin,
                    "source_excerpt": c.source_excerpt,
                    "confidence": c.confidence,
                }
                for c in self.claims
            ],
            "derived_strategy_id": self.derived_strategy_id,
            "similar_to": self.similar_to,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, payload):
        claims = [StrategyClaim(**c) for c in payload["claims"]]
        return cls(
            source_id=payload["source_id"],
            source_type=payload["source_type"],
            title=payload["title"],
            reference=payload["reference"],
            collected_at=payload["collected_at"],
            version=payload["version"],
            validation_status=payload["validation_status"],
            claims=claims,
            derived_strategy_id=payload.get("derived_strategy_id"),
            similar_to=payload.get("similar_to", []),
            notes=payload.get("notes", ""),
        )
