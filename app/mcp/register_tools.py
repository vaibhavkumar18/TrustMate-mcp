
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel

from app.mcp.schemas.inputs import RankOffersInput, SellerInput
from app.mcp.schemas.offers import Offer, RankOffersOutput
from app.mcp.schemas.trust import Trust

from app.mcp.tools.explain_score import explain_score
from app.mcp.tools.get_trust_score import get_trust_score
from app.mcp.tools.rank_offers import rank_offers


# ============================================================
# RANK OFFERS: PUBLIC STRUCTURED OUTPUT SCHEMA
# ============================================================

class RankOffersToolOffers(BaseModel):
    """Marketplace metadata and complete offer results."""

    query: str
    budget: float
    currency: str

    found: int
    ranked: int
    not_ranked: int
    recommended: int

    items: list[Offer]
    unverified_notes: list[str]


class RankOffersToolResult(BaseModel):
    """Public response structure for rank_offers."""

    target_country: str
    offers: RankOffersToolOffers


def _build_rank_offers_structured_result(
    output: RankOffersOutput,
    query: str,
    budget: float,
    currency: str,
) -> dict:
    """
    Preserve all original offer records and evidence while wrapping
    the result in the requested public response structure.
    """

    original = output.model_dump(mode="json")

    items = original.get("offers") or []
    unverified_notes = original.get("unverified_notes") or []

    ranked_count = sum(
        1
        for item in items
        if item.get("rank") is not None
    )

    recommended_count = sum(
        1
        for item in items
        if item.get("recommended") is True
    )

    response = RankOffersToolResult(
        target_country=output.target_country,
        offers=RankOffersToolOffers(
            query=query,
            budget=budget,
            currency=currency,
            found=len(items),
            ranked=ranked_count,
            not_ranked=len(items) - ranked_count,
            recommended=recommended_count,
            items=items,
            unverified_notes=unverified_notes,
        ),
    )

    return response.model_dump(mode="json")


# ============================================================
# SELLER TRUST ASSESSMENT TEXT
# ============================================================

_STATUS_MEANINGS = {
    "ok": "supporting evidence observed",
    "warn": "limited or neutral evidence; not proof of safety",
    "bad": "negative evidence observed",
    "unverified": "not verified; no positive or negative conclusion",
}


def _score_text(trust: Trust) -> str:
    if trust.score is None:
        return "not calculated"

    return f"{trust.score:.2f}/5"


def _trust_interpretation(trust: Trust) -> str:

    if trust.score is None:
        return (
            "There is not enough verified evidence to calculate a "
            "defensible score. Unrated does not mean fraudulent; "
            "it means evidence coverage is insufficient."
        )

    if trust.label == "Trusted":
        return (
            "Available evidence is comparatively positive. This is not "
            "a guarantee of fulfilment, product authenticity, or a "
            "safe transaction."
        )

    if trust.label == "Mixed":
        return (
            "Evidence is mixed or incomplete. Perform the relevant "
            "checks before relying on this seller."
        )

    return (
        "Material negative evidence was observed. Do not treat this "
        "seller as a safe choice unless the negative signals are "
        "independently resolved."
    )


def _format_trust_assessment_text(
    trust: Trust,
    seller: str,
    target_country: str | None,
    url: str | None = None,
) -> str:

    market = (
        target_country.upper()
        if target_country
        else "not specified (global context)"
    )

    lines = [
        "TRUSTMATE SELLER ASSESSMENT",
        f"Seller: {seller}",
        f"Marketplace context: {market}",
        (
            f"Assessment: {trust.label} | "
            f"Score: {_score_text(trust)} | "
            f"Confidence: {trust.confidence}"
        ),
    ]

    if url:
        lines.append(f"Official website supplied: {url}")

    lines.extend([
        "",
        "WHAT THIS MEANS",
        _trust_interpretation(trust),
        "",
        "EVIDENCE BREAKDOWN",
    ])

    for factor in trust.factors:

        status = str(
            getattr(factor.status, "value", factor.status)
        ).lower()

        meaning = _STATUS_MEANINGS.get(
            status,
            "status requires review",
        )

        lines.append(
            f"- {factor.name}: {status.upper()} "
            f"({meaning}). {factor.detail}"
        )

    lines.extend([
        "",
        "HOW TO USE THIS RESULT",
        (
            "This is an evidence assessment, not an automatic "
            "buy/don't-buy verdict. A business listing can support "
            "business presence but does not prove online "
            "transaction reliability."
        ),
        (
            "A neutral URL-reputation result means no known threat "
            "indicator was returned; it is not proof that the "
            "website is safe."
        ),
        (
            "HTTP 403 responses, timeouts, and other access failures "
            "mean the website could not be inspected; they are not "
            "by themselves proof of fraud. An explicit TLS "
            "certificate validation failure is a separate "
            "negative signal."
        ),
        (
            "Product reviews must not be substituted for "
            "seller-specific service or transaction reviews."
        ),
        "",
        "BEFORE PURCHASING",
        (
            "Confirm the merchant domain at checkout, inspect "
            "return/refund terms, use a payment method with buyer "
            "protection, and look for seller-specific service history."
        ),
    ])

    return "\n".join(lines)


