import httpx
import pytest

from app.mcp.schemas.inputs import SellerInput
from app.mcp.tools import explain_score as explain_module
from app.providers.factory import ProviderRegistry
from app.providers.implementations import (
    MockBusinessProvider,
    MockDomainProvider,
    MockProductProvider,
    MockReputationProvider,
    MockWebsiteProvider,
    OpenRouterProvider,
)
from app.services.seller_verification_service import collect_seller_evidence
from sqlalchemy.exc import SQLAlchemyError


class _Session:
    async def commit(self):
        raise SQLAlchemyError("synthetic commit failure")

    async def rollback(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _Factory:
    def __call__(self):
        return _Session()


@pytest.mark.asyncio
async def test_openrouter_is_called_at_most_once_when_cache_commit_fails(monkeypatch):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "The evidence indicates a published contact page and a return policy."}}]},
            request=request,
        )

    llm = OpenRouterProvider(
        "secret",
        enabled=True,
        transport=httpx.MockTransport(handler),
    )
    registry = ProviderRegistry(
        product=MockProductProvider(),
        business=MockBusinessProvider(),
        business_fallback=MockBusinessProvider(),
        reputation=MockReputationProvider(),
        domain=MockDomainProvider(),
        website=MockWebsiteProvider(),
        llm=llm,
    )

    evidence = await collect_seller_evidence(
        "Synthetic Store",
        "https://example.com",
        registry.business,
        registry.business_fallback,
        registry.domain,
        registry.website,
        registry.reputation,
    )

    class _Assessment:
        seller_id = "seller"
        id = "assessment"

    class _Cache:
        async def get_explanation(self, *args):
            return None

        async def add_explanation(self, **kwargs):
            raise SQLAlchemyError("synthetic cache write failure")

    async def persist(*args):
        return _Assessment()

    monkeypatch.setattr(explain_module, "persist_assessment", persist)
    monkeypatch.setattr(explain_module, "CacheRepository", lambda session: _Cache())

    result = await explain_module._cached_explanation(
        _Factory(),
        evidence,
        registry,
        {"score": 4.5, "label": "Trusted", "confidence": "high", "factors": []},
        "a" * 64,
    )

    assert result.summary == (
        "TRUST ASSESSMENT: Trusted (4.5/5; high confidence). "
        "The evidence indicates a published contact page and a return policy."
    )
    assert calls == 1
