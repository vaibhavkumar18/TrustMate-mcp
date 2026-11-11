"""Helpers for stable seller-assessment cache identities and namespaces."""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import Settings
from app.providers.factory import ProviderRegistry
from app.services.seller_identity import normalize_seller
from app.storage.sqlite_assessment_cache import SQLiteAssessmentCache


def provider_cache_profile(settings: Settings, registry: ProviderRegistry) -> dict[str, Any]:
    """Invalidate cached assessments when evidence-provider configuration changes."""
    return {
        "business": type(registry.business).__name__,
        "business_fallback": type(registry.business_fallback).__name__,
        "domain": type(registry.domain).__name__,
        "website": type(registry.website).__name__,
        "website_max_pages": settings.website_max_pages,
        "reputation": type(registry.reputation).__name__,
        "urlhaus_authorized": settings.urlhaus_authorized,
        "urlhaus_live_test": settings.urlhaus_live_test,
        "urlhaus_base_url": settings.urlhaus_base_url,
        "reviews": type(registry.reviews).__name__ if registry.reviews else "disabled",
        "trustpilot_max_review_pages": settings.trustpilot_max_review_pages,
        "seller_location_hints": settings.seller_location_hints,
    }


def assessment_cache_key(
    cache: SQLiteAssessmentCache,
    *,
    seller_key: str,
    target_country: str | None,
    settings: Settings,
    registry: ProviderRegistry,
) -> str:
    from app.domain.policies.scoring_policy import SCORING_VERSION

    return cache.assessment_key(
        seller_key=seller_key,
        target_country=target_country or "GLOBAL",
        scoring_version=SCORING_VERSION,
        provider_profile=provider_cache_profile(settings, registry),
    )


async def validated_cache_identity(
    seller_name: str,
    seller_url: str | None,
    website_provider: object,
):
    """Normalize and public-host validate identity before looking up a cache key.

    Invalid or unresolved merchant URLs are intentionally not cacheable. This
    prevents an invalid URL or affiliate redirect from selecting another
    seller's cached assessment.
    """
    try:
        return await asyncio.to_thread(
            normalize_seller,
            seller_name,
            seller_url,
            allow_synthetic=type(website_provider).__name__ == "MockWebsiteProvider",
        )
    except (TypeError, ValueError):
        return None
