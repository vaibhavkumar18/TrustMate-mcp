"""Orchestrate independent seller-evidence providers without masking failures."""

import asyncio

import logging

import time

from datetime import datetime, timezone

from typing import Awaitable, TypeVar

from app.domain.interfaces import BusinessProvider, DomainProvider, ReputationProvider, ReviewProvider, WebsiteProvider

from app.domain.models import (

    BusinessEvidence,

    DomainEvidence,

    ProviderResult,

    ReputationEvidence,

    ReviewEvidence,

    RetentionPolicy,

    SellerEvidence,

    SellerIdentity,

    WebsiteEvidence,

)

from app.services.seller_identity import (

    business_lookup_name,

    normalize_seller,

    same_registered_domain,

)

_T = TypeVar("_T")

logger = logging.getLogger(__name__)

async def _safe(

    awaitable: Awaitable[_T],

    fallback: _T,

    errors: list[str],

    name: str,

    timeout: float = 10.0,

) -> _T:

    """Bound a provider call and preserve a safe diagnostic on exceptions."""

    started = time.perf_counter()

    try:

        result = await asyncio.wait_for(awaitable, timeout=timeout)

        provider_result = getattr(result, "provider", None)

        logger.info(

            "seller_provider_check_complete",

            extra={

                "provider_check": name,

                "provider_status": getattr(provider_result, "status", "unknown"),

                "provider_error_code": getattr(provider_result, "error_code", None),

                "duration_ms": round((time.perf_counter() - started) * 1000),

            },

        )

        return result

    except asyncio.CancelledError:

        raise

    except asyncio.TimeoutError:

        errors.append(f"{name}: TimeoutError")

        logger.warning(

            "seller_provider_check_timeout",

            extra={

                "provider_check": name,

                "duration_ms": round((time.perf_counter() - started) * 1000),

            },

        )

        return _fallback_with_error(fallback, "timeout", "Provider check timed out.")

    except Exception as exc:

        # Never copy exception text: HTTP clients may include URLs or

        # provider-specific response details. Type names are safe diagnostics.

        errors.append(f"{name}: {type(exc).__name__}")

        logger.warning(

            "seller_provider_check_failed",

            extra={

                "provider_check": name,

                "error_type": type(exc).__name__,

                "duration_ms": round((time.perf_counter() - started) * 1000),

            },

        )

        return _fallback_with_error(

            fallback, "provider_error", "Provider check failed and remains unverified."

        )

def _fallback_with_error(value: _T, error_code: str, detail: str) -> _T:

    """Attach a safe error code to typed fallback evidence when possible."""

    provider = getattr(value, "provider", None)

    if provider is None or not hasattr(provider, "model_copy"):

        return value

    updated_provider = provider.model_copy(

        update={

            "status": "unverified",

            "error_code": error_code,

            "error_message": detail,

        }

    )

    if hasattr(value, "model_copy"):

        return value.model_copy(update={"provider": updated_provider})

    return value

def _unverified_provider(

    capability: str,

    detail: str,

    *,

    error_code: str | None = None,

) -> ProviderResult:

    return ProviderResult(

        provider_code="orchestrator",

        capability=capability,

        status="unverified",

        observed_at=datetime.now(timezone.utc),

        cache_policy=RetentionPolicy(cacheable=False),

        error_code=error_code,

        error_message=detail,

    )

def _merchant_business_match(

    result: BusinessEvidence | None,

    expected_domain: str | None,

) -> bool:

    """Require a provider record and exact registrable-domain corroboration."""

    if (

        result is None

        or result.provider.status != "checked"

        or not result.provider_place_id

        or result.match_confidence not in {"high", "medium"}

        or not expected_domain

        or not result.official_website

    ):

        return False

    return same_registered_domain(expected_domain, result.official_website)

