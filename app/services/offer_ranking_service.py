
"""Deterministic trust-first offer eligibility and ranking.

The original 70% trust / 30% price-value ranking formula remains unchanged.

Automatic recommendation is a separate decision and requires BOTH:

1. Identity-oriented evidence:
   - Domain-corroborated business presence, OR
   - Reliable seller-scoped transaction reviews.

2. Overall evidence confidence of medium or high.

Offers failing the recommendation gate retain their ordinary numerical rank
for transparency, but cannot be automatically recommended.
"""

from dataclasses import dataclass
import math

from app.domain.models import Offer as ProductOffer
from app.mcp.schemas.trust import Trust


@dataclass(frozen=True)
class RankingCandidate:
    offer: ProductOffer
    trust: Trust


def _normalize_value(value) -> str:
    """Normalize plain strings and enum-like values."""
    if value is None:
        return ""

    value = getattr(value, "value", value)

    return str(value).strip().casefold()


def eligibility_reasons(
    candidate: RankingCandidate,
    budget: float,
) -> list[str]:
    """Return baseline reasons why an offer cannot be ranked."""
    offer = candidate.offer
    reasons: list[str] = []

    trust_score = candidate.trust.score

    if trust_score is None:
        reasons.append(
            "Seller is Unrated because available evidence does not meet "
            "the minimum scoring coverage."
        )
    elif trust_score < 2.5:
        reasons.append(
            "Seller trust score is below the 2.5 ranking eligibility threshold."
        )

    try:
        price = float(offer.price)
    except (TypeError, ValueError, OverflowError):
        price = math.nan

    if not math.isfinite(price) or price <= 0:
        reasons.append("Offer price is invalid.")
    elif price > budget:
        reasons.append(
            f"Offer exceeds the supplied budget of "
            f"{budget:.2f} {offer.currency}."
        )

    if (
        not offer.id
        or not offer.title
        or not offer.url
        or not offer.seller.seller_key
    ):
        reasons.append(
            "Required offer or seller identity fields are missing."
        )

    return reasons


def _eligibility(
    candidate: RankingCandidate,
    budget: float,
) -> str | None:
    reasons = eligibility_reasons(candidate, budget)

    return "; ".join(reasons) if reasons else None


def _get_factor_statuses(candidate: RankingCandidate) -> dict[str, str]:
    """Extract normalized factor statuses from the Trust object."""
    statuses: dict[str, str] = {}

    for factor in candidate.trust.factors:
        factor_name = str(factor.name).strip()
        statuses[factor_name] = _normalize_value(factor.status)

    return statuses


def recommendation_eligibility_reasons(
    candidate: RankingCandidate,
) -> list[str]:
    """Evaluate the stricter automatic recommendation safety gate.

    A seller must have at least one identity-oriented evidence source:

    - Business presence: accepted by the scoring policy and corroborated
      with the seller's registered domain.
    - Reviews: accepted as reliable seller-scoped transaction reviews by
      the existing review scoring policy.

    In addition, overall evidence confidence must be medium or high.

    Website signals and a neutral URL reputation result cannot independently
    establish seller identity.
    """
    reasons: list[str] = []

    factor_statuses = _get_factor_statuses(candidate)

    business_status = factor_statuses.get(
        "Business presence",
        "unverified",
    )

    reviews_status = factor_statuses.get(
        "Reviews",
        "unverified",
    )

    # Existing business scoring policy:
    # "ok" and "warn" represent accepted business evidence. The scoring
    # policy is responsible for requiring domain corroboration and a
    # qualifying business-name match before assigning these statuses.
    business_is_corroborated = business_status in {
        "ok",
        "warn",
    }

    # Existing review scoring policy:
    # "ok" is required. A "warn" review result does not independently
    # qualify the seller for automatic recommendation.
    seller_reviews_are_reliable = (
        reviews_status == "ok"
    )

    identity_evidence_available = (
        business_is_corroborated
        or seller_reviews_are_reliable
    )

    if not identity_evidence_available:
        reasons.append(
            "Automatic recommendation requires either a business listing "
            "corroborated with the seller's registered domain or reliable "
            "seller-scoped transaction reviews."
        )

    # This is an additional requirement, not a replacement for identity
    # evidence. A seller with valid identity evidence but low overall
    # confidence still cannot be automatically recommended.
    confidence = _normalize_value(
        candidate.trust.confidence
    )

    if confidence not in {"medium", "high"}:
        reasons.append(
            "Overall evidence confidence must be medium or high. "
            "Low-confidence assessments cannot receive automatic "
            "recommendations, even when one identity evidence source "
            "is available."
        )

    return reasons


