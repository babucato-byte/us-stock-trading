"""Stage 6: user/YouTube strategy source structuring tests.

Every repository test isolates STRATEGY_SOURCES_DIR to tmp_path -- never
touches the real docs/strategy/sources/ catalog.
"""
import pytest

from strategy.status import ACTIVE, COLLECTED, REJECTED, REVIEWED, STRUCTURED
from strategy_sources import known_sources, repository, similarity
from strategy_sources.models import (
    CATEGORY_ENTRY,
    CATEGORY_STOP,
    ORIGIN_ASSUMPTION,
    ORIGIN_SOURCE,
    ORIGIN_UNKNOWN,
    SOURCE_TYPE_YOUTUBE,
    InvalidStrategyClaimError,
    InvalidStrategySourceError,
    StrategyClaim,
    StrategySource,
)


def _claim(**overrides):
    defaults = dict(category=CATEGORY_ENTRY, statement="enter on breakout", origin=ORIGIN_ASSUMPTION)
    defaults.update(overrides)
    return StrategyClaim(**defaults)


def _source(**overrides):
    defaults = dict(
        source_id="TEST_SOURCE",
        source_type=SOURCE_TYPE_YOUTUBE,
        title="Test Strategy",
        reference="https://example.invalid/video",
        collected_at="2026-07-25T00:00:00+00:00",
        version=1,
        validation_status=COLLECTED,
        claims=[_claim()],
    )
    defaults.update(overrides)
    return StrategySource(**defaults)


# ---------------------------------------------------------------------------
# StrategyClaim validation
# ---------------------------------------------------------------------------

def test_claim_requires_valid_category():
    with pytest.raises(InvalidStrategyClaimError):
        _claim(category="NOT_A_CATEGORY")


def test_claim_requires_valid_origin():
    with pytest.raises(InvalidStrategyClaimError):
        _claim(origin="NOT_AN_ORIGIN")


def test_claim_origin_source_requires_excerpt():
    with pytest.raises(InvalidStrategyClaimError):
        _claim(origin=ORIGIN_SOURCE, source_excerpt=None)


def test_claim_origin_source_with_excerpt_is_valid():
    claim = _claim(origin=ORIGIN_SOURCE, source_excerpt="the video literally says this")
    assert claim.origin == ORIGIN_SOURCE


def test_claim_unknown_origin_does_not_require_excerpt():
    claim = _claim(origin=ORIGIN_UNKNOWN)
    assert claim.source_excerpt is None


def test_claim_confidence_out_of_range_rejected():
    with pytest.raises(InvalidStrategyClaimError):
        _claim(confidence=1.5)


# ---------------------------------------------------------------------------
# StrategySource validation -- the "never ACTIVE" guarantee
# ---------------------------------------------------------------------------

def test_source_active_status_is_structurally_rejected():
    with pytest.raises(InvalidStrategySourceError):
        _source(validation_status=ACTIVE)


def test_source_rejects_every_non_source_status():
    for bad_status in ["BACKTESTED", "PAPER_APPROVED", "LIMITED_LIVE_APPROVED", "PAUSED", "not-a-status"]:
        with pytest.raises(InvalidStrategySourceError):
            _source(validation_status=bad_status)


def test_source_accepts_all_four_source_statuses():
    for status in [COLLECTED, STRUCTURED, REVIEWED, REJECTED]:
        source = _source(validation_status=status)
        assert source.validation_status == status


def test_source_requires_at_least_one_claim():
    with pytest.raises(InvalidStrategySourceError):
        _source(claims=[])


def test_source_requires_nonempty_source_id():
    with pytest.raises(InvalidStrategySourceError):
        _source(source_id="   ")


def test_source_to_dict_and_from_dict_round_trip():
    source = _source(claims=[_claim(origin=ORIGIN_SOURCE, source_excerpt="quoted text")])
    payload = source.to_dict()
    reloaded = StrategySource.from_dict(payload)
    assert reloaded.source_id == source.source_id
    assert reloaded.claims[0].origin == ORIGIN_SOURCE
    assert reloaded.claims[0].source_excerpt == "quoted text"


