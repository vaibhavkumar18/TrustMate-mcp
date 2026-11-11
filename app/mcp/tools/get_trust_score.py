import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.mcp.schemas.inputs import SellerInput
from app.mcp.schemas.trust import Trust
from app.core.config import get_settings
from app.domain.policies.scoring_policy import SCORING_VERSION
from app.providers.factory import ProviderRegistry, create_provider_registry
from app.services.seller_assessment_cache import (
    assessment_cache_key,
    validated_cache_identity,
)
from app.services.seller_verification_service import collect_seller_evidence
from app.services.trust_scoring_service import assess_evidence
from app.services.assessment_persistence import persist_assessment

logger = logging.getLogger(__name__)


async def get_trust_score(
    input_data: SellerInput,
    registry: ProviderRegistry | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    assessment_cache=None,
) -> Trust:
    """Assess one seller in the supplied marketplace context.

    target_country is a retrieval hint for business discovery, not proof that the
    seller is headquartered in that country. Foursquare results still have to
    pass the existing seller-name and registered-domain corroboration checks.
    """
    settings = get_settings()
    registry = registry or create_provider_registry(settings)
    target_country = input_data.target_country or "GLOBAL"

    identity = await validated_cache_identity(
        input_data.seller, input_data.url, registry.website
    )
    cache_key = None
    if assessment_cache is not None and identity is not None:
        cache_key = assessment_cache_key(
            assessment_cache,
            seller_key=identity.seller_key,
            target_country=target_country,
            settings=settings,
            registry=registry,
        )
        cached_trust = await assessment_cache.get_assessment(cache_key)
        if cached_trust is not None:
            logger.info(
                "seller_assessment_cache_hit",
                extra={"cache_scope": "single_seller", "seller_key": identity.seller_key},
            )
            return cached_trust
        logger.info(
            "seller_assessment_cache_miss",
            extra={"cache_scope": "single_seller", "seller_key": identity.seller_key},
        )

    evidence = await collect_seller_evidence(
        input_data.seller,
        input_data.url,
        registry.business,
        registry.business_fallback,
        registry.domain,
        registry.website,
        registry.reputation,
        target_country=input_data.target_country,
        seller_location_hints=settings.seller_location_hints,
        reviews_provider=registry.reviews,
        prevalidated_identity=identity,
    )
    trust = assess_evidence(evidence)

    if assessment_cache is not None and cache_key is not None and trust.score is not None:
        await assessment_cache.put_assessment(
            cache_key=cache_key,
            seller_key=evidence.identity.seller_key,
            seller_name=evidence.identity.display_name,
            normalized_domain=evidence.identity.normalized_domain,
            target_country=target_country,
            scoring_version=SCORING_VERSION,
            trust=trust,
        )

    if session_factory is not None:
        async with session_factory() as session:
            await persist_assessment(session, evidence)
            await session.commit()
    return trust
