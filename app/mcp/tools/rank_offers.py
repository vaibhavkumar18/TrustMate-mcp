
import asyncio
import logging
import time
from collections import Counter

from app.core.config import get_settings
from app.domain.policies.scoring_policy import SCORING_VERSION
from app.mcp.schemas.inputs import RankOffersInput
from app.mcp.schemas.offers import Offer, RankOffersOutput
from app.mcp.schemas.trust import Trust, TrustFactor
from app.providers.factory import ProviderRegistry, create_provider_registry
from app.services.offer_ranking_service import (
    RankingCandidate,
    rank_candidates,
    recommendation_eligibility_reasons,
    tradeoff_note,
)
from app.services.seller_assessment_cache import (
    assessment_cache_key,
    validated_cache_identity,
)
from app.services.seller_verification_service import collect_seller_evidence
from app.services.trust_scoring_service import assess_evidence

logger = logging.getLogger(__name__)


_ERROR_CODE_MESSAGES: dict[str, str] = {
    "timeout": "the request to the product provider timed out",
    "transport_error": "a network error occurred while contacting the product provider",
    "rate_limited": "the product provider rate-limited this request",
    "authentication_failed": "the product provider rejected the API credentials",
    "malformed_json": "the product provider returned an unreadable response",
    "malformed_response": "the product provider returned an unexpected response format",
    "request_failed": "the product provider could not complete the request",
    "unsupported_locale": (
        "the requested country or currency is not supported by the product provider "
        "for this market"
    ),
    "invalid_budget": "the product provider received an invalid budget",
}


def _unrated(detail: str) -> Trust:
    return Trust(
        score=None,
        label="Unrated",
        confidence="low",
        factors=[
            TrustFactor(
                name="Business presence",
                status="unverified",
                detail=detail,
            ),
            TrustFactor(
                name="Reviews",
                status="unverified",
                detail="Seller-specific reviews are unavailable.",
            ),
            TrustFactor(
                name="Website signals",
                status="unverified",
                detail="Merchant website evidence is unavailable.",
            ),
            TrustFactor(
                name="URL reputation",
                status="unverified",
                detail="Authorized URL reputation evidence is unavailable.",
            ),
        ],
    )


def _discovery_failure_note(error_code: str | None) -> str:
    reason = _ERROR_CODE_MESSAGES.get(error_code or "", "")

    if reason:
        return (
            f"Product discovery is currently unavailable because {reason}."
        )

    if error_code and error_code.startswith("http_"):
        try:
            code = int(error_code.split("_", 1)[1])
        except (ValueError, IndexError):
            code = 0

        if 500 <= code < 600:
            return (
                "Product discovery is unavailable because the product "
                "provider returned a server error."
            )

        if 400 <= code < 500:
            return (
                "Product discovery is unavailable because the product "
                "provider rejected the request."
            )

    return (
        "Product discovery is currently unavailable because the product "
        "provider could not complete the request."
    )


def _currency_skip_note(currency: str) -> str:
    return (
        f"Some offers were skipped because their currency was not explicitly "
        f"{currency}."
    )


def _recommendation_gate_profile(
    candidate: RankingCandidate,
) -> tuple[bool, bool]:
    """Return (has_identity_evidence, has_required_confidence).

    Keep these conditions aligned with recommendation_eligibility_reasons()
    in offer_ranking_service.py:

    - Business presence status is ok/warn OR Reviews status is ok.
    - Overall confidence is medium/high.

    This function only classifies the reason an offer was blocked.
    The actual recommendation decision remains in rank_candidates().
    """
    factor_statuses: dict[str, str] = {}

    for factor in candidate.trust.factors:
        status = getattr(factor.status, "value", factor.status)

        factor_statuses[str(factor.name).strip()] = (
            str(status).strip().casefold()
        )

    business_status = factor_statuses.get(
        "Business presence",
        "unverified",
    )

    reviews_status = factor_statuses.get(
        "Reviews",
        "unverified",
    )

    has_identity_evidence = (
        business_status in {"ok", "warn"}
        or reviews_status == "ok"
    )

    confidence = getattr(
        candidate.trust.confidence,
        "value",
        candidate.trust.confidence,
    )

    has_required_confidence = (
        str(confidence).strip().casefold() in {"medium", "high"}
    )

    return has_identity_evidence, has_required_confidence


