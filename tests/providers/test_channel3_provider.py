import httpx
import pytest
import json

from app.providers.implementations import Channel3Provider


def transport_for(payload, status_code=200, headers=None):
    def handler(request):
        return httpx.Response(status_code, json=payload, headers=headers, request=request)
    return httpx.MockTransport(handler)


def payload():
    return {
        "products": [{
            "id": "p-1",
            "title": "Phone",
            "offers": [{
                "url": "https://shop.example/phone",
                "domain": "shop.example",
                "seller": "Shop",
                "price": {"price": 19999, "currency": "USD"},
            }],
        }],
        "next_page_token": None,
    }


@pytest.mark.asyncio
async def test_normalizes_explicit_usd_offer():
    provider = Channel3Provider("secret", transport=transport_for(payload()))
    result = await provider.search("phone", 50000, target_country="US", currency="USD")
    assert result.provider.status == "checked"
    assert result.offers[0].price == 19999
    assert result.offers[0].currency == "USD"
    assert result.offers[0].seller.normalized_domain == "shop.example"


@pytest.mark.asyncio
async def test_uses_documented_auth_and_search_request():
    seen = {}

    def handler(request):
        seen["api_key"] = request.headers["x-api-key"]
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"products": []}, request=request)

    await Channel3Provider("test-key", transport=httpx.MockTransport(handler)).search(
        "phone", 50000, target_country="US", currency="USD"
    )
    assert seen == {
        "api_key": "test-key",
        "path": "/v1/search",
        "body": {
            "query": "phone",
            "limit": 20,
            "config": {"country": "US", "currency": "USD", "language": "en"},
        },
    }


@pytest.mark.asyncio
async def test_locale_fields_inside_config_not_top_level():
    """Locale fields must be inside config, not at the request body top level."""
    seen_body: dict = {}

    def handler(request):
        seen_body.update(json.loads(request.content))
        return httpx.Response(200, json={"products": []}, request=request)

    await Channel3Provider("test-key", transport=httpx.MockTransport(handler)).search(
        "phone", 50000, target_country="GB", currency="GBP"
    )
    assert "config" in seen_body, "config block must be present"
    assert seen_body["config"] == {"country": "GB", "currency": "GBP", "language": "en"}
    assert "country" not in seen_body
    assert "currency" not in seen_body
    assert "language" not in seen_body


@pytest.mark.asyncio
async def test_pagination_token_is_normalized_and_forwarded():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(200, json={"products": [], "next_page_token": "page-2"}, request=request)
        return httpx.Response(200, json={"products": [], "next_page_token": None}, request=request)

    provider = Channel3Provider("test-key", transport=httpx.MockTransport(handler))
    first = await provider.search("phone", 50000, target_country="US", currency="USD")
    second = await provider.search("phone", 50000, target_country="US", currency="USD", page_token=first.next_page_token)
    assert first.next_page_token == "page-2"
    assert "page_token" not in seen[0]
    assert seen[1]["page_token"] == "page-2"
    assert second.next_page_token is None


@pytest.mark.asyncio
async def test_skips_unsupported_currency_invalid_price_and_url():
    body = payload()
    body["products"][0]["offers"] += [
        {"url": "https://shop.example/us", "price": {"price": 10, "currency": "EUR"}},
        {"url": "https://shop.example/zero", "price": {"price": 0, "currency": "USD"}},
        {"url": "not-a-url", "price": {"price": 10, "currency": "USD"}},
    ]
    result = await Channel3Provider("secret", transport=transport_for(body)).search(
        "phone", 50000, target_country="US", currency="USD"
    )
    assert len(result.offers) == 1
    assert result.provider.data["skipped"]["unsupported_or_ambiguous_currency"] == 1
    assert result.provider.data["skipped"]["invalid_price"] == 1
    assert result.provider.data["skipped"]["invalid_url"] == 1


@pytest.mark.asyncio
async def test_missing_optional_seller_uses_provider_domain():
    body = payload()
    del body["products"][0]["offers"][0]["seller"]
    result = await Channel3Provider("secret", transport=transport_for(body)).search(
        "phone", 50000, target_country="US", currency="USD"
    )
    assert result.offers[0].seller.display_name == "shop.example"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(401),
    httpx.Response(429, headers={"retry-after": "0"}),
    httpx.Response(500),
])
async def test_http_failures_do_not_fabricate_offers(response):
    def handler(request):
        return response
    provider = Channel3Provider("secret", max_retries=0, transport=httpx.MockTransport(handler))
    result = await provider.search("phone", 50000, target_country="US", currency="USD")
    assert result.offers == []
    assert result.provider.status == "error"


@pytest.mark.asyncio
async def test_malformed_json_is_safe():
    def handler(request):
        return httpx.Response(200, content=b"not-json", request=request)
    result = await Channel3Provider("secret", transport=httpx.MockTransport(handler)).search(
        "phone", 50000, target_country="US", currency="USD"
    )
    assert result.offers == []
    assert result.provider.error_code == "malformed_json"


@pytest.mark.asyncio
async def test_timeout_is_safe():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)
    result = await Channel3Provider("secret", transport=httpx.MockTransport(handler)).search(
        "phone", 50000, target_country="US", currency="USD"
    )
    assert result.offers == []
    assert result.provider.error_code == "timeout"


@pytest.mark.asyncio
async def test_empty_results_are_checked_without_fabrication():
    result = await Channel3Provider(
        "secret", transport=transport_for({"products": [], "next_page_token": None})
    ).search("phone", 50000, target_country="US", currency="USD")
    assert result.provider.status == "checked"
    assert result.products == []
    assert result.offers == []
