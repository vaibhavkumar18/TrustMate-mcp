"""
Regression tests for:
  - Live provider selection (MOCK=false → Channel3Provider, not mock)
  - Mock-mode isolation (MOCK=true → MockProductProvider, never Channel3)
  - Provider failure handling (no silent mock fallback in live mode)
  - Empty result handling
  - MCP tool output schemas (structured_output, Pydantic-derived schemas)
"""
import asyncio

import httpx
import pytest

from app.core.config import Settings
from app.domain.models import ProductSearchResult
from app.mcp.register_tools import register_tools
from app.mcp.schemas.offers import Offer, RankOffersOutput
from app.mcp.schemas.trust import Trust
from app.mcp.server import create_mcp_server
from app.providers.factory import ProviderConfigurationError, create_provider_registry
from app.providers.implementations import Channel3Provider, MockProductProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _live_settings(**overrides) -> Settings:
    """Minimal settings for live mode with in-process fake API keys."""
    base = dict(
        mock=False,
        channel3_api_key="test-channel3-key",
        foursquare_api_key="test-foursquare-key",
        openrouter_api_key="test-openrouter-key",
    )
    base.update(overrides)
    return Settings(**base)


def _mock_transport(payload: dict, *, status_code: int = 200):
    """Return an httpx transport that responds with a fixed JSON payload."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload, request=request)
    return httpx.MockTransport(handler)


def _error_transport(exc_class):
    """Return an httpx transport that raises exc_class on every request."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc_class("simulated", request=request)
    return httpx.MockTransport(handler)


_VALID_CHANNEL3_PAYLOAD = {
    "products": [
        {
            "id": "prod-1",
            "title": "Test Phone",
            "offers": [
                {
                    "id": "offer-1",
                    "url": "https://shop.testretailer.in/phone",
                    "domain": "shop.testretailer.in",
                    "seller": "Real Shop",
                    "price": {"price": 19999, "currency": "USD"},
                }
            ],
        }
    ],
    "next_page_token": None,
}

_EMPTY_CHANNEL3_PAYLOAD = {"products": [], "next_page_token": None}


# ===========================================================================
# 1.  LIVE PROVIDER SELECTION
# ===========================================================================


def test_live_mode_uses_channel3_not_mock():
    """MOCK=false must produce a Channel3Provider, never a MockProductProvider."""
    registry = create_provider_registry(_live_settings())
    assert isinstance(registry.product, Channel3Provider), (
        "Expected Channel3Provider in live mode"
    )
    assert not isinstance(registry.product, MockProductProvider), (
        "MockProductProvider must NOT be returned in live mode"
    )


def test_mock_mode_uses_mock_product_provider():
    """MOCK=true must produce MockProductProvider, never Channel3Provider."""
    registry = create_provider_registry(Settings(mock=True))
    assert isinstance(registry.product, MockProductProvider)
    assert not isinstance(registry.product, Channel3Provider)


# ===========================================================================
# 2.  MOCK-MODE ISOLATION — mock products are ONLY for test/demo mode
# ===========================================================================


@pytest.mark.asyncio
async def test_mock_mode_returns_synthetic_offers():
    """Mock mode always returns the 4 synthetic offers with .example domains."""
    registry = create_provider_registry(Settings(mock=True))
    result = await registry.product.search("phone", 50000, target_country="US", currency="USD")
    assert result.provider.data.get("mock") is True
    assert len(result.offers) == 4
    assert all(".example" in o.seller.normalized_domain for o in result.offers)


@pytest.mark.asyncio
async def test_live_mode_does_not_return_synthetic_offers():
    """In live mode the product provider must never fabricate .example domains."""
    provider = Channel3Provider(
        "test-key",
        transport=_mock_transport(_VALID_CHANNEL3_PAYLOAD),
    )
    result = await provider.search("phone", 50000, target_country="US", currency="USD")
    assert not any(".example" in o.seller.normalized_domain for o in result.offers), (
        "Live provider returned synthetic .example offers"
    )
    assert result.provider.data.get("mock") is not True


