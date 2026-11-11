"""
Offline regression tests for Channel3Provider locale capability guard.

Verifies that requests for unsupported countries/currencies are rejected
before an HTTP call is made, with a distinct error_code and log record.

No live network calls are made.
"""
from __future__ import annotations

import logging

import httpx
import pytest

from app.providers.implementations import (
    Channel3Provider,
    _CHANNEL3_SUPPORTED_COUNTRIES,
    _CHANNEL3_SUPPORTED_CURRENCIES,
)


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def find(self, event: str) -> logging.LogRecord | None:
        return next((r for r in self.records if r.getMessage() == event), None)


@pytest.fixture()
def cap():
    handler = _LogCapture()
    handler.setLevel(logging.DEBUG)
    logger = logging.getLogger("app.providers.implementations")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield handler
    logger.removeHandler(handler)


def _counting_transport() -> tuple[httpx.MockTransport, list[int]]:
    calls: list[int] = []
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"products": [], "next_page_token": None}, request=req)
    return httpx.MockTransport(handler), calls


# ---------------------------------------------------------------------------
# Capability set tests
# ---------------------------------------------------------------------------

def test_supported_countries_does_not_include_india():
    assert "IN" not in _CHANNEL3_SUPPORTED_COUNTRIES

def test_inr_not_in_supported_currencies():
    assert "INR" not in _CHANNEL3_SUPPORTED_CURRENCIES

def test_us_is_supported():
    assert "US" in _CHANNEL3_SUPPORTED_COUNTRIES

def test_usd_is_supported():
    assert "USD" in _CHANNEL3_SUPPORTED_CURRENCIES


# ---------------------------------------------------------------------------
# Locale guard — unsupported country
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_in_locale_returns_error_without_http_call(cap):
    transport, calls = _counting_transport()
    result = await Channel3Provider("k", transport=transport).search(
        "phone", 50000, target_country="IN", currency="INR"
    )
    assert len(calls) == 0
    assert result.provider.status == "error"
    assert result.provider.error_code == "unsupported_locale"
    assert result.offers == []
    assert result.products == []


@pytest.mark.asyncio
async def test_in_locale_emits_warning_log(cap):
    transport, _ = _counting_transport()
    await Channel3Provider("k", transport=transport).search(
        "phone", 50000, target_country="IN", currency="INR"
    )
    rec = cap.find("channel3_locale_unsupported")
    assert rec is not None
    assert rec.levelno == logging.WARNING
    assert rec.__dict__["error_code"] == "unsupported_locale"
    assert rec.__dict__["requested_country"] == "IN"
    assert rec.__dict__["requested_currency"] == "INR"


@pytest.mark.asyncio
async def test_unsupported_locale_log_does_not_expose_api_key(cap):
    secret = "my-secret-channel3-api-key-xyz"
    transport, _ = _counting_transport()
    await Channel3Provider(secret, transport=transport).search(
        "phone", 50000, target_country="IN", currency="INR"
    )
    for rec in cap.records:
        assert secret not in str(vars(rec))


@pytest.mark.asyncio
async def test_unsupported_locale_note_flows_through_rank_offers():
    from dataclasses import replace
    from app.core.config import Settings
    from app.mcp.schemas.inputs import RankOffersInput
    from app.mcp.tools.rank_offers import rank_offers
    from app.providers.factory import create_provider_registry

    transport, _ = _counting_transport()
    registry = replace(
        create_provider_registry(Settings(
            mock=False, channel3_api_key="k", foursquare_api_key="k", openrouter_api_key="k",
        )),
        product=Channel3Provider("k", transport=transport),
    )
    output = await rank_offers(
        RankOffersInput(query="phone", budget=50000, currency="INR", target_country="IN"),
        registry,
    )
    assert output.offers == []
    assert any(
        "not supported" in n.lower() or "unsupported" in n.lower()
        for n in output.unverified_notes
    ), f"Expected unsupported-locale note, got: {output.unverified_notes}"
    assert not any("met the minimum trust" in n for n in output.unverified_notes)


# ---------------------------------------------------------------------------
# Locale guard — mismatched currency for a supported country
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mismatched_currency_rejected_before_http(cap):
    """US with EUR must be rejected — US maps to USD."""
    transport, calls = _counting_transport()
    result = await Channel3Provider("k", transport=transport).search(
        "phone", 50000, target_country="US", currency="EUR"
    )
    assert len(calls) == 0
    assert result.provider.error_code == "unsupported_locale"


# ---------------------------------------------------------------------------
# Locale guard — supported combinations pass through
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_us_locale_makes_http_call():
    transport, calls = _counting_transport()
    result = await Channel3Provider("k", transport=transport).search(
        "phone", 50000, target_country="US", currency="USD"
    )
    assert len(calls) == 1
    assert result.provider.status == "checked"


@pytest.mark.asyncio
async def test_gb_locale_makes_http_call():
    transport, calls = _counting_transport()
    await Channel3Provider("k", transport=transport).search(
        "phone", 50000, target_country="GB", currency="GBP"
    )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_supported_locale_sends_config_block():
    """A supported locale must include a config block with country, currency, language."""
    import json
    seen_payloads: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(req.content))
        return httpx.Response(200, json={"products": [], "next_page_token": None}, request=req)

    provider = Channel3Provider("k", transport=httpx.MockTransport(handler))
    await provider.search("phone", 50000, target_country="US", currency="USD")
    assert len(seen_payloads) == 1
    assert seen_payloads[0].get("config") == {"country": "US", "currency": "USD", "language": "en"}
    # Locale fields must NOT appear at top level
    assert "country" not in seen_payloads[0]
    assert "currency" not in seen_payloads[0]
    assert "language" not in seen_payloads[0]


@pytest.mark.asyncio
async def test_unsupported_locale_is_not_retried(cap):
    transport, calls = _counting_transport()
    provider = Channel3Provider("k", max_retries=3, transport=transport)
    result = await provider.search(
        "phone", 50000, target_country="IN", currency="INR"
    )
    assert len(calls) == 0
    assert result.provider.error_code == "unsupported_locale"
