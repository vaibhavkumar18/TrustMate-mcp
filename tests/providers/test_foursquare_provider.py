import httpx
import pytest

from app.providers.implementations import FoursquareProvider


def place(*, website="https://seller.example", name="Example Seller"):
    return {
        "fsq_place_id": "fsq-1",
        "name": name,
        "website": website,
        "tel": "+1 555 0100",
        "email": "hello@example.com",
        "location": {"formatted_address": "1 Main Street"},
        "categories": [{"id": 1, "name": "Retail"}],
        "unresolved_flags": [],
    }


def transport_for(payload, status_code=200):
    def handler(request):
        return httpx.Response(status_code, json=payload, request=request)
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_pro_search_headers_fields_and_strong_domain_match():
    seen = {}

    def handler(request):
        seen["headers"] = request.headers
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"results": [place()]}, request=request)

    result = await FoursquareProvider(
        "secret", transport=httpx.MockTransport(handler)
    ).search_business("Example Seller", "seller.example")
    assert result.provider.status == "checked"
    assert result.match_confidence == "high"
    assert result.provider_place_id == "fsq-1"
    assert seen["headers"]["x-places-api-version"] == "2025-06-17"
    assert seen["headers"]["accept"] == "application/json"
    assert seen["headers"]["authorization"] == "Bearer secret"
    assert seen["params"]["query"] == "Example Seller"
    assert seen["params"]["limit"] == "5"
    assert "rating" not in seen["params"]["fields"]


@pytest.mark.asyncio
async def test_same_name_without_domain_corroboration_is_not_strong():
    result = await FoursquareProvider(
        "secret", transport=transport_for({"results": [place(website="https://other.example")]})
    ).search_business("Example Seller", "seller.example")
    assert result.match_confidence == "medium"


@pytest.mark.asyncio
async def test_domain_mismatch_with_different_name_is_not_accepted():
    result = await FoursquareProvider(
        "secret", transport=transport_for({"results": [place(name="Other Shop")]})
    ).search_business("Example Seller", "seller.example")
    assert result.provider.status == "not_found"
    assert result.provider_place_id is None


@pytest.mark.asyncio
async def test_premium_details_are_disabled_by_default():
    result = await FoursquareProvider("secret").get_place_details("fsq-1")
    assert result.provider.status == "unverified"
    assert result.provider.error_message == "Premium fields are disabled"


@pytest.mark.asyncio
async def test_premium_details_are_opt_in_and_normalized():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["fields"] = request.url.params["fields"]
        return httpx.Response(200, json={
            **place(),
            "rating": 8.5,
            "stats": {"total_ratings": 12, "total_tips": 3},
        }, request=request)

    result = await FoursquareProvider(
        "secret", premium_enabled=True, transport=httpx.MockTransport(handler)
    ).get_place_details("fsq-1")
    assert seen["path"] == "/places/fsq-1"
    assert "rating" in seen["fields"]
    assert result.reviews.rating == 4.25
    assert result.reviews.review_count == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_provider_failures_are_unverified(status):
    result = await FoursquareProvider(
        "secret", max_retries=0, transport=transport_for({}, status)
    ).search_business("Example Seller", "seller.example")
    assert result.provider.status == "unverified"
    assert result.reviews is None


@pytest.mark.asyncio
async def test_empty_and_malformed_results_are_unverified_or_not_found():
    empty = await FoursquareProvider(
        "secret", transport=transport_for({"results": []})
    ).search_business("Example Seller")
    assert empty.provider.status == "not_found"

    malformed = await FoursquareProvider(
        "secret", transport=transport_for({"results": "bad"})
    ).search_business("Example Seller")
    assert malformed.provider.status == "unverified"