_COUNTRY_NAMES: dict[str, str] = {
    "US": "United States",
    "CA": "Canada",
    "GB": "United Kingdom",
    "UK": "United Kingdom",
    "IE": "Ireland",
    "AU": "Australia",
    "DE": "Germany",
    "AT": "Austria",
    "FR": "France",
    "BE": "Belgium",
    "IT": "Italy",
    "ES": "Spain",
    "NL": "Netherlands",
    "SE": "Sweden",
    "FI": "Finland",
    "PT": "Portugal",
    "CZ": "Czechia",
    "GR": "Greece",
    "RO": "Romania",
    "CH": "Switzerland",
    "PL": "Poland",
    "EU": "Europe",
}

def _country_hint(target_country: str | None) -> str | None:
    """Convert a marketplace country code to a geocodable name for fallback search."""
    if not target_country:
        return None
    value = target_country.strip()
    return _COUNTRY_NAMES.get(value.upper(), value)


_COUNTRY_CODE_TLDS: dict[str, str] = {
    "uk": "United Kingdom", "de": "Germany", "fr": "France",
    "ca": "Canada", "au": "Australia", "ie": "Ireland",
    "nl": "Netherlands", "se": "Sweden", "fi": "Finland",
    "pt": "Portugal", "cz": "Czechia", "gr": "Greece",
    "ro": "Romania", "ch": "Switzerland", "pl": "Poland",
    "nz": "New Zealand", "jp": "Japan", "sg": "Singapore",
    "in": "India", "br": "Brazil", "mx": "Mexico",
}
_MULTI_LABEL_TLD_HINTS: dict[str, str] = {
    "co.uk": "United Kingdom", "org.uk": "United Kingdom",
    "com.au": "Australia", "net.au": "Australia",
    "co.nz": "New Zealand", "co.jp": "Japan",
    "com.sg": "Singapore", "com.br": "Brazil",
}

def _seller_location_hint(
    seller_domain: str | None,
    target_country: str | None,
    configured_hints: dict[str, str] | None,
) -> tuple[str | None, str]:
    """Resolve a Foursquare search locality without pretending the market is HQ.

    Explicit domain overrides win. Country-code domains are a weak discovery
    hint only. The shopping market is the final fallback, and strict official
    domain corroboration remains mandatory before business evidence is accepted.
    """
    if seller_domain and configured_hints:
        for configured_domain, locality in configured_hints.items():
            if isinstance(configured_domain, str) and isinstance(locality, str):
                if same_registered_domain(seller_domain, configured_domain) and locality.strip():
                    return locality.strip(), "configured_seller_locality"

    if seller_domain:
        normalized = seller_domain.strip().lower().rstrip(".")
        labels = normalized.split(".")
        for suffix, country in _MULTI_LABEL_TLD_HINTS.items():
            if normalized.endswith("." + suffix):
                return country, "seller_domain_country_hint"
        if labels and labels[-1] in _COUNTRY_CODE_TLDS:
            return _COUNTRY_CODE_TLDS[labels[-1]], "seller_domain_country_hint"

    market_hint = _country_hint(target_country)
    if market_hint:
        return market_hint, "target_market_fallback"
    return None, "location_unavailable"

def _name_only_identity(seller_name: str) -> SellerIdentity:

    """Represent an invalid/untrusted URL without reusing its host as identity."""

    display_name = seller_name.strip() if isinstance(seller_name, str) else "Unknown seller"

    if not display_name:

        display_name = "Unknown seller"

    return SellerIdentity(

        display_name=display_name,

        seller_key=f"name:{display_name.casefold()}",

        identity_status="name_only",

    )

