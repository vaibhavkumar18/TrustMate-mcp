import ssl

import httpx
import pytest

from app.providers.implementations import NativeWebsiteProvider


def website_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/returns":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<html><body>Our returns and refund policy explains refunds, exchanges, and cancellation within thirty days.</body></html>",
                request=request,
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=(
                "<html><body>Contact us at help@example.com or "
                "<a href='tel:+911234567890'>phone</a>. "
                "<a href='/returns'>Returns</a></body></html>"
            ),
            request=request,
        )

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_native_website_detects_contact_and_policy_content():
    evidence = await NativeWebsiteProvider(
        transport=website_transport(),
    ).inspect("https://seller.example/")

    assert evidence.provider.status == "checked"
    assert evidence.https_reachable is True
    assert evidence.tls_valid is True
    assert evidence.contact_present is True
    assert evidence.contact_email_present is True
    assert evidence.contact_phone_present is True
    assert evidence.return_policy_present is True
    assert evidence.refund_policy_present is True


@pytest.mark.asyncio
async def test_native_website_rejects_unsafe_urls_and_non_html():
    private = await NativeWebsiteProvider(transport=website_transport()).inspect(
        "https://127.0.0.1/"
    )
    assert private.provider.error_code == "private_host"

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-type": "application/json"}, text="{}"
        )
    )
    non_html = await NativeWebsiteProvider(transport=transport).inspect(
        "https://seller.example/"
    )
    assert non_html.provider.error_code == "non_html"


@pytest.mark.asyncio
async def test_native_website_rejects_private_redirect_and_oversized_body():
    redirect = httpx.MockTransport(
        lambda request: httpx.Response(
            302, headers={"location": "https://127.0.0.1/"}
        )
    )
    redirected = await NativeWebsiteProvider(transport=redirect).inspect(
        "https://seller.example/"
    )
    assert redirected.provider.error_code == "unsafe_redirect"

    oversized = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"x" * (1024 * 1024 + 1),
        )
    )
    result = await NativeWebsiteProvider(transport=oversized).inspect(
        "https://seller.example/"
    )
    assert result.provider.error_code == "response_too_large"


@pytest.mark.asyncio
async def test_native_website_maps_tls_failure_without_negative_evidence():
    def fail(request: httpx.Request) -> httpx.Response:
        raise ssl.SSLCertVerificationError("invalid certificate")

    evidence = await NativeWebsiteProvider(
        transport=httpx.MockTransport(fail),
    ).inspect("https://seller.example/")

    assert evidence.provider.status == "unverified"
    assert evidence.provider.error_code == "tls_certificate_failure"
    assert evidence.contact_present is None
