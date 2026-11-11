import pytest

from app.mcp.schemas.inputs import RankOffersInput
from app.providers.factory import ProviderRegistry
from app.providers.implementations import (
    MockBusinessProvider,
    MockDomainProvider,
    MockLLMProvider,
    MockProductProvider,
    MockReputationProvider,
    MockWebsiteProvider,
)
from app.mcp.tools.rank_offers import rank_offers
from app.storage.sqlite_assessment_cache import SQLiteAssessmentCache


class CountingBusinessProvider(MockBusinessProvider):
    def __init__(self):
        self.calls = 0

    async def search_business(self, seller_name, domain=None, location_hint=None):
        self.calls += 1
        return await super().search_business(seller_name, domain, location_hint)


@pytest.mark.asyncio
async def test_rank_offers_reuses_seller_assessments_for_72h_cache(tmp_path):
    business = CountingBusinessProvider()
    registry = ProviderRegistry(
        product=MockProductProvider(),
        business=business,
        business_fallback=MockBusinessProvider(),
        reputation=MockReputationProvider(),
        domain=MockDomainProvider(),
        website=MockWebsiteProvider(),
        llm=MockLLMProvider(),
    )
    cache = SQLiteAssessmentCache(str(tmp_path / "trustmate.sqlite"), ttl_days=3)
    await cache.initialize()
    request = RankOffersInput(
        query="wireless headphones",
        budget=100,
        currency="USD",
        target_country="US",
    )

    first = await rank_offers(request, registry, cache)
    provider_calls_after_first_search = business.calls
    second = await rank_offers(request, registry, cache)

    assert len(first.offers) == 4
    assert len(second.offers) == 4
    assert provider_calls_after_first_search == 4
    assert business.calls == provider_calls_after_first_search
    assert [offer.trust.score for offer in first.offers] == [
        offer.trust.score for offer in second.offers
    ]