def test_claims_by_origin_filters_correctly():
    source = _source(claims=[
        _claim(origin=ORIGIN_SOURCE, source_excerpt="x"),
        _claim(origin=ORIGIN_ASSUMPTION),
        _claim(origin=ORIGIN_ASSUMPTION),
    ])
    assert len(source.claims_by_origin(ORIGIN_ASSUMPTION)) == 2
    assert len(source.claims_by_origin(ORIGIN_SOURCE)) == 1


# ---------------------------------------------------------------------------
# Repository: versioned, append-only storage
# ---------------------------------------------------------------------------

@pytest.fixture
def sources_dir(tmp_path):
    return tmp_path / "sources"


def test_save_and_load_round_trip(sources_dir):
    source = _source()
    repository.save_source(source, sources_dir=sources_dir)

    loaded = repository.load_source(source.source_id, sources_dir=sources_dir)
    assert loaded.source_id == source.source_id
    assert loaded.version == 1


def test_load_nonexistent_source_returns_none(sources_dir):
    assert repository.load_source("DOES_NOT_EXIST", sources_dir=sources_dir) is None


def test_save_wrong_version_number_rejected(sources_dir):
    source = _source(version=2)  # first save must be version 1
    with pytest.raises(repository.RepositoryError):
        repository.save_source(source, sources_dir=sources_dir)


def test_save_new_version_after_existing_succeeds(sources_dir):
    v1 = _source(version=1)
    repository.save_source(v1, sources_dir=sources_dir)
    v2 = _source(version=2, notes="revised")
    repository.save_source(v2, sources_dir=sources_dir)

    latest = repository.load_source(v1.source_id, sources_dir=sources_dir)
    assert latest.version == 2
    assert latest.notes == "revised"

    original = repository.load_source(v1.source_id, version=1, sources_dir=sources_dir)
    assert original.notes == ""  # v1 preserved, not overwritten


def test_save_skipping_a_version_number_rejected(sources_dir):
    v1 = _source(version=1)
    repository.save_source(v1, sources_dir=sources_dir)
    v3 = _source(version=3)
    with pytest.raises(repository.RepositoryError):
        repository.save_source(v3, sources_dir=sources_dir)


def test_next_version_helper(sources_dir):
    assert repository.next_version("NEW_ID", sources_dir=sources_dir) == 1
    repository.save_source(_source(version=1), sources_dir=sources_dir)
    assert repository.next_version("TEST_SOURCE", sources_dir=sources_dir) == 2


def test_load_all_versions_returns_every_version_in_order(sources_dir):
    repository.save_source(_source(version=1), sources_dir=sources_dir)
    repository.save_source(_source(version=2), sources_dir=sources_dir)
    versions = repository.load_all_versions("TEST_SOURCE", sources_dir=sources_dir)
    assert [v.version for v in versions] == [1, 2]


def test_list_source_ids_and_load_all_latest(sources_dir):
    repository.save_source(_source(source_id="A", version=1), sources_dir=sources_dir)
    repository.save_source(_source(source_id="B", version=1), sources_dir=sources_dir)
    repository.save_source(_source(source_id="A", version=2), sources_dir=sources_dir)

    assert repository.list_source_ids(sources_dir=sources_dir) == ["A", "B"]
    latest = repository.load_all_latest(sources_dir=sources_dir)
    assert latest["A"].version == 2
    assert latest["B"].version == 1


def test_real_sources_dir_untouched_by_tests():
    assert not repository.DEFAULT_SOURCES_DIR.exists() or not any(
        p.name.startswith("TEST_SOURCE") for p in repository.DEFAULT_SOURCES_DIR.glob("*")
    )


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------

def test_identical_claims_score_similarity_one():
    a = _source(source_id="A", claims=[_claim(statement="enter on vwap reclaim with volume")])
    b = _source(source_id="B", claims=[_claim(statement="enter on vwap reclaim with volume")])
    result = similarity.compute_similarity(a, b)
    assert result["overall_score"] == pytest.approx(1.0)
    assert result["shared_categories"] == [CATEGORY_ENTRY]


