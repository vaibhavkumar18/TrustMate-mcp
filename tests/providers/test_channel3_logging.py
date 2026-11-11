"""
Offline regression tests for Channel3Provider structured diagnostic logging.

Section A  — log records emitted (fields on LogRecord)
Section B  — log records rendered (fields appear in formatted terminal output)

All HTTP responses are delivered via httpx.MockTransport — no live calls.
"""
from __future__ import annotations

import logging
from typing import Generator

import httpx
import pytest

from app.providers.implementations import Channel3Provider


# ---------------------------------------------------------------------------
# Helpers shared by both sections
# ---------------------------------------------------------------------------

class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def find(self, event: str) -> logging.LogRecord | None:
        return next((r for r in self.records if r.getMessage() == event), None)


@pytest.fixture()
def cap() -> Generator[_LogCapture, None, None]:
    """Attach a capture handler to the implementations logger for one test."""
    handler = _LogCapture()
    handler.setLevel(logging.DEBUG)
    logger = logging.getLogger("app.providers.implementations")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield handler
    logger.removeHandler(handler)


def _json(payload: dict, *, status: int = 200) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda req: httpx.Response(status, json=payload, request=req)
    )


def _exc(cls) -> httpx.MockTransport:
    def h(req: httpx.Request) -> httpx.Response:
        raise cls("simulated", request=req)
    return httpx.MockTransport(h)


_VALID = {
    "products": [{
        "id": "p1", "title": "Phone",
        "offers": [{
            "id": "o1", "url": "https://shop.testretailer.in/phone",
            "domain": "shop.testretailer.in", "seller": "Shop",
            "price": {"price": 19999, "currency": "USD"},
        }],
    }],
    "next_page_token": None,
}


# ===========================================================================
# Section A — LogRecord fields emitted correctly
# ===========================================================================

