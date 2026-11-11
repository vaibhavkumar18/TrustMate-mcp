
import pytest

from app.domain.models import Offer as ProductOffer, SellerIdentity
from app.mcp.schemas.trust import Trust, TrustFactor
from app.services.offer_ranking_service import (
    RankingCandidate,
    rank_candidates,
    recommendation_eligibility_reasons,
    ranking_score,
    tradeoff_note,
)


def candidate(
    offer_id: str,
    price: float,
    score: float | None,
    *,
    business_status: str = "warn",
    reviews_status: str = "unverified",
) -> RankingCandidate:
    seller = SellerIdentity(
        display_name=f"Seller {offer_id}",
        seller_key=f"seller:{offer_id}",
        normalized_domain=f"{offer_id}.example",
        canonical_url=f"https://{offer_id}.example",
        identity_status="domain_matched",
    )

    label = (
        "Unrated" if score is None else
        "Trusted" if score >= 4 else
        "Mixed" if score >= 2.5 else
        "Risky"
    )

    factors = [
        TrustFactor(
            name="Business presence",
            status=business_status,
            detail="test",
        ),
        TrustFactor(
            name="Reviews",
            status=reviews_status,
            detail="test",
        ),
        TrustFactor(
            name="Website signals",
            status="ok",
            detail="test",
        ),
        TrustFactor(
            name="URL reputation",
            status="warn",
            detail="test",
        ),
    ]

    return RankingCandidate(
        ProductOffer(
            id=offer_id,
            product_id="p",
            title=offer_id,
            price=price,
            currency="USD",
            url=f"https://{offer_id}.example/item",
            seller=seller,
        ),
        Trust(
            score=score,
            label=label,
            confidence="high",
            factors=factors,
        ),
    )


def test_70_30_formula_and_recommendation_are_preserved():
    first = candidate("a", 900, 4)
    second = candidate("b", 500, 3)

    results = rank_candidates([first, second], 1000)

    assert results[0][0].offer.id == "a"
    assert results[0][1] == 1
    assert results[0][2] is True
    assert results[1][2] is False

    assert ranking_score(first, 1000) == pytest.approx(
        0.70 * 0.8 + 0.30 * 0.1
    )


def test_identity_gate_does_not_change_rank_but_changes_recommendation():
    unverified_identity = candidate(
        "a",
        300,
        4.5,
        business_status="unverified",
        reviews_status="unverified",
    )

    seller_reviewed = candidate(
        "b",
        600,
        3.5,
        business_status="unverified",
        reviews_status="ok",
    )

    results = rank_candidates(
        [unverified_identity, seller_reviewed],
        1000,
    )

    by_id = {
        row[0].offer.id: row
        for row in results
    }

    assert by_id["a"][1] == 1
    assert by_id["a"][2] is False

    assert by_id["b"][1] == 2
    assert by_id["b"][2] is True

    note = tradeoff_note(
        unverified_identity,
        1000,
        False,
    ).casefold()

    assert "not recommended automatically" in note
    assert "ranking position" in note


def test_no_candidate_passes_gate_means_no_automatic_recommendation():
    items = [
        candidate(
            "a",
            300,
            4.5,
            business_status="unverified",
            reviews_status="unverified",
        ),
        candidate(
            "b",
            500,
            3.5,
            business_status="unverified",
            reviews_status="unverified",
        ),
    ]

    results = rank_candidates(items, 1000)

    assert all(row[1] is not None for row in results)
    assert all(row[2] is False for row in results)

    assert all(
        recommendation_eligibility_reasons(row[0])
        for row in results
    )


def test_corroborated_business_presence_alone_passes_gate():
    item = candidate(
        "business",
        500,
        3.5,
        business_status="warn",
    )

    assert recommendation_eligibility_reasons(item) == []

    results = rank_candidates([item], 1000)

    assert results[0][2] is True


def test_reliable_seller_scoped_reviews_alone_pass_gate():
    item = candidate(
        "reviews",
        500,
        3.5,
        business_status="unverified",
        reviews_status="ok",
    )

    assert recommendation_eligibility_reasons(item) == []

    results = rank_candidates([item], 1000)

    assert results[0][2] is True


def test_low_volume_or_low_rating_seller_reviews_do_not_pass_gate_alone():
    item = candidate(
        "limited-reviews",
        500,
        3.5,
        business_status="unverified",
        reviews_status="warn",
    )

    assert recommendation_eligibility_reasons(item)

    results = rank_candidates([item], 1000)

    assert results[0][2] is False


def test_bad_factor_status_does_not_pass_identity_gate():
    item = candidate(
        "bad",
        500,
        3.5,
        business_status="bad",
        reviews_status="bad",
    )

    assert recommendation_eligibility_reasons(item)

    results = rank_candidates([item], 1000)

    assert results[0][2] is False


def test_ineligible_offers_are_preserved_and_not_recommended():
    results = rank_candidates(
        [
            candidate("trusted", 900, 4),
            candidate("risky", 400, 2),
            candidate("unrated", 300, None),
            candidate("over", 1100, 5),
            candidate("invalid", 0, 5),
        ],
        1000,
    )

    by_id = {
        item.offer.id: item
        for item, *_ in results
    }

    assert by_id["trusted"].offer.id == "trusted"
    assert by_id["risky"].offer.id == "risky"

    assert next(
        item[2]
        for item in results
        if item[0].offer.id == "trusted"
    )

    assert not next(
        item[2]
        for item in results
        if item[0].offer.id == "unrated"
    )

    assert not next(
        item[2]
        for item in results
        if item[0].offer.id == "over"
    )


def test_ties_use_trust_then_price_then_id():
    results = rank_candidates(
        [
            candidate("b", 500, 4),
            candidate("a", 500, 4),
            candidate("c", 500, 3.5),
        ],
        1000,
    )

    assert [
        item[0].offer.id
        for item in results[:2]
    ] == ["a", "b"]


def test_invalid_budget_is_rejected():
    with pytest.raises(ValueError):
        rank_candidates([], 0)
