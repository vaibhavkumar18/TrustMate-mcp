import socket

import pytest

from app.core.config import Settings
from app.domain.models import BusinessEvidence, ProviderResult, RetentionPolicy, ReviewEvidence
from app.providers.factory import create_provider_registry
from app.services.seller_identity import normalize_domain, normalize_seller
from app.services.seller_verification_service import collect_seller_evidence


def test_identity_normalizes_www_tracking_and_idna(monkeypatch):
    def public_dns_result(host, port, type=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr("app.services.seller_identity.socket.getaddrinfo", public_dns_result)
    identity = normalize_seller(
        "Example", "https://www.Example.COM/shop?utm_source=x&ref=keep"
    )
    assert identity.normalized_domain == "example.com"
    assert identity.canonical_url == "https://example.com/"
    assert identity.original_url.startswith("https://")
    assert normalize_domain("www.Bücher.de") == "xn--bcher-kva.de"


def test_identity_rejects_private_hosts():
    with pytest.raises(ValueError):
        normalize_seller("Local", "http://127.0.0.1/admin")


def test_identity_rejects_unresolvable_hosts(monkeypatch):
    def fail(*args, **kwargs):
        raise socket.gaierror()

    monkeypatch.setattr("app.services.seller_identity.socket.getaddrinfo", fail)
    with pytest.raises(ValueError):
        normalize_seller("Unknown", "https://unresolvable.example")


@pytest.mark.asyncio
async def test_orchestration_collects_all_mock_evidence():
    registry = create_provider_registry(Settings(mock=True))
    result = await collect_seller_evidence(
        "Mock Store", "https://mock.example/item",
        registry.business, registry.business_fallback, registry.domain,
        registry.website, registry.reputation,
    )
    assert result.identity.normalized_domain == "mock.example"
    assert result.business.provider.status == "checked"
    assert result.domain.provider.status == "unverified"
    assert result.website.provider.status == "checked"
    assert result.reputation.provider.status == "checked"


class FailingBusiness:
    async def search_business(self, seller_name, domain=None, location_hint=None):
        raise TimeoutError()


@pytest.mark.asyncio
async def test_orchestration_preserves_fallback_and_errors():
    registry = create_provider_registry(Settings(mock=True))
    result = await collect_seller_evidence(
        "Fallback Store", "https://fallback.example",
        FailingBusiness(), registry.business_fallback, registry.domain,
        registry.website, registry.reputation,
    )
    assert result.business.provider.provider_code == "openstreetmap"
    assert "business: TimeoutError" in result.errors


def test_provider_provenance_and_retention_are_present():
    result = ProviderResult(
        provider_code="foursquare", capability="business", status="unverified",
        cache_policy=RetentionPolicy(cacheable=False, attribution="Powered by Foursquare"),
    )
    assert result.cache_policy.cacheable is False
    assert result.cache_policy.attribution == "Powered by Foursquare"


@pytest.mark.asyncio
async def test_orchestration_collects_optional_seller_review_evidence():
    registry = create_provider_registry(Settings(mock=True))

    class StaticReviewProvider:
        async def lookup_seller_reviews(self, domain, target_country=None):
            assert domain == "mock.example"
            assert target_country == "US"
            return ReviewEvidence(
                scope="seller",
                rating=4.2,
                review_count=12,
                provider=ProviderResult(
                    provider_code="test-reviews",
                    capability="seller_reviews",
                    status="checked",
                    cache_policy=RetentionPolicy(cacheable=False),
                ),
            )

    result = await collect_seller_evidence(
        "Mock Store",
        "https://mock.example/item",
        registry.business,
        registry.business_fallback,
        registry.domain,
        registry.website,
        registry.reputation,
        target_country="US",
        reviews_provider=StaticReviewProvider(),
    )

    assert result.reviews is not None
    assert result.reviews.scope == "seller"
    assert result.reviews.review_count == 12