# ============================================================
# TOOL REGISTRATION
# ============================================================

def register_tools(
    server: MCPServer,
    provider_registry=None,
    session_factory=None,
    assessment_cache=None,
) -> None:

    # --------------------------------------------------------
    # RANK OFFERS
    # --------------------------------------------------------

    @server.tool(
        name="rank_offers",
        structured_output=True,
    )
    async def rank_offers_tool(
        query: str,
        budget: float,
        currency: str,
        target_country: str,
    ) -> Annotated[CallToolResult, RankOffersToolResult]:
        """
        Compare discovered offers using deterministic seller evidence
        and price value.

        Rank represents the ordinary 70/30 ordering, not an endorsement.

        Automatic recommendation separately requires:
        - Corroborated seller identity evidence.
        - Medium or high confidence.

        The complete offer records and evidence are retained in the
        structured response.
        """

        output = await rank_offers(
            RankOffersInput(
                query=query,
                budget=budget,
                currency=currency,
                target_country=target_country,
            ),
            provider_registry,
            assessment_cache,
        )

        ranked_count = sum(
            1
            for offer in output.offers
            if offer.rank is not None
        )

        recommended_count = sum(
            1
            for offer in output.offers
            if offer.recommended
        )

        content_summary = (
            f"TRUSTMATE completed for {output.target_country}: "
            f"{len(output.offers)} offers found, "
            f"{ranked_count} ranked, "
            f"{len(output.offers) - ranked_count} not ranked, "
            f"{recommended_count} recommended."
        )

        structured_result = _build_rank_offers_structured_result(
            output=output,
            query=query,
            budget=budget,
            currency=currency,
        )

        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=content_summary,
                )
            ],
            structured_content=structured_result,
        )

    # --------------------------------------------------------
    # GET TRUST SCORE
    # --------------------------------------------------------

    @server.tool(
        name="get_trust_score",
        structured_output=True,
    )
    async def get_trust_score_tool(
        seller: str,
        url: str | None = None,
        target_country: str | None = None,
    ) -> Annotated[CallToolResult, Trust]:
        """
        Assess a seller and explain every evidence factor
        and its limitations.

        Supply target_country (ISO alpha-2, e.g. US) to guide
        business-listing discovery to the intended marketplace.

        This is a search hint only. Strict seller-name and
        registered-domain corroboration remains mandatory.
        """

        input_data = SellerInput(
            seller=seller,
            url=url,
            target_country=target_country,
        )

        trust = await get_trust_score(
            input_data,
            provider_registry,
            session_factory,
            assessment_cache,
        )

        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=_format_trust_assessment_text(
                        trust=trust,
                        seller=seller,
                        target_country=target_country,
                        url=url,
                    ),
                )
            ],
            structured_content=trust.model_dump(mode="json"),
        )

    # --------------------------------------------------------
    # EXPLAIN SCORE
    # --------------------------------------------------------

    @server.tool(
        name="explain_score",
        structured_output=True,
    )
    async def explain_score_tool(
        seller: str,
        url: str | None = None,
        target_country: str | None = None,
    ) -> CallToolResult:
        """
        Explain the seller score, evidence strength, trade-offs,
        and next checks.

        Supply target_country (ISO alpha-2, e.g. US) to use the
        same marketplace context as rank_offers.

        The explanation is not an automatic purchase verdict.
        """

        input_data = SellerInput(
            seller=seller,
            url=url,
            target_country=target_country,
        )

        result = await explain_score(
            input_data,
            provider_registry,
            session_factory,
            assessment_cache,
        )

        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=result["summary"],
                )
            ],
            structured_content=result,
        )
