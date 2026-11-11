from dataclasses import dataclass

from app.core.config import Settings
from app.providers.trustpilot import TrustpilotReviewProvider
from app.providers.implementations import (
    Channel3Provider, FoursquareProvider, MockBusinessProvider, MockDomainProvider,
    MockLLMProvider, MockProductProvider, MockReputationProvider, MockWebsiteProvider,
    OpenRouterProvider, OpenStreetMapProvider, RDAPProvider, URLhausProvider,
    AuthorizationGatedURLhausProvider,
    WebsiteProviderAdapter, NativeWebsiteProvider, UnavailableBusinessProvider, NominatimProvider,
    LiveRDAPProvider,
)


class ProviderConfigurationError(ValueError):
    pass


def _require(settings: Settings, value: str | None, name: str) -> None:
    if not settings.mock and not value:
        raise ProviderConfigurationError(f"{name} is required when MOCK=false")


@dataclass(frozen=True)
class ProviderRegistry:
    product: object
    business: object
    business_fallback: object
    reputation: object
    domain: object
    website: object
    llm: object
    reviews: object | None = None


def create_provider_registry(settings: Settings) -> ProviderRegistry:
    if settings.mock:
        return ProviderRegistry(
            MockProductProvider(), MockBusinessProvider(), OpenStreetMapProvider(),
            MockReputationProvider(), MockDomainProvider(), MockWebsiteProvider(), MockLLMProvider()
        )

    _require(settings, settings.channel3_api_key, "CHANNEL3_API_KEY")
    _require(settings, settings.foursquare_api_key, "FOURSQUARE_API_KEY")
    if settings.openrouter_explainer_enabled:
        _require(settings, settings.openrouter_api_key, "OPENROUTER_API_KEY")
    if settings.product_provider != "channel3":
        raise ProviderConfigurationError(f"Unknown product provider: {settings.product_provider}")
    if settings.business_provider != "foursquare":
        raise ProviderConfigurationError(f"Unknown business provider: {settings.business_provider}")
    if settings.business_fallback_provider != "openstreetmap":
        raise ProviderConfigurationError(f"Unknown business fallback: {settings.business_fallback_provider}")
    if settings.reputation_provider != "urlhaus":
        raise ProviderConfigurationError(f"Unknown reputation provider: {settings.reputation_provider}")
    if settings.domain_provider != "rdap":
        raise ProviderConfigurationError(f"Unknown domain provider: {settings.domain_provider}")
    if settings.website_provider != "native":
        raise ProviderConfigurationError(f"Unknown website provider: {settings.website_provider}")
    if settings.llm_provider != "openrouter":
        raise ProviderConfigurationError(f"Unknown LLM provider: {settings.llm_provider}")

    review_provider = None
    if settings.review_provider == "trustpilot":
        _require(settings, settings.trustpilot_api_key, "TRUSTPILOT_API_KEY")
        review_provider = TrustpilotReviewProvider(
            settings.trustpilot_api_key or "",
            base_url=settings.trustpilot_base_url,
            timeout_seconds=settings.trustpilot_timeout_seconds,
            max_review_pages=settings.trustpilot_max_review_pages,
        )
    elif settings.review_provider != "none":
        raise ProviderConfigurationError(f"Unknown review provider: {settings.review_provider}")

    registry = ProviderRegistry(
        Channel3Provider(
            settings.channel3_api_key,
            base_url=settings.channel3_base_url,
            timeout_seconds=settings.channel3_timeout_seconds,
            max_retries=settings.channel3_max_retries,
        ), FoursquareProvider(
            settings.foursquare_api_key,
            base_url=settings.foursquare_base_url,
            api_version=settings.foursquare_api_version,
            timeout_seconds=settings.foursquare_timeout_seconds,
            max_retries=settings.foursquare_max_retries,
            premium_enabled=settings.foursquare_premium_enabled,
        ), NominatimProvider(
            base_url=settings.osm_base_url,
            user_agent=settings.osm_user_agent,
            contact_email=settings.osm_contact_email,
            timeout_seconds=settings.osm_timeout_seconds,
            cache_ttl_seconds=settings.osm_cache_ttl_seconds,
        ),
        AuthorizationGatedURLhausProvider(
            settings.urlhaus_auth_key,
            authorized=settings.urlhaus_authorized,
            live_test=settings.urlhaus_live_test,
            base_url=settings.urlhaus_base_url,
            timeout_seconds=settings.urlhaus_timeout_seconds,
            max_retries=settings.urlhaus_max_retries,
        ), LiveRDAPProvider(
            bootstrap_url=settings.rdap_bootstrap_url,
            timeout_seconds=settings.rdap_timeout_seconds,
            cache_ttl_seconds=settings.rdap_cache_ttl_seconds,
        ), NativeWebsiteProvider(
            timeout_seconds=settings.website_timeout_seconds,
            max_pages=settings.website_max_pages,
        ), OpenRouterProvider(
            settings.openrouter_api_key,
            model=settings.openrouter_model,
            base_url=settings.openrouter_base_url,
            enabled=settings.openrouter_explainer_enabled,
            timeout_seconds=settings.openrouter_timeout_seconds,
            max_retries=settings.openrouter_max_retries,
        ),
        reviews=review_provider,
    )
    return registry
