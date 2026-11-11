"""Versioned, deterministic seller trust scoring policy.

The policy scores only evidence that directly measures the named group. Missing
or inconclusive evidence remains unverified and is excluded from the weighted
average. Provider timestamps, raw payloads, review text, and volatile metadata
are deliberately excluded from the evidence fingerprint.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.domain.models import SellerEvidence
from app.services.seller_identity import same_registered_domain

# Bump this whenever a normalization rule or evidence interpretation changes.
SCORING_VERSION = "2.3"

WEIGHTS: dict[str, float] = {
    "Business presence": 0.30,
    "Reviews": 0.30,
    "Website signals": 0.25,
    "URL reputation": 0.15,
}


def _unavailable_detail(provider: Any, default: str) -> str:
    """Return a safe provider diagnostic without leaking URLs or payloads."""
    error_code = getattr(provider, "error_code", None)
    if error_code:
        return f"{default} Provider check returned '{error_code}'."
    return default


def business_score(evidence: SellerEvidence) -> tuple[float | None, str, str]:
    """Score a physical-business record only after seller-domain corroboration."""
    item = evidence.business
    expected_domain = evidence.identity.normalized_domain

    if item.provider.status != "checked":
        return (
            None,
            "unverified",
            _unavailable_detail(
                item.provider,
                "Business-presence evidence could not be verified.",
            ),
        )

    if (
        not item.provider_place_id
        or item.match_confidence not in {"high", "medium"}
        or not item.official_website
        or not expected_domain
        or not same_registered_domain(expected_domain, item.official_website)
    ):
        return (
            None,
            "unverified",
            "No business listing was sufficiently corroborated with the seller's registered domain and identity.",
        )

    # A directory listing confirms a matching public business record. It does
    # not establish online order fulfilment or customer-service reliability.
    if item.match_confidence == "high":
        return (
            3.5,
            "warn",
            "A business listing has a strong name match and a corroborated merchant domain. This confirms business presence, not online transaction reliability.",
        )

    return (
        3.0,
        "warn",
        "A business listing has a corroborated merchant domain but only a medium name match. This is limited presence evidence.",
    )


def review_score(evidence: SellerEvidence) -> tuple[float | None, str, str]:
    """Score only reviews explicitly scoped to the online seller."""
    reviews = evidence.reviews or evidence.business.reviews
    if reviews is None:
        return None, "unverified", "Seller-specific service reviews are unavailable."

    provider = reviews.provider
    if provider is not None and provider.status != "checked":
        return (
            None,
            "unverified",
            _unavailable_detail(
                provider,
                "Seller-specific service reviews could not be verified.",
            ),
        )

    if reviews.scope != "seller":
        return (
            None,
            "unverified",
            "Available ratings describe a physical place or have unknown scope; they cannot represent online seller transactions.",
        )

    if reviews.rating is None or reviews.review_count is None or reviews.review_count <= 0:
        if provider is not None and provider.provider_code == "trustpilot":
            return (
                None,
                "unverified",
                "The seller's Trustpilot profile matched the merchant domain, but no verified service reviews were available in the inspected sample.",
            )
        return None, "unverified", "Seller-specific review rating or positive review volume is unavailable."

    rating = max(0.0, min(5.0, float(reviews.rating)))
    count = int(reviews.review_count)

    # Map the provider's 0–5 rating onto the trust factor's 1–5 scale.
    score = 1.0 + (rating / 5.0) * 4.0
    is_trustpilot = bool(provider and provider.provider_code == "trustpilot")
    source = "Trustpilot verified service-review sample" if is_trustpilot else "Seller-specific rating"
    count_label = "verified reviews" if is_trustpilot else "reviews"
    sample_note = " The inspected sample is partial." if not reviews.sample_complete else ""
    total_note = (
        f" The profile lists {reviews.total_review_count} total reviews; only verified service reviews were used."
        if provider and provider.provider_code == "trustpilot" and reviews.total_review_count is not None
        else ""
    )

    if count < 10:
        score = min(score, 3.5)
        return (
            round(score, 2),
            "warn",
            f"{source} is {rating:.1f}/5 from {count} {count_label}. The sample is below the 10-review reliability threshold and is capped at 3.5.{sample_note}{total_note}",
        )

    status = "ok" if rating >= 3.5 else "warn"
    return (
        round(score, 2),
        status,
        f"{source} is {rating:.1f}/5 from {count} {count_label}.{sample_note}{total_note}",
    )


def _contact_presence(evidence: Any) -> bool | None:
    """Resolve top-level and component contact checks without false negatives."""
    component_values = (
        evidence.contact_email_present,
        evidence.contact_phone_present,
        evidence.contact_address_present,
    )

    # Any positively observed contact method proves some contact information
    # is present, even if a broader provider flag is inconsistent.
    if evidence.contact_present is True or any(value is True for value in component_values):
        return True

    if evidence.contact_present is False:
        return False

    observed_components = [value for value in component_values if value is not None]
    if observed_components and all(value is False for value in observed_components):
        # At least one contact check ran, but we cannot claim the entire site
        # lacks contact details unless all supported contact checks ran.
        if len(observed_components) == len(component_values):
            return False

    return None


def _policy_presence(evidence: Any) -> bool | None:
    """Return true/false only when policy evidence is conclusive."""
    values = (evidence.return_policy_present, evidence.refund_policy_present)
    if any(value is True for value in values):
        return True
    if all(value is False for value in values):
        return False
    # One missing page/check must not be interpreted as absence of all policy.
    return None


def website_score(evidence: SellerEvidence) -> tuple[float | None, str, str]:
    """Score meaningful website evidence, not duplicated transport checks.

    HTTPS reachability and TLS validity form one transport-security group. A
    positive transport result alone is not evidence of business legitimacy.
    Scoring therefore requires at least two observed groups and at least one
    commercial-information group (contact or policy).
    """
    website = evidence.website
    if website.provider.status != "checked":
        return (
            None,
            "unverified",
            _unavailable_detail(
                website.provider,
                "Merchant website signals could not be checked.",
            ),
        )

    # A completed HTTPS/TLS check that explicitly fails is negative evidence.
    # It remains a scoreable website group even if other checks are unavailable.
    if website.https_reachable is False or website.tls_valid is False:
        failed_controls = []
        if website.https_reachable is False:
            failed_controls.append("HTTPS reachability")
        if website.tls_valid is False:
            failed_controls.append("TLS certificate validation")
        return (
            1.5,
            "bad",
            "The completed website check failed " + " and ".join(failed_controls) + ".",
        )

    transport_observed = (
        website.https_reachable is not None or website.tls_valid is not None
    )
    contact = _contact_presence(website)
    policy = _policy_presence(website)
    commercial_groups_observed = int(contact is not None) + int(policy is not None)
    independent_groups_observed = int(transport_observed) + commercial_groups_observed

    if independent_groups_observed < 2 or commercial_groups_observed == 0:
        return (
            None,
            "unverified",
            "Insufficient independent website evidence. HTTPS reachability and TLS validity count as one transport-security group; contact or policy evidence is also required.",
        )

    # The neutral baseline does not reward HTTPS/TLS. Only separately observed
    # commercial-information controls influence the numeric factor score.
    score = 3.0
    if contact is True:
        score += 0.5
    elif contact is False:
        score -= 0.25

    if policy is True:
        score += 0.5
    elif policy is False:
        score -= 0.25

    score = round(max(1.0, min(4.0, score)), 2)
    if contact is True and policy is True:
        status = "ok"
        detail = (
            "Contact information and return/refund policy evidence were observed. "
            "HTTPS/TLS are treated as baseline transport controls, not proof of seller legitimacy."
        )
    else:
        status = "warn"
        detail = (
            "Website evidence is partial. Only observed contact and policy controls affect the score; "
            "HTTPS/TLS do not independently increase trust. Uninspected policy checks remain unknown."
        )

    return score, status, detail


def reputation_score(evidence: SellerEvidence) -> tuple[float | None, str, str]:
    """Score explicit results from an enabled, authorized URL-reputation check."""
    reputation = evidence.reputation
    if reputation.provider.status != "checked" or reputation.is_malicious is None:
        return (
            None,
            "unverified",
            _unavailable_detail(
                reputation.provider,
                "Authorized URL reputation evidence is unavailable.",
            ),
        )

    if reputation.is_malicious:
        return (
            1.0,
            "bad",
            "The enabled and authorized reputation provider returned a confirmed malicious URL indicator.",
        )

    return (
        3.0,
        "warn",
        "No known URL threat indicator was returned. This is a neutral, limited-scope result—not positive proof that the seller or website is safe.",
    )


def factor_scores(
    evidence: SellerEvidence,
) -> dict[str, tuple[float | None, str, str]]:
    """Return all four factor diagnostics in the fixed contract order."""
    return {
        "Business presence": business_score(evidence),
        "Reviews": review_score(evidence),
        "Website signals": website_score(evidence),
        "URL reputation": reputation_score(evidence),
    }


def evidence_fingerprint(evidence: SellerEvidence) -> str:
    """Hash only scoring-relevant normalized signals, not volatile metadata.

    The fingerprint is stable across repeated checks that observe the same
    scoring evidence. It deliberately excludes provider timestamps, raw review
    snippets, raw provider payloads, source references, and transport metadata.
    """
    business = evidence.business
    reviews = evidence.reviews or business.reviews
    website = evidence.website
    reputation = evidence.reputation

    payload = {
        "scoring_version": SCORING_VERSION,
        "identity": {
            "seller_key": evidence.identity.seller_key,
            "normalized_domain": evidence.identity.normalized_domain,
            "identity_status": evidence.identity.identity_status,
        },
        "business": {
            "provider_status": business.provider.status,
            "provider_place_id": business.provider_place_id,
            "match_confidence": business.match_confidence,
            "official_website": business.official_website,
        },
        "reviews": None if reviews is None else {
            "scope": reviews.scope,
            "rating": reviews.rating,
            "review_count": reviews.review_count,
            "total_review_count": reviews.total_review_count,
            "sample_complete": reviews.sample_complete,
            "provider_code": reviews.provider.provider_code if reviews.provider else None,
            "provider_status": reviews.provider.status if reviews.provider else None,
        },
        "website": {
            "provider_status": website.provider.status,
            "https_reachable": website.https_reachable,
            "tls_valid": website.tls_valid,
            "contact_present": website.contact_present,
            "contact_email_present": website.contact_email_present,
            "contact_phone_present": website.contact_phone_present,
            "contact_address_present": website.contact_address_present,
            "return_policy_present": website.return_policy_present,
            "refund_policy_present": website.refund_policy_present,
        },
        "reputation": {
            "provider_status": reputation.provider.status,
            "is_malicious": reputation.is_malicious,
        },
    }

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