def _diagnostic_business_result(

    result: BusinessEvidence | None,

    *,

    detail: str,

) -> BusinessEvidence:

    """Return a non-scorable identity diagnostic without leaking a bad match.

    Provider transport/authentication failures are preserved unchanged. A

    completed search with a name/domain mismatch is represented as unverified

    and does not expose the mismatched place's ID, address, or contact fields.

    """

    if result is None:

        return BusinessEvidence(

            provider=_unverified_provider(

                "business", detail, error_code="business_evidence_unavailable"

            )

        )

    if result.provider.status in {"unverified", "error"}:

        return result

    if result.provider.status == "not_found":

        return BusinessEvidence(

            provider=result.provider.model_copy(

                update={

                    "error_code": result.provider.error_code or "no_matching_business",

                    "error_message": result.provider.error_message or detail,

                }

            ),

            attribution=result.attribution,

        )

    # The provider returned a candidate, but it could not be corroborated to

    # the merchant's registrable domain. Do not pass candidate data downstream.

    return BusinessEvidence(

        provider=ProviderResult(

            provider_code=result.provider.provider_code,

            capability="business",

            status="unverified",

            observed_at=result.provider.observed_at,

            data={"identity_corroborated": False},

            source_reference=None,

            cache_policy=result.provider.cache_policy,

            error_code="identity_not_corroborated",

            error_message=detail,

        ),

        attribution=result.attribution,

    )

def _record_provider_issue(

    errors: list[str],

    name: str,

    provider_result: ProviderResult,

) -> None:

    """Capture unverified provider status without converting it to negative evidence."""

    if provider_result.status in {"unverified", "error"}:

        code = provider_result.error_code or provider_result.status

        diagnostic = f"{name}: {code}"

        if diagnostic not in errors:

            errors.append(diagnostic)

async def _immediate(value: _T) -> _T:

    return value

