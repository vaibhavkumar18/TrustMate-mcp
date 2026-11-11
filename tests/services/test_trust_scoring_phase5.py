import pytest

from app.domain.models import (
    BusinessEvidence,
    DomainEvidence,
    ProviderResult,
    ReputationEvidence,
    RetentionPolicy,
    ReviewEvidence,
    SellerEvidence,
    SellerIdentity,
    WebsiteEvidence,
)
from app.domain.policies.confidence_policy import coverage, confidence, sufficient_evidence
from app.domain.policies.scoring_policy import (
    SCORING_VERSION,
    business_score,
    evidence_fingerprint,
    factor_scores,
    review_score,
    website_score,
)
from app.services.trust_scoring_service import assess_evidence


def result(code: str, capability: str, status: str = "checked", **kwargs):
    return ProviderResult(
        provider_code=code,
        capability=capability,
        status=status,
        cache_policy=RetentionPolicy(cacheable=False),
        **kwargs,
    )


def evidence(
    *,
    reviews=True,
    review_scope="seller",
    review_rating=4.5,
    review_count=100,
    malicious=False,
    website=True,
    https=True,
    tls=True,
    contact=True,
    return_policy=True,
    refund_policy=True,
    business=True,
    match="high",
    official_website="https://store.example/",
):
    identity = SellerIdentity(
        display_name="Store",
        seller_key="domain:store.example",
        normalized_domain="store.example",
        canonical_url="https://store.example/",
        identity_status="domain_matched",
    )
    return SellerEvidence(
        identity=identity,
        business=BusinessEvidence(
            provider=result("foursquare", "business") if business else result("foursquare", "business", "unverified"),
            provider_place_id="place-1" if business else None,
            match_confidence=match if business else None,
            official_website=official_website if business else None,
            reviews=ReviewEvidence(
                scope=review_scope,
                rating=review_rating,
                review_count=review_count,
            ) if reviews else None,
        ),
        domain=DomainEvidence(
            provider=result("rdap", "domain", "unverified"),
            domain="store.example",
        ),
        website=WebsiteEvidence(
            provider=result("native", "website") if website else result("native", "website", "unverified"),
            https_reachable=https if website else None,
            tls_valid=tls if website else None,
            contact_present=contact if website else None,
            return_policy_present=return_policy if website else None,
            refund_policy_present=refund_policy if website else None,
        ),
        reputation=ReputationEvidence(
            provider=result("urlhaus", "reputation", "checked" if malicious is not None else "unverified"),
            is_malicious=malicious,
        ),
    )


def test_full_evidence_is_trusted_high_confidence():
    trust = assess_evidence(evidence())
    # Current factor weights and normalized factor values yield 3.88;
    # HTTPS/TLS are baseline controls and do not add a trust bonus.
    assert trust.score == 3.88
    assert trust.label == "Mixed"
    assert trust.confidence == "high"
    assert len(trust.factors) == 4


def test_business_factor_requires_registered_domain_corroboration():
    item = evidence(official_website="https://unrelated.example/")
    score, status, detail = business_score(item)
    assert score is None
    assert status == "unverified"
    assert "corroborated" in detail


def test_medium_business_match_is_lower_than_high_match():
    high = business_score(evidence(match="high"))[0]
    medium = business_score(evidence(match="medium"))[0]
    assert high == 3.5
    assert medium == 3.0


def test_place_reviews_are_not_seller_reviews():
    score, status, _ = review_score(evidence(review_scope="place"))
    assert score is None
    assert status == "unverified"


def test_unknown_review_scope_is_not_seller_review():
    score, status, _ = review_score(evidence(review_scope="unknown"))
    assert score is None
    assert status == "unverified"


def test_low_review_volume_is_capped_and_warned():
    score, status, detail = review_score(
        evidence(review_rating=5.0, review_count=1)
    )
    assert score == 3.5
    assert status == "warn"
    assert "1 reviews" in detail


