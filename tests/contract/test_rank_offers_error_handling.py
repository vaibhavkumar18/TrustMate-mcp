"""
Regression tests for rank_offers error handling and result reporting (Cases 1–5).

All tests are offline — no live API calls are made.  Channel3Provider is backed by
httpx.MockTransport.  For tests that reach trust-scoring, collect_seller_evidence is
patched at the rank_offers module level (where the name is imported) to return
pre-built SellerEvidence without DNS lookups.
"""
import asyncio
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
from app.mcp.schemas.trust import Trust
from app.mcp.tools.rank_offers import rank_offers
from app.providers.factory import create_provider_registry
from app.providers.implementations import Channel3Provider


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _retention(cacheable: bool = False) -> RetentionPolicy:
    return RetentionPolicy(cacheable=cacheable)


def _pr(capability: str) -> ProviderResult:
    return ProviderResult(
        provider_code="stub", capability=capability, status="checked",
        cache_policy=_retention(),
    )


def _inr_offer(offer_id: str, seller: str, price: float) -> ProductOffer:
    domain = f"{offer_id}.shop"
    return ProductOffer(
        id=offer_id, product_id="p-1", title=f"Product {offer_id}", price=price,
        currency="USD",
        url=f"https://{domain}/item",
        seller=SellerIdentity(
            display_name=seller, seller_key=f"domain:{domain}",
            normalized_domain=domain, canonical_url=f"https://{domain}",
            identity_status="domain_matched",
        ),
    )


def _channel3_transport(payload: dict, *, status_code: int = 200) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload, request=req)
    return httpx.MockTransport(handler)


def _error_transport(exc_class) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        raise exc_class("simulated", request=req)
    return httpx.MockTransport(handler)


def _live_settings() -> Settings:
    return Settings(
        mock=False,
        channel3_api_key="test-key",
        foursquare_api_key="test-key",
        openrouter_api_key="test-key",
    )


def _live_registry_with(channel3: Channel3Provider):
    """Live-mode registry with only the product provider replaced."""
    from dataclasses import replace as _replace
    proxied = Channel3Provider(
        "test-key",
        base_url=channel3._base_url,
        timeout_seconds=channel3._timeout_seconds,
        max_retries=channel3._max_retries,
        transport=channel3._transport,
    )
    return _replace(create_provider_registry(_live_settings()), product=proxied)