async def collect_seller_evidence(

    seller_name: str,

    seller_url: str | None,

    business: BusinessProvider,

    business_fallback: BusinessProvider,

    domain: DomainProvider,

    website: WebsiteProvider,

    reputation: ReputationProvider,

    *,

    target_country: str | None = None,

    seller_location_hints: dict[str, str] | None = None,

    allow_public_fallback: bool = True,

    reviews_provider: ReviewProvider | None = None,

    prevalidated_identity: SellerIdentity | None = None,

) -> SellerEvidence:

    """Collect independent evidence against the seller's validated domain.

    Channel3's affiliate ``offer.url`` must never be passed as ``seller_url``;

    use the merchant's normalized domain/canonical URL. Business, RDAP,

    website, and reputation checks run concurrently because none depends on

    another provider's result. OSM/Nominatim fallback runs afterward only when

    explicitly allowed (single-seller/user-triggered flows), preserving its

    public one-request-per-second limit.

    """

    errors: list[str] = []


    try:
        if prevalidated_identity is not None:
            # A cache lookup may already have performed the public-host and
            # seller-identity checks. Reuse that exact validated identity so a
            # cache miss does not repeat synchronous DNS resolution.
            identity = prevalidated_identity
        else:
            # DNS resolution is synchronous in the identity guard. Run it off
            # the event loop so one slow resolver does not block provider tasks.
            identity = await asyncio.to_thread(
                normalize_seller,
                seller_name,
                seller_url,
                allow_synthetic=type(website).__name__ == "MockWebsiteProvider",
            )
    except (TypeError, ValueError):
        identity = _name_only_identity(seller_name)
        errors.append("identity: invalid_merchant_url")

    domain_name = identity.normalized_domain

    canonical_url = identity.canonical_url

    if not domain_name:

        business_task = _immediate(

            BusinessEvidence(

                provider=_unverified_provider(

                    "business",

                    "Merchant domain is unavailable; business identity cannot be corroborated.",

                    error_code="merchant_domain_missing",

                )

            )

        )

        domain_task = _immediate(

            DomainEvidence(

                provider=_unverified_provider(

                    "domain", "Merchant domain is unavailable.",

                    error_code="merchant_domain_missing",

                ),

                domain="",

            )

        )

        website_task = _immediate(

            WebsiteEvidence(

                provider=_unverified_provider(

                    "website", "Merchant website cannot be identified safely.",

                    error_code="merchant_domain_missing",

                )

            )

        )

        reputation_task = _immediate(

            ReputationEvidence(

                provider=_unverified_provider(

                    "reputation", "Merchant URL reputation cannot be checked safely.",

                    error_code="merchant_domain_missing",

                )

            )

        )
        reviews_task = _immediate(None)

    else:

        lookup_name = business_lookup_name(seller_name, domain_name)
        location_hint, location_hint_source = _seller_location_hint(
            domain_name, target_country, seller_location_hints
        )
        logger.info(
            "seller_business_location_resolved",
            extra={
                "provider_check": "business",
                "location_hint_source": location_hint_source,
                "location_hint_used": bool(location_hint),
            },
        )
        # Foursquare's `near` is only a retrieval hint. Prefer an explicit
        # merchant-locality override, then a weak ccTLD hint, then the shopping
        # market fallback. Cross-border .com merchants with no known locality
        # should be added to SELLER_LOCATION_HINTS_JSON; do not infer HQ from
        # target_country. A candidate is still accepted only after exact
        # registrable-domain corroboration.
        business_task = business.search_business(
            lookup_name, domain_name, location_hint
        )

        domain_task = domain.lookup_domain(domain_name)

        website_task = website.inspect(canonical_url)

        reputation_task = reputation.check_url(canonical_url)
        reviews_task = (
            reviews_provider.lookup_seller_reviews(domain_name, target_country)
            if reviews_provider is not None
            else _immediate(None)
        )

    # Start all independent provider checks in the same event-loop turn. The

    # previous implementation waited for Foursquare before starting the other

    # three checks, adding avoidable wall-clock latency to every seller.

    business_result, domain_result, website_result, reputation_result, reviews_result = await asyncio.gather(

        _safe(

            business_task,

            BusinessEvidence(

                provider=_unverified_provider(

                    "business", "Business evidence unavailable.",

                    error_code="business_evidence_unavailable",

                )

            ),

            errors,

            "business",

        ),

        _safe(

            domain_task,

            DomainEvidence(

                provider=_unverified_provider(

                    "domain", "Domain evidence unavailable.",

                    error_code="domain_evidence_unavailable",

                ),

                domain=domain_name or "",

            ),

            errors,

            "domain",

        ),

        _safe(

            website_task,

            WebsiteEvidence(

                provider=_unverified_provider(

                    "website", "Website evidence unavailable.",

                    error_code="website_evidence_unavailable",

                )

            ),

            errors,

            "website",

        ),

        _safe(

            reputation_task,

            ReputationEvidence(

                provider=_unverified_provider(

                    "reputation", "URL reputation evidence unavailable.",

                    error_code="reputation_evidence_unavailable",

                )

            ),

            errors,

            "reputation",

        ),
        _safe(
            reviews_task,
            None,
            errors,
            "reviews",
        ),

    )

    _record_provider_issue(errors, "business", business_result.provider)

    _record_provider_issue(errors, "domain", domain_result.provider)

    _record_provider_issue(errors, "website", website_result.provider)

    _record_provider_issue(errors, "reputation", reputation_result.provider)
    if reviews_result is not None and reviews_result.provider is not None:
        _record_provider_issue(errors, "reviews", reviews_result.provider)

    if domain_name and not _merchant_business_match(business_result, domain_name):

        primary_result = business_result

        primary_detail = (

            "A business listing was returned, but its name/official website could not be corroborated to the merchant domain."

        )

        business_result = _diagnostic_business_result(

            primary_result,

            detail=primary_detail,

        )

        if allow_public_fallback:

            lookup_name = business_lookup_name(seller_name, domain_name)

            fallback_result = await _safe(

                business_fallback.search_business(

                    lookup_name,

                    domain_name,

                    location_hint,

                ),

                BusinessEvidence(

                    provider=_unverified_provider(

                        "business", "Fallback business evidence unavailable.",

                        error_code="fallback_unavailable",

                    )

                ),

                errors,

                "business_fallback",

            )

            _record_provider_issue(errors, "business_fallback", fallback_result.provider)

            if _merchant_business_match(fallback_result, domain_name):

                business_result = fallback_result

    # RDAP age remains informational. Provider failures stay unverified and

    # do not turn into negative trust evidence.

    return SellerEvidence(

        identity=identity,

        business=business_result,

        domain=domain_result,

        website=website_result,

        reputation=reputation_result,
        reviews=reviews_result,

        errors=errors,

    )
