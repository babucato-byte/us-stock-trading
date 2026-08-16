"""Deterministic, rule-based similarity scoring between two StrategySource
records.

Explicitly NOT an LLM judgment call -- mirrors the user's Stage 8
requirement that strategy *selection* be explainable/rule-based rather
than free-form model judgment; the same reasoning applies here, one stage
earlier: "these two sources look similar" should be a fact a human can
verify by re-running the same deterministic computation, not an opaque
model opinion. The method is plain per-category Jaccard overlap of
normalized claim-statement words, weighted equally across categories both
sources actually make claims in.
"""

import re

STOPWORDS = {
    "a", "an", "and", "at", "for", "if", "in", "is", "it", "of", "on",
    "or", "than", "that", "the", "then", "this", "to", "when", "with",
}


def _normalize_words(statement):
    words = re.findall(r"[a-z0-9]+", statement.lower())
    return {w for w in words if w not in STOPWORDS}


def _jaccard(set_a, set_b):
    if not set_a and not set_b:
        return 0.0
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


def compute_similarity(source_a, source_b):
    """Return {"overall_score": float, "per_category": {category: score},
    "shared_categories": [...]}. overall_score is the mean of per-category
    Jaccard scores across categories both sources make at least one claim
    in; categories only one source addresses are excluded from the
    average (silence isn't evidence of similarity or difference) but are
    still listed for a human reviewer under per_category with score 0.0
    marked via shared_categories membership.
    """
    words_a = _words_by_category(source_a)
    words_b = _words_by_category(source_b)
    all_categories = sorted(set(words_a) | set(words_b))
    shared_categories = sorted(set(words_a) & set(words_b))

    per_category = {}
    for category in all_categories:
        per_category[category] = _jaccard(words_a.get(category, set()), words_b.get(category, set()))

    if shared_categories:
        overall_score = sum(per_category[c] for c in shared_categories) / len(shared_categories)
    else:
        overall_score = 0.0

    return {
        "overall_score": overall_score,
        "per_category": per_category,
        "shared_categories": shared_categories,
    }


def _words_by_category(source):
    by_category = {}
    for claim in source.claims:
        by_category.setdefault(claim.category, set()).update(_normalize_words(claim.statement))
    return by_category


def find_similar(source, candidates, *, threshold=0.3):
    """Return [(candidate, score_dict), ...] for every candidate in
    `candidates` (an iterable of StrategySource, excluding `source`
    itself if present) whose overall_score >= threshold, sorted by score
    descending. Pure function -- does not mutate source.similar_to;
    callers decide whether/how to record the result."""
    results = []
    for candidate in candidates:
        if candidate.source_id == source.source_id:
            continue
        score_info = compute_similarity(source, candidate)
        if score_info["overall_score"] >= threshold:
            results.append((candidate, score_info))
    results.sort(key=lambda pair: pair[1]["overall_score"], reverse=True)
    return results
