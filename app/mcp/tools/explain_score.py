
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import SQLAlchemyError

from app.mcp.schemas.inputs import SellerInput
from app.core.config import get_settings
from app.domain.models import LLMExplanation, ProviderResult, RetentionPolicy
from app.providers.factory import ProviderRegistry, create_provider_registry
from app.services.seller_assessment_cache import (
    assessment_cache_key,
    validated_cache_identity,
)
from app.services.seller_verification_service import collect_seller_evidence
from app.services.trust_scoring_service import assess_evidence
from app.domain.policies.scoring_policy import SCORING_VERSION
from app.services.assessment_persistence import persist_assessment
from app.storage.repositories import CacheRepository

logger = logging.getLogger(__name__)


async def explain_score(
    input_data: SellerInput,
    registry: ProviderRegistry | None = None,
    session_factory=None,
    assessment_cache=None,
) -> dict[str, str]:
    settings = get_settings()
    registry = registry or create_provider_registry(settings)

    identity = await validated_cache_identity(
        input_data.seller,
        input_data.url,
        registry.website,
    )

    cache_key = None
    evidence = None
    trust = None

    if assessment_cache is not None and identity is not None:
        cache_key = assessment_cache_key(
            assessment_cache,
            seller_key=identity.seller_key,
            target_country=input_data.target_country or "GLOBAL",
            settings=settings,
            registry=registry,
        )

        trust = await assessment_cache.get_assessment(cache_key)

        if trust is not None:
            logger.info(
                "seller_assessment_cache_hit",
                extra={
                    "cache_scope": "explain_score",
                    "seller_key": identity.seller_key,
                },
            )
        else:
            logger.info(
                "seller_assessment_cache_miss",
                extra={
                    "cache_scope": "explain_score",
                    "seller_key": identity.seller_key,
                },
            )

    if trust is None:
        evidence = await collect_seller_evidence(
            input_data.seller,
            input_data.url,
            registry.business,
            registry.business_fallback,
            registry.domain,
            registry.website,
            registry.reputation,
            target_country=input_data.target_country,
            seller_location_hints=settings.seller_location_hints,
            reviews_provider=registry.reviews,
            prevalidated_identity=identity,
        )

        trust = assess_evidence(evidence)

        if (
            assessment_cache is not None
            and cache_key is not None
            and trust.score is not None
        ):
            await assessment_cache.put_assessment(
                cache_key=cache_key,
                seller_key=evidence.identity.seller_key,
                seller_name=evidence.identity.display_name,
                normalized_domain=evidence.identity.normalized_domain,
                target_country=input_data.target_country or "GLOBAL",
                scoring_version=SCORING_VERSION,
                trust=trust,
            )

    summary = {
        "score": trust.score,
        "label": trust.label,
        "confidence": trust.confidence,
        "factors": [
            {
                "name": factor.name,
                "status": factor.status,
                "detail": _safe_detail(factor.detail),
            }
            for factor in trust.factors
        ],
    }

    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "trust": summary,
                "model": getattr(registry.llm, "model_id", "mock"),
                "prompt_version": getattr(registry.llm, "prompt_version", "mock"),
                "schema_version": "trust-object-1",
                "scoring_version": SCORING_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()

    explanation_cache_key = f"explain:v1:{fingerprint}"

    if assessment_cache is not None:
        cached_summary = await assessment_cache.get_explanation(
            explanation_cache_key
        )

        if cached_summary is not None:
            logger.info(
                "seller_explanation_cache_hit",
                extra={"cache_scope": "explain_score"},
            )

            cached_context = _extract_additional_context(
                cached_summary,
                trust,
            )

            return {
                "summary": _build_user_summary(
                    trust,
                    cached_context,
                    input_data.target_country,
                    input_data.seller,
                )
            }

    if session_factory is not None and evidence is not None:
        explanation = await _cached_explanation(
            session_factory,
            evidence,
            registry,
            summary,
            fingerprint,
        )
    else:
        explanation = await registry.llm.generate_explanation(
            summary,
            [],
        )

    if (
        assessment_cache is not None
        and explanation.provider.status == "checked"
        and explanation.summary
    ):
        await assessment_cache.put_explanation(
            explanation_cache_key,
            explanation.summary,
        )

    additional_context = (
        _extract_additional_context(explanation.summary, trust)
        if explanation.provider.status == "checked"
        else None
    )

    return {
        "summary": _build_user_summary(
            trust,
            additional_context,
            input_data.target_country,
            input_data.seller,
        )
    }


