import os

import pytest

from app.core.config import Settings
from app.providers.implementations import Channel3Provider


@pytest.mark.asyncio
async def test_opt_in_channel3_live_search():
    if os.getenv("CHANNEL3_LIVE_TEST", "").lower() != "true":
        pytest.skip("Set CHANNEL3_LIVE_TEST=true to run the deliberate live test.")
    api_key = os.getenv("CHANNEL3_API_KEY")
    if not api_key:
        pytest.skip("CHANNEL3_API_KEY is unavailable.")
    settings = Settings(
        mock=False,
        channel3_api_key=api_key,
        channel3_base_url=os.getenv("CHANNEL3_BASE_URL", "https://api.trychannel3.com"),
    )
    result = await Channel3Provider(
        api_key,
        base_url=settings.channel3_base_url,
        timeout_seconds=settings.channel3_timeout_seconds,
        max_retries=settings.channel3_max_retries,
    ).search("laptop", 60000, target_country="US", currency="USD")
    assert result.provider.status in {"checked", "error"}
    print(
        "Channel3 live result:",
        result.provider.status,
        "products=", len(result.products),
        "offers=", len(result.offers),
        "currencies=USD-normalized",
    )