def test_https_and_tls_only_do_not_create_a_website_score():
    item = evidence(contact=None, return_policy=None, refund_policy=None)
    score, status, detail = website_score(item)
    assert score is None
    assert status == "unverified"
    assert "count as one" in detail


def test_https_tls_are_one_group_and_contact_is_required_as_second_group():
    item = evidence(contact=True, return_policy=None, refund_policy=None)
    score, status, _ = website_score(item)
    assert score == 3.5
    assert status == "warn"


def test_contact_and_policy_can_be_scored_without_transport_bonus():
    item = evidence(https=None, tls=None, contact=True, return_policy=True, refund_policy=None)
    score, status, _ = website_score(item)
    assert score == 4.0
    assert status == "ok"


def test_one_missing_policy_check_does_not_mean_all_policy_is_absent():
    item = evidence(
        https=True,
        tls=True,
        contact=None,
        return_policy=False,
        refund_policy=None,
    )
    score, status, _ = website_score(item)
    assert score is None
    assert status == "unverified"


def test_both_policy_checks_confirmed_absent_are_negative_evidence():
    item = evidence(
        https=None,
        tls=None,
        contact=True,
        return_policy=False,
        refund_policy=False,
    )
    score, status, _ = website_score(item)
    assert score == 3.25
    assert status == "warn"


def test_failed_tls_is_a_negative_website_factor():
    item = evidence(tls=False, contact=None, return_policy=None, refund_policy=None)
    score, status, detail = website_score(item)
    assert score == 1.5
    assert status == "bad"
    assert "TLS certificate validation" in detail


def test_missing_provider_evidence_stays_unverified():
    trust = assess_evidence(
        evidence(reviews=False, website=False, business=False, malicious=None)
    )
    assert trust.score is None
    assert trust.label == "Unrated"
    assert trust.confidence == "low"
    # Four diagnostics are preserved even when the overall score is Unrated.
    assert len(trust.factors) == 4
    assert all(f.status == "unverified" for f in trust.factors)


def test_malicious_reputation_is_a_one_point_factor_not_missing_evidence():
    trust = assess_evidence(evidence(reviews=False, malicious=True))
    reputation = next(f for f in trust.factors if f.name == "URL reputation")
    assert reputation.status == "bad"
    assert trust.score == 3.14
    assert trust.label == "Mixed"


def test_coverage_uses_only_valid_fixed_group_scores():
    scores = {
        "Business presence": (5.0, "ok", "x"),
        "Reviews": (None, "unverified", "x"),
        "Website signals": (4.0, "ok", "x"),
        "URL reputation": (float("nan"), "warn", "x"),
        "unknown": (5.0, "ok", "x"),
    }
    assert coverage(scores) == pytest.approx(0.55)
    assert confidence(scores) == "low"
    assert sufficient_evidence(scores) is True


def test_two_groups_and_55_percent_coverage_produce_low_confidence_score():
    scores = {
        "Business presence": (3.5, "warn", "x"),
        "Reviews": (None, "unverified", "x"),
        "Website signals": (3.5, "warn", "x"),
        "URL reputation": (None, "unverified", "x"),
    }
    assert coverage(scores) == pytest.approx(0.55)
    assert sufficient_evidence(scores) is True
    assert confidence(scores) == "low"


def test_evidence_fingerprint_is_stable_across_provider_timestamps():
    first = evidence()
    second = evidence()
    assert first.business.provider.observed_at != second.business.provider.observed_at
    assert evidence_fingerprint(first) == evidence_fingerprint(second)


def test_evidence_fingerprint_changes_when_a_scoring_signal_changes():
    first = evidence()
    second = evidence(contact=False)
    assert evidence_fingerprint(first) != evidence_fingerprint(second)


def test_scoring_version_is_bumped_for_normalization_change():
    assert SCORING_VERSION == "2.3"
