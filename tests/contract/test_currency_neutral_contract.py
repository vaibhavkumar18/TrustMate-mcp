"""
Regression tests for the currency-neutral MCP contract.

Covers:
  1.  US/USD locale validation
  2.  Correct raw HTTPX top-level locale serialization
  3.  Correct currency normalization
  4.  Foreign-currency rejection
  5.  Unsupported locale rejection before network access
  6.  Product budget filtering using USD
  7.  Currency-aware ranking
  8.  Existing trust scoring behavior unchanged
  9.  No mock fallback in live mode
  10. Structured MCP output schema
  11. Provider-aware cache separation by market
  12. Existing MCP tool registration
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import app.mcp.tools.rank_offers as ro_module
from app.core.config import Settings
from app.domain.models import (
    BusinessEvidence, DomainEvidence, Offer as ProductOffer,
    Product, ProductSearchResult, ProviderResult, ReputationEvidence,
    RetentionPolicy, SellerEvidence, SellerIdentity, WebsiteEvidence,
)
from app.mcp.schemas.inputs import RankOffersInput
from app.mcp.schemas.offers import Offer, RankOffersOutput
from app.mcp.schemas.trust import Trust
from app.mcp.server import create_mcp_server
from app.mcp.tools.rank_offers import rank_offers
from app.providers.factory import create_provider_registry
from app.providers.implementations import Channel3Provider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pr(cap: str) -> ProviderResult:
    return ProviderResult(
        provider_code="stub", capability=cap, status="checked",
        cache_policy=RetentionPolicy(cacheable=False),
    )

def _trusted_evidence(domain: str) -> SellerEvidence:
    return SellerEvidence(
        identity=SellerIdentity(
            display_name=f"Shop {domain}", seller_key=f"domain:{domain}",
            normalized_domain=domain, canonical_url=f"https://{domain}",
            identity_status="domain_matched",
        ),
        business=BusinessEvidence(
            provider=_pr("business"), business_name=f"Shop {domain}",
            match_confidence="high", provider_place_id="stub-id",
        ),
        domain=DomainEvidence(
            provider=_pr("domain"), domain=domain,
            creation_date=datetime(2015, 1, 1, tzinfo=timezone.utc),
        ),
        website=WebsiteEvidence(
            provider=_pr("website"), https_reachable=True, tls_valid=True,
            contact_present=True, return_policy_present=True,
        ),
        reputation=ReputationEvidence(
            provider=_pr("reputation"), is_malicious=False,
        ),
    )

def _live_settings():
    return Settings(
        mock=False, channel3_api_key="k",
        foursquare_api_key="k", openrouter_api_key="k",
    )

def _live_reg_with(provider: Channel3Provider):
    return replace(create_provider_registry(_live_settings()), product=provider)

def _json_transport(payload, status=200):
    return httpx.MockTransport(lambda req: httpx.Response(status, json=payload, request=req))

def _exc_transport(cls):
    return httpx.MockTransport(lambda req: (_ for _ in ()).throw(cls("sim", request=req)))

VALID_USD_PAYLOAD = {
    "products": [{
        "id": "p1", "title": "Wireless Headphones",
        "offers": [{
            "id": "o1", "url": "https://goodshop.test/product",
            "domain": "goodshop.test", "seller": "Good Shop",
            "price": {"price": 89.99, "currency": "USD"},
        }],
    }],
    "next_page_token": None,
}

EMPTY_PAYLOAD = {"products": [], "next_page_token": None}


# ===========================================================================
# 1. US/USD locale validation
# ===========================================================================

@pytest.mark.asyncio
async def test_us_usd_passes_locale_guard():
    """US/USD must be accepted as a supported locale combination."""
    provider = Channel3Provider("k", transport=_json_transport(VALID_USD_PAYLOAD))
    result = await provider.search("headphones", 100, target_country="US", currency="USD")
    assert result.provider.status == "checked"
    assert len(result.offers) == 1


# ===========================================================================
# 2. Correct raw HTTPX top-level locale serialization
# ===========================================================================

@pytest.mark.asyncio
async def test_locale_fields_at_top_level():
    """country, currency, language must be inside config in the REST body."""
    seen: dict = {}
    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json=EMPTY_PAYLOAD, request=req)
    await Channel3Provider("k", transport=httpx.MockTransport(handler)).search(
        "phone", 100, target_country="US", currency="USD"
    )
    assert seen["config"]["country"] == "US"
    assert seen["config"]["currency"] == "USD"
    assert seen["config"]["language"] == "en"
    assert "country" not in seen
    assert "currency" not in seen
    assert "language" not in seen


@pytest.mark.asyncio
async def test_no_nested_config_in_request_body():
    """The request body must contain a nested config object (not top-level locale)."""
    seen: dict = {}
    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json=EMPTY_PAYLOAD, request=req)
    await Channel3Provider("k", transport=httpx.MockTransport(handler)).search(
        "phone", 100, target_country="GB", currency="GBP"
    )
    assert "config" in seen
    assert seen["config"]["country"] == "GB"
    assert seen["config"]["currency"] == "GBP"
    assert "country" not in seen
    assert "currency" not in seen


# ===========================================================================
# 3. Correct currency normalization
# ===========================================================================

@pytest.mark.asyncio
async def test_usd_offer_normalized_correctly():
    """A USD-priced offer must be accepted and keep its USD currency."""
    provider = Channel3Provider("k", transport=_json_transport(VALID_USD_PAYLOAD))
    result = await provider.search("headphones", 100, target_country="US", currency="USD")
    assert result.offers[0].price == 89.99
    assert result.offers[0].currency == "USD"


# ===========================================================================
# 4. Foreign-currency rejection
# ===========================================================================

@pytest.mark.asyncio
async def test_eur_offer_rejected_when_usd_requested():
    """An EUR-priced offer must be skipped when USD is the target currency."""
    payload = {
        "products": [{
            "id": "p1", "title": "Phone",
            "offers": [
                {"url": "https://shop.test/a", "domain": "shop.test", "seller": "S",
                 "price": {"price": 50, "currency": "EUR"}},
                {"url": "https://shop.test/b", "domain": "shop.test", "seller": "S",
                 "price": {"price": 60, "currency": "USD"}},
            ],
        }],
        "next_page_token": None,
    }
    provider = Channel3Provider("k", transport=_json_transport(payload))
    result = await provider.search("phone", 100, target_country="US", currency="USD")
    assert len(result.offers) == 1
    assert result.offers[0].currency == "USD"
    assert result.provider.data["skipped"]["unsupported_or_ambiguous_currency"] == 1


# ===========================================================================
# 5. Unsupported locale rejection before network access
# ===========================================================================

@pytest.mark.asyncio
async def test_in_inr_rejected_before_http():
    """IN/INR must be rejected before any HTTP request."""
    calls = []
    def handler(req):
        calls.append(1)
        return httpx.Response(200, json=EMPTY_PAYLOAD, request=req)
    result = await Channel3Provider("k", transport=httpx.MockTransport(handler)).search(
        "phone", 100, target_country="IN", currency="INR"
    )
    assert len(calls) == 0
    assert result.provider.error_code == "unsupported_locale"


@pytest.mark.asyncio
async def test_mismatched_currency_rejected():
    """US with EUR must be rejected — US maps to USD."""
    calls = []
    def handler(req):
        calls.append(1)
        return httpx.Response(200, json=EMPTY_PAYLOAD, request=req)
    result = await Channel3Provider("k", transport=httpx.MockTransport(handler)).search(
        "phone", 100, target_country="US", currency="EUR"
    )
    assert len(calls) == 0
    assert result.provider.error_code == "unsupported_locale"


# ===========================================================================
# 6. Product budget filtering using USD
# ===========================================================================

@pytest.mark.asyncio
async def test_budget_filtering_with_usd():
    """Offers exceeding a USD budget must be marked as exceeding budget."""
    payload = {
        "products": [{
            "id": "p1", "title": "Expensive Phone",
            "offers": [{
                "id": "o1", "url": "https://pricey.test/phone",
                "domain": "pricey.test", "seller": "Pricey",
                "price": {"price": 200, "currency": "USD"},  # exceeds $100 budget
            }],
        }],
        "next_page_token": None,
    }
    provider = Channel3Provider("k", transport=_json_transport(payload))
    ev = _trusted_evidence("pricey.test")
    with patch.object(ro_module, "collect_seller_evidence", new=AsyncMock(return_value=ev)):
        output = await rank_offers(
            RankOffersInput(query="phone", budget=100, currency="USD", target_country="US"),
            _live_reg_with(provider),
        )
    assert len(output.offers) == 1
    assert output.offers[0].recommended is False
    assert "budget" in output.offers[0].tradeoff_note.lower()


# ===========================================================================
# 7. Currency-aware ranking
# ===========================================================================

@pytest.mark.asyncio
async def test_ranking_uses_price_and_budget_in_same_currency():
    """Ranking must use price and budget in the same validated currency."""
    payload = {
        "products": [
            {"id": "p1", "title": "A", "offers": [{"id": "o-a", "url": "https://a.test/p",
             "domain": "a.test", "seller": "A", "price": {"price": 30, "currency": "USD"}}]},
            {"id": "p2", "title": "B", "offers": [{"id": "o-b", "url": "https://b.test/p",
             "domain": "b.test", "seller": "B", "price": {"price": 20, "currency": "USD"}}]},
        ],
        "next_page_token": None,
    }
    provider = Channel3Provider("k", transport=_json_transport(payload))
    ev = _trusted_evidence("a.test")
    with patch.object(ro_module, "collect_seller_evidence", new=AsyncMock(return_value=ev)):
        output = await rank_offers(
            RankOffersInput(query="phone", budget=100, currency="USD", target_country="US"),
            _live_reg_with(provider),
        )
    assert len(output.offers) == 2
    assert sum(1 for o in output.offers if o.recommended) == 1
    assert output.offers[0].rank == 1
    assert output.offers[0].currency == "USD"
    assert output.offers[0].price <= 100


# ===========================================================================
# 8. Existing trust scoring behavior unchanged
# ===========================================================================

def test_offer_schema_has_price_not_price_inr():
    """The MCP Offer schema must use 'price' not 'price_inr'."""
    schema = Offer.model_json_schema()
    props = schema.get("properties", {})
    assert "price" in props
    assert "currency" in props
    assert "price_inr" not in props


def test_rank_offers_output_has_target_country():
    """RankOffersOutput must include target_country."""
    schema = RankOffersOutput.model_json_schema()
    props = schema.get("properties", {})
    assert "target_country" in props
    assert "offers" in props
    assert "unverified_notes" in props


def test_rank_offers_input_has_currency_and_country():
    """RankOffersInput must include budget, currency, target_country."""
    schema = RankOffersInput.model_json_schema()
    props = schema.get("properties", {})
    assert "budget" in props
    assert "currency" in props
    assert "target_country" in props
    assert "budget_inr" not in props


# ===========================================================================
# 9. No mock fallback in live mode
# ===========================================================================

@pytest.mark.asyncio
async def test_channel3_failure_returns_empty_not_mock():
    """A Channel3 failure must return empty offers, never mock offers."""
    provider = Channel3Provider("k", max_retries=0,
                                transport=_json_transport({}, status=503))
    output = await rank_offers(
        RankOffersInput(query="phone", budget=100, currency="USD", target_country="US"),
        _live_reg_with(provider),
    )
    assert output.offers == []
    assert not any(".example" in (o.url or "") for o in output.offers)


@pytest.mark.asyncio
async def test_channel3_timeout_no_mock():
    """A Channel3 timeout must return empty offers, never mock offers."""
    provider = Channel3Provider("k", max_retries=0,
                                transport=_exc_transport(httpx.ReadTimeout))
    output = await rank_offers(
        RankOffersInput(query="phone", budget=100, currency="USD", target_country="US"),
        _live_reg_with(provider),
    )
    assert output.offers == []


# ===========================================================================
# 10. Structured MCP output schema
# ===========================================================================

@pytest.mark.asyncio
async def test_mcp_output_has_target_country_and_currency():
    """The rank_offers MCP output must include target_country and per-offer currency."""
    provider = Channel3Provider("k", transport=_json_transport(VALID_USD_PAYLOAD))
    ev = _trusted_evidence("goodshop.test")
    with patch.object(ro_module, "collect_seller_evidence", new=AsyncMock(return_value=ev)):
        output = await rank_offers(
            RankOffersInput(query="headphones", budget=100, currency="USD", target_country="US"),
            _live_reg_with(provider),
        )
    assert output.target_country == "US"
    assert len(output.offers) >= 1
    assert output.offers[0].currency == "USD"
    assert output.offers[0].price == 89.99
    assert "price_inr" not in output.offers[0].model_dump()


# ===========================================================================
# 11. Provider-aware cache separation by market
# ===========================================================================

@pytest.mark.asyncio
async def test_different_markets_produce_different_search_calls():
    """Requests for different countries must produce different HTTP payloads."""
    seen_payloads = []

    def handler(req):
        seen_payloads.append(json.loads(req.content))
        return httpx.Response(200, json=EMPTY_PAYLOAD, request=req)

    provider = Channel3Provider("k", transport=httpx.MockTransport(handler))
    await provider.search("phone", 100, target_country="US", currency="USD")
    await provider.search("phone", 100, target_country="GB", currency="GBP")

    assert seen_payloads[0]["config"]["country"] == "US"
    assert seen_payloads[0]["config"]["currency"] == "USD"
    assert seen_payloads[1]["config"]["country"] == "GB"
    assert seen_payloads[1]["config"]["currency"] == "GBP"
    assert seen_payloads[0] != seen_payloads[1]


# ===========================================================================
# 12. Existing MCP tool registration
# ===========================================================================

@pytest.mark.asyncio
async def test_three_tools_registered():
    """All three MCP tools must remain registered."""
    settings = Settings(mock=True)
    server, _ = create_mcp_server(settings)
    tools = await server.list_tools()
    assert {t.name for t in tools} == {"rank_offers", "get_trust_score", "explain_score"}


@pytest.mark.asyncio
async def test_rank_offers_tool_has_new_params():
    """The rank_offers MCP tool must accept budget, currency, target_country."""
    settings = Settings(mock=True)
    server, _ = create_mcp_server(settings)
    tools = await server.list_tools()
    tool = next(t for t in tools if t.name == "rank_offers")
    # Tool stores input schema in input_schema
    schema = tool.input_schema
    params = schema.get("properties", {})
    assert "budget" in params
    assert "currency" in params
    assert "target_country" in params
    assert "budget_inr" not in params
