import asyncio

import httpx
import pytest

from app.providers.implementations import _PinnedHTTPS


def test_pinned_https_emits_exactly_one_host_and_connection_header():
    request = httpx.Request(
        "GET",
        "https://shop.example/contact",
        headers={"Host": "attacker.invalid", "Connection": "keep-alive", "User-Agent": "TRUSTMATE-MCP/0.1"},
    )

    lines = [line.decode("latin1").strip() for line in _PinnedHTTPS._wire_headers(request, "shop.example")]
    host_lines = [line for line in lines if line.lower().startswith("host:")]
    connection_lines = [line for line in lines if line.lower().startswith("connection:")]

    assert host_lines == ["Host: shop.example"]
    assert connection_lines == ["Connection: close"]
    assert "user-agent: TRUSTMATE-MCP/0.1" in lines


@pytest.mark.asyncio
async def test_pinned_https_tries_next_validated_ip_if_first_fails(monkeypatch):
    provider = _PinnedHTTPS()
    monkeypatch.setattr(provider, "resolve_public", lambda host: ["203.0.113.10", "203.0.113.11"])
    # These are test-only fake addresses; resolve_public is deliberately
    # replaced so this test does not perform DNS or connect to the internet.
    attempts = []
    request = httpx.Request("GET", "https://shop.example/")
    expected = httpx.Response(200, request=request, content=b"ok")

    async def fake_request_address(req, hostname, address, timeout):
        attempts.append(address)
        if address.endswith(".10"):
            raise OSError("connection refused")
        return expected

    monkeypatch.setattr(provider, "_request_address", fake_request_address)
    result = await provider.request(request, timeout=2.0)

    assert result.status_code == 200
    assert attempts == ["203.0.113.10", "203.0.113.11"]