def _make_trusted_evidence(domain: str) -> SellerEvidence:
    """Pre-built SellerEvidence that scores Trusted, without any DNS lookup."""
    return SellerEvidence(
        identity=SellerIdentity(
            display_name=f"Shop {domain}", seller_key=f"domain:{domain}",
            normalized_domain=domain, canonical_url=f"https://{domain}",
            identity_status="domain_matched",
        ),
        business=BusinessEvidence(
            provider=_pr("business"), business_name=f"Shop {domain}",
            match_confidence="high", provider_place_id="stub-place-id",
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


# A minimal Channel3-format payload with one real INR offer
_SINGLE_OFFER_PAYLOAD = {
    "products": [{
        "id": "p-real",
        "title": "Real Product",
        "offers": [{
            "id": "o-real",
            "url": "https://realshop.example/real",
            "domain": "realshop.example",
            "seller": "Real Shop",
            "price": {"price": 4500, "currency": "USD"},
        }],
    }],
    "next_page_token": None,
}


# ===========================================================================
# CASE 1 – Provider failure: no eligibility note, clear unavailability note
# ===========================================================================

@pytest.mark.asyncio
async def test_case1_timeout_produces_no_eligibility_note():
    """A timeout must produce an unavailability note, NOT an eligibility message."""
    provider = Channel3Provider("test-key", max_retries=0,
                                transport=_error_transport(httpx.ReadTimeout))
    output = await rank_offers(RankOffersInput(query="phone", budget=50000, currency="USD", target_country="US"),
                               _live_registry_with(provider))
    assert output.offers == []
    assert not any("met the minimum trust" in n or "were discovered" in n
                   for n in output.unverified_notes), \
        f"False eligibility note on timeout: {output.unverified_notes}"
    assert any("unavailable" in n.lower() for n in output.unverified_notes), \
        f"No unavailability note on timeout: {output.unverified_notes}"
    assert any("timed out" in n.lower() or "timeout" in n.lower()
               for n in output.unverified_notes), \
        f"No timeout reason in note: {output.unverified_notes}"


@pytest.mark.asyncio
async def test_case1_auth_failure_produces_no_eligibility_note():
    """A 401 must not produce an eligibility message; must mention credentials."""
    provider = Channel3Provider("bad-key", max_retries=0,
                                transport=_channel3_transport({}, status_code=401))
    output = await rank_offers(RankOffersInput(query="phone", budget=50000, currency="USD", target_country="US"),
                               _live_registry_with(provider))
    assert output.offers == []
    assert not any("met the minimum trust" in n for n in output.unverified_notes), \
        f"False eligibility note on 401: {output.unverified_notes}"
    assert any("credential" in n.lower() or "api" in n.lower()
               for n in output.unverified_notes), \
        f"No credential hint on 401: {output.unverified_notes}"


@pytest.mark.asyncio
async def test_case1_server_error_produces_no_eligibility_note():
    """A persistent 500 must not produce an eligibility message."""
    provider = Channel3Provider("test-key", max_retries=0,
                                transport=_channel3_transport({}, status_code=500))
    output = await rank_offers(RankOffersInput(query="phone", budget=50000, currency="USD", target_country="US"),
                               _live_registry_with(provider))
    assert output.offers == []
    assert not any("met the minimum trust" in n for n in output.unverified_notes), \
        f"False eligibility note on 500: {output.unverified_notes}"
    assert any("unavailable" in n.lower() for n in output.unverified_notes)


@pytest.mark.asyncio
async def test_case1_malformed_json_produces_no_eligibility_note():
    """Malformed JSON must not produce an eligibility message."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json", request=req)
    provider = Channel3Provider("test-key", transport=httpx.MockTransport(handler))
    output = await rank_offers(RankOffersInput(query="phone", budget=50000, currency="USD", target_country="US"),
                               _live_registry_with(provider))
    assert output.offers == []
    assert not any("met the minimum trust" in n for n in output.unverified_notes), \
        f"False eligibility note on bad JSON: {output.unverified_notes}"
    assert any("unavailable" in n.lower() for n in output.unverified_notes)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 429, 500, 503])
async def test_case1_no_mock_offers_on_http_error(status_code: int):
    """No HTTP error should silently produce mock/synthetic offers."""
    provider = Channel3Provider("test-key", max_retries=0,
                                transport=_channel3_transport({}, status_code=status_code))
    output = await rank_offers(RankOffersInput(query="laptop", budget=60000, currency="USD", target_country="US"),
                               _live_registry_with(provider))
    assert output.offers == [], \
        f"Expected no offers on HTTP {status_code}, got {len(output.offers)}"


@pytest.mark.asyncio
async def test_case1_no_mock_offers_on_transport_error():
    """A transport error must not produce synthetic mock offers."""
    provider = Channel3Provider("test-key", max_retries=0,
                                transport=_error_transport(httpx.ConnectError))
    output = await rank_offers(RankOffersInput(query="laptop", budget=60000, currency="USD", target_country="US"),
                               _live_registry_with(provider))
    assert output.offers == []


# ===========================================================================
# CASE 2 – Provider succeeds, zero products: "no matching products" note
# ===========================================================================

@pytest.mark.asyncio
async def test_case2_empty_discovery_says_no_matching_products():
    """Empty successful discovery must say 'no matching products', not eligibility."""
    provider = Channel3Provider(
        "test-key",
        transport=_channel3_transport({"products": [], "next_page_token": None}),
    )
    output = await rank_offers(RankOffersInput(query="unobtainium", budget=50000, currency="USD", target_country="US"),
                               _live_registry_with(provider))
    assert output.offers == []
    assert not any("met the minimum trust" in n for n in output.unverified_notes), \
        f"False eligibility note on empty: {output.unverified_notes}"
    assert any("no matching products" in n.lower() for n in output.unverified_notes), \
        f"Missing 'no matching products' note: {output.unverified_notes}"


@pytest.mark.asyncio
async def test_case2_no_mock_fallback_on_empty_results():
    """Empty discovery must stay empty — never substitute mock offers."""
    provider = Channel3Provider(
        "test-key",
        transport=_channel3_transport({"products": [], "next_page_token": None}),
    )
    output = await rank_offers(RankOffersInput(query="unobtainium", budget=50000, currency="USD", target_country="US"),
                               _live_registry_with(provider))
    assert output.offers == []


# ===========================================================================
# CASE 3 – Products discovered but all ineligible
# ===========================================================================

@pytest.mark.asyncio
async def test_case3_ineligible_products_produce_eligibility_note():
    """When products exist but all fail eligibility, the note must say 'were discovered'."""
    payload = {
        "products": [{
            "id": "p-over", "title": "Expensive Phone",
            "offers": [{
                "id": "o-over", "url": "https://pricey.shop/phone",
                "domain": "pricey.shop", "seller": "Pricey Store",
                "price": {"price": 90000, "currency": "USD"},  # exceeds budget 50000
            }],
        }],
        "next_page_token": None,
    }
    provider = Channel3Provider("test-key", transport=_channel3_transport(payload))
    ev = _make_trusted_evidence("pricey.shop")
    with patch.object(ro_module, "collect_seller_evidence", new=AsyncMock(return_value=ev)):
        output = await rank_offers(RankOffersInput(query="phone", budget=50000, currency="USD", target_country="US"),
                                   _live_registry_with(provider))
    assert len(output.offers) == 1
    assert output.offers[0].recommended is False
    assert any("were discovered" in n and "met the minimum" in n
               for n in output.unverified_notes), \
        f"Expected eligibility note, got: {output.unverified_notes}"


@pytest.mark.asyncio
async def test_case3_eligibility_note_absent_without_products():
    """The eligibility note must NOT appear when zero products were returned."""
    provider = Channel3Provider(
        "test-key",
        transport=_channel3_transport({"products": [], "next_page_token": None}),
    )
    output = await rank_offers(RankOffersInput(query="phone", budget=50000, currency="USD", target_country="US"),
                               _live_registry_with(provider))
    assert not any("were discovered" in n for n in output.unverified_notes), \
        f"Eligibility note appeared without products: {output.unverified_notes}"


# ===========================================================================
# CASE 4 – Partial / incomplete results (non-error, non-checked status)
# ===========================================================================

@pytest.mark.asyncio
async def test_case4_partial_results_noted_when_status_is_not_checked():
    """A non-error, non-checked provider status must produce an incompleteness note."""
    class _PartialProvider:
        async def search(self, query: str, budget: float, target_country: str,
                         currency: str, page_token=None) -> ProductSearchResult:
            return ProductSearchResult(
                provider=ProviderResult(
                    provider_code="partial_stub", capability="product",
                    status="unverified", cache_policy=_retention(),
                    data={"product_count": 1, "offer_count": 1, "skipped": {}},
                ),
                products=[Product(id="p-partial", title="Partial Product")],
                offers=[_inr_offer("partial-offer", "Partial Store", 3000)],
            )

    ev = _make_trusted_evidence("partial-offer.shop")
    with patch.object(ro_module, "collect_seller_evidence", new=AsyncMock(return_value=ev)):
        output = await rank_offers(
            RankOffersInput(query="phone", budget=50000, currency="USD", target_country="US"),
            replace(create_provider_registry(_live_settings()), product=_PartialProvider()),
        )
    assert any("incomplete" in n.lower() for n in output.unverified_notes), \
        f"No incompleteness note for partial results: {output.unverified_notes}"


@pytest.mark.asyncio
async def test_case4_partial_empty_no_eligibility_note():
    """Incomplete provider with zero offers must say 'incomplete', not eligibility."""
    class _PartialEmptyProvider:
        async def search(self, query: str, budget: float, target_country: str,
                         currency: str, page_token=None) -> ProductSearchResult:
            return ProductSearchResult(
                provider=ProviderResult(
                    provider_code="partial_stub", capability="product",
                    status="unverified", cache_policy=_retention(),
                    data={"skipped": {}},
                ),
            )

    output = await rank_offers(
        RankOffersInput(query="phone", budget=50000, currency="USD", target_country="US"),
        replace(create_provider_registry(_live_settings()), product=_PartialEmptyProvider()),
    )
    assert output.offers == []
    assert any("incomplete" in n.lower() for n in output.unverified_notes), \
        f"No incompleteness note for partial empty: {output.unverified_notes}"
    assert not any("met the minimum trust" in n for n in output.unverified_notes), \
        f"False eligibility note on partial empty: {output.unverified_notes}"


# ===========================================================================
# CASE 5 – Provider succeeds, eligible offers: existing ranking preserved
# ===========================================================================

@pytest.mark.asyncio
async def test_case5_eligible_offer_is_recommended():
    """A within-budget trusted offer must be ranked and recommended."""
    provider = Channel3Provider("test-key", transport=_channel3_transport(_SINGLE_OFFER_PAYLOAD))
    ev = _make_trusted_evidence("realshop.example")
    with patch.object(ro_module, "collect_seller_evidence", new=AsyncMock(return_value=ev)):
        output = await rank_offers(RankOffersInput(query="phone", budget=10000, currency="USD", target_country="US"),
                                   _live_registry_with(provider))
    assert len(output.offers) >= 1
    assert output.offers[0].recommended is True
    assert output.offers[0].rank == 1
    assert not any("unavailable" in n.lower() for n in output.unverified_notes)
    assert not any("were discovered, but none" in n for n in output.unverified_notes)


@pytest.mark.asyncio
async def test_case5_multiple_offers_ranked():
    """Multiple eligible offers must all be ranked; exactly one recommended."""
    payload = {
        "products": [
            {"id": "p-1", "title": "Phone A", "offers": [{"id": "o-a", "url": "https://alpha.shop/phone", "domain": "alpha.shop", "seller": "Alpha Shop", "price": {"price": 3000, "currency": "USD"}}]},
            {"id": "p-2", "title": "Phone B", "offers": [{"id": "o-b", "url": "https://beta.shop/phone",  "domain": "beta.shop",  "seller": "Beta Shop",  "price": {"price": 2000, "currency": "USD"}}]},
        ],
        "next_page_token": None,
    }
    provider = Channel3Provider("test-key", transport=_channel3_transport(payload))
    ev = _make_trusted_evidence("alpha.shop")
    with patch.object(ro_module, "collect_seller_evidence", new=AsyncMock(return_value=ev)):
        output = await rank_offers(RankOffersInput(query="phone", budget=10000, currency="USD", target_country="US"),
                                   _live_registry_with(provider))
    assert len(output.offers) == 2
    assert sorted(o.rank for o in output.offers) == [1, 2]
    assert sum(1 for o in output.offers if o.recommended) == 1


@pytest.mark.asyncio
async def test_case5_no_eligibility_note_when_recommended():
    """When an offer is recommended, no eligibility failure note must appear."""
    provider = Channel3Provider("test-key", transport=_channel3_transport(_SINGLE_OFFER_PAYLOAD))
    ev = _make_trusted_evidence("realshop.example")
    with patch.object(ro_module, "collect_seller_evidence", new=AsyncMock(return_value=ev)):
        output = await rank_offers(RankOffersInput(query="phone", budget=10000, currency="USD", target_country="US"),
                                   _live_registry_with(provider))
    assert any(o.recommended for o in output.offers)
    assert not any("none met the minimum" in n for n in output.unverified_notes), \
        f"Unexpected eligibility note: {output.unverified_notes}"
