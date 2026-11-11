from typing import Literal

from pydantic import BaseModel, Field


FactorStatus = Literal["ok", "warn", "bad", "unverified"]
TrustLabel = Literal["Trusted", "Mixed", "Risky", "Unrated"]
Confidence = Literal["high", "medium", "low"]


class TrustFactor(BaseModel):
    name: str
    status: FactorStatus
    detail: str


class Trust(BaseModel):
    score: float | None = Field(default=None, ge=1.0, le=5.0)
    label: TrustLabel
    confidence: Confidence
    factors: list[TrustFactor]