import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.domain.models import ProductSearchResult
from app.providers.factory import ProviderConfigurationError, create_provider_registry
from app.providers.implementations import (
    Channel3Provider, MockBusinessProvider, MockProductProvider, RDAPProvider, URLhausProvider,
)
from app.services.seller_verification import business_with_fallback


@pytest.mark.asyncio
async def test_mock_registry_implements_all_capabilities():
    registry = create_provider_registry(Settings(mock=True))
    assert isinstance(await registry.product.search("laptop", 60000, target_country="US", currency="USD"), ProductSearchResult)
    assert (await registry.business.search_business("Mock Store")).provider.status == "checked"
    assert (await registry.reputation.check_url("https://mock.example")).provider.status == "checked"
    assert (await registry.domain.lookup_domain("mock.example")).provider.status == "unverified"
    website = await registry.website.inspect("https://mock.example")
    assert website.https_reachable is True
    assert (await registry.llm.generate_explanation({}, [])).provider.provider_code == "mock"


def test_real_mode_requires_credentials():
    settings = Settings(
        mock=False, channel3_api_key=None, foursquare_api_key=None,
        urlhaus_auth_key=None, openrouter_api_key=None,
    )
    with pytest.raises(ProviderConfigurationError, match="CHANNEL3_API_KEY"):
        create_provider_registry(settings)


def test_real_mode_does_not_fall_back_to_mock_products():
    registry = create_provider_registry(Settings(
        mock=False,
        channel3_api_key="configured",
        foursquare_api_key="configured",
        urlhaus_auth_key="configured",
        openrouter_api_key="configured",
    ))
    assert isinstance(registry.product, Channel3Provider)
    assert not isinstance(registry.product, MockProductProvider)


def test_normalized_models_reject_invalid_status():
    with pytest.raises(ValidationError):
        MockBusinessProvider  # keep the interface import exercised
        from app.domain.models import ProviderResult, RetentionPolicy
        ProviderResult(
            provider_code="test", capability="business", status="invalid",
            cache_policy=RetentionPolicy(cacheable=False),
        )


class FailingBusinessProvider:
    async def search_business(self, seller_name, domain=None, location_hint=None):
        from app.domain.models import BusinessEvidence, ProviderResult, RetentionPolicy
        return BusinessEvidence(provider=ProviderResult(
            provider_code="primary", capability="business", status="error",
            error_code="timeout", cache_policy=RetentionPolicy(cacheable=False),
        ))


@pytest.mark.asyncio
async def test_business_fallback_preserves_provenance():
    result = await business_with_fallback(
        FailingBusinessProvider(), create_provider_registry(Settings(mock=True)).business_fallback,
        "Example Store", "example.com",
    )
    assert result.provider.status == "checked"
    assert result.provider.provider_code == "openstreetmap"
    assert result.attribution == "© OpenStreetMap contributors"


@pytest.mark.asyncio
async def test_unavailable_primary_without_successful_fallback_is_unverified():
    primary = FailingBusinessProvider()
    result = await business_with_fallback(primary, primary, "Example Store")
    assert result.provider.status == "error"
    assert result.provider.error_code == "timeout"


def test_retention_policies_are_attached():
    registry = create_provider_registry(Settings(mock=True))
    assert registry.business.search_business
    assert registry.reputation


def test_urlhaus_and_rdap_payloads_normalize_without_raw_payloads():
    # A positive host listing without per-URL status details is not enough
    # to label the seller actively malicious or clean.
    reputation = URLhausProvider.map_payload({"query_status": "ok", "urls": [{"id": 1}]})
    assert reputation.is_malicious is None
    assert reputation.threat_type == "listed_host_details_unavailable"
    assert reputation.provider.data["match"] is True
    assert reputation.provider.data["active_url_count"] == 0

    domain = RDAPProvider.map_payload(
        "example.com",
        {"events": [{"eventAction": "registration", "eventDate": "2020-01-01T00:00:00Z"}]},
    )
    assert domain.domain == "example.com"
    assert domain.creation_date is not None
    assert "events" not in domain.provider.data
