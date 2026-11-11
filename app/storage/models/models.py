import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.storage.base import Base

JsonType = JSON().with_variant(JSONB, "postgresql")


class Seller(Base):
    __tablename__ = "sellers"
    __table_args__ = (Index("ix_sellers_normalized_domain", "normalized_domain"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seller_key: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_domain: Mapped[str | None] = mapped_column(String(255))
    canonical_url: Mapped[str | None] = mapped_column(Text)
    identity_status: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    assessments: Mapped[list["SellerAssessment"]] = relationship(
        back_populates="seller", cascade="all, delete-orphan"
    )


class SellerAssessment(Base):
    __tablename__ = "seller_assessments"
    __table_args__ = (
        Index("ix_seller_assessments_seller_checked", "seller_id", "checked_at"),
        Index("ix_seller_assessments_expires_at", "expires_at"),
        CheckConstraint("score IS NULL OR score BETWEEN 1.00 AND 5.00", name="ck_assessment_score"),
        CheckConstraint("label IN ('Trusted', 'Mixed', 'Risky', 'Unrated')", name="ck_assessment_label"),
        CheckConstraint("confidence IN ('high', 'medium', 'low')", name="ck_assessment_confidence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seller_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sellers.id", ondelete="CASCADE"), nullable=False
    )
    score: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    label: Mapped[str] = mapped_column(String(20), nullable=False)
    confidence: Mapped[str] = mapped_column(String(10), nullable=False)
    assessment_status: Mapped[str] = mapped_column(String(20), nullable=False)
    scoring_version: Mapped[str] = mapped_column(String(30), nullable=False)
    evidence_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    seller: Mapped[Seller] = relationship(back_populates="assessments")
    factors: Mapped[list["AssessmentFactor"]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan"
    )


class AssessmentFactor(Base):
    __tablename__ = "assessment_factors"
    __table_args__ = (
        UniqueConstraint("assessment_id", "factor_key", name="uq_assessment_factors_key"),
        CheckConstraint("factor_score IS NULL OR factor_score BETWEEN 1.00 AND 5.00", name="ck_factor_score"),
        CheckConstraint("status IN ('ok', 'warn', 'bad', 'unverified')", name="ck_factor_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("seller_assessments.id", ondelete="CASCADE"), nullable=False
    )
    factor_key: Mapped[str] = mapped_column(String(40), nullable=False)
    factor_name: Mapped[str] = mapped_column(String(80), nullable=False)
    factor_weight: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    factor_score: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_summary: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    assessment: Mapped[SellerAssessment] = relationship(back_populates="factors")


class SellerProviderReference(Base):
    __tablename__ = "seller_provider_references"
    __table_args__ = (
        UniqueConstraint("provider_code", "external_id", name="uq_provider_external_id"),
        Index("ix_provider_references_seller_provider", "seller_id", "provider_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seller_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sellers.id", ondelete="CASCADE"), nullable=False
    )
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    match_confidence: Mapped[str] = mapped_column(String(10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProviderCache(Base):
    __tablename__ = "provider_cache"
    __table_args__ = (
        UniqueConstraint("provider_code", "cache_key", name="uq_provider_cache_key"),
        Index("ix_provider_cache_expires_at", "expires_at"),
        Index("ix_provider_cache_seller_capability", "seller_id", "capability"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_code: Mapped[str] = mapped_column(String(40), nullable=False)
    capability: Mapped[str] = mapped_column(String(40), nullable=False)
    cache_key: Mapped[str] = mapped_column(String(512), nullable=False)
    seller_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sellers.id", ondelete="CASCADE")
    )
    result_status: Mapped[str] = mapped_column(String(20), nullable=False)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    provider_version: Mapped[str | None] = mapped_column(String(80))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExplanationCache(Base):
    __tablename__ = "explanation_cache"
    __table_args__ = (
        UniqueConstraint(
            "seller_id", "evidence_fingerprint", "llm_provider", "model_id",
            name="uq_explanation_cache_fingerprint",
        ),
        Index("ix_explanation_cache_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seller_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sellers.id", ondelete="CASCADE"), nullable=False
    )
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("seller_assessments.id", ondelete="CASCADE"), nullable=False
    )
    evidence_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    llm_provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model_id: Mapped[str] = mapped_column(String(160), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
