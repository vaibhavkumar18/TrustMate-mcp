import httpx
import pytest

from app.providers.implementations import LiveRDAPProvider


@pytest.mark.asyncio
async def test_rdap_bootstrap_and_normalization():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("dns.json"):
            return httpx.Response(
                200,
                json={"services": [[["com"], ["https://rdap.example/"]]]},
                headers={"ETag": "one"},
            )
        return httpx.Response(
            200,
            json={
                "objectClassName": "domain",
                "status": ["active"],
                "events": [
                    {"eventAction": "registration", "eventDate": "2020-01-01T00:00:00Z"},
                    {"eventAction": "expiration", "eventDate": "2099-01-01T00:00:00Z"},
                ],
                "nameservers": [{"ldhName": "ns.example"}],
                "secureDNS": {"delegationSigned": True},
            },
        )

    evidence = await LiveRDAPProvider(
        transport=httpx.MockTransport(handler),
    ).lookup_domain("WWW.Example.com.")

    assert evidence.domain == "example.com"
    assert evidence.creation_date is not None
    assert evidence.expiration_date is None
    assert evidence.nameservers_present is True
    assert evidence.dnssec_enabled is True
    assert evidence.provider.status == "checked"


@pytest.mark.asyncio
async def test_rdap_rejects_unsupported_and_private_redirects():
    provider = LiveRDAPProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"services": [[["com"], ["http://rdap.example/"]]]}
            )
        )
    )
    unsupported = await provider.lookup_domain("example.invalid")

    assert unsupported.provider.status == "unverified"
    assert unsupported.provider.error_code == "invalid_domain"
    assert provider._safe_redirect("https://127.0.0.1/domain/example.com") is False