def ranking_score(
    candidate: RankingCandidate,
    budget: float,
) -> float | None:
    """Calculate the unchanged 70% trust / 30% price-value score."""
    if budget <= 0:
        return None

    if _eligibility(candidate, budget) is not None:
        return None

    trust_score = candidate.trust.score
    assert trust_score is not None

    price = float(candidate.offer.price)

    trust_component = max(
        0.0,
        min(1.0, trust_score / 5.0),
    )

    price_component = max(
        0.0,
        min(1.0, (budget - price) / budget),
    )

    # Frozen ranking policy. Do not change these weights as part of
    # recommendation safety-gate changes.
    return (
        0.70 * trust_component
        + 0.30 * price_component
    )


def rank_candidates(
    candidates: list[RankingCandidate],
    budget: float,
) -> list[tuple[RankingCandidate, int | None, bool, str | None]]:
    """Rank eligible offers and recommend the best safety-gate passer.

    Returns:
        (candidate, rank, recommended, baseline_ineligibility_reason)

    The ordinary ranking position is independent of recommendation
    eligibility. Consequently, an offer can retain rank 1 while being
    blocked from automatic recommendation.
    """
    if budget <= 0:
        raise ValueError("budget must be greater than zero")

    eligible: list[RankingCandidate] = []
    ineligible: list[RankingCandidate] = []

    for candidate in candidates:
        if _eligibility(candidate, budget) is None:
            eligible.append(candidate)
        else:
            ineligible.append(candidate)

    # Preserve the original deterministic 70/30 ranking order.
    eligible.sort(
        key=lambda candidate: (
            -ranking_score(candidate, budget),
            -candidate.trust.score,
            float(candidate.offer.price),
            candidate.offer.id,
        )
    )

    # The recommended candidate is the highest-ranked offer satisfying
    # BOTH identity evidence and medium/high confidence.
    recommended_candidate = next(
        (
            candidate
            for candidate in eligible
            if not recommendation_eligibility_reasons(candidate)
        ),
        None,
    )

    result: list[
        tuple[RankingCandidate, int | None, bool, str | None]
    ] = []

    for index, candidate in enumerate(eligible, start=1):
        result.append(
            (
                candidate,
                index,
                candidate is recommended_candidate,
                None,
            )
        )

    # Preserve baseline-ineligible offers for transparency.
    for candidate in ineligible:
        result.append(
            (
                candidate,
                None,
                False,
                _eligibility(candidate, budget),
            )
        )

    return result


def tradeoff_note(
    candidate: RankingCandidate,
    budget: float,
    recommended: bool,
) -> str:
    """Explain ranking eligibility and recommendation decisions."""
    baseline_reasons = eligibility_reasons(
        candidate,
        budget,
    )

    if baseline_reasons:
        return (
            "Not ranked: "
            + " ".join(baseline_reasons)
        )

    if recommended:
        confidence = _normalize_value(
            candidate.trust.confidence
        )

        if candidate.trust.label != "Trusted":
            return (
                "Highest-ranked offer that passed both recommendation "
                "safety requirements. The seller is classified as "
                f"{candidate.trust.label} with {confidence} confidence. "
                "This is not a Trusted classification; review the available "
                "evidence before purchase."
            )

        return (
            "Recommended: highest-ranked offer that passed both the "
            "seller-identity evidence requirement and the medium/high "
            "overall confidence requirement under the 70/30 trust/value policy."
        )

    recommendation_reasons = recommendation_eligibility_reasons(
        candidate
    )

    if recommendation_reasons:
        return (
            "Not recommended automatically: "
            + " ".join(recommendation_reasons)
            + " The offer retains its ordinary 70/30 ranking position "
            "for transparency."
        )

    return (
        "Eligible for automatic recommendation, but another offer that "
        "also passed both recommendation safety requirements achieved "
        "a higher 70/30 trust/value ranking score."
    )
