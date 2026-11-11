from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.policies.scoring_policy import SCORING_VERSION, WEIGHTS, evidence_fingerprint, factor_scores
from app.domain.models import SellerEvidence
from app.services.trust_scoring_service import assess_evidence
from app.storage.repositories import AssessmentRepository, SellerRepository


async def persist_assessment(
    session: AsyncSession, evidence: SellerEvidence, ttl_hours: int = 72
) -> object:
    identity = evidence.identity
    sellers = SellerRepository(session)
    seller = await sellers.get_by_key(identity.seller_key)
    if seller is None:
        seller = await sellers.create(
            seller_key=identity.seller_key, display_name=identity.display_name,
            normalized_domain=identity.normalized_domain, canonical_url=identity.canonical_url,
            identity_status=identity.identity_status,
        )
    trust = assess_evidence(evidence)
    now = datetime.now(timezone.utc)
    assessment = await AssessmentRepository(session).create(
        seller_id=seller.id,
        score=Decimal(str(trust.score)) if trust.score is not None else None,
        label=trust.label, confidence=trust.confidence,
        assessment_status="unrated" if trust.score is None else "complete",
        scoring_version=SCORING_VERSION,
        evidence_fingerprint=evidence_fingerprint(evidence),
        checked_at=now, expires_at=now + timedelta(hours=ttl_hours),
    )
    for name, (score, status, detail) in factor_scores(evidence).items():
        await AssessmentRepository(session).add_factor(
            assessment_id=assessment.id, factor_key=name.casefold().replace(" ", "_"),
            factor_name=name, factor_weight=Decimal(str(WEIGHTS[name])),
            factor_score=Decimal(str(score)) if score is not None else None,
            status=status, detail=detail, evidence_summary={"provider": evidence.identity.seller_key},
        )
    return assessment
