import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import Seller


class SellerRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_key(self, seller_key: str) -> Seller | None:
        return await self.session.scalar(select(Seller).where(Seller.seller_key == seller_key))

    async def create(self, **values: object) -> Seller:
        seller = Seller(id=uuid.uuid4(), **values)
        self.session.add(seller)
        await self.session.flush()
        return seller
