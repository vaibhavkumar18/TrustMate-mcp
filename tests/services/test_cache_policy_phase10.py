from datetime import timedelta

import pytest

from app.services.cache_policy_service import CachePolicy, ProviderCacheService, POLICIES


@pytest.mark.asyncio
async def test_cache_hit_miss_expiry_and_namespace_isolation():
    service = ProviderCacheService()
    policy = CachePolicy("test", "evidence", True, False, timedelta(seconds=1), 2, frozenset({"value"}))
    key = service.key("test", "seller:one", {"value": 1})

    assert await service.get(policy, key) is None
    assert await service.put(policy, key, {"value": 1, "secret": "excluded"}) is True
    assert await service.get(policy, key) == {"value": 1}
    assert await service.get(CachePolicy("other", "evidence", True, False, timedelta(hours=1), 2, frozenset({"value"})), key) is None


@pytest.mark.asyncio
async def test_cache_capacity_and_disallowed_policies():
    service = ProviderCacheService()
    policy = CachePolicy("bounded", "x", True, False, timedelta(hours=1), 2, frozenset({"value"}))
    for value in range(3):
        key = service.key("bounded", str(value), {"value": value})
        await service.put(policy, key, {"value": value})
    first = service.key("bounded", "0", {"value": 0})
    last = service.key("bounded", "2", {"value": 2})
    assert await service.get(policy, first) is None
    assert await service.get(policy, last) == {"value": 2}
    assert await service.put(POLICIES["foursquare"], "x", {"value": 1}) is False
    assert await service.put(POLICIES["urlhaus"], "x", {"value": 1}) is False


def test_cache_key_stability_and_provider_fields():
    service = ProviderCacheService()
    assert service.key("website", "seller:one", {"b": 2, "a": 1}) == service.key(
        "website", "seller:one", {"a": 1, "b": 2}
    )
    assert "price" not in POLICIES["channel3"].fields
    assert "html" not in POLICIES["website"].fields
    assert POLICIES["nominatim"].attribution
    assert POLICIES["foursquare"].persistent is False
