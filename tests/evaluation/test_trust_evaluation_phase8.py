"""Synthetic policy scenarios; these are not real seller accuracy measurements."""

import pytest

from app.domain.models import (
    BusinessEvidence,
    DomainEvidence,
    Offer,
    ProviderResult,
    ReputationEvidence,
    RetentionPolicy,
    ReviewEvidence,
    SellerEvidence,
    SellerIdentity,
    WebsiteEvidence,
)
from app.domain.policies.confidence_policy import coverage
from app.domain.policies.scoring_policy import factor_scores
from app.services.offer_ranking_service import RankingCandidate, rank_candidates
from app.services.trust_scoring_service import assess_evidence


def provider(code: str, capability: str, status: str = "checked") -> ProviderResult:
    return ProviderResult(
        provider_code=code,
        capability=capability,
        status=status,
        cache_policy=RetentionPolicy(cacheable=False),
    )


def scenario(
    *,
    business_status: str = "checked",
    match: str | None = "high",
    place_id: str | None = "place-1",
    official_website: str | None = "https://synthetic.example/",
    reviews: tuple[float, int] | None = (4.5, 100),
    website: tuple[bool | None, bool | None, bool | None, bool | None] | None = (
        True, True, True, True
    ),
    malicious: bool | None = False,
) -> SellerEvidence:
    website_evidence = WebsiteEvidence(provider=provider("native", "website", "unverified"))
    if website is not None:
        website_evidence = WebsiteEvidence(
            provider=provider("native", "website"),
            https_reachable=website[0],
            tls_valid=website[1],
            contact_present=website[2],
            return_policy_present=website[3],
            # Synthetic fixture treats one observed return/refund page as the
            # policy group; the other policy field remains unknown.
            refund_policy_present=None,
        )

    return SellerEvidence(
        identity=SellerIdentity(
            display_name="Synthetic Store",
            seller_key="domain:synthetic.example",
            normalized_domain="synthetic.example",
            canonical_url="https://synthetic.example/",
            identity_status="domain_matched",
        ),
        business=BusinessEvidence(
            provider=provider("foursquare", "business", business_status),
            provider_place_id=place_id,
            match_confidence=match,
            official_website=official_website,
            reviews=ReviewEvidence(
                scope="seller",
                rating=reviews[0],
                review_count=reviews[1],
            ) if reviews is not None else None,
        ),
        domain=DomainEvidence(
            provider=provider("rdap", "domain", "unverified"),
            domain="synthetic.example",
        ),
        website=website_evidence,
        reputation=ReputationEvidence(
            provider=provider("urlhaus", "reputation", "checked" if malicious is not None else "unverified"),
            is_malicious=malicious,
        ),
    )


SCENARIOS = [
    ("strong positive", scenario(), "Trusted", "high", 4.03),
    (
        "partial positive",
        scenario(reviews=(4.5, 5), website=(True, True, False, None), malicious=None),
        "Mixed", "medium", 3.28,
    ),
    (
        "conflicting evidence",
        scenario(match="medium", reviews=(2.0, 20),
                 website=(True, True, False, None), malicious=True),
        "Mixed", "high", 2.52,
    ),
    (
        "low evidence coverage",
        scenario(reviews=None, website=None, malicious=None),
        "Unrated", "low", None,
    ),
    (
        "missing evidence",
        scenario(business_status="unverified", match=None, place_id=None,
                 official_website=None, reviews=None, website=None, malicious=None),
        "Unrated", "low", None,
    ),
    (
        "new domain",
        scenario(reviews=(4.0, 20), website=(True, True, False, None), malicious=None),
        "Mixed", "medium", 3.53,
    ),
    (
        "low review count",
        scenario(reviews=(5.0, 1), malicious=None),
        "Mixed", "medium", 3.65,
    ),
    (
        "business identity mismatch",
        scenario(match="low", reviews=None,
                 website=(True, True, True, True), malicious=None),
        "Unrated", "low", None,
    ),
    (
        "provider failures",
        scenario(business_status="error", match=None, place_id=None,
                 official_website=None, reviews=None, website=None, malicious=None),
        "Unrated", "low", None,
    ),
    (
        "mixed positive and negative",
        scenario(match="low", reviews=(1.0, 100),
                 website=(False, False, True, True), malicious=True),
        "Risky", "medium", 1.52,
    ),
]


@pytest.mark.parametrize(
    "name,evidence,expected_label,expected_confidence,expected_score",
    SCENARIOS,
)
def test_synthetic_trust_scenario(
    name, evidence, expected_label, expected_confidence, expected_score
):
    trust = assess_evidence(evidence)
    assert trust.label == expected_label, name
    assert trust.confidence == expected_confidence, name
    assert trust.score == expected_score, name
    assert len(trust.factors) == 4, name


def test_provider_failures_and_missing_evidence_are_not_negative_scores():
    failed = assess_evidence(SCENARIOS[8][1])
    missing = assess_evidence(SCENARIOS[4][1])
    assert failed.score is None
    assert missing.score is None
    assert all(f.status == "unverified" for f in failed.factors)


def test_two_groups_only_produce_low_confidence_not_high_confidence():
    scores = factor_scores(
        scenario(reviews=None, website=(True, True, True, None), malicious=None)
    )
    assert coverage(scores) == pytest.approx(0.55)
    assert assess_evidence(
        scenario(reviews=None, website=(True, True, True, None), malicious=None)
    ).confidence == "low"


def test_identity_mismatch_is_not_a_scorable_business_factor():
    trust = assess_evidence(SCENARIOS[7][1])
    business_factor = next(f for f in trust.factors if f.name == "Business presence")
    assert business_factor.status == "unverified"
    assert trust.score is None


def _candidate(identifier: str, trust):
    seller = SellerIdentity(
        display_name=identifier,
        seller_key=f"domain:{identifier}.example",
        normalized_domain=f"{identifier}.example",
        canonical_url=f"https://{identifier}.example/",
        identity_status="domain_matched",
    )
    return RankingCandidate(
        offer=Offer(
            id=identifier,
            product_id="synthetic-product",
            title=identifier,
            price=500,
            currency="USD",
            url=f"https://{identifier}.example/item",
            seller=seller,
        ),
        trust=trust,
    )


def test_ranking_consumes_evaluation_trust_without_recommending_unrated_or_risky():
    trusts = [assess_evidence(item[1]) for item in SCENARIOS]
    candidates = [
        _candidate("trusted", trusts[0]),
        _candidate("mixed", trusts[2]),
        _candidate("risky", trusts[9]),
        _candidate("unrated", trusts[4]),
    ]
    results = rank_candidates(candidates, 1000, currency="USD")
    by_id = {item[0].offer.id: item[0] for item in results}
    assert by_id["trusted"].trust.label == "Trusted"
    assert by_id["mixed"].trust.label == "Mixed"
    assert not next(item[2] for item in results if item[0].offer.id == "risky")
    assert not next(item[2] for item in results if item[0].offer.id == "unrated")
    assert sum(1 for item in results if item[2]) == 1