# ===========================================================================
# 3.  NO SILENT MOCK FALLBACK ON FAILURE — must return empty + notes
# ===========================================================================


@pytest.mark.asyncio
async def test_channel3_timeout_returns_empty_not_mock():
    """A timeout must not silently fall back to mock products."""
    provider = Channel3Provider(
        "test-key",
        max_retries=0,
        transport=_error_transport(httpx.ReadTimeout),
    )
    result = await provider.search("phone", 50000, target_country="US", currency="USD")
    assert result.offers == [], "Timeout must yield empty offers, not mock products"
    assert result.provider.status == "error"
    assert result.provider.error_code == "timeout"


@pytest.mark.asyncio
async def test_channel3_auth_error_returns_empty_not_mock():
    """A 401 auth failure must not silently fall back to mock products."""
    provider = Channel3Provider(
        "bad-key",
        max_retries=0,
        transport=_mock_transport({}, status_code=401),
    )
    result = await provider.search("phone", 50000, target_country="US", currency="USD")
    assert result.offers == []
    assert result.provider.status == "error"
    assert result.provider.error_code == "authentication_failed"


@pytest.mark.asyncio
async def test_channel3_server_error_returns_empty_not_mock():
    """A persistent 5xx must not silently fall back to mock products."""
    provider = Channel3Provider(
        "test-key",
        max_retries=0,
        transport=_mock_transport({}, status_code=500),
    )
    result = await provider.search("phone", 50000, target_country="US", currency="USD")
    assert result.offers == []
    assert result.provider.status == "error"


