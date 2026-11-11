import pytest

from app.mcp.schemas.inputs import SellerInput
from app.mcp.tools.explain_score import explain_score
from app.mcp.tools.get_trust_score import get_trust_score
from app.providers.factory import ProviderRegistry
from app.providers.implementations import (
    MockBusinessProvider,
    MockDomainProvider,
    MockLLMProvider,
    MockProductProvider,
    MockReputationProvider,
    MockWebsiteProvider,
)
from app.storage.sqlite_assessment_cache import SQLiteAssessmentCache


class CountingBusinessProvider(MockBusinessProvider):
    def __init__(self):
        self.calls = 0

    async def search_business(self, seller_name, domain=None, location_hint=None):
        self.calls += 1
        return await super().search_business(seller_name, domain, location_hint)


class CountingLLMProvider(MockLLMProvider):
    def __init__(self):
        self.calls = 0

    async def generate_explanation(self, evidence_summary, review_snippets):
        self.calls += 1
        return await super().generate_explanation(evidence_summary, review_snippets)


def _registry():
    business = CountingBusinessProvider()
    llm = CountingLLMProvider()
    registry = ProviderRegistry(
        product=MockProductProvider(),
        business=business,
        business_fallback=MockBusinessProvider(),
        reputation=MockReputationProvider(),
        domain=MockDomainProvider(),
        website=MockWebsiteProvider(),
        llm=llm,
    )
    return registry, business, llm


@pytest.mark.asyncio
async def test_get_trust_score_reuses_cached_seller_assessment(tmp_path):
    registry, business, _ = _registry()
    cache = SQLiteAssessmentCache(str(tmp_path / "trustmate.sqlite"), ttl_days=3)
    await cache.initialize()
    seller = SellerInput(seller="Synthetic Store", url="https://store.example")

    first = await get_trust_score(seller, registry, assessment_cache=cache)
    first_call_count = business.calls
    second = await get_trust_score(seller, registry, assessment_cache=cache)

    assert first.score is not None
    assert second == first
    assert first_call_count > 0
    assert business.calls == first_call_count


@pytest.mark.asyncio
async def test_explain_score_reuses_cached_assessment_and_explanation(tmp_path):
    registry, business, llm = _registry()
    cache = SQLiteAssessmentCache(str(tmp_path / "trustmate.sqlite"), ttl_days=3)
    await cache.initialize()
    seller = SellerInput(seller="Synthetic Store", url="https://store.example")

    first = await explain_score(seller, registry, assessment_cache=cache)
    business_calls_after_first = business.calls
    llm_calls_after_first = llm.calls
    second = await explain_score(seller, registry, assessment_cache=cache)

    assert first["summary"] == second["summary"]
    assert business_calls_after_first > 0
    assert llm_calls_after_first == 1
    assert business.calls == business_calls_after_first
    assert llm.calls == llm_calls_after_first
