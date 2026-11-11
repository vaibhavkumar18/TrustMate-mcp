"""Evidence-coverage and confidence policy for TRUSTMATE scoring."""

from __future__ import annotations

import math
from collections.abc import Mapping

from app.domain.policies.scoring_policy import WEIGHTS

# These are the current project's documented deterministic heuristics.
MINIMUM_SCORING_GROUPS = 2
MINIMUM_EVIDENCE_COVERAGE = 0.40
HIGH_CONFIDENCE_GROUPS = 4
HIGH_CONFIDENCE_COVERAGE = 0.90
MEDIUM_CONFIDENCE_GROUPS = 3
MEDIUM_CONFIDENCE_COVERAGE = 0.60

ScoreTuple = tuple[float | None, str, str]


def _is_usable(score: float | None) -> bool:
    if score is None or isinstance(score, bool):
        return False
    try:
        numeric = float(score)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(numeric) and 1.0 <= numeric <= 5.0


def usable_group_names(scores: Mapping[str, ScoreTuple]) -> set[str]:
    """Return known scoring groups with a valid, finite 1–5 factor score."""
    return {
        name
        for name, values in scores.items()
        if name in WEIGHTS and isinstance(values, tuple) and len(values) == 3
        and _is_usable(values[0])
    }


def coverage(scores: Mapping[str, ScoreTuple]) -> float:
    """Weighted evidence coverage over the fixed four groups (0.0–1.0)."""
    return sum(WEIGHTS[name] for name in usable_group_names(scores))


def confidence(scores: Mapping[str, ScoreTuple]) -> str:
    """Classify evidence completeness, independently of trust score quality."""
    usable_count = len(usable_group_names(scores))
    covered = coverage(scores)

    if usable_count == HIGH_CONFIDENCE_GROUPS and covered >= HIGH_CONFIDENCE_COVERAGE:
        return "high"
    if (
        usable_count >= MEDIUM_CONFIDENCE_GROUPS
        and covered >= MEDIUM_CONFIDENCE_COVERAGE
    ):
        return "medium"
    return "low"


def sufficient_evidence(scores: Mapping[str, ScoreTuple]) -> bool:
    """Require the existing minimum of two groups and 40% weighted coverage."""
    return (
        len(usable_group_names(scores)) >= MINIMUM_SCORING_GROUPS
        and coverage(scores) >= MINIMUM_EVIDENCE_COVERAGE
    )
