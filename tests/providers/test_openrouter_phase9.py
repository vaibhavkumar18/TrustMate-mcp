import httpx
import pytest

from app.providers.implementations import OpenRouterProvider
from app.providers.factory import ProviderRegistry
from app.providers.implementations import (
    MockBusinessProvider, MockDomainProvider, MockProductProvider,
    MockReputationProvider, MockWebsiteProvider,
)
from app.mcp.tools.explain_score import explain_score
from app.mcp.schemas.inputs import SellerInput


def summary() -> dict[str, object]:
    return {
        "score": 4.5,
        "label": "Trusted",
        "confidence": "high",
        "factors": [
            {"name": "Business presence", "status": "ok", "detail": "Confirmed."},
        ],
    }


@pytest.mark.asyncio
async def test_openrouter_request_is_configured_and_sanitized():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["authorization"] = request.headers["authorization"]
        seen["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Trusted: score 4.5/5 with high confidence."}}]},
            request=request,
        )

    result = await OpenRouterProvider(
        "secret-key",
        enabled=True,
        model="openai/gpt-oss-20b:free",
        transport=httpx.MockTransport(handler),
    ).generate_explanation(summary(), [])

    assert result.summary == "Trusted: score 4.5/5 with high confidence."
    assert str(seen["path"]).endswith("/chat/completions")
    assert seen["authorization"] == "Bearer secret-key"
    assert "gpt-oss-20b:free" in str(seen["body"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401),
        httpx.Response(402),
        httpx.Response(429),
        httpx.Response(503),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}),
    ],
)
async def test_openrouter_failures_use_deterministic_fallback(response: httpx.Response):
    provider = OpenRouterProvider(
        "secret-key",
        enabled=True,
        max_retries=0,
        transport=httpx.MockTransport(lambda request: response),
    )
    first = await provider.generate_explanation(summary(), [])
    second = await provider.generate_explanation(summary(), [])

    assert first.summary == second.summary
    assert "Trusted" in first.summary
    assert "4.5/5" in first.summary
    assert first.provider.data["fallback"] is True


@pytest.mark.asyncio
async def test_disabled_openrouter_never_sends_request():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    result = await OpenRouterProvider(
        "secret-key",
        enabled=False,
        transport=httpx.MockTransport(handler),
    ).generate_explanation(summary(), [])

    assert called is False
    assert result.provider.error_code == "disabled"


@pytest.mark.asyncio
async def test_explain_workflow_excludes_urlhaus_factor():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Trusted: score 4.5/5."}}]},
            request=request,
        )

    registry = ProviderRegistry(
        product=MockProductProvider(),
        business=MockBusinessProvider(),
        business_fallback=MockBusinessProvider(),
        reputation=MockReputationProvider(),
        domain=MockDomainProvider(),
        website=MockWebsiteProvider(),
        llm=OpenRouterProvider(
            "secret-key", enabled=True, transport=httpx.MockTransport(handler)
        ),
    )
    await explain_score(SellerInput(seller="Synthetic Store", url="https://store.example"), registry)

    assert "URL reputation" not in seen["body"]