def test_disjoint_claims_score_similarity_zero():
    a = _source(source_id="A", claims=[_claim(statement="enter on vwap reclaim")])
    b = _source(source_id="B", claims=[_claim(statement="totally unrelated indicator combo")])
    result = similarity.compute_similarity(a, b)
    assert result["overall_score"] == pytest.approx(0.0)


def test_categories_only_one_source_has_are_excluded_from_overall_score():
    a = _source(source_id="A", claims=[
        _claim(category=CATEGORY_ENTRY, statement="enter on breakout"),
        _claim(category=CATEGORY_STOP, statement="stop below recent low"),
    ])
    b = _source(source_id="B", claims=[_claim(category=CATEGORY_ENTRY, statement="enter on breakout")])
    result = similarity.compute_similarity(a, b)
    assert result["shared_categories"] == [CATEGORY_ENTRY]
    assert CATEGORY_STOP in result["per_category"]  # still reported
    assert result["overall_score"] == pytest.approx(1.0)  # only ENTRY (shared) counted


def test_find_similar_filters_by_threshold_and_sorts_descending():
    target = _source(source_id="TARGET", claims=[_claim(statement="enter on vwap reclaim with volume expansion")])
    close = _source(source_id="CLOSE", claims=[_claim(statement="enter on vwap reclaim with volume")])
    far = _source(source_id="FAR", claims=[_claim(statement="completely different setup entirely")])

    results = similarity.find_similar(target, [close, far], threshold=0.3)
    assert [c.source_id for c, _ in results] == ["CLOSE"]


def test_find_similar_excludes_self():
    target = _source(source_id="TARGET")
    results = similarity.find_similar(target, [target], threshold=0.0)
    assert results == []


# ---------------------------------------------------------------------------
# Known sources catalog (Stage 6's 8 required entries)
# ---------------------------------------------------------------------------

def test_all_known_sources_returns_eight_entries():
    sources = known_sources.all_known_sources()
    assert len(sources) == 8
    assert len({s.source_id for s in sources}) == 8  # all unique


def test_no_known_source_is_active_or_beyond_reviewed():
    for source in known_sources.all_known_sources():
        assert source.validation_status in (COLLECTED, STRUCTURED, REVIEWED, REJECTED)


def test_unverified_candidate_sources_have_no_source_origin_claims():
    # Turtle/multi-TF RSI/Bollinger/CCI-RSI-ADX have no real cited source
    # material -- every claim must be ASSUMPTION, never fabricated SOURCE.
    unverified_ids = {"TURTLE_TREND_FOLLOWING", "MULTI_TIMEFRAME_RSI", "BOLLINGER_BAND_PULLBACK", "CCI_RSI_ADX_COMBO"}
    for source in known_sources.all_known_sources():
        if source.source_id in unverified_ids:
            assert source.reference.startswith("TBD_OPERATOR")
            assert all(c.origin == ORIGIN_ASSUMPTION for c in source.claims)
            assert source.derived_strategy_id is None


def test_reviewed_sources_cite_real_constitution_excerpts():
    reviewed = {s.source_id: s for s in known_sources.all_known_sources()}
    vwap = reviewed["VWAP_MOMENTUM_ENTRY"]
    assert vwap.validation_status == REVIEWED
    source_claims = vwap.claims_by_origin(ORIGIN_SOURCE)
    assert source_claims
    assert all(c.source_excerpt for c in source_claims)


def test_seed_known_sources_writes_all_eight_and_is_idempotent(tmp_path):
    sources_dir = tmp_path / "sources"
    first = known_sources.seed_known_sources(sources_dir=sources_dir)
    assert len(first["saved"]) == 8
    assert first["skipped"] == []

    second = known_sources.seed_known_sources(sources_dir=sources_dir)
    assert second["saved"] == []
    assert len(second["skipped"]) == 8

    assert len(repository.list_source_ids(sources_dir=sources_dir)) == 8


def test_seeded_sources_are_loadable_and_valid(tmp_path):
    sources_dir = tmp_path / "sources"
    known_sources.seed_known_sources(sources_dir=sources_dir)
    for source_id in repository.list_source_ids(sources_dir=sources_dir):
        loaded = repository.load_source(source_id, sources_dir=sources_dir)
        assert loaded is not None
        assert loaded.claims
