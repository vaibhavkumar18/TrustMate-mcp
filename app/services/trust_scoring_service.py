"""Deterministic aggregation of normalized seller evidence."""

from __future__ import annotations

import math

from app.domain.models import SellerEvidence
from app.domain.policies.confidence_policy import confidence, sufficient_evidence, usable_group_names
from app.domain.policies.scoring_policy import WEIGHTS, factor_scores
from app.domain.policies.trust_policy import classify
from app.mcp.schemas.trust import Trust, TrustFactor


def assess_evidence(evidence: SellerEvidence) -> Trust:
    """Build the public TRUST object from the four deterministic factor scores."""
    scores = factor_scores(evidence)
    factors = [
        TrustFactor(name=name, status=status, detail=detail)
        for name, (value, status, detail) in scores.items()
    ]

    # Preserve all factor diagnostics even when the overall result is Unrated.
    if not sufficient_evidence(scores):
        return Trust(
            score=None,
            label="Unrated",
            confidence=confidence(scores),
            factors=factors,
        )

    usable_names = usable_group_names(scores)
    total_weight = sum(WEIGHTS[name] for name in usable_names)
    if total_weight <= 0:
        return Trust(
            score=None,
            label="Unrated",
            confidence="low",
            factors=factors,
        )

    weighted_total = sum(
        WEIGHTS[name] * float(scores[name][0])
        for name in usable_names
    )
    raw_score = weighted_total / total_weight
    if not math.isfinite(raw_score):
        return Trust(
            score=None,
            label="Unrated",
            confidence="low",
            factors=factors,
        )

    score = round(max(1.0, min(5.0, raw_score)), 2)
    return Trust(
        score=score,
        label=classify(score),
        confidence=confidence(scores),
        factors=factors,
    )
