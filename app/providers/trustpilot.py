"""Trustpilot Data Solutions Insights adapter for seller-scoped service reviews.

Only verified, whole-business service reviews are used. Physical-location
reviews are excluded. Raw review text and consumer details are never retained.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.domain.models import ProviderResult, RetentionPolicy, ReviewEvidence
from app.services.seller_identity import normalize_domain, same_registered_domain

logger = logging.getLogger(__name__)


class TrustpilotReviewProvider:
    """Read Trustpilot Insights service-review aggregates for an exact domain.

    Trustpilot's Insights API entitlement is required for the service-reviews
    endpoint. This adapter is opt-in; it does not scrape Trustpilot pages or
    substitute physical-place/product reviews.
    """

    PROVIDER_CODE = "trustpilot"
    CACHE_TTL_SECONDS = 24 * 60 * 60

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://datasolutions.trustpilot.com/v1",
        timeout_seconds: float = 10.0,
        max_review_pages: int = 5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("Trustpilot API key is required")
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Trustpilot base URL must be an HTTPS origin")
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_review_pages = max(1, min(int(max_review_pages), 10))
        self._transport = transport
        self._cache: dict[str, tuple[float, ReviewEvidence]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._request_semaphore = asyncio.Semaphore(3)

    async def lookup_seller_reviews(
        self, domain: str, target_country: str | None = None
    ) -> ReviewEvidence:
        normalized = normalize_domain(domain)
        if not normalized:
            return self._unavailable("invalid_seller_domain")

        cached = self._cache.get(normalized)
        now = time.monotonic()
        if cached and cached[0] > now:
            return cached[1].model_copy(deep=True)

        lock = self._locks.setdefault(normalized, asyncio.Lock())
        async with lock:
            cached = self._cache.get(normalized)
            now = time.monotonic()
            if cached and cached[0] > now:
                return cached[1].model_copy(deep=True)

            async with self._request_semaphore:
                evidence = await self._fetch(normalized)
            if evidence.provider.status == "checked":
                self._cache[normalized] = (
                    time.monotonic() + self.CACHE_TTL_SECONDS,
                    evidence.model_copy(deep=True),
                )
                # Bound process-local cache growth without evicting fresh data
                # unnecessarily. The provider stores aggregates, not reviews.
                if len(self._cache) > 2048:
                    expired = [key for key, (expiry, _) in self._cache.items() if expiry <= time.monotonic()]
                    for key in expired:
                        self._cache.pop(key, None)
                    while len(self._cache) > 2048:
                        self._cache.pop(next(iter(self._cache)))
            return evidence

    async def _fetch(self, domain: str) -> ReviewEvidence:
        async with self._client() as client:
            try:
                response = await client.get(
                    f"{self._base_url}/business-units", params={"domain": domain}
                )
                if response.status_code in {401, 403}:
                    return self._unavailable("api_access_denied")
                if response.status_code == 429:
                    return self._unavailable("rate_limited")
                if response.status_code == 404:
                    return self._not_found("trustpilot_profile_not_found")
                if response.status_code >= 500:
                    return self._unavailable("provider_unavailable")
                response.raise_for_status()
                payload = response.json()
            except httpx.TimeoutException:
                return self._unavailable("timeout")
            except httpx.HTTPError:
                return self._unavailable("provider_request_failed")
            except (ValueError, TypeError):
                return self._unavailable("invalid_provider_response")

            candidates = payload.get("businessUnits") if isinstance(payload, dict) else None
            if not isinstance(candidates, list):
                return self._unavailable("invalid_provider_response")
            # The find-by-domain response's `name.identifying` field is not
            # consistently a URL in every documented response shape. Treat
            # the domain query only as candidate discovery; confirm the
            # actual website/domain from each Business Unit details response.
            ids = list(dict.fromkeys(
                str(item.get("id"))
                for item in candidates
                if isinstance(item, dict) and item.get("id")
            ))
            if not ids:
                return self._not_found("trustpilot_profile_not_found")
            if len(ids) > 5:
                return self._unavailable("ambiguous_trustpilot_profile")

            matching_profiles: list[tuple[str, dict[str, Any]]] = []
            for candidate_id in ids:
                try:
                    details_response = await client.get(
                        f"{self._base_url}/business-units/{candidate_id}"
                    )
                    if details_response.status_code in {401, 403}:
                        return self._unavailable("api_access_denied")
                    if details_response.status_code == 429:
                        return self._unavailable("rate_limited")
                    if details_response.status_code == 404:
                        continue
                    if details_response.status_code >= 500:
                        return self._unavailable("provider_unavailable")
                    details_response.raise_for_status()
                    details = details_response.json()
                except httpx.TimeoutException:
                    return self._unavailable("timeout")
                except httpx.HTTPError:
                    return self._unavailable("provider_request_failed")
                except (ValueError, TypeError):
                    return self._unavailable("invalid_provider_response")

                if isinstance(details, dict) and self._matches_domain(details, domain):
                    matching_profiles.append((candidate_id, details))

            if not matching_profiles:
                return self._unavailable("trustpilot_domain_not_corroborated")
            if len(matching_profiles) != 1:
                return self._unavailable("ambiguous_trustpilot_profile")
            business_unit_id, details = matching_profiles[0]
            if str(details.get("status", "active")).lower() == "closed":
                return self._not_found("trustpilot_profile_closed")

            total_count = self._nested_nonnegative_int(details, "numberOfReviews", "total")
            verified_stars: list[float] = []
            latest_review_at: datetime | None = None
            next_token: str | None = None
            sample_complete = False
            pages_inspected = 0

            for page_number in range(self._max_review_pages):
                params = {"nextToken": next_token} if next_token else None
                try:
                    review_response = await client.get(
                        f"{self._base_url}/business-units/{business_unit_id}/reviews",
                        params=params,
                    )
                    if review_response.status_code == 403:
                        return self._unavailable("insights_api_access_required")
                    if review_response.status_code == 401:
                        return self._unavailable("api_authentication_failed")
                    if review_response.status_code == 429:
                        return self._unavailable("rate_limited")
                    if review_response.status_code == 404:
                        return self._not_found("trustpilot_service_reviews_unavailable")
                    if review_response.status_code >= 500:
                        return self._unavailable("provider_unavailable")
                    review_response.raise_for_status()
                    review_payload = review_response.json()
                except httpx.TimeoutException:
                    return self._unavailable("timeout")
                except httpx.HTTPError:
                    return self._unavailable("provider_request_failed")
                except (ValueError, TypeError):
                    return self._unavailable("invalid_provider_response")

                records = review_payload.get("reviews") if isinstance(review_payload, dict) else None
                if not isinstance(records, list):
                    return self._unavailable("invalid_provider_response")
                pages_inspected += 1
                for record in records:
                    if not isinstance(record, dict) or record.get("isVerified") is not True:
                        continue
                    # Store-level reviews describe a physical outlet, not the
                    # online seller's service across the business.
                    if record.get("reviewedLocationName"):
                        continue
                    stars = record.get("stars")
                    if isinstance(stars, bool) or not isinstance(stars, (int, float)):
                        continue
                    stars_float = float(stars)
                    if not math.isfinite(stars_float) or not 0 <= stars_float <= 5:
                        continue
                    verified_stars.append(stars_float)
                    parsed_date = self._parse_datetime(
                        record.get("experiencedAt") or record.get("createdAt")
                    )
                    if parsed_date and (latest_review_at is None or parsed_date > latest_review_at):
                        latest_review_at = parsed_date

                token = review_payload.get("nextToken")
                next_token = token if isinstance(token, str) and token else None
                if not next_token:
                    sample_complete = True
                    break

            count = len(verified_stars)
            rating = round(sum(verified_stars) / count, 4) if count else None
            provider = ProviderResult(
                provider_code=self.PROVIDER_CODE,
                capability="seller_reviews",
                status="checked",
                data={
                    "business_unit_id": business_unit_id,
                    "verified_reviews_inspected": count,
                    "pages_inspected": pages_inspected,
                    "sample_complete": sample_complete,
                },
                cache_policy=RetentionPolicy(cacheable=True, max_ttl_hours=24),
            )
            return ReviewEvidence(
                scope="seller",
                rating=rating,
                review_count=count,
                total_review_count=total_count,
                sample_complete=sample_complete,
                snippets=[],
                latest_review_at=latest_review_at,
                provider=provider,
            )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
            trust_env=False,
            headers={"apikey": self._api_key, "Accept": "application/json"},
            transport=self._transport,
        )

    @staticmethod
    def _matches_domain(payload: dict[str, Any], expected_domain: str) -> bool:
        name = payload.get("name") if isinstance(payload.get("name"), dict) else {}
        possible_values: list[str] = []
        for value in (name.get("identifying"), payload.get("websiteUrl")):
            if isinstance(value, str):
                possible_values.append(value)
        referring = name.get("referring")
        if isinstance(referring, list):
            possible_values.extend(value for value in referring if isinstance(value, str))
        return any(same_registered_domain(expected_domain, value) for value in possible_values)

    @staticmethod
    def _nested_nonnegative_int(payload: dict[str, Any], parent: str, key: str) -> int | None:
        value = payload.get(parent)
        if not isinstance(value, dict):
            return None
        number = value.get(key)
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            return None
        return number

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            return None

    @staticmethod
    def _unavailable(code: str) -> ReviewEvidence:
        return ReviewEvidence(
            scope="unknown",
            provider=ProviderResult(
                provider_code="trustpilot",
                capability="seller_reviews",
                status="unverified",
                error_code=code,
                error_message="Trustpilot seller-review evidence is unavailable.",
                cache_policy=RetentionPolicy(cacheable=False),
            ),
        )

    @staticmethod
    def _not_found(code: str) -> ReviewEvidence:
        return ReviewEvidence(
            scope="unknown",
            provider=ProviderResult(
                provider_code="trustpilot",
                capability="seller_reviews",
                status="not_found",
                error_code=code,
                error_message="No corroborated Trustpilot seller-review profile was found.",
                cache_policy=RetentionPolicy(cacheable=False),
            ),
        )