def _build_user_summary(
    trust,
    additional_context: str | None,
    target_country: str | None,
    seller: str,
) -> str:
    """Return interpretation and next actions, not a duplicate trust report.

    get_trust_score already returns the score and all factor details. This tool
    should explain what those signals mean together and what the buyer should
    verify next.
    """

    score = (
        f"{trust.score:.2f}/5"
        if trust.score is not None
        else "not calculated"
    )

    country = (
        target_country.upper()
        if target_country
        else "global context"
    )

    factors = {
        factor.name.strip().casefold(): factor
        for factor in trust.factors
    }

    def factor_for(*names):
        for name in names:
            factor = factors.get(name.casefold())
            if factor is not None:
                return factor
        return None

    lines = [
        "TRUSTMATE EXPLANATION",
        f"Seller: {seller}",
        f"Marketplace context: {country}",
        f"Assessment context: {trust.label} | {score} | {trust.confidence} confidence",
        "",
        "HOW TO INTERPRET THE RESULT",
    ]

    if trust.score is None or trust.label == "Unrated":
        lines.append(
            "There is not enough usable evidence to calculate a score. This is an "
            "evidence gap, not a finding that the seller is fraudulent. Do not use "
            "this result to compare sellers numerically."
        )

    elif trust.label == "Trusted":
        lines.append(
            "The available evidence is comparatively positive, but it cannot "
            "guarantee delivery, product authenticity, refunds, or a safe payment. "
            "Treat this as a screening result rather than a warranty."
        )

    elif trust.label == "Mixed":
        lines.append(
            "The evidence supports some aspects of the seller but leaves material "
            "questions unresolved. The next step is to close the specific evidence "
            "gaps below, not to interpret the score as a purchase verdict."
        )

    else:
        lines.append(
            "At least one material negative signal affected the assessment. Resolve "
            "that signal independently before entering payment details. A Risky "
            "classification is a warning based on observed evidence, not a legal "
            "finding that the seller committed fraud."
        )

    lines.extend(["", "WHAT IS DRIVING IT"])

    business = factor_for("Business presence")
    reviews = factor_for("Reviews")
    website = factor_for("Website signals")
    reputation = factor_for("URL reputation")

    if business is not None:
        status = str(
            getattr(business.status, "value", business.status)
        ).lower()

        if status in {"ok", "warn"}:
            lines.append(
                "- Business identity: a directory match was observed. This supports "
                "the seller's business presence only; it does not establish order "
                "fulfilment or customer-service quality."
            )

        elif status == "bad":
            lines.append(
                "- Business identity: the available lookup returned a negative "
                "identity signal. Verify the legal/business name and official domain "
                "before proceeding."
            )

        else:
            lines.append(
                "- Business identity: the seller could not be corroborated from the "
                "available directory evidence. That means unknown, not nonexistent."
            )

    if reviews is not None:
        status = str(
            getattr(reviews.status, "value", reviews.status)
        ).lower()

        if status == "unverified":
            lines.append(
                "- Seller experience: no usable seller-specific service or "
                "transaction-review sample was available. Product reviews cannot "
                "fill this gap."
            )

        elif status == "bad":
            lines.append(
                "- Seller experience: negative review evidence was observed; check "
                "its scope, recency, and sample quality before relying on it."
            )

        elif status == "ok":
            lines.append(
                "- Seller experience: supporting seller-specific review evidence "
                "was available. Review volume and recency still matter."
            )

        else:
            lines.append(
                "- Seller experience: the available review signal is limited and "
                "should not be treated as conclusive."
            )

    if website is not None:
        status = str(
            getattr(website.status, "value", website.status)
        ).lower()

        detail = _safe_detail(website.detail).casefold()

        if status == "bad" and (
            "tls" in detail or "certificate" in detail
        ):
            lines.append(
                "- Website security: certificate validation failed. Do not enter "
                "credentials or payment information until the certificate issue is "
                "resolved and independently verified."
            )

        elif status == "bad":
            lines.append(
                "- Website signals: a negative website signal was observed and "
                "should be resolved before purchase."
            )

        elif status == "unverified":
            if "403" in detail or "forbidden" in detail:
                reason = "the site refused automated inspection (HTTP 403)"
            elif "timeout" in detail:
                reason = "the automated check timed out"
            elif "too large" in detail or "response_too_large" in detail:
                reason = "the site response exceeded the inspection limit"
            else:
                reason = "the site could not be inspected"

            lines.append(
                f"- Website signals: {reason}. Contact details, policies, and other "
                "website practices therefore remain unknown; this failure alone is "
                "not proof of fraud."
            )

        elif status == "warn":
            lines.append(
                "- Website signals: only partial website evidence was available. "
                "Uninspected controls remain unknown and should be checked manually."
            )

        else:
            lines.append(
                "- Website signals: contact or policy evidence was observed. HTTPS "
                "is a baseline transport control, not proof of seller legitimacy."
            )

    if reputation is not None:
        status = str(
            getattr(reputation.status, "value", reputation.status)
        ).lower()

        if status == "bad":
            lines.append(
                "- URL reputation: a known threat indicator was returned. Avoid "
                "opening the flagged destination or submitting information there."
            )

        elif status == "unverified":
            lines.append(
                "- URL reputation: no usable provider result was obtained. The "
                "destination's reputation is unknown, not cleared."
            )

        else:
            lines.append(
                "- URL reputation: no known threat indicator was returned by this "
                "check. This is neutral evidence, not proof that the site is safe."
            )

    status_values = [
        str(getattr(f.status, "value", f.status)).lower()
        for f in trust.factors
    ]

    observed = sum(
        status in {"ok", "warn", "bad"}
        for status in status_values
    )

    missing = sum(
        status == "unverified"
        for status in status_values
    )

    lines.extend(["", "CONFIDENCE AND LIMITS"])

    if trust.confidence == "low":
        lines.append(
            f"Confidence is low: {observed} of {len(status_values)} evidence groups "
            f"returned a usable status and {missing} remain unverified. Treat the "
            "score as provisional; more evidence could materially change it."
        )

    elif trust.confidence == "medium":
        lines.append(
            f"Confidence is medium: {observed} of {len(status_values)} evidence "
            f"groups returned a usable status and {missing} remain unverified. "
            "This is useful screening evidence, but some uncertainty remains."
        )

    else:
        lines.append(
            f"Confidence is high under TRUSTMATE's evidence-coverage policy "
            f"({observed} usable groups out of {len(status_values)}). It still does "
            "not guarantee transaction outcomes or product authenticity."
        )

    actions = []

    if (
        business is not None
        and str(
            getattr(business.status, "value", business.status)
        ).lower() == "unverified"
    ):
        actions.append(
            "Confirm the seller's registered business name and official domain "
            "from an independent source."
        )

    if (
        reviews is not None
        and str(
            getattr(reviews.status, "value", reviews.status)
        ).lower() in {"unverified", "warn"}
    ):
        actions.append(
            "Look for service or transaction reviews tied to this seller, "
            "not reviews of the product itself."
        )

    if (
        website is not None
        and str(
            getattr(website.status, "value", website.status)
        ).lower() in {"unverified", "warn", "bad"}
    ):
        actions.append(
            "Open the official website yourself and inspect contact, shipping, "
            "returns, refunds, and warranty terms; resolve any certificate "
            "warning before checkout."
        )

    if (
        reputation is not None
        and str(
            getattr(reputation.status, "value", reputation.status)
        ).lower() == "unverified"
    ):
        actions.append(
            "Treat the URL reputation as unknown until a provider check completes; "
            "do not interpret a timeout as a clean result."
        )

    if not actions:
        actions.append(
            "Check the checkout domain and payment protections, then compare the "
            "seller's written delivery and refund terms with the offer."
        )

    lines.extend(["", "MOST USEFUL NEXT CHECKS"])
    lines.extend(
        f"{index}. {action}"
        for index, action in enumerate(actions[:3], start=1)
    )

    if additional_context:
        lines.extend(["", "ADDITIONAL CONTEXT"])
        lines.append(_safe_detail(additional_context))

    lines.extend([
        "",
        "This explanation interprets the evidence; it does not issue an automatic "
        "buy/don't-buy decision.",
    ])

    return "\n".join(lines)