async def rank_offers(
    input_data: RankOffersInput,
    registry: ProviderRegistry | None = None,
    assessment_cache=None,
) -> RankOffersOutput:
    settings = get_settings()
    registry = registry or create_provider_registry(settings)

    seller_location_hints = settings.seller_location_hints

    started_at = time.monotonic()
    currency = input_data.currency.upper()
    target_country = input_data.target_country.upper()

    # ---------------------------------------------------------
    # 1. PRODUCT DISCOVERY
    # ---------------------------------------------------------

    try:
        product_result = await registry.product.search(
            input_data.query,
            input_data.budget,
            target_country=target_country,
            currency=currency,
        )

    except Exception as exc:
        logger.exception(
            "product_discovery_exception",
            extra={"query": input_data.query},
        )

        return RankOffersOutput(
            target_country=target_country,
            offers=[],
            unverified_notes=[
                "Product discovery failed before seller verification "
                f"could start ({type(exc).__name__})."
            ],
        )

    provider_status = product_result.provider.status
    skipped = product_result.provider.data.get("skipped", {})

    logger.info(
        "product_discovery_complete",
        extra={
            "provider": product_result.provider.provider_code,
            "provider_status": provider_status,
            "product_count": len(product_result.products),
            "offer_count": len(product_result.offers),
            "query": input_data.query,
            "budget": input_data.budget,
            "currency": currency,
            "target_country": target_country,
        },
    )

    if provider_status == "error":
        notes = [
            _discovery_failure_note(
                product_result.provider.error_code
            )
        ]

        if skipped.get("unsupported_or_ambiguous_currency"):
            notes.append(_currency_skip_note(currency))

        return RankOffersOutput(
            target_country=target_country,
            offers=[],
            unverified_notes=notes,
        )

    offers = product_result.offers

    if not offers:
        note = (
            "No offers matched the product query, target market, currency "
            "and supplied budget."
            if provider_status == "checked"
            else "The product provider did not return usable offers."
        )

        return RankOffersOutput(
            target_country=target_country,
            offers=[],
            unverified_notes=[note],
        )

    # ---------------------------------------------------------
    # 2. SELLER VERIFICATION
    # ---------------------------------------------------------
    # Verify each unique seller once.
    # Maximum four concurrent provider requests.
    #
    # Public Nominatim is deliberately not fanned out across all
    # sellers in a multi-offer search.

    trust_by_seller: dict[str, Trust] = {}
    tasks: dict[str, asyncio.Task] = {}

    semaphore = asyncio.Semaphore(4)

    async def verify_seller(product_offer):
        async with semaphore:
            identity = await validated_cache_identity(
                product_offer.seller.display_name,
                product_offer.seller.canonical_url,
                registry.website,
            )
            cache_key = None
            if assessment_cache is not None and identity is not None:
                cache_key = assessment_cache_key(
                    assessment_cache,
                    seller_key=identity.seller_key,
                    target_country=target_country,
                    settings=settings,
                    registry=registry,
                )
                cached_trust = await assessment_cache.get_assessment(cache_key)
                if cached_trust is not None:
                    logger.info(
                        "seller_assessment_cache_hit",
                        extra={"cache_scope": "rank_offers", "seller_key": identity.seller_key},
                    )
                    return cached_trust
                logger.info(
                    "seller_assessment_cache_miss",
                    extra={"cache_scope": "rank_offers", "seller_key": identity.seller_key},
                )

            evidence = await collect_seller_evidence(
                product_offer.seller.display_name,
                product_offer.seller.canonical_url,
                registry.business,
                registry.business_fallback,
                registry.domain,
                registry.website,
                registry.reputation,
                target_country=target_country,
                seller_location_hints=seller_location_hints,
                allow_public_fallback=False,
                reviews_provider=registry.reviews,
                prevalidated_identity=identity,
            )
            trust = assess_evidence(evidence)
            if assessment_cache is not None and cache_key is not None and trust.score is not None:
                await assessment_cache.put_assessment(
                    cache_key=cache_key,
                    seller_key=evidence.identity.seller_key,
                    seller_name=evidence.identity.display_name,
                    normalized_domain=evidence.identity.normalized_domain,
                    target_country=target_country,
                    scoring_version=SCORING_VERSION,
                    trust=trust,
                )
            return trust

    representatives = {}

    for product_offer in offers:
        seller_key = product_offer.seller.seller_key

        representatives.setdefault(
            seller_key,
            product_offer,
        )

    for seller_key, product_offer in representatives.items():
        tasks[seller_key] = asyncio.create_task(
            verify_seller(product_offer)
        )

    if tasks:
        keys = list(tasks)

        results = await asyncio.gather(
            *(tasks[key] for key in keys),
            return_exceptions=True,
        )

        for seller_key, trust in zip(keys, results):
            if isinstance(trust, Exception):
                logger.warning(
                    "seller_evidence_failed",
                    extra={
                        "seller_key": seller_key,
                        "error_type": type(trust).__name__,
                    },
                )
                trust_by_seller[seller_key] = _unrated(
                    "Seller evidence collection failed; no score was assigned."
                )
            else:
                trust_by_seller[seller_key] = trust

    # ---------------------------------------------------------
    # 3. TRUST SCORING, RANKING AND RECOMMENDATION
    # ---------------------------------------------------------
    # Trust scoring and ranking remain deterministic.
    # OpenRouter is not involved.
    #
    # The 70/30 formula is unchanged.
    # Recommendation requires both:
    #   A. Identity-oriented evidence.
    #   B. Medium or high overall confidence.

    candidates = [
        RankingCandidate(
            offer,
            trust_by_seller.get(
                offer.seller.seller_key,
                _unrated(
                    "Seller evidence was not available; no score was assigned."
                ),
            ),
        )
        for offer in offers
    ]

    ranked = rank_candidates(
        candidates,
        input_data.budget,
    )

    output: list[Offer] = []

    rejection_counts: Counter[str] = Counter()

    # Mutually exclusive recommendation-gate failure categories:
    #
    # identity_and_confidence:
    #   No identity evidence AND low confidence.
    #
    # identity_only:
    #   Medium/high confidence BUT no identity evidence.
    #
    # confidence_only:
    #   Identity evidence exists BUT confidence is low.
    #
    # These counts partition the offers blocked by the recommendation gate.

    recommendation_block_counts: Counter[str] = Counter()

    for candidate, position, recommended, reason in ranked:
        output_offer = Offer(
            id=candidate.offer.id,
            title=candidate.offer.title,
            price=candidate.offer.price,
            currency=candidate.offer.currency,
            url=candidate.offer.url,
            seller=candidate.offer.seller.display_name,
            trust=candidate.trust,
            rank=position,
            recommended=recommended,
            tradeoff_note=tradeoff_note(
                candidate,
                input_data.budget,
                recommended,
            ),
        )

        output.append(output_offer)

        # Baseline ranking ineligibility is separate from recommendation
        # safety-gate ineligibility.
        if reason:
            if candidate.trust.score is None:
                rejection_counts["unrated"] += 1

            elif candidate.trust.score < 2.5:
                rejection_counts["below_trust_threshold"] += 1

            elif candidate.offer.price > input_data.budget:
                rejection_counts["over_budget"] += 1

            else:
                rejection_counts["other_ineligible"] += 1

            continue

        if recommended:
            continue

        gate_reasons = recommendation_eligibility_reasons(candidate)

        if not gate_reasons:
            # The offer passed the safety gate but another passing offer
            # achieved a higher ranking score.
            continue

        has_identity_evidence, has_required_confidence = (
            _recommendation_gate_profile(candidate)
        )

        if (
            not has_identity_evidence
            and not has_required_confidence
        ):
            recommendation_block_counts[
                "identity_and_confidence"
            ] += 1

        elif not has_identity_evidence:
            recommendation_block_counts[
                "identity_only"
            ] += 1

        elif not has_required_confidence:
            recommendation_block_counts[
                "confidence_only"
            ] += 1

        else:
            # Defensive fallback if the service policy and this diagnostic
            # profile ever diverge.
            recommendation_block_counts[
                "other_policy"
            ] += 1

    # ---------------------------------------------------------
    # 4. SUMMARY COUNTS
    # ---------------------------------------------------------

    eligible_count = sum(
        1
        for item in output
        if item.rank is not None
    )

    recommended_count = sum(
        1
        for item in output
        if item.recommended
    )

    recommendation_blocked_count = sum(
        recommendation_block_counts.values()
    )

    recommended_offer = next(
        (
            item
            for item in output
            if item.recommended
        ),
        None,
    )

    # ---------------------------------------------------------
    # 5. LIMITATION AND DECISION NOTES
    # ---------------------------------------------------------

    notes: list[str] = []

    if eligible_count == 0:
        notes.append(
            f"{len(output)} offers were discovered, but none passed "
            "the standard trust, price and offer-field eligibility checks. "
            "No offer was ranked or recommended."
        )

    elif recommended_count == 0:
        notes.append(
            f"{eligible_count} offers passed standard ranking eligibility, "
            "but none satisfied BOTH recommendation safety requirements: "
            "identity-oriented evidence and at least medium overall evidence "
            "confidence. No automatic recommendation was made."
        )

    else:
        notes.append(
            f"{eligible_count} eligible offers were ranked using 70% seller "
            "trust and 30% within-budget price value. Automatic recommendation "
            "requires BOTH identity-oriented evidence (domain-corroborated "
            "business presence or reliable seller-scoped reviews) and at least "
            "medium overall evidence confidence."
        )

    # Report the actual reason each blocked offer failed.
    # The categories below are mutually exclusive.

    if recommendation_blocked_count:
        breakdown: list[str] = []

        both_count = recommendation_block_counts[
            "identity_and_confidence"
        ]

        identity_count = recommendation_block_counts[
            "identity_only"
        ]

        confidence_count = recommendation_block_counts[
            "confidence_only"
        ]

        other_count = recommendation_block_counts[
            "other_policy"
        ]

        if both_count:
            breakdown.append(
                f"{both_count} lacked accepted identity evidence and "
                "also had low overall confidence"
            )

        if identity_count:
            breakdown.append(
                f"{identity_count} had medium/high confidence but lacked "
                "accepted identity evidence"
            )

        if confidence_count:
            breakdown.append(
                f"{confidence_count} had accepted identity evidence but "
                "were blocked because overall confidence was low"
            )

        if other_count:
            breakdown.append(
                f"{other_count} were blocked by another "
                "recommendation-policy condition"
            )

        notes.append(
            f"{recommendation_blocked_count} ranked offers were blocked "
            "by the recommendation safety gate. Breakdown: "
            + "; ".join(breakdown)
            + ". These categories are mutually exclusive; blocked offers "
            "keep their ordinary ranking positions for transparency."
        )

    if (
        recommended_offer is not None
        and recommended_offer.rank not in (None, 1)
    ):
        notes.append(
            "The highest 70/30-ranked offer did not pass both "
            "recommendation safety requirements; the highest-ranked offer "
            "that passed both requirements was recommended instead."
        )

    # Baseline ranking eligibility explanations.

    if rejection_counts.get("unrated"):
        notes.append(
            f"{rejection_counts['unrated']} offers were not ranked because "
            "seller evidence coverage was insufficient to calculate a trust score."
        )

    if rejection_counts.get("below_trust_threshold"):
        notes.append(
            f"{rejection_counts['below_trust_threshold']} offers were not "
            "ranked because trust scores were below 2.5."
        )

    if (
        rejection_counts.get("over_budget")
        or skipped.get("over_budget")
    ):
        count = (
            rejection_counts.get("over_budget", 0)
            + skipped.get("over_budget", 0)
        )

        notes.append(
            f"{count} offers were excluded because their price exceeded "
            "the supplied budget."
        )

    if skipped.get("unsupported_or_ambiguous_currency"):
        notes.append(
            _currency_skip_note(currency)
        )

    if skipped.get("invalid_price"):
        notes.append(
            "Some provider offers were skipped because their prices were invalid."
        )

    if provider_status != "checked":
        notes.append(
            "Product discovery returned incomplete results from the provider."
        )

    # ---------------------------------------------------------
    # 6. LOGGING
    # ---------------------------------------------------------

    logger.info(
        "rank_offers_complete",
        extra={
            "offers_returned": len(output),
            "offers_eligible_and_ranked": eligible_count,
            "offers_recommended": recommended_count,
            "offers_blocked_by_recommendation_gate": (
                recommendation_blocked_count
            ),
            "recommendation_blocks_identity_and_confidence": (
                recommendation_block_counts["identity_and_confidence"]
            ),
            "recommendation_blocks_identity_only": (
                recommendation_block_counts["identity_only"]
            ),
            "recommendation_blocks_confidence_only": (
                recommendation_block_counts["confidence_only"]
            ),
            "seller_count": len(representatives),
            "duration_ms": round(
                (time.monotonic() - started_at) * 1000
            ),
            "query": input_data.query,
        },
    )

    # ---------------------------------------------------------
    # 7. STRUCTURED MCP RESPONSE
    # ---------------------------------------------------------

    return RankOffersOutput(
        target_country=target_country,
        offers=output,
        unverified_notes=list(dict.fromkeys(notes)),
    )