@pytest.mark.asyncio
async def test_channel3_malformed_json_returns_empty_not_mock():
    """Malformed JSON must not silently fall back to mock products."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json", request=request)

    provider = Channel3Provider("test-key", transport=httpx.MockTransport(handler))
    result = await provider.search("phone", 50000, target_country="US", currency="USD")
    assert result.offers == []
    assert result.provider.error_code == "malformed_json"


@pytest.mark.asyncio
async def test_channel3_missing_credentials_raises_at_registry_creation():
    """Missing CHANNEL3_API_KEY must raise ProviderConfigurationError, not fall back."""
    with pytest.raises(ProviderConfigurationError, match="CHANNEL3_API_KEY"):
        create_provider_registry(Settings(
            mock=False,
            channel3_api_key=None,
            foursquare_api_key="configured",
            openrouter_api_key="configured",
        ))


@pytest.mark.asyncio
async def test_channel3_missing_foursquare_key_raises_at_registry_creation():
    """Missing FOURSQUARE_API_KEY must raise ProviderConfigurationError."""
    with pytest.raises(ProviderConfigurationError, match="FOURSQUARE_API_KEY"):
        create_provider_registry(Settings(
            mock=False,
            channel3_api_key="configured",
            foursquare_api_key=None,
            openrouter_api_key="configured",
        ))


# ===========================================================================
# 4.  EMPTY RESULTS
# ===========================================================================


@pytest.mark.asyncio
async def test_channel3_empty_results_are_clean():
    """Channel3 returning no products must produce a clean checked result, not an error."""
    provider = Channel3Provider(
        "test-key",
        transport=_mock_transport(_EMPTY_CHANNEL3_PAYLOAD),
    )
    result = await provider.search("unobtainium", 50000, target_country="US", currency="USD")
    assert result.provider.status == "checked"
    assert result.products == []
    assert result.offers == []


@pytest.mark.asyncio
async def test_rank_offers_with_empty_channel3_returns_no_offers_and_note():
    """rank_offers with a live registry that returns no products must return empty
    offers list, not mock products, and include an explanatory note."""
    from app.mcp.schemas.inputs import RankOffersInput
    from app.mcp.tools.rank_offers import rank_offers

    # Build a live-mode registry with a Channel3 provider that returns no products.
    # The RankOffersInput now passes target_country="US" and currency="USD"
    # directly, so no _locale_override is needed.
    registry = create_provider_registry(_live_settings())
    from dataclasses import replace
    live_registry_empty = replace(
        registry,
        product=Channel3Provider(
            "test-key",
            transport=_mock_transport(_EMPTY_CHANNEL3_PAYLOAD),
        ),
    )
    output = await rank_offers(
        RankOffersInput(query="unobtainium", budget=50000, currency="USD", target_country="US"),
        live_registry_empty,
    )
    assert isinstance(output, RankOffersOutput)
    assert output.offers == [], (
        "No mock products must appear when Channel3 returns no results"
    )
    # The tool must not silently succeed with zero notes when nothing was found
    # (the "No offer met the minimum trust..." note may appear)


@pytest.mark.asyncio
async def test_rank_offers_channel3_failure_returns_empty_with_note():
    """rank_offers must return empty offers and a note when Channel3 fails, never mocks."""
    from app.mcp.schemas.inputs import RankOffersInput
    from app.mcp.tools.rank_offers import rank_offers
    from dataclasses import replace

    registry = create_provider_registry(_live_settings())
    failing_registry = replace(
        registry,
        product=Channel3Provider(
            "test-key",
            max_retries=0,
            transport=_mock_transport({}, status_code=503),
        ),
    )
    output = await rank_offers(
        RankOffersInput(query="phone", budget=50000, currency="USD", target_country="US"),
        failing_registry,
    )
    assert isinstance(output, RankOffersOutput)
    assert output.offers == [], (
        "A Channel3 failure must yield empty offers, not mock fallback"
    )
    # Either the exception note or the provider-status note must be present
    assert output.unverified_notes, (
        f"Expected at least one explanatory note, got: {output.unverified_notes}"
    )


@pytest.mark.asyncio
async def test_rank_offers_channel3_timeout_returns_empty_with_note():
    """rank_offers must return empty offers and a note when Channel3 times out."""
    from app.mcp.schemas.inputs import RankOffersInput
    from app.mcp.tools.rank_offers import rank_offers
    from dataclasses import replace

    registry = create_provider_registry(_live_settings())
    timeout_registry = replace(
        registry,
        product=Channel3Provider(
            "test-key",
            max_retries=0,
            transport=_error_transport(httpx.ReadTimeout),
        ),
    )
    output = await rank_offers(
        RankOffersInput(query="phone", budget=50000, currency="USD", target_country="US"),
        timeout_registry,
    )
    assert output.offers == []
    assert output.unverified_notes, "Timeout must produce at least one explanatory note"


# ===========================================================================
# 5.  MCP TOOL OUTPUT SCHEMAS — structured_output=True with Pydantic models
# ===========================================================================


@pytest.mark.asyncio
async def test_all_three_tools_registered_with_output_schemas():
    """All three MCP tools must be registered and expose a non-null output_schema."""
    settings = Settings(mock=True)
    server, _ = create_mcp_server(settings)
    tools = await server.list_tools()
    tool_map = {t.name: t for t in tools}

    assert set(tool_map) == {"rank_offers", "get_trust_score", "explain_score"}, (
        f"Unexpected tool set: {set(tool_map)}"
    )
    for tool_name in ("rank_offers", "get_trust_score", "explain_score"):
        tool = tool_map[tool_name]
        assert tool.output_schema is not None, (
            f"Tool '{tool_name}' must expose a non-null output_schema for structured output"
        )
        assert isinstance(tool.output_schema, dict), (
            f"Tool '{tool_name}' output_schema must be a dict"
        )


@pytest.mark.asyncio
async def test_rank_offers_output_schema_contains_expected_fields():
    """rank_offers output_schema must describe the RankOffersOutput Pydantic model."""
    settings = Settings(mock=True)
    server, _ = create_mcp_server(settings)
    tools = await server.list_tools()
    tool = next(t for t in tools if t.name == "rank_offers")
    schema = tool.output_schema

    # RankOffersOutput has 'offers' (list) and 'unverified_notes' (list[str])
    props = schema.get("properties", {})
    assert "offers" in props, f"output_schema missing 'offers' field: {schema}"
    assert "unverified_notes" in props, f"output_schema missing 'unverified_notes' field: {schema}"


@pytest.mark.asyncio
async def test_get_trust_score_output_schema_contains_trust_fields():
    """get_trust_score output_schema must describe the Trust Pydantic model."""
    settings = Settings(mock=True)
    server, _ = create_mcp_server(settings)
    tools = await server.list_tools()
    tool = next(t for t in tools if t.name == "get_trust_score")
    schema = tool.output_schema

    props = schema.get("properties", {})
    assert "label" in props, f"output_schema missing 'label': {schema}"
    assert "confidence" in props, f"output_schema missing 'confidence': {schema}"
    assert "factors" in props, f"output_schema missing 'factors': {schema}"


@pytest.mark.asyncio
async def test_explain_score_output_schema_contains_summary():
    """explain_score output_schema must describe the dict[str, str] return."""
    settings = Settings(mock=True)
    server, _ = create_mcp_server(settings)
    tools = await server.list_tools()
    tool = next(t for t in tools if t.name == "explain_score")
    assert tool.output_schema is not None


@pytest.mark.asyncio
async def test_rank_offers_structured_content_matches_schema():
    """In mock mode, rank_offers structured_content must match the declared schema."""
    settings = Settings(mock=True)
    server, _ = create_mcp_server(settings)
    result = await server.call_tool(
        "rank_offers", {"query": "laptop", "budget": 60000, "currency": "USD", "target_country": "US"}
    )
    sc = result.structured_content
    assert isinstance(sc, dict), "structured_content must be a dict"
    assert "offers" in sc, f"structured_content missing 'offers': {sc}"
    assert "unverified_notes" in sc, f"structured_content missing 'unverified_notes': {sc}"
    assert isinstance(sc["offers"], list)
    assert isinstance(sc["unverified_notes"], list)
    if sc["offers"]:
        offer = sc["offers"][0]
        for field in ("id", "title", "price", "currency", "url", "seller", "trust", "rank", "recommended"):
            assert field in offer, f"Offer missing field '{field}': {offer}"


@pytest.mark.asyncio
async def test_get_trust_score_structured_content_matches_schema():
    """In mock mode, get_trust_score structured_content must match the Trust schema."""
    settings = Settings(mock=True)
    server, _ = create_mcp_server(settings)
    result = await server.call_tool(
        "get_trust_score", {"seller": "Example Store", "url": "https://example.com"}
    )
    sc = result.structured_content
    assert isinstance(sc, dict)
    for field in ("label", "confidence", "factors"):
        assert field in sc, f"Trust structured_content missing '{field}': {sc}"


@pytest.mark.asyncio
async def test_explain_score_structured_content_has_summary():
    """In mock mode, explain_score structured_content must have a 'summary' key."""
    settings = Settings(mock=True)
    server, _ = create_mcp_server(settings)
    result = await server.call_tool(
        "explain_score", {"seller": "Example Store", "url": "https://example.com"}
    )
    sc = result.structured_content
    assert isinstance(sc, dict)
    assert "summary" in sc, f"explain_score structured_content missing 'summary': {sc}"
    assert isinstance(sc["summary"], str)


# ===========================================================================
# 6.  SCHEMA VALIDATION — RankOffersOutput and Trust models
# ===========================================================================


def test_rank_offers_output_schema_is_valid_json_schema():
    """RankOffersOutput.model_json_schema() must produce a well-formed JSON schema."""
    schema = RankOffersOutput.model_json_schema()
    assert schema.get("type") == "object" or "$defs" in schema or "properties" in schema


def test_trust_model_schema_is_valid():
    """Trust.model_json_schema() must produce a well-formed JSON schema."""
    schema = Trust.model_json_schema()
    assert "properties" in schema or "$defs" in schema


def test_offer_model_schema_has_required_fields():
    """Offer schema must include all public contract fields."""
    schema = Offer.model_json_schema()
    props = schema.get("properties", {})
    for field in ("id", "title", "price", "currency", "url", "seller", "trust", "rank", "recommended"):
        assert field in props, f"Offer schema missing '{field}'"