def _extract_additional_context(
    model_summary: str,
    trust,
) -> str | None:
    """Strip deterministic score boilerplate and reject generic fallback text."""

    if not model_summary or not model_summary.strip():
        return None

    text = " ".join(
        model_summary.replace("\x00", "").split()
    ).strip()

    score = (
        f"{trust.score:g}/5"
        if trust.score is not None
        else "score unavailable"
    )

    prefix = (
        f"TRUST ASSESSMENT: {trust.label} "
        f"({score}; {trust.confidence} confidence)."
    )

    if text.startswith(prefix):
        text = text[len(prefix):].strip()

    if not text or text.casefold().startswith("mock explanation:"):
        return None

    generic_phrases = (
        "some evidence remains limited for",
        "evidence is missing or unverified for",
        "the available evidence does not support a more detailed explanation",
    )

    if any(
        phrase in text.casefold()
        for phrase in generic_phrases
    ):
        return None

    return text[:800] or None


async def _cached_explanation(
    session_factory,
    evidence,
    registry,
    summary,
    fingerprint,
):
    from app.providers.implementations import OpenRouterProvider

    llm = registry.llm

    if not isinstance(llm, OpenRouterProvider):
        return await llm.generate_explanation(summary, [])

    model = llm.model_id

    async with session_factory() as session:
        try:
            assessment = await persist_assessment(
                session,
                evidence,
            )

            repository = CacheRepository(session)

            cached = await repository.get_explanation(
                assessment.seller_id,
                fingerprint,
                "openrouter",
                model,
                datetime.now(timezone.utc),
            )

            if cached is not None:
                await session.commit()

                return LLMExplanation(
                    provider=ProviderResult(
                        provider_code="openrouter",
                        capability="llm",
                        status="checked",
                        data={"cache_hit": True},
                        cache_policy=RetentionPolicy(cacheable=False),
                    ),
                    summary=cached.summary,
                )

            explanation = await llm.generate_explanation(
                summary,
                [],
            )

            if explanation.provider.status == "checked":
                await repository.add_explanation(
                    seller_id=assessment.seller_id,
                    assessment_id=assessment.id,
                    evidence_fingerprint=fingerprint,
                    llm_provider="openrouter",
                    model_id=model,
                    summary=explanation.summary,
                    expires_at=(
                        datetime.now(timezone.utc)
                        + timedelta(hours=72)
                    ),
                )

            await session.commit()
            return explanation

        except SQLAlchemyError:
            await session.rollback()

            return (
                explanation
                if "explanation" in locals()
                else llm._result(
                    "cache_write_failed",
                    llm._fallback(summary),
                )
            )


def _safe_detail(detail: str) -> str:
    return " ".join(
        detail.replace("\x00", "").split()
    )[:300]
