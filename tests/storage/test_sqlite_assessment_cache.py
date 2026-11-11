import sqlite3
import time

import pytest

from app.mcp.schemas.trust import Trust, TrustFactor
from app.storage.sqlite_assessment_cache import SQLiteAssessmentCache


def _trust(score: float | None = 3.5) -> Trust:
    return Trust(
        score=score,
        label="Mixed" if score is not None else "Unrated",
        confidence="medium" if score is not None else "low",
        factors=[
            TrustFactor(
                name="Business presence",
                status="warn",
                detail="A corroborated business listing was observed.",
            ),
            TrustFactor(
                name="Reviews",
                status="unverified",
                detail="Seller-specific transaction reviews are unavailable.",
            ),
        ],
    )


@pytest.mark.asyncio
async def test_assessment_cache_persists_across_cache_instances(tmp_path):
    path = str(tmp_path / "cache.sqlite")
    cache = SQLiteAssessmentCache(path, ttl_days=3)
    await cache.initialize()

    key = cache.assessment_key("domain:example.com", "US", "2.3", {"reviews": "none"})
    assert await cache.get_assessment(key) is None
    assert await cache.put_assessment(
        cache_key=key,
        seller_key="domain:example.com",
        seller_name="Example Store",
        normalized_domain="example.com",
        target_country="US",
        scoring_version="2.3",
        trust=_trust(),
    ) is True

    restarted_cache = SQLiteAssessmentCache(path, ttl_days=3)
    await restarted_cache.initialize()
    cached = await restarted_cache.get_assessment(key)
    assert cached is not None
    assert cached.score == 3.5
    assert cached.label == "Mixed"
    assert cached.factors[0].name == "Business presence"


@pytest.mark.asyncio
async def test_expired_assessment_is_not_returned(tmp_path):
    path = str(tmp_path / "cache.sqlite")
    cache = SQLiteAssessmentCache(path, ttl_days=3)
    await cache.initialize()
    key = "expired-assessment"
    await cache.put_assessment(
        cache_key=key,
        seller_key="domain:example.com",
        seller_name="Example Store",
        normalized_domain="example.com",
        target_country="US",
        scoring_version="2.3",
        trust=_trust(),
    )

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE seller_assessment_cache SET expires_at = ? WHERE cache_key = ?",
            (time.time() - 1, key),
        )

    assert await cache.get_assessment(key) is None


@pytest.mark.asyncio
async def test_unrated_assessments_are_not_cached(tmp_path):
    cache = SQLiteAssessmentCache(str(tmp_path / "cache.sqlite"), ttl_days=3)
    await cache.initialize()
    result = await cache.put_assessment(
        cache_key="unrated",
        seller_key="name:unknown seller",
        seller_name="Unknown Seller",
        normalized_domain=None,
        target_country="US",
        scoring_version="2.3",
        trust=_trust(None),
    )
    assert result is False
    assert await cache.get_assessment("unrated") is None


@pytest.mark.asyncio
async def test_explanation_cache_persists_and_expires(tmp_path):
    path = str(tmp_path / "cache.sqlite")
    cache = SQLiteAssessmentCache(path, ttl_days=3)
    await cache.initialize()
    assert await cache.get_explanation("explain:v1:test") is None
    assert await cache.put_explanation("explain:v1:test", "Evidence summary.") is True
    assert await cache.get_explanation("explain:v1:test") == "Evidence summary."

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE explanation_cache SET expires_at = ? WHERE cache_key = ?",
            (time.time() - 1, "explain:v1:test"),
        )

    assert await cache.get_explanation("explain:v1:test") is None


@pytest.mark.asyncio
async def test_cache_key_isolated_by_market_scoring_and_provider_profile():
    base = SQLiteAssessmentCache.assessment_key(
        "domain:example.com", "US", "2.3", {"reviews": "none"}
    )
    assert base != SQLiteAssessmentCache.assessment_key(
        "domain:example.com", "GB", "2.3", {"reviews": "none"}
    )
    assert base != SQLiteAssessmentCache.assessment_key(
        "domain:example.com", "US", "2.4", {"reviews": "none"}
    )
    assert base != SQLiteAssessmentCache.assessment_key(
        "domain:example.com", "US", "2.3", {"reviews": "trustpilot"}
    )


@pytest.mark.asyncio
async def test_default_three_day_ttl_is_exactly_72_hours(tmp_path):
    path = str(tmp_path / "cache.sqlite")
    cache = SQLiteAssessmentCache(path, ttl_days=3)
    await cache.initialize()
    await cache.put_assessment(
        cache_key="ttl-check",
        seller_key="domain:example.com",
        seller_name="Example Store",
        normalized_domain="example.com",
        target_country="US",
        scoring_version="2.3",
        trust=_trust(),
    )

    with sqlite3.connect(path) as connection:
        checked_at, expires_at = connection.execute(
            "SELECT checked_at, expires_at FROM seller_assessment_cache WHERE cache_key = ?",
            ("ttl-check",),
        ).fetchone()

    assert expires_at - checked_at == 3 * 24 * 60 * 60
