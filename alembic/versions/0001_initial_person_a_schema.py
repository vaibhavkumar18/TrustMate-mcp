"""Create Person A persistence schema.

Revision ID: 0001_initial_person_a_schema
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial_person_a_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sellers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("seller_key", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("normalized_domain", sa.String(255)),
        sa.Column("canonical_url", sa.Text()),
        sa.Column("identity_status", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("seller_key", name="uq_sellers_seller_key"),
    )
    op.create_index("ix_sellers_normalized_domain", "sellers", ["normalized_domain"])

    op.create_table(
        "seller_assessments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("seller_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("score", sa.Numeric(3, 2)),
        sa.Column("label", sa.String(20), nullable=False),
        sa.Column("confidence", sa.String(10), nullable=False),
        sa.Column("assessment_status", sa.String(20), nullable=False),
        sa.Column("scoring_version", sa.String(30), nullable=False),
        sa.Column("evidence_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["seller_id"], ["sellers.id"], ondelete="CASCADE"),
        sa.CheckConstraint("score IS NULL OR score BETWEEN 1.00 AND 5.00", name="ck_assessment_score"),
        sa.CheckConstraint("label IN ('Trusted', 'Mixed', 'Risky', 'Unrated')", name="ck_assessment_label"),
        sa.CheckConstraint("confidence IN ('high', 'medium', 'low')", name="ck_assessment_confidence"),
    )
    op.create_index("ix_seller_assessments_seller_checked", "seller_assessments", ["seller_id", "checked_at"])
    op.create_index("ix_seller_assessments_expires_at", "seller_assessments", ["expires_at"])

    op.create_table(
        "assessment_factors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("assessment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("factor_key", sa.String(40), nullable=False),
        sa.Column("factor_name", sa.String(80), nullable=False),
        sa.Column("factor_weight", sa.Numeric(4, 3), nullable=False),
        sa.Column("factor_score", sa.Numeric(3, 2)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("evidence_summary", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["assessment_id"], ["seller_assessments.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("assessment_id", "factor_key", name="uq_assessment_factors_key"),
        sa.CheckConstraint("factor_score IS NULL OR factor_score BETWEEN 1.00 AND 5.00", name="ck_factor_score"),
        sa.CheckConstraint("status IN ('ok', 'warn', 'bad', 'unverified')", name="ck_factor_status"),
    )

    op.create_table(
        "seller_provider_references",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("seller_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_code", sa.String(40), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("match_confidence", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["seller_id"], ["sellers.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("provider_code", "external_id", name="uq_provider_external_id"),
    )
    op.create_index("ix_provider_references_seller_provider", "seller_provider_references", ["seller_id", "provider_code"])

    op.create_table(
        "provider_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("provider_code", sa.String(40), nullable=False),
        sa.Column("capability", sa.String(40), nullable=False),
        sa.Column("cache_key", sa.String(512), nullable=False),
        sa.Column("seller_id", postgresql.UUID(as_uuid=True)),
        sa.Column("result_status", sa.String(20), nullable=False),
        sa.Column("normalized_payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("provider_version", sa.String(80)),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["seller_id"], ["sellers.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("provider_code", "cache_key", name="uq_provider_cache_key"),
    )
    op.create_index("ix_provider_cache_expires_at", "provider_cache", ["expires_at"])
    op.create_index("ix_provider_cache_seller_capability", "provider_cache", ["seller_id", "capability"])

    op.create_table(
        "explanation_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("seller_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assessment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("llm_provider", sa.String(40), nullable=False),
        sa.Column("model_id", sa.String(160), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["seller_id"], ["sellers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assessment_id"], ["seller_assessments.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "seller_id", "evidence_fingerprint", "llm_provider", "model_id",
            name="uq_explanation_cache_fingerprint",
        ),
    )
    op.create_index("ix_explanation_cache_expires_at", "explanation_cache", ["expires_at"])


def downgrade() -> None:
    op.drop_table("explanation_cache")
    op.drop_index("ix_provider_cache_seller_capability", table_name="provider_cache")
    op.drop_index("ix_provider_cache_expires_at", table_name="provider_cache")
    op.drop_table("provider_cache")
    op.drop_index("ix_provider_references_seller_provider", table_name="seller_provider_references")
    op.drop_table("seller_provider_references")
    op.drop_table("assessment_factors")
    op.drop_index("ix_seller_assessments_expires_at", table_name="seller_assessments")
    op.drop_index("ix_seller_assessments_seller_checked", table_name="seller_assessments")
    op.drop_table("seller_assessments")
    op.drop_index("ix_sellers_normalized_domain", table_name="sellers")
    op.drop_table("sellers")
