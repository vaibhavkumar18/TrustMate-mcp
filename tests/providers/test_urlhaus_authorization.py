import pytest

from app.core.config import Settings
from app.providers.factory import create_provider_registry
from app.providers.implementations import AuthorizationGatedURLhausProvider


@pytest.mark.asyncio
async def test_live_urlhaus_is_disabled_without_authorization():
    provider = AuthorizationGatedURLhausProvider("configured", live_test=True)
    result = await provider.check_url("https://seller.example")
    assert result.provider.status == "unverified"
    assert result.provider.error_code == "authorization_required"
    assert result.provider.data == {}


@pytest.mark.asyncio
async def test_live_urlhaus_requires_explicit_live_test_flag():
    provider = AuthorizationGatedURLhausProvider("configured", authorized=True)
    result = await provider.check_url("https://seller.example")
    assert result.provider.status == "unverified"
    assert result.provider.data == {}


@pytest.mark.asyncio
async def test_live_urlhaus_requires_auth_key_after_authorization():
    provider = AuthorizationGatedURLhausProvider(
        None, authorized=True, live_test=True
    )
    result = await provider.check_url("https://seller.example")
    assert result.provider.status == "unverified"
    assert result.provider.data == {}


def test_real_registry_uses_gated_urlhaus_provider():
    registry = create_provider_registry(Settings(
        mock=False,
        channel3_api_key="configured",
        foursquare_api_key="configured",
        urlhaus_auth_key="configured",
        openrouter_api_key="configured",
    ))
    assert isinstance(registry.reputation, AuthorizationGatedURLhausProvider)


@pytest.mark.asyncio
async def test_gated_provider_does_not_create_ai_explanation_evidence():
    provider = AuthorizationGatedURLhausProvider("configured")
    result = await provider.check_url("https://seller.example")
    assert result.provider.data == {}
