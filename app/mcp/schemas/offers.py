from pydantic import BaseModel, Field

from app.mcp.schemas.trust import Trust


class Offer(BaseModel):
    id: str
    title: str
    price: float
    currency: str
    url: str
    seller: str
    trust: Trust
    # Null means the offer was inspected but failed eligibility; it was not
    # placed in the trust/value ranking set.
    # Required key with a nullable value: null means inspected but not eligible.
    rank: int | None = Field(ge=1)
    recommended: bool
    tradeoff_note: str | None = None


class RankOffersOutput(BaseModel):
    target_country: str
    offers: list[Offer]
    unverified_notes: list[str]
