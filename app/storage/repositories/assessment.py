import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import AssessmentFactor, SellerAssessment


class AssessmentRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, **values: object) -> SellerAssessment:
        assessment = SellerAssessment(id=uuid.uuid4(), **values)
        self.session.add(assessment)
        await self.session.flush()
        return assessment

    async def add_factor(self, **values: object) -> AssessmentFactor:
        factor = AssessmentFactor(id=uuid.uuid4(), **values)
        self.session.add(factor)
        await self.session.flush()
        return factor

    async def latest_valid(
        self, seller_id: uuid.UUID, scoring_version: str, now: datetime
    ) -> SellerAssessment | None:
        statement = (
            select(SellerAssessment)
            .where(
                SellerAssessment.seller_id == seller_id,
                SellerAssessment.scoring_version == scoring_version,
                SellerAssessment.expires_at > now,
            )
            .order_by(SellerAssessment.checked_at.desc())
            .limit(1)
        )
        return await self.session.scalar(statement)