@pytest.mark.asyncio
async def test_success_emits_info_with_status_200(cap):
    r = await Channel3Provider("k", transport=_json(_VALID)).search("phone", 50000, target_country="US", currency="USD")
    assert r.provider.status == "checked"
    rec = cap.find("channel3_search_complete")
    assert rec is not None
    assert rec.levelno == logging.INFO
    assert rec.__dict__["http_status_code"] == 200
    assert rec.__dict__["provider"] == "channel3"
    assert rec.__dict__["endpoint"] == "/v1/search"
    assert rec.__dict__["method"] == "POST"
    assert rec.__dict__["error_code"] is None
    assert rec.__dict__["exception_type"] is None
    assert isinstance(rec.__dict__["duration_ms"], int)
    assert rec.__dict__["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_success_records_country_and_currency(cap):
    await Channel3Provider("k", transport=_json(_VALID)).search("phone", 50000, target_country="US", currency="USD")
    rec = cap.find("channel3_search_complete")
    assert rec is not None
    assert rec.__dict__["country"] == "US"
    assert rec.__dict__["currency"] == "USD"


@pytest.mark.asyncio
async def test_401_emits_warning_with_exact_status(cap):
    r = await Channel3Provider(
        "bad", max_retries=0,
        transport=_json({"error": "Unauthorized"}, status=401),
    ).search("phone", 50000, target_country="US", currency="USD")
    assert r.provider.error_code == "authentication_failed"
    rec = cap.find("channel3_search_failed")
    assert rec is not None
    assert rec.levelno == logging.WARNING
    assert rec.__dict__["http_status_code"] == 401
    assert rec.__dict__["error_code"] == "authentication_failed"
    assert rec.__dict__["exception_type"] is None


@pytest.mark.asyncio
async def test_401_includes_sanitized_body(cap):
    await Channel3Provider(
        "bad", max_retries=0,
        transport=_json({"error": "Invalid API key provided"}, status=401),
    ).search("phone", 50000, target_country="US", currency="USD")
    rec = cap.find("channel3_search_failed")
    body = rec.__dict__.get("sanitized_error_body")
    assert body is not None
    assert "Invalid API key" in body or "[redacted]" in body


@pytest.mark.asyncio
async def test_403_exact_status(cap):
    await Channel3Provider("k", max_retries=0, transport=_json({}, status=403)).search("phone", 50000, target_country="US", currency="USD")
    rec = cap.find("channel3_search_failed")
    assert rec is not None and rec.__dict__["http_status_code"] == 403


@pytest.mark.asyncio
async def test_429_exact_status(cap):
    r = await Channel3Provider(
        "k", max_retries=0, transport=_json({}, status=429)
    ).search("phone", 50000, target_country="US", currency="USD")
    assert r.provider.error_code == "rate_limited"
    rec = cap.find("channel3_search_failed")
    assert rec is not None
    assert rec.__dict__["http_status_code"] == 429
    assert rec.__dict__["error_code"] == "rate_limited"


@pytest.mark.asyncio
async def test_500_exact_status(cap):
    await Channel3Provider("k", max_retries=0, transport=_json({}, status=500)).search("phone", 50000, target_country="US", currency="USD")
    rec = cap.find("channel3_search_failed")
    assert rec is not None
    assert rec.__dict__["http_status_code"] == 500
    assert rec.__dict__["error_code"] == "http_500"


@pytest.mark.asyncio
async def test_503_exact_status(cap):
    await Channel3Provider("k", max_retries=0, transport=_json({}, status=503)).search("phone", 50000, target_country="US", currency="USD")
    rec = cap.find("channel3_search_failed")
    assert rec is not None and rec.__dict__["http_status_code"] == 503


@pytest.mark.asyncio
async def test_400_exact_status(cap):
    r = await Channel3Provider(
        "k", max_retries=0,
        transport=_json({"error": "Bad request"}, status=400),
    ).search("phone", 50000, target_country="US", currency="USD")
    assert r.provider.error_code == "http_400"
    rec = cap.find("channel3_search_failed")
    assert rec is not None
    assert rec.__dict__["http_status_code"] == 400
    assert rec.__dict__["error_code"] == "http_400"


@pytest.mark.asyncio
async def test_timeout_no_http_status(cap):
    r = await Channel3Provider(
        "k", max_retries=0, transport=_exc(httpx.ReadTimeout)
    ).search("phone", 50000, target_country="US", currency="USD")
    assert r.provider.error_code == "timeout"
    rec = cap.find("channel3_search_failed")
    assert rec is not None
    assert rec.__dict__["http_status_code"] is None
    assert rec.__dict__["exception_type"] == "ReadTimeout"
    assert rec.__dict__["error_code"] == "timeout"


@pytest.mark.asyncio
async def test_connect_error_no_http_status(cap):
    r = await Channel3Provider(
        "k", max_retries=0, transport=_exc(httpx.ConnectError)
    ).search("phone", 50000, target_country="US", currency="USD")
    assert r.provider.error_code == "transport_error"
    rec = cap.find("channel3_search_failed")
    assert rec is not None
    assert rec.__dict__["http_status_code"] is None
    assert rec.__dict__["exception_type"] == "ConnectError"


@pytest.mark.asyncio
async def test_malformed_json_status_200(cap):
    def bh(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json", request=req)
    r = await Channel3Provider("k", transport=httpx.MockTransport(bh)).search("phone", 50000, target_country="US", currency="USD")
    assert r.provider.error_code == "malformed_json"
    rec = cap.find("channel3_search_failed")
    assert rec is not None
    assert rec.__dict__["http_status_code"] == 200
    assert rec.__dict__["error_code"] == "malformed_json"
    assert rec.__dict__["exception_type"] is None


@pytest.mark.asyncio
async def test_no_api_key_in_any_record(cap):
    api_key = "super-secret-api-key-do-not-log"
    await Channel3Provider(
        api_key, max_retries=0, transport=_json({}, status=401)
    ).search("phone", 50000, target_country="US", currency="USD")
    for rec in cap.records:
        assert api_key not in str(vars(rec))


@pytest.mark.asyncio
async def test_duration_is_non_negative_int(cap):
    for transport, event in [
        (_json(_VALID), "channel3_search_complete"),
        (_json({}, status=401), "channel3_search_failed"),
        (_exc(httpx.ReadTimeout), "channel3_search_failed"),
    ]:
        cap.records.clear()
        await Channel3Provider("k", max_retries=0, transport=transport).search("phone", 50000, target_country="US", currency="USD")
        rec = cap.find(event)
        assert rec is not None
        ms = rec.__dict__["duration_ms"]
        assert isinstance(ms, int) and ms >= 0


@pytest.mark.asyncio
async def test_long_token_redacted_in_body(cap):
    long_token = "A" * 30
    await Channel3Provider(
        "k", max_retries=0,
        transport=_json({"error": f"denied: {long_token}"}, status=403),
    ).search("phone", 50000, target_country="US", currency="USD")
    rec = cap.find("channel3_search_failed")
    body = rec.__dict__.get("sanitized_error_body", "")
    assert long_token not in (body or "")
    assert "[redacted]" in (body or "")


@pytest.mark.asyncio
async def test_empty_200_still_info(cap):
    r = await Channel3Provider(
        "k", transport=_json({"products": [], "next_page_token": None})
    ).search("phone", 50000, target_country="US", currency="USD")
    assert r.provider.status == "checked"
    rec = cap.find("channel3_search_complete")
    assert rec is not None
    assert rec.levelno == logging.INFO
    assert rec.__dict__["http_status_code"] == 200


@pytest.mark.asyncio
async def test_retry_count_logged(cap):
    calls: list[int] = []

    def ch(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, json={}, request=req)

    await Channel3Provider("k", max_retries=1, transport=httpx.MockTransport(ch)).search("phone", 50000, target_country="US", currency="USD")
    assert len(calls) == 2
    rec = cap.find("channel3_search_failed")
    assert rec is not None
    assert rec.__dict__["attempts"] == 2
    assert rec.__dict__["http_status_code"] == 500


# ===========================================================================
# Section B — StructuredFormatter renders required fields in terminal output
# ===========================================================================

from app.core.logging_setup import StructuredFormatter


def _format(record: logging.LogRecord) -> str:
    return StructuredFormatter().format(record)


def _make_record(
    event: str,
    level: int = logging.WARNING,
    **extra: object,
) -> logging.LogRecord:
    """Build a LogRecord manually with the given extra fields."""
    record = logging.LogRecord(
        name="app.providers.implementations",
        level=level,
        pathname="implementations.py",
        lineno=1,
        msg=event,
        args=(),
        exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_formatter_renders_http_status_code():
    rec = _make_record(
        "channel3_search_failed",
        http_status_code=401,
        error_code="authentication_failed",
        exception_type=None,
        sanitized_error_body="Unauthorized",
        duration_ms=142,
        country="IN",
        currency="INR",
        attempts=1,
    )
    output = _format(rec)
    assert "http_status_code=401" in output


def test_formatter_renders_error_code():
    rec = _make_record(
        "channel3_search_failed",
        http_status_code=401,
        error_code="authentication_failed",
        duration_ms=50,
    )
    output = _format(rec)
    assert "error_code=authentication_failed" in output


def test_formatter_renders_none_status_for_timeout():
    """http_status_code=None must be explicitly printed, not omitted."""
    rec = _make_record(
        "channel3_search_failed",
        http_status_code=None,
        error_code="timeout",
        exception_type="ReadTimeout",
        duration_ms=10001,
        country="IN",
        currency="INR",
        attempts=1,
    )
    output = _format(rec)
    assert "http_status_code=None" in output
    assert "exception_type=ReadTimeout" in output


def test_formatter_renders_exception_type():
    rec = _make_record(
        "channel3_search_failed",
        http_status_code=None,
        exception_type="ConnectError",
        error_code="transport_error",
        duration_ms=5,
    )
    output = _format(rec)
    assert "exception_type=ConnectError" in output


def test_formatter_renders_sanitized_error_body():
    rec = _make_record(
        "channel3_search_failed",
        http_status_code=403,
        error_code="authentication_failed",
        sanitized_error_body="Forbidden endpoint",
        duration_ms=88,
    )
    output = _format(rec)
    assert "sanitized_error_body=" in output
    assert "Forbidden endpoint" in output


def test_formatter_renders_duration_ms():
    rec = _make_record(
        "channel3_search_failed",
        http_status_code=500,
        error_code="http_500",
        duration_ms=342,
    )
    output = _format(rec)
    assert "duration_ms=342" in output


def test_formatter_renders_country_and_currency():
    rec = _make_record(
        "channel3_search_complete",
        level=logging.INFO,
        http_status_code=200,
        error_code=None,
        exception_type=None,
        duration_ms=95,
        country="IN",
        currency="INR",
    )
    output = _format(rec)
    assert "country=IN" in output
    assert "currency=INR" in output


def test_formatter_renders_all_required_fields_together():
    """All seven required fields must appear in a single formatted line."""
    rec = _make_record(
        "channel3_search_failed",
        http_status_code=400,
        error_code="http_400",
        exception_type=None,
        sanitized_error_body="Bad query",
        duration_ms=77,
        country="IN",
        currency="INR",
        attempts=1,
    )
    output = _format(rec)
    for expected in (
        "http_status_code=400",
        "error_code=http_400",
        "exception_type=None",
        "sanitized_error_body=",
        "duration_ms=77",
        "country=IN",
        "currency=INR",
    ):
        assert expected in output, f"Missing {expected!r} in: {output}"


def test_formatter_does_not_leak_blocked_field_names():
    """Fields named 'api_key', 'token', etc. must not appear in the output."""
    rec = _make_record(
        "channel3_search_failed",
        http_status_code=401,
        error_code="authentication_failed",
        duration_ms=10,
        # These must be blocked
        api_key="should-not-appear",
        token="also-hidden",
    )
    output = _format(rec)
    assert "api_key" not in output
    assert "should-not-appear" not in output
    assert "token" not in output
    assert "also-hidden" not in output


def test_formatter_record_without_extra_fields_has_no_trailing_spaces():
    """A plain record with no extra fields must not have trailing whitespace."""
    rec = logging.LogRecord(
        name="app.test", level=logging.INFO, pathname="x.py", lineno=1,
        msg="simple message", args=(), exc_info=None,
    )
    output = _format(rec)
    assert output == output.rstrip()
    assert "  " not in output.split("simple message")[1]  # nothing after message


def test_formatter_quotes_values_with_spaces():
    """A sanitized body containing spaces must be double-quoted in the output."""
    rec = _make_record(
        "channel3_search_failed",
        sanitized_error_body="Invalid API key supplied",
        duration_ms=5,
    )
    output = _format(rec)
    assert 'sanitized_error_body="Invalid API key supplied"' in output


def test_formatter_is_idempotent_on_repeated_calls():
    """Formatting the same record twice must produce identical output."""
    rec = _make_record(
        "channel3_search_failed",
        http_status_code=503,
        error_code="http_503",
        duration_ms=1,
    )
    fmt = StructuredFormatter()
    assert fmt.format(rec) == fmt.format(rec)


@pytest.mark.asyncio
async def test_formatted_output_contains_http_status_for_401(cap):
    """End-to-end: a real 401 response must produce a line with http_status_code=401."""
    fmt = StructuredFormatter()
    await Channel3Provider(
        "bad", max_retries=0,
        transport=_json({"error": "Unauthorized"}, status=401),
    ).search("phone", 50000, target_country="US", currency="USD")
    rec = cap.find("channel3_search_failed")
    assert rec is not None
    output = fmt.format(rec)
    assert "http_status_code=401" in output, (
        f"http_status_code=401 not found in formatted output:\n{output}"
    )


@pytest.mark.asyncio
async def test_formatted_output_contains_none_status_for_timeout(cap):
    """End-to-end: a timeout must produce a line with http_status_code=None."""
    fmt = StructuredFormatter()
    await Channel3Provider(
        "k", max_retries=0, transport=_exc(httpx.ReadTimeout)
    ).search("phone", 50000, target_country="US", currency="USD")
    rec = cap.find("channel3_search_failed")
    assert rec is not None
    output = fmt.format(rec)
    assert "http_status_code=None" in output, (
        f"http_status_code=None not found in formatted output:\n{output}"
    )
    assert "exception_type=ReadTimeout" in output


@pytest.mark.asyncio
async def test_formatted_output_contains_all_required_fields_for_success(cap):
    """End-to-end: a 200 success must produce a line with all required fields."""
    fmt = StructuredFormatter()
    await Channel3Provider("k", transport=_json(_VALID)).search("phone", 50000, target_country="US", currency="USD")
    rec = cap.find("channel3_search_complete")
    assert rec is not None
    output = fmt.format(rec)
    for expected in (
        "http_status_code=200",
        "error_code=None",
        "exception_type=None",
        "duration_ms=",
        "country=US",
        "currency=USD",
    ):
        assert expected in output, f"Missing {expected!r} in:\n{output}"


# ===========================================================================
# Section C — 422 validation-error body extraction
# ===========================================================================

from app.providers.implementations import _safe_error_body, _sanitize_validation_obj


def _resp(payload: object, *, status: int = 422) -> httpx.Response:
    """Build a fake httpx.Response carrying a JSON body."""
    import json as _json
    content = _json.dumps(payload).encode()
    return httpx.Response(
        status,
        content=content,
        headers={"content-type": "application/json"},
    )


def test_422_detail_list_extracts_loc_msg_type():
    """Standard FastAPI/Pydantic 422 body: detail is a list of objects."""
    body = {
        "detail": [
            {
                "loc": ["body", "config", "country"],
                "msg": "value is not a valid enumeration member",
                "type": "type_error.enum",
            }
        ]
    }
    result = _safe_error_body(_resp(body))
    assert result is not None
    assert "loc=body.config.country" in result
    assert "msg=" in result
    assert "type=type_error.enum" in result


def test_422_detail_list_multiple_errors_bounded_to_three():
    """At most three validation errors must appear; no fourth entry."""
    errors = [
        {"loc": ["body", f"field{i}"], "msg": f"error {i}", "type": f"t{i}"}
        for i in range(5)
    ]
    result = _safe_error_body(_resp({"detail": errors}))
    assert result is not None
    # Only errors 0-2 should appear (bounded to 3)
    assert "error 3" not in result
    assert "error 4" not in result


def test_422_detail_string_is_returned():
    """detail as a plain string must be returned directly."""
    result = _safe_error_body(_resp({"detail": "country IN is not supported"}))
    assert result is not None
    assert "country IN" in result


def test_422_detail_dict_single_object():
    """detail as a single validation dict (not list) must be handled."""
    body = {
        "detail": {
            "loc": ["body", "config"],
            "msg": "field required",
            "type": "value_error.missing",
        }
    }
    result = _safe_error_body(_resp(body))
    assert result is not None
    assert "loc=body.config" in result
    assert "msg=" in result


def test_flat_error_field_is_returned():
    """{"error": "..."} body must still be extracted."""
    result = _safe_error_body(_resp({"error": "Invalid request"}))
    assert result == "Invalid request"


def test_flat_message_field_is_returned():
    """{"message": "..."} body must be extracted."""
    result = _safe_error_body(_resp({"message": "Unrecognised country code"}))
    assert result == "Unrecognised country code"


def test_no_known_fields_returns_none():
    """A body with no extractable fields must return None."""
    result = _safe_error_body(_resp({"unknown_key": "some value"}))
    assert result is None


def test_detail_list_with_non_dict_items_skipped():
    """Non-dict items in the detail list must be skipped silently."""
    body = {"detail": ["a plain string", None, {"loc": ["body"], "msg": "bad", "type": "t"}]}
    result = _safe_error_body(_resp(body))
    assert result is not None
    assert "loc=body" in result


def test_long_token_in_detail_msg_redacted():
    """A credential-shaped token inside msg must be redacted."""
    token = "Nx0hIVzlxR3LwkuivfE736v1WPPU1VIW"   # 32 chars, > 20-char threshold
    body = {"detail": [{"loc": ["body"], "msg": f"key {token} rejected", "type": "auth"}]}
    result = _safe_error_body(_resp(body))
    assert result is not None
    assert token not in result
    assert "[redacted]" in result


def test_detail_list_loc_integer_segments_included():
    """Integer path segments (list indices) in loc must be included."""
    body = {
        "detail": [
            {"loc": ["body", "items", 0, "country"], "msg": "bad value", "type": "t"}
        ]
    }
    result = _safe_error_body(_resp(body))
    assert "0" in result


def test_non_json_response_returns_none():
    """A non-JSON response body must return None without raising."""
    resp = httpx.Response(422, content=b"not json", headers={"content-type": "text/plain"})
    result = _safe_error_body(resp)
    assert result is None


def test_empty_detail_list_returns_none():
    """An empty detail list must return None."""
    result = _safe_error_body(_resp({"detail": []}))
    assert result is None


def test_detail_list_all_empty_objects_returns_none():
    """A detail list of objects with no safe fields must return None."""
    body = {"detail": [{"unknown": "x"}, {"also_unknown": 1}]}
    result = _safe_error_body(_resp(body))
    assert result is None


@pytest.mark.asyncio
async def test_422_response_populates_sanitized_error_body_in_log(cap):
    """End-to-end: a 422 with a detail list must produce a non-None sanitized_error_body.

    Uses locale='US' so the provider-level locale guard passes; the mock
    transport then returns a 422 to exercise HTTP-level body extraction.
    """
    import json
    validation_body = {
        "detail": [
            {
                "loc": ["body", "config", "country"],
                "msg": "value is not a valid enumeration member; permitted: US, CA, GB",
                "type": "type_error.enum",
            }
        ]
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            content=json.dumps(validation_body).encode(),
            headers={"content-type": "application/json"},
            request=req,
        )

    await Channel3Provider(
        "k", max_retries=0, transport=httpx.MockTransport(handler)
    ).search("phone", 50000, target_country="US", currency="USD")   # US passes the locale guard

    rec = cap.find("channel3_search_failed")
    assert rec is not None
    assert rec.__dict__["http_status_code"] == 422
    body = rec.__dict__.get("sanitized_error_body")
    assert body is not None, (
        "sanitized_error_body must not be None for a 422 with a detail list"
    )
    assert "loc=body.config.country" in body
