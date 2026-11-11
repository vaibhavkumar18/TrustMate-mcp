import asyncio
import json
import time

import httpx
import pytest

from app.providers.implementations import NominatimProvider


def result(name="Example Seller", website="https://seller.example"):
    return [{
        "osm_type": "node",
        "osm_id": 123,
        "name": name,
        "display_name": "Example Seller, Main Street",
        "extratags": {"website": website} if website else {},
        "namedetails": {"name": name},
    }]


@pytest.mark.asyncio
async def test_nominatim_request_and_strong_match():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, json=result(), request=request)

    provider = NominatimProvider(
        user_agent="TRUSTMATE/test (contact@example.com)",
        transport=httpx.MockTransport(handler),
        cache_ttl_seconds=0,
    )
    evidence = await provider.search_business("Example Seller", "seller.example")
    assert evidence.provider.status == "checked"
    assert evidence.match_confidence == "high"
    assert evidence.provider.source_reference.endswith("/node/123")
    assert "format=jsonv2" in seen["url"]
    assert "addressdetails=1" in seen["url"]
    assert "extratags=1" in seen["url"]
    assert "namedetails=1" in seen["url"]
    assert "limit=5" in seen["url"]
    assert "TRUSTMATE" in seen["ua"]


@pytest.mark.asyncio
async def test_same_name_without_website_is_possible_not_strong():
    provider = NominatimProvider(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=result(website=None), request=request)),
        cache_ttl_seconds=0,
    )
    evidence = await provider.search_business("Example Seller", "seller.example")
    assert evidence.match_confidence == "medium"
    assert evidence.official_website is None


@pytest.mark.asyncio
async def test_domain_mismatch_is_not_accepted():
    provider = NominatimProvider(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=result(website="https://other.example"), request=request)),
        cache_ttl_seconds=0,
    )
    evidence = await provider.search_business("Example Seller", "seller.example")
    assert evidence.provider.status == "not_found"


@pytest.mark.asyncio
async def test_identical_queries_are_cached():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=result(), request=request)

    provider = NominatimProvider(transport=httpx.MockTransport(handler))
    await provider.search_business("Example Seller", "seller.example")
    await provider.search_business("Example Seller", "seller.example")
    assert calls == 1


@pytest.mark.asyncio
async def test_concurrent_requests_are_rate_limited():
    times = []

    def handler(request):
        times.append(time.monotonic())
        return httpx.Response(200, json=[], request=request)

    provider = NominatimProvider(
        transport=httpx.MockTransport(handler),
        cache_ttl_seconds=0,
    )
    await asyncio.gather(
        provider.search_business("Seller One"),
        provider.search_business("Seller Two"),
    )
    assert times[1] - times[0] >= 0.95


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429, 500])
async def test_provider_failures_are_unverified(status):
    provider = NominatimProvider(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, request=request)),
        cache_ttl_seconds=0,
    )
    evidence = await provider.search_business("Example Seller")
    assert evidence.provider.status == "unverified"
    assert evidence.provider.error_message == f"http_{status}"


@pytest.mark.asyncio
async def test_empty_and_malformed_results_are_safe():
    empty = NominatimProvider(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[], request=request)),
        cache_ttl_seconds=0,
    )
    assert (await empty.search_business("Example Seller")).provider.status == "not_found"

    malformed = NominatimProvider(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"bad": True}, request=request)),
        cache_ttl_seconds=0,
    )
    assert (await malformed.search_business("Example Seller")).provider.status == "unverified"


@pytest.mark.asyncio
async def test_timeout_is_unverified():
    def handler(request):
        raise httpx.ReadTimeout("timeout", request=request)

    evidence = await NominatimProvider(
        transport=httpx.MockTransport(handler), cache_ttl_seconds=0
    ).search_business("Example Seller")
    assert evidence.provider.error_message == "timeout"
    assert evidence.attribution == "© OpenStreetMap contributors"
