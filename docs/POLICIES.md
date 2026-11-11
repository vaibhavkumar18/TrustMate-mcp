# TRUSTMATE Scoring, Confidence, Ranking and Recommendation Policies

This document describes the current implementation in `app/domain/policies/` and `app/services/`. The code is authoritative. Bump `SCORING_VERSION` whenever evidence interpretation or normalization changes.

## Trust scoring — version 2.3

Four fixed evidence groups are used:

| Factor | Weight |
|---|---:|
| Business presence | 30% |
| Reviews | 30% |
| Website signals | 25% |
| URL reputation | 15% |

Only scoreable evidence groups participate in the weighted average. Weights are renormalized across the groups with usable scores. Missing, ambiguous, unauthorized, or failed evidence is `unverified`; it is not automatically assigned a score of 1.

### Business presence

A business factor is scoreable only when all of the following hold:

- The provider check completed.
- A provider place/business ID exists.
- The business-name match is `high` or `medium`.
- The business record's official website has the same registered domain as the seller's normalized merchant domain.

A high name match scores 3.5 (`warn`); a medium name match scores 3.0 (`warn`). This confirms business presence, not online order fulfilment or transaction reliability.

### Reviews

Only review evidence explicitly scoped to the online seller can contribute. Place/POI ratings and unknown-scope ratings do not count as seller transaction reviews.

The provider's 0–5 rating is mapped to the 1–5 factor scale using `1 + (rating / 5 * 4)`. Fewer than 10 seller reviews caps the factor at 3.5 and marks it `warn`. With 10 or more reviews, ratings of at least 3.5/5 are `ok`; lower ratings are `warn`. For the optional Trustpilot Insights adapter, only verified whole-business service reviews (`isVerified=true`, no physical `reviewedLocationName`) enter the sample count and average. A profile's lifetime review total does not satisfy the 10-review threshold.

### Website signals

HTTPS reachability and TLS validity are one transport-security group, not two independent trust groups. Successful HTTPS/TLS is baseline security and does not raise the numerical score by itself.

A numeric website score requires at least two observed independent groups and at least one commercial-information group (contact information or return/refund policy). The neutral baseline is 3.0; observed contact presence adds 0.5, confirmed absence subtracts 0.25; observed return/refund policy presence adds 0.5, and absence is penalized only when both return and refund checks are explicitly false. The result is bounded to 1–4.

A completed explicit HTTPS reachability or TLS certificate validation failure scores 1.5 and is `bad`. A blocked, denied, timed-out, or incomplete website inspection remains unverified rather than being treated as a confirmed security failure.

### URL reputation

A confirmed active malicious indicator from an enabled/authorized provider scores 1.0 (`bad`). A completed no-known-indicator response scores 3.0 (`warn`) and is neutral evidence only. Historical-only, ambiguous, unauthorized, unavailable, or failed checks are unverified. A no-hit response is not proof that a seller or URL is safe.

### Minimum evidence and confidence

A trust score requires at least two usable groups and at least 0.40 weighted evidence coverage. Otherwise the score is `null`, the label is `Unrated`, and confidence is `low`.

Confidence is an evidence-completeness heuristic separate from score quality:

- `high`: all four groups and at least 0.90 coverage.
- `medium`: at least three groups and at least 0.60 coverage.
- `low`: anything below those thresholds.

These thresholds are implementation heuristics, not externally validated fraud-detection thresholds.

### Labels

- `Trusted`: score >= 4.0
- `Mixed`: score >= 2.5 and < 4.0
- `Risky`: score < 2.5
- `Unrated`: score is null

RDAP data is collected as domain evidence but is not a fifth weighted scoring factor in the current implementation.

## Offer eligibility and ranking

An offer is rank-eligible only when:

- Its trust score is non-null and at least 2.5.
- Its price is finite and positive.
- Its price is no greater than the supplied budget.
- Required offer and seller identity fields are present.

Ineligible offers remain in the response with `rank: null` and `recommended: false`.

For eligible offers, the existing formula is:

```text
trust_component = clamp(trust_score / 5, 0, 1)
price_component = clamp((budget - offer_price) / budget, 0, 1)
ranking_score = 0.70 * trust_component + 0.30 * price_component
```

The values are compared in the request's specified currency; TRUSTMATE does not perform currency conversion in the ranking service. Ordering is ranking score descending, trust score descending, price ascending, then offer ID ascending. The internal ranking score is not exposed in the MCP response.

**Important:** 70/30 is a weighted trade-off, not strict lexicographic trust-first ordering. A lower-trust offer with a sufficiently lower price can outrank a higher-trust offer. The formula has been preserved as the agreed policy.

## Automatic recommendation safety gate

Recommendation is distinct from rank eligibility. An offer can retain rank 1 and still be blocked from recommendation.

An offer can be automatically recommended only when BOTH conditions hold:

1. At least one identity-oriented signal exists:
   - business presence has already passed the scoring policy's registered-domain corroboration and name-match requirements (`ok` or `warn`), OR
   - reliable seller-scoped reviews are `ok` under the review policy.
2. Overall evidence confidence is `medium` or `high`.

Website signals and URL reputation alone cannot establish seller identity. The highest-ranked offer that passes this gate becomes the only automatic recommendation. If no offer passes, no offer is recommended.

A recommendation is not the same as a `Trusted` classification. The returned label and confidence must remain visible to the user.
