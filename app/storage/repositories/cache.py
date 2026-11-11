import uuid
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import ExplanationCache, ProviderCache


class CacheRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_provider(
        self, provider_code: str, cache_key: str, now: datetime
    ) -> ProviderCache | None:
        statement = select(ProviderCache).where(
            ProviderCache.provider_code == provider_code,
            ProviderCache.cache_key == cache_key,
            ProviderCache.expires_at > now,
        )
        return await self.session.scalar(statement)

    async def upsert_provider(self, values: dict[str, object]) -> None:
        statement = insert(ProviderCache).values(id=uuid.uuid4(), **values)
        statement = statement.on_conflict_do_update(
            constraint="uq_provider_cache_key",
            set_={key: statement.excluded[key] for key in values if key != "provider_code" and key != "cache_key"},
        )
        await self.session.execute(statement)

    async def get_explanation(
        self, seller_id: uuid.UUID, fingerprint: str, provider: str, model: str, now: datetime
    ) -> ExplanationCache | None:
        statement = select(ExplanationCache).where(
            ExplanationCache.seller_id == seller_id,
            ExplanationCache.evidence_fingerprint == fingerprint,
            ExplanationCache.llm_provider == provider,
            ExplanationCache.model_id == model,
            ExplanationCache.expires_at > now,
        )
        return await self.session.scalar(statement)

    async def add_explanation(self, **values: object) -> ExplanationCache:
        explanation = ExplanationCache(id=uuid.uuid4(), **values)
        self.session.add(explanation)
        await self.session.flush()
        return explanation

    async def delete_expired(self, now: datetime) -> None:
        await self.session.execute(delete(ProviderCache).where(ProviderCache.expires_at <= now))
        await self.session.execute(delete(ExplanationCache).where(ExplanationCache.expires_at <= now))
