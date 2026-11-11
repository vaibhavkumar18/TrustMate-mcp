# TRUSTMATE Architecture

## Scope and ownership

This repository implements Person A's standalone seller-intelligence backend. Person B's customer-facing agent, frontend, PayPal MCP integration, payment/checkout flow, and dashboard live in a separate application and are outside this repository.

## Runtime topology

```text
Person B shopping agent / UI
          |
          | MCP Streamable HTTP
          v
FastAPI application
  |-- GET /health
  |-- GET /ready
  `-- /mcp
       `-- MCPServer
            |-- rank_offers
            |-- get_trust_score
            `-- explain_score
                  |
                  v
            Core services
            |-- seller identity resolution
            |-- evidence collection
            |-- deterministic trust scoring
            |-- offer eligibility/ranking
            `-- evidence-grounded explanation
                  |
                  v
            Provider factory / registry
            |-- Channel3
            |-- Foursquare
            |-- OpenStreetMap/Nominatim fallback
            |-- URLhaus
            |-- RDAP
            |-- native website checks
            |-- optional Trustpilot Insights seller reviews
            `-- OpenRouter (explain_score only)
                  |
                  v
            +-- Neon PostgreSQL
            |   `-- durable seller/assessment/factor persistence
            `-- SQLite (72-hour local cache)
                +-- scored seller assessments keyed by normalized seller + market + policy/provider profile
                `-- successful explanations keyed by evidence/model/prompt fingerprint
```

## Actual source layout

| Path | Responsibility |
|---|---|
| `app/main.py` | FastAPI construction, lifespan, provider/database initialization, MCP mounting |
| `app/api/routes/health.py` | Liveness endpoint |
| `app/api/routes/readiness.py` | Database-backed readiness endpoint |
| `app/core/config.py` | Environment-backed Pydantic settings and database URL normalization |
| `app/core/logging_setup.py` | Structured application/provider logging and redaction |
| `app/domain/interfaces/providers.py` | Internal async provider protocols |
| `app/domain/models/provider.py` | Normalized provider/evidence/offer models |
| `app/domain/policies/scoring_policy.py` | Factor normalization, weights, evidence fingerprint, scoring version |
| `app/domain/policies/confidence_policy.py` | Evidence coverage and confidence heuristics |
| `app/domain/policies/trust_policy.py` | Trusted/Mixed/Risky/Unrated classification |
| `app/mcp/server.py` | Official MCP SDK v2 server and Streamable HTTP transport |
| `app/mcp/register_tools.py` | Registration and frozen public tool contracts/readable result text |
| `app/mcp/schemas/` | MCP input, trust, and offer response schemas |
| `app/mcp/tools/` | Thin handlers for the three public MCP tools |
| `app/providers/factory.py` | Configuration validation and provider selection |
| `app/providers/implementations.py` | Channel3, Foursquare, OSM, URLhaus, RDAP, native website, OpenRouter and mock adapters |
| `app/providers/trustpilot.py` | Optional verified whole-business service-review adapter (Insights entitlement required) |
| `app/services/seller_identity.py` | Domain/URL normalization and seller identity validation |
| `app/services/seller_verification_service.py` | Concurrent evidence collection and business fallback orchestration |
| `app/services/trust_scoring_service.py` | Deterministic trust object construction |
| `app/services/offer_ranking_service.py` | Ranking eligibility, 70/30 ranking, recommendation gate and trade-off notes |
| `app/services/assessment_persistence.py` | Persist seller assessments and factor records |
| `app/services/cache_policy_service.py` | Provider-specific retention policy component for normalized provider payloads |
| `app/services/seller_assessment_cache.py` | Validated seller cache identity and provider/policy cache namespace |
| `app/storage/sqlite_assessment_cache.py` | SQLite-backed 72-hour seller-assessment and explanation cache |
| `app/storage/` | Async engine/session, SQLAlchemy models, and repositories |
| `alembic/` | Versioned database schema migration |
| `tests/` | Contract, provider, service, integration, and evaluation tests |

Most existing provider classes remain grouped in `app/providers/implementations.py`; the optional Trustpilot adapter is intentionally isolated in `app/providers/trustpilot.py`. Avoid broad provider-module restructuring in the same change as scoring or deployment work; that requires separate import-boundary tests and provider-by-provider verification.

## Main shopping request flow

1. `rank_offers` validates query, budget, currency, and target-country inputs.
2. Channel3 returns product and offer data. Offers are normalized with the retailer's merchant domain separated from the Channel3 purchase/affiliate URL.
3. Offers are deduplicated by seller identity so evidence is collected once per seller per request.
4. Seller verification collects business, RDAP, website, URL-reputation and (when enabled) seller service-review evidence through provider interfaces.
5. Deterministic scoring creates one `Trust` object per seller. The LLM is not used in scoring or ranking.
6. `offer_ranking_service` applies baseline eligibility and the unchanged 70/30 formula.
7. A separate automatic-recommendation gate requires identity-oriented evidence and at least medium confidence.
8. The result includes ordered offers, scores, factor diagnostics, rank/recommendation fields, trade-off notes, and limitation notes.

## Individual seller tools

- `get_trust_score` runs seller evidence collection, deterministic scoring, and assessment persistence when a database session is configured.
- `explain_score` runs the same deterministic assessment and asks OpenRouter for a constrained explanation only when the explainer is enabled. The explanation must preserve the supplied score and label; it cannot change them.

## Persistence

SQLAlchemy 2.x async models and repositories persist seller identity, assessments, factor diagnostics, provider cache entries, and explanation cache entries. The current Alembic migration creates:

- `sellers`
- `seller_assessments`
- `assessment_factors`
- `seller_provider_references`
- `provider_cache`
- `explanation_cache`

See [OPERATIONS.md](OPERATIONS.md) for the runtime/migration connection split and deployment limitations.

## Three-day SQLite cache

The SQLite cache is separate from Neon. It stores application-generated scored seller-assessment summaries (score, label, confidence, factor summaries, seller key/domain, market, scoring version, checked time and expiry) and successful explanation summaries. It does not store raw provider payloads, page HTML, review text, URLs or credentials; confirm provider terms for derived assessment retention before public/commercial use. Its default TTL is `CACHE_TTL_DAYS=3`. Cache keys include normalized seller identity, target country, scoring version and the active evidence-provider profile so a country change, provider change or scoring-policy change does not reuse an incompatible result. Unrated assessments are not cached; they are retried on a later request. Product prices/listings and raw provider payloads are not cached by this layer.

`rank_offers`, `get_trust_score` and `explain_score` share this cache. The first request for a seller performs live evidence collection; subsequent requests in the same market and provider/policy profile reuse the cached scored result until expiry.
