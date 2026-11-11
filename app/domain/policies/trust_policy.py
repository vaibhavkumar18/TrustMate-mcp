def classify(score: float | None) -> str:
    if score is None:
        return "Unrated"
    if score >= 4.0:
        return "Trusted"
    if score >= 2.5:
        return "Mixed"
    return "Risky"
