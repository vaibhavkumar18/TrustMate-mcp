import httpx
import pytest

from app.domain.policies.scoring_policy import review_score
from app.domain.models import (
    BusinessEvidence, DomainEvidence, ProviderResult, ReputationEvidence,
    RetentionPolicy, SellerEvidence, SellerIdentity, WebsiteEvidence,
)
from app.providers.trustpilot import TrustpilotReviewProvider
from app.core.config import Settings
from app.providers.factory import ProviderConfigurationError, create_provider_registry


def _profile():
    return {
        "id": "tp-unit-1",
        "name": {"display": "Example Shop", "identifying": "https://www.example.com", "referring": ["example.com"]},
        "websiteUrl": "https://www.example.com",
        "status": "active",
        "numberOfReviews": {"total": 231},
    }


def _review(stars, verified=True, location=""):
    return {
        "stars": stars,
        "isVerified": verified,
        "reviewedLocationName": location,
        "createdAt": "2026-10-01T12:00:00Z",
    }


def _trustpilot_transport(*, review_status=200, expected_key="test-key"):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        assert request.headers.get("apikey") == expected_key
        if path.endswith("/business-units"):
            assert request.url.params.get("domain") == "example.com"
            return httpx.Response(200, json={"businessUnits": [_profile()]}, request=request)
        if path.endswith("/business-units/tp-unit-1"):
            return httpx.Response(200, json=_profile(), request=request)
        if path.endswith("/business-units/tp-unit-1/reviews"):
            if review_status != 200:
                return httpx.Response(review_status, json={"error": "not exposed"}, request=request)
            if request.url.params.get("nextToken") == "page-2":
                return httpx.Response(
                    200,
                    json={"reviews": [_review(5), _review(4), _review(1, location="Outlet 2")]},
                    request=request,
                )
            return httpx.Response(
                200,
                json={
                    "reviews": [
                        _review(5), _review(4), _review(4), _review(3), _review(5),
                        _review(4), _review(5), _review(4), _review(5), _review(4),
                        _review(1, verified=False), _review(1, location="Outlet 1"),
                    ],
                    "nextToken": "page-2",
                },
                request=request,
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    return httpx.MockTransport(handler), calls


@pytest.mark.asyncio
async def test_trustpilot_uses_only_verified_whole_business_service_reviews():
    transport, calls = _trustpilot_transport()
    provider = TrustpilotReviewProvider("test-key", transport=transport)

    result = await provider.lookup_seller_reviews("example.com", "US")

    assert result.scope == "seller"
    assert result.review_count == 12
    assert result.total_review_count == 231
    assert result.rating == pytest.approx(4.3333)
    assert result.sample_complete is True
    assert result.snippets == []
    assert result.provider.status == "checked"
    assert result.provider.data["verified_reviews_inspected"] == 12
    assert len(calls) == 4
    assert all("text" not in str(call.url) for call in calls)



@pytest.mark.asyncio
async def test_trustpilot_uses_business_unit_details_to_confirm_domain():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/business-units"):
            return httpx.Response(
                200,
                json={"businessUnits": [{"id": "tp-unit-1", "name": {"identifying": "Example Retail"}}]},
                request=request,
            )
        if request.url.path.endswith("/business-units/tp-unit-1"):
            return httpx.Response(200, json=_profile(), request=request)
        if request.url.path.endswith("/business-units/tp-unit-1/reviews"):
            return httpx.Response(200, json={"reviews": []}, request=request)
        raise AssertionError(f"Unexpected request: {request.url}")

    provider = TrustpilotReviewProvider("test-key", transport=httpx.MockTransport(handler))
    result = await provider.lookup_seller_reviews("example.com")

    assert result.provider.status == "checked"
    assert result.scope == "seller"
    assert result.review_count == 0


@pytest.mark.asyncio
async def test_trustpilot_requires_exact_domain_corroboration():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/business-units"):
            # Search results are candidate discovery only; the details record
            # must independently corroborate the seller's registered domain.
            return httpx.Response(
                200,
                json={"businessUnits": [{"id": "wrong", "name": {"identifying": "Example Retail"}}]},
                request=request,
            )
        if request.url.path.endswith("/business-units/wrong"):
            return httpx.Response(
                200,
                json={
                    "id": "wrong",
                    "name": {"identifying": "https://example.com.attacker.invalid"},
                    "websiteUrl": "https://example.com.attacker.invalid",
                    "status": "active",
                },
                request=request,
            )
        raise AssertionError("Review retrieval must not follow a domain mismatch")

    provider = TrustpilotReviewProvider("test-key", transport=httpx.MockTransport(handler))
    result = await provider.lookup_seller_reviews("example.com")

    assert result.provider.status == "unverified"
    assert result.provider.error_code == "trustpilot_domain_not_corroborated"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_display_only_key_does_not_masquerade_as_review_evidence():
    transport, _ = _trustpilot_transport(review_status=403, expected_key="display-only-key")
    provider = TrustpilotReviewProvider("display-only-key", transport=transport)

    result = await provider.lookup_seller_reviews("example.com")

    assert result.scope == "unknown"
    assert result.provider.status == "unverified"
    assert result.provider.error_code == "insights_api_access_required"


def _seller_evidence(reviews):
    retention = RetentionPolicy(cacheable=False)
    return SellerEvidence(
        identity=SellerIdentity(
            display_name="Example", seller_key="domain:example.com",
            normalized_domain="example.com", canonical_url="https://example.com/",
            identity_status="domain_matched",
        ),
        business=BusinessEvidence(provider=ProviderResult(
            provider_code="test", capability="business", status="not_found", cache_policy=retention
        )),
        domain=DomainEvidence(provider=ProviderResult(
            provider_code="test", capability="domain", status="not_found", cache_policy=retention
        ), domain="example.com"),
        website=WebsiteEvidence(provider=ProviderResult(
            provider_code="test", capability="website", status="unverified", cache_policy=retention
        )),
        reputation=ReputationEvidence(provider=ProviderResult(
            provider_code="test", capability="reputation", status="unverified", cache_policy=retention
        )),
        reviews=reviews,
    )


@pytest.mark.asyncio
async def test_trustpilot_twelve_verified_reviews_meet_existing_minimum():
    transport, _ = _trustpilot_transport()
    reviews = await TrustpilotReviewProvider("test-key", transport=transport).lookup_seller_reviews("example.com")
    score, status, detail = review_score(_seller_evidence(reviews))

    assert score == pytest.approx(4.47)
    assert status == "ok"
    assert "12 verified reviews" in detail


@pytest.mark.asyncio
async def test_trustpilot_403_remains_unverified_not_negative():
    transport, _ = _trustpilot_transport(review_status=403, expected_key="display-only-key")
    reviews = await TrustpilotReviewProvider("display-only-key", transport=transport).lookup_seller_reviews("example.com")
    score, status, detail = review_score(_seller_evidence(reviews))

    assert score is None
    assert status == "unverified"
    assert "insights_api_access_required" in detail


def test_trustpilot_provider_is_opt_in_and_requires_key():
    base = {
        "mock": False,
        "channel3_api_key": "channel3-test-key",
        "foursquare_api_key": "foursquare-test-key",
        "review_provider": "trustpilot",
    }
    with pytest.raises(ProviderConfigurationError, match="TRUSTPILOT_API_KEY"):
        create_provider_registry(Settings(**base))

    registry = create_provider_registry(
        Settings(**base, trustpilot_api_key="trustpilot-test-key")
    )
    assert isinstance(registry.reviews, TrustpilotReviewProvider)
