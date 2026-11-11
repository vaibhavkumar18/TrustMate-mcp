import os

import httpx
import pytest

from app.providers.implementations import NominatimProvider


@pytest.mark.asyncio
async def test_opt_in_nominatim_live_search():
    if os.getenv("OSM_LIVE_TEST", "").lower() != "true":
        pytest.skip("Set OSM_LIVE_TEST=true to run the deliberate live test.")
    user_agent = os.getenv("OSM_USER_AGENT")
    if not user_agent:
        pytest.skip("OSM_USER_AGENT is unavailable.")
    result = await NominatimProvider(
        user_agent=user_agent,
        contact_email=os.getenv("OSM_CONTACT_EMAIL"),
    ).search_business(
        os.getenv("OSM_LIVE_QUERY", "OpenStreetMap Foundation"),
        os.getenv("OSM_LIVE_DOMAIN"),
    )
    print(
        "Nominatim live result:",
        result.provider.status,
        "place_id_present=", bool(result.provider_place_id),
        "website_present=", bool(result.official_website),
        "attribution=", result.attribution,
    )
    assert result.provider.status in {"checked", "not_found", "unverified"}
