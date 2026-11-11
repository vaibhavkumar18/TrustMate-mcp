import os

import pytest

from app.providers.implementations import FoursquareProvider


@pytest.mark.asyncio
async def test_opt_in_foursquare_live_search():
    if os.getenv("FOURSQUARE_LIVE_TEST", "").lower() != "true":
        pytest.skip("Set FOURSQUARE_LIVE_TEST=true to run the deliberate live test.")
    api_key = os.getenv("FOURSQUARE_API_KEY")
    if not api_key:
        pytest.skip("FOURSQUARE_API_KEY is unavailable.")
    result = await FoursquareProvider(api_key).search_business(
        os.getenv("FOURSQUARE_LIVE_QUERY", "Starbucks"),
        os.getenv("FOURSQUARE_LIVE_DOMAIN"),
    )
    print(
        "Foursquare live result:",
        result.provider.status,
        "place_id_present=", bool(result.provider_place_id),
        "website_present=", bool(result.official_website),
        "premium_rating_present=", bool(result.reviews),
    )
    assert result.provider.status in {"checked", "not_found", "unverified"}
