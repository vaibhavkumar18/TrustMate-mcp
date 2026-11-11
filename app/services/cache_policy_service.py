from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.storage.repositories import CacheRepository


@dataclass(frozen=True)
class CachePolicy:
    provider: str
    capability: str
    allowed: bool
    persistent: bool
    ttl: timedelta
    max_entries: int
    fields: frozenset[str]
    attribution: str | None = None
    version: str = "1"


POLICIES = {
    "channel3": CachePolicy("channel3", "product", True, True, timedelta(hours=1), 256,
                            frozenset({"product_id", "category"})),
    "foursquare": CachePolicy("foursquare", "business", False, False, timedelta(0), 64, frozenset()),
    "nominatim": CachePolicy("nominatim", "business", True, True, timedelta(hours=24), 256,
                             frozenset({"business_name", "official_website", "match_confidence"}),
                             "© OpenStreetMap contributors"),
    "urlhaus": CachePolicy("urlhaus", "reputation", False, False, timedelta(0), 0, frozenset()),
    "rdap": CachePolicy("rdap", "domain", True, False, timedelta(hours=1), 128,
                        frozenset({"domain", "creation_date", "expiration_date", "status"})),
    "website": CachePolicy("native", "website", True, True, timedelta(hours=6), 256,
                           frozenset({"https_reachable", "tls_valid", "contact_present",
                                      "return_policy_present", "refund_policy_present"})),
    "openrouter": CachePolicy("openrouter", "llm", True, True, timedelta(hours=72), 256,
                              frozenset({"summary", "evidence_fingerprint", "model_id",
                                         "prompt_version", "schema_version"})),
}


class ProviderCacheService:
    def __init__(self) -> None:
        self._memory: OrderedDict[tuple[str, str], tuple[datetime, dict[str, Any]]] = OrderedDict()

    @staticmethod
    def key(provider: str, identity: str, value: Any) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        digest = hashlib.sha256(f"{provider}:{identity}:{payload}".encode()).hexdigest()
        return f"{provider}:v1:{digest}"

    def _trim(self, policy: CachePolicy) -> None:
        entries = [key for key in self._memory if key[0] == policy.provider]
        while len(entries) > policy.max_entries:
            self._memory.pop(entries.pop(0), None)

    async def get(
        self, policy: CachePolicy, cache_key: str, repository: CacheRepository | None = None
    ) -> dict[str, Any] | None:
        if not policy.allowed:
            return None
        now = datetime.now(timezone.utc)
        cached = self._memory.get((policy.provider, cache_key))
        if cached:
            expires, payload = cached
            if expires > now:
                self._memory.move_to_end((policy.provider, cache_key))
                return dict(payload)
            self._memory.pop((policy.provider, cache_key), None)
        if repository is None or not policy.persistent:
            return None
        try:
            row = await repository.get_provider(policy.provider, cache_key, now)
        except SQLAlchemyError:
            return None
        if row is None:
            return None
        payload = self._filter(policy, row.normalized_payload)
        self._memory[(policy.provider, cache_key)] = (row.expires_at, payload)
        self._trim(policy)
        return dict(payload)

    async def put(
        self,
        policy: CachePolicy,
        cache_key: str,
        payload: dict[str, Any],
        *,
        status: str = "checked",
        repository: CacheRepository | None = None,
    ) -> bool:
        if not policy.allowed or status != "checked" or policy.ttl <= timedelta(0):
            return False
        normalized = self._filter(policy, payload)
        if not normalized:
            return False
        now = datetime.now(timezone.utc)
        expires = now + policy.ttl
        self._memory[(policy.provider, cache_key)] = (expires, normalized)
        self._memory.move_to_end((policy.provider, cache_key))
        self._trim(policy)
        if repository is None or not policy.persistent:
            return True
        try:
            await repository.upsert_provider({
                "provider_code": policy.provider,
                "capability": policy.capability,
                "cache_key": cache_key,
                "result_status": status,
                "normalized_payload": normalized,
                "provider_version": policy.version,
                "checked_at": now,
                "expires_at": expires,
            })
        except SQLAlchemyError:
            return True
        return True

    @staticmethod
    def _filter(policy: CachePolicy, payload: dict[str, Any]) -> dict[str, Any]:
        return {key: payload[key] for key in policy.fields if key in payload}

